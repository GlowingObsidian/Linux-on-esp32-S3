#!/usr/bin/env python3
"""Reject firmware loads that overwrite Linux IRAM owners or their aliases."""
from pathlib import Path
import json
import struct
import sys

def elf(path):
    b = Path(path).read_bytes()
    assert b[:6] == b'\x7fELF\x01\x01'
    off = struct.unpack_from('<I', b, 28)[0]
    size, count = struct.unpack_from('<HH', b, 42)
    loads = [struct.unpack_from('<8I', b, off + size*i) for i in range(count)]
    off = struct.unpack_from('<I', b, 32)[0]
    size, count, names_idx = struct.unpack_from('<HHH', b, 46)
    rows = [struct.unpack_from('<10I', b, off + size*i) for i in range(count)]
    names = rows[names_idx]
    names = b[names[4]:names[4]+names[5]]
    sections = [(names[r[0]:names.index(0, r[0])].decode(), r) for r in rows]
    return b, loads, sections

def physical(lo, hi):
    for a, z, delta in [(0x40378000, 0x403e0000, 0x6f0000),
                        (0x3fc88000, 0x3fd00000, 0)]:
        start, end = max(lo, a), min(hi, z)
        if start < end:
            yield start-delta, end-delta

kernel, firmware = map(elf, sys.argv[1:3])
# Include the guard even when it is represented by a zero-file-size segment.
owners = [(0x3fc89000, 0x3fc8a000, 'Linux vectors/atomic guard')]
for typ, _, addr, _, _, memsz, flags, _ in kernel[1]:
    if typ == 1 and 0x40378000 <= addr < 0x403e0000:
        owners.extend((a, z, 'Linux IRAM') for a, z in physical(addr, addr+memsz))
conflicts = []
for section, row in firmware[2]:
    _, typ, flags, addr, _, memsz, *_ = row
    # Dummy sections reserve aliases without containing live code/data.
    if not flags & 2 or typ not in (1, 8) or '.dummy' in section:
        continue
    for a, z in physical(addr, addr+memsz):
        for lo, hi, name in owners:
            if a < hi and z > lo:
                conflicts.append(dict(owner=name, section=section, firmware_start=hex(addr),
                                      overlap=[hex(max(a, lo)), hex(min(z, hi))]))
print(json.dumps(dict(passed=not conflicts, conflicts=conflicts), indent=2))
sys.exit(bool(conflicts))
