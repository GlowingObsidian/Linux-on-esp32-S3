#!/usr/bin/env python3
"""Verify the opt-in S3 Wi-Fi SRAM profile against the exact preceding build."""
from pathlib import Path
import argparse
import hashlib
import json
import struct
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('artifacts', type=Path)
parser.add_argument('baseline', type=Path)
parser.add_argument('firmware_elf', type=Path)
args = parser.parse_args()
p, base = args.artifacts, args.baseline

class Elf:
    def __init__(self, path):
        self.data = path.read_bytes()
        assert self.data[:6] == b'\x7fELF\x01\x01'
        off = struct.unpack_from('<I', self.data, 28)[0]
        size, count = struct.unpack_from('<HH', self.data, 42)
        self.loads = [struct.unpack_from('<8I', self.data, off + i * size)
                      for i in range(count)]
        self.loads = [x for x in self.loads if x[0] == 1 and x[5]]
        off = struct.unpack_from('<I', self.data, 32)[0]
        size, count, strings = struct.unpack_from('<HHH', self.data, 46)
        rows = [struct.unpack_from('<10I', self.data, off + i * size)
                for i in range(count)]
        table = rows[strings]
        names = self.data[table[4]:table[4] + table[5]]
        self.sections = {names[x[0]:names.index(0, x[0])].decode(): x for x in rows}

    def read(self, start, end):
        for _, off, va, _, filesz, _, _, _ in self.loads:
            if va <= start <= end <= va + filesz:
                return self.data[off + start - va:off + end - va]
        raise AssertionError(('No file-backed ELF range', hex(start), hex(end)))

def symbols(folder):
    return {x.split()[2]: int(x.split()[0], 16)
            for x in (folder / 'System.map').read_text().splitlines()
            if len(x.split()) == 3}

def config(path):
    return {x.split('=', 1)[0]: x.split('=', 1)[1]
            for x in path.read_text().splitlines() if x.startswith('CONFIG_')}

def dt_nodes(path):
    b = path.read_bytes()
    h = struct.unpack_from('>10I', b)
    assert h[0] == 0xd00dfeed
    pos, strings, stack, nodes = h[2], h[3], [], {}
    while True:
        token = struct.unpack_from('>I', b, pos)[0]
        pos += 4
        if token == 1:
            end = b.index(0, pos)
            stack.append(b[pos:end].decode())
            pos = (end + 4) & ~3
        elif token == 2:
            stack.pop()
        elif token == 3:
            length, noff = struct.unpack_from('>II', b, pos)
            pos += 8
            name = b[strings + noff:b.index(0, strings + noff)].decode()
            nodes.setdefault('/' + '/'.join(stack[1:]), {})[name] = b[pos:pos + length]
            pos = (pos + length + 3) & ~3
        elif token == 4:
            continue
        elif token == 9:
            return nodes
        else:
            raise AssertionError(token)

elf, firmware = Elf(p / 'vmlinux'), Elf(args.firmware_elf)
subprocess.run([sys.executable, str(Path(__file__).with_name('verify-firmware-linux-ownership.py')),
                str(p / 'vmlinux'), str(args.firmware_elf)], check=True)
s = symbols(p)
start, end, load = [s['_esp32_wifi_iram_' + x] for x in ['start', 'end', 'load']]
assert start == 0x40390000 and start < end <= 0x403a0000
assert start % 4 == end % 4 == load % 4 == 0
hot = elf.read(start, end)
image = (p / 'linux.bin').read_bytes()
assert len(image) <= 0x3d0000 and (p / 'board.dtb').read_bytes() in image
assert image[load - 0x42140000:load - 0x42140000 + len(hot)] == hot
reloc = elf.read(s['__boot_reloc_table_start'], s['__boot_reloc_table_end'])
assert len(reloc) % 12 == 0
triples = list(struct.iter_unpack('<III', reloc))
assert triples.count((start, end, load)) == 1, 'Boot copy entry missing or duplicated'
assert elf.sections['.esp32.wifi.iram'][5] == len(hot)
assert not any(name.startswith(('.wifi0iram', '.wifirxiram', '.wifislpiram',
                               '.wifiorslpiram', '.wifislprxiram', '.wifiextrairam',
                               '.iram1')) for name in elf.sections), 'Orphan hot sections remain'

