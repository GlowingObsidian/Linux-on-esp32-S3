#!/usr/bin/env python3
"""Verify a built S3 handoff against its source snapshots and target kernel."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import struct

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("firmware", type=Path)
parser.add_argument("kernel", type=Path)
args = parser.parse_args()
b, k = args.firmware, args.kernel


def symbols(path):
    result = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]+)\s+\w\s+(\S+)", line)
        if match:
            result[match[2]] = int(match[1], 16)
    return result


def function(disassembly, name):
    match = re.search(r"^[0-9a-f]+ <" + re.escape(name) + r">:\n(.*?)(?=\n\n|\Z)",
                      disassembly, re.M | re.S)
    assert match, name
    return match[1]


def load_segments(path):
    data = path.read_bytes()
    assert data[:6] == b"\x7fELF\x01\x01", path
    assert struct.unpack_from("<H", data, 18)[0] == 94, path
    offset = struct.unpack_from("<I", data, 28)[0]
    size, count = struct.unpack_from("<HH", data, 42)
    result = []
    for i in range(count):
        typ, off, va, pa, fs, ms, flags, align = struct.unpack_from("<8I", data, offset + i * size)
        if typ == 1:
            result.append({"address": va, "memory_bytes": ms, "file_bytes": fs, "flags": flags})
    return result


old = symbols(b / "audit/firmware-before.symbols")
new = symbols(b / "audit/firmware.symbols")
config = (k / "kernel.config").read_text()
vector = int(re.search(r"^CONFIG_VECTORS_ADDR=(0x[0-9a-fA-F]+)$", config, re.M)[1], 16)
assert old["space_for_vectors"] == new["space_for_vectors"] == vector
assert (b / "sdkconfig.final").read_bytes() == (b / "sdkconfig.before").read_bytes()
assert (b / "firmware.bin").read_bytes()[2:4] == (b / "firmware-before.bin").read_bytes()[2:4]
for symbol in ["CONFIG_SMP", "CONFIG_XTENSA_NOMMU_FORK", "CONFIG_JFFS2_FS", "CONFIG_USB_DWC2"]:
    assert re.search(r"^" + symbol + "=y$", config, re.M), symbol
assert not re.search(r"^CONFIG_MMC(?:_\w+)?=[ym]$", config, re.M)
assert "linux_flash_read" not in new and "linux_flash_init" not in new

dis = (b / "audit/firmware.dis").read_text()
boot = function(dis, "map_flash_and_go")
assert boot.index("<esp_partition_find>") < boot.index("rsil") < boot.index("<init_linux_mmap>")
assert "wsr.intenable" in boot
assert not re.search(r"\bwsr\.ps\b", boot[boot.index("rsil"):])
assert re.search(r"\bmemw\b.*\bjx\b", boot, re.S)
assert "<esp_partition_find>" not in boot[boot.index("<init_linux_mmap>"):]
assert "<printf>" not in boot[boot.index("rsil"):], "newlib stdio aborts with IRQs masked"

mm = function(dis, "init_linux_mmap")
assert "<printf>" not in mm, "handoff diagnostics require lock-free ROM output"
iram_start, iram_end = new["_iram_start"], new["_iram_end"]
icache_size = int(re.search(r"^CONFIG_ESP32S3_INSTRUCTION_CACHE_SIZE=(0x[0-9a-f]+)$",
                            (b / "sdkconfig.final").read_text(), re.M)[1], 16)
assert iram_start >= 0x40370000 + icache_size
assert iram_start <= new["init_linux_mmap"] < iram_end
ordered = ["Cache_WriteBack_All", "Cache_Disable_ICache", "Cache_Disable_DCache",
           "Cache_Enable_DCache", "Cache_Enable_ICache"]
positions = [mm.index("<" + name + ">") for name in ordered]
assert positions == sorted(positions)
off = mm[positions[1]:positions[-1]]
assert "<printf>" not in off and "<abort>" not in off
for literal in re.finditer(r"\bl32r\s+\w+,\s*([0-9a-f]+).*?\(([0-9a-f]+)\s+<", off):
    addr, value = int(literal[1], 16), int(literal[2], 16)
    assert iram_start <= addr < iram_end, hex(addr)
    assert not (0x3C000000 <= value < 0x3E000000 or 0x42000000 <= value < 0x44000000), hex(value)
for call in re.finditer(r"\bcall(?:0|4|8|12)\s+([0-9a-f]+)", off):
    target = int(call[1], 16)
    assert 0x40000000 <= target < 0x40080000 or iram_start <= target < iram_end, hex(target)

# Follow actual branch targets, including loop bodies placed after the return.
# Cache state belongs to the executed path, not the instruction's file order.
nodes, resolved_calls, last_literals = {}, {}, {}
for line in mm.splitlines():
    match = re.match(r"^([0-9a-f]+):\s+[0-9a-f]+\s+([\w.]+)\s*(.*)$", line)
    if not match:
        continue
    address, opcode, operands = int(match[1], 16), match[2], match[3]
    nodes[address] = (opcode, operands)
    if opcode == "l32r":
        literal = re.search(r"(\w+),\s*([0-9a-f]+).*?\(([0-9a-f]+)\s+<([^>]+)>\)", operands)
        assert literal, line
        last_literals[literal[1]] = (int(literal[2], 16), int(literal[3], 16), literal[4])
    elif opcode == "callx8":
        resolved_calls[address] = last_literals[operands.strip()]
    elif opcode.startswith("call"):
        direct = re.search(r"([0-9a-f]+)\s+<([^>]+)>", operands)
        assert direct, line
        resolved_calls[address] = (address, int(direct[1], 16), direct[2])
addresses = sorted(nodes)
following = dict(zip(addresses, addresses[1:]))
loop_back = {}
for address, (opcode, operands) in nodes.items():
    if opcode.startswith("loop"):
        end = int(re.search(r",\s*([0-9a-f]+)", operands)[1], 16)
        loop_back[end] = following[address]
pending, visited = [(addresses[0], 0, False)], set()
while pending:
    address, disabled, written_back = pending.pop()
    if (address, disabled, written_back) in visited:
        continue
    visited.add((address, disabled, written_back))
    opcode, operands = nodes[address]
    assert iram_start <= address < iram_end
    if opcode == "l32r":
        literal = re.search(r"\w+,\s*([0-9a-f]+).*?\(([0-9a-f]+)\s+<", operands)
        addr, value = int(literal[1], 16), int(literal[2], 16)
        assert iram_start <= addr < iram_end
        if disabled:
            assert not (0x3C000000 <= value < 0x3E000000 or 0x42000000 <= value < 0x44000000), hex(value)
    if address in resolved_calls:
        _, target, name = resolved_calls[address]
        if name == "Cache_WriteBack_All":
            assert disabled == 0
            written_back = True
        elif name == "Cache_Disable_ICache":
            assert written_back and disabled == 0
            disabled = 1
        elif name == "Cache_Disable_DCache":
            assert disabled == 1
            disabled = 3
        elif name == "Cache_Enable_DCache":
            assert disabled == 3
            disabled = 1
        elif name == "Cache_Enable_ICache":
            assert disabled == 1
            disabled = 0
        elif disabled:
            assert 0x40000000 <= target < 0x40080000, name
        if name == "abort":
            assert not disabled
            continue
    if opcode.startswith("ret"):
        assert not disabled
        continue
    assert opcode not in ["ill", ".byte"], (hex(address), opcode)
    successors = [following[address]]
    if opcode == "j" or opcode.startswith("b"):
        target = int(re.search(r"(?:^|,\s*)([0-9a-f]+)\s+<", operands)[1], 16)
        successors = [target] if opcode == "j" else successors + [target]
    successors += [loop_back[n] for n in successors if n in loop_back]
    pending.extend((n, disabled, written_back) for n in successors)

reserved = [(0x3FCC8000, 0x3FCCA000), (0x403B8000, 0x403BA000)]
segments = load_segments(b / "firmware.elf")
for segment in segments:
    lo, hi = segment["address"], segment["address"] + segment["memory_bytes"]
    for start, end in reserved:
        assert hi <= start or lo >= end, (segment, hex(start))

images = []
for path, address, limit in [(b / "firmware.bin", 0x10000, 0xC0000),
                             (k / "linux.bin", 0x140000, 0x3D0000)]:
    data = path.read_bytes()
    assert len(data) <= limit
    end = (address + len(data) + 0xFFF) & ~0xFFF
    for start, stop in [(0xD0000, 0x140000), (0xC00000, 0x1000000)]:
        assert end <= start or address >= stop
    images.append({"file": str(path.resolve()), "offset": hex(address), "bytes": len(data),
                   "erase_end_exclusive": hex(end), "sha256": hashlib.sha256(data).hexdigest()})

report = {"software_checks_passed": True, "hardware_validated": False,
          "vector_address": hex(vector), "cache_transition_in_iram": True,
          "instruction_path_states_verified": len(visited),
          "idf_config_and_flash_mode_unchanged": True, "load_segments": segments,
          "images": images, "preserve": ["bootloader", "partition_table", "rootfs", "etc", "home"],
          "limitations": ["The board must confirm Nano, sustained writes, and reboot persistence after installation."]}
(b / "handoff-verification.json").write_text(json.dumps(report, indent=2) + "\n")
print("PASS: firmware/kernel vector coupling, final IRQ/MMU order, internal cache-off code/literals, "
      "dirty-data preservation order, unchanged clocks/config, retired flash server, SD removal, "
      "guard SRAM ownership and persistent-partition erase boundaries")