cfg, oldcfg = config(p / 'kernel.config'), config(base / 'kernel.config')
changed = {k for k in set(cfg) | set(oldcfg) if cfg.get(k) != oldcfg.get(k)}
assert changed == {'CONFIG_ESP32_WIFI_IRAM', 'CONFIG_BUILTIN_DTB_NAME'}, changed
assert cfg['CONFIG_ESP32_WIFI_IRAM'] == 'y'

newdt, olddt = dt_nodes(p / 'board.dtb'), dt_nodes(base / 'board.dtb')
expected = struct.pack('>4I', 0x3fccc700, 0x10000, 0x3fca0000, 0x10000)
assert newdt['/wifi_blob']['reg'] == expected
assert newdt['/wifi_blob']['reg-names'] == b'wifi-sram\0wifi-iram\0'
for prop in ['reg', 'reg-names']:
    newdt['/wifi_blob'][prop] = olddt['/wifi_blob'][prop]
assert newdt == olddt, 'Unexpected DT change outside the SRAM resource'

reservation = (0x3fca0000, 0x3fcb0000)
owners = []
for label, obj in [('firmware', firmware), ('kernel', elf)]:
    for _, _, va, _, _, memsz, _, _ in obj.loads:
        if label == 'kernel' and va == start:
            continue
        ranges = []
        lo, hi = max(va, 0x3fc88000), min(va + memsz, 0x3fd00000)
        if lo < hi:
            ranges.append((lo, hi))
        lo, hi = max(va, 0x40378000), min(va + memsz, 0x403e0000)
        if lo < hi:
            ranges.append((lo - 0x6f0000, hi - 0x6f0000))
        for lo, hi in ranges:
            assert not (lo < reservation[1] and hi > reservation[0]), (label, hex(lo), hex(hi))
            owners.append(dict(owner=label, start=hex(lo), end=hex(hi)))
for path, props in olddt.items():
    reg = props.get('reg', b'')
    if len(reg) % 8:
        continue
    for lo, size in struct.iter_unpack('>II', reg):
        if 0x3fc88000 <= lo < 0x3fd00000:
            assert not (lo < reservation[1] and lo + size > reservation[0]), path
            owners.append(dict(owner=path, start=hex(lo), end=hex(lo + size)))

preserved = json.loads((base / 'preservation-checks.json').read_text())
for name in ['esp32_flash_guard_template', 'esp32_flash_cache_template', 'esp32s3_nor_blob']:
    data = elf.read(s[name + '_start'], s[name + '_end'])
    assert hashlib.sha256(data).hexdigest() == preserved[name]['sha256'], name
    assert len(data) == preserved[name]['bytes']
    offset = s[name + '_start'] - 0x42140000
    assert image[offset:offset + len(data)] == data

proof = dict(hot_bytes=len(hot), hot_sha256=hashlib.sha256(hot).hexdigest(),
             instruction_start=hex(start), instruction_end=hex(end), flash_load=hex(load),
             reserved_data_alias=[hex(x) for x in reservation], boot_copy_entry='verified',
             kernel_bytes=len(image), kernel_sha256=hashlib.sha256(image).hexdigest(),
             config_changes=sorted(changed), known_sram_owners=owners,
             cache_off_templates='three byte-identical to baseline',
             limitations=['Static-owner proof; firmware boot heap retires at the Linux handoff.',
                          'Loaded bytes and actual performance must still be verified on the board.'])
(p / 'iram-layout-verification.json').write_text(json.dumps(proof, indent=2) + '\n')
print(json.dumps(proof, indent=2))
