#!/usr/bin/env python3
"""Static gates for the non-writing SRAM guard; not hardware execution proof."""
from pathlib import Path
import argparse, hashlib, json, re, struct, subprocess
p=argparse.ArgumentParser();p.add_argument('artifacts',type=Path);a=p.parse_args()
r=Path(__file__).resolve().parent.parent;b=a.artifacts
subprocess.run(['python3',str(r/'build/verify-wifi-artifacts.py'),str(b)],check=True)
config=(b/'kernel.config').read_text()
assert 'CONFIG_MTD_ESP32_FLASH_GUARD_TEST=y' in config
assert 'CONFIG_BUILTIN_DTB_NAME="arduino-nano-esp32-guard-test"' in config
dt=(b/'board.dtb').read_bytes()
assert b'espressif,esp32s3-flash-guard-test\0' in dt
assert struct.pack('>II',0x3fcc8000,0x2000) in dt
start,end=0x3fcc8000,0x3fcca000
for name in [b/'vmlinux',r/'build-output/wifi-smp-audit/base/firmware.elf']:
    elf=name.read_bytes();ph=struct.unpack_from('<I',elf,28)[0];size,n=struct.unpack_from('<HH',elf,42)
    for i in range(n):
        typ,off,va,pa,filesz,memsz,flags,align=struct.unpack_from('<8I',elf,ph+i*size)
        if typ!=1 or not memsz:continue
        ranges=[(va,va+memsz)]
        # SRAM0 is not a data alias. Only normalize the SRAM1 intersection.
        lo,hi=max(va,0x40378000),min(va+memsz,0x403e0000)
        if lo<hi:ranges.append((lo-0x6f0000,hi-0x6f0000))
        for lo,hi in ranges:assert not(lo<end and hi>start),(name,hex(lo),hex(hi))
assert end<=0x3fccc700 and end<=0x3fcce800
symbols={}
for line in (b/'System.map').read_text().splitlines():
    parts=line.split()
    if len(parts)>=3:symbols[parts[2]]=int(parts[0],16)
low=symbols['esp32_flash_guard_template_start'];high=symbols['esp32_flash_guard_template_end']
assert 0<high-low<=0x800
instructions={}
for line in (b/'guard-template.dis').read_text().splitlines():
    m=re.match(r'\s*([0-9a-f]+):\s+([0-9a-f]+)\s+(\S+)\s*(.*)',line)
    if m and low<=int(m[1],16)<high:
        instructions[int(m[1],16)]=(len(m[2])//2,m[3],m[4])
assert low in instructions and instructions[low][1]=='entry'
allowed={'entry','or','addi','s32i','slli','add','call0','ill','rsr.ccount','movi','memw','bnez','l32i','sub','bltu','j','xor','beq','retw'}
todo=[low];reached=set()
while todo:
    pc=todo.pop()
    if pc in reached:continue
    assert pc in instructions,hex(pc)
    reached.add(pc);size,op,args=instructions[pc]
    assert op in allowed and op!='ill',(hex(pc),op,args)
    if op in {'s32i','l32i'}:
        base=args.split(',')[1].strip();assert base in {'a1','a2','a14'},(op,args)
    if op=='retw':continue
    if op in {'call0','j','bnez','bltu','beq'}:
        target=re.search(r'\b([0-9a-f]+)\s+<',args);assert target,args
        target=int(target[1],16);assert low<=target<high
        todo.append(target)
        if op in {'call0','j'}:continue
    todo.append(pc+size)
assert any(instructions[x][1]=='retw' for x in reached)
assert not any(instructions[x][1] in {'callx0','callx8','l32r','wsr','wsr.ps'} for x in reached)
# The copy source must be the actual bytes in this exact flash image.
elf=(b/'vmlinux').read_bytes();ph=struct.unpack_from('<I',elf,28)[0];size,n=struct.unpack_from('<HH',elf,42)
blob=None
for i in range(n):
    typ,off,va,pa,filesz,memsz,flags,align=struct.unpack_from('<8I',elf,ph+i*size)
    if typ==1 and va<=low and high<=va+filesz:blob=elf[off+low-va:off+high-va]
assert blob is not None
image=(b/'linux.bin').read_bytes();assert image[low-0x42140000:high-0x42140000]==blob
working=json.loads((r/'build-output/wifi-smp-audit/wlan0-user-control/integration-manifest.json').read_text())
m=json.loads((b/'integration-manifest.json').read_text())
m.update(profile='flash-guard-internal-memory-dry-run',rootfs_sha256=working['rootfs_sha256'],
    radio_enabled_at_boot=True,automatic_network_connection=False,
    source_patch_count=26,flash_writes_enabled=False,cache_changes_enabled=False,
    guard_sram={'start':hex(start),'end':hex(end),'bytes':end-start,'runtime_verified':False},
    template_bytes=len(blob),template_sha256=hashlib.sha256(blob).hexdigest(),
    reachable_instructions_checked=len(reached),flashed=False,hardware_verified=False,
    limitations=['Only internal code/stack/rendezvous plumbing; caches remain enabled and flash remains read-only.',
                 'Runtime SRAM ownership/permissions and actual ABI execution require board testing.',
                 'Cache-off, native write/erase, JFFS2 mounts and SD-free boot are not implemented.'])
(b/'integration-manifest.json').write_text(json.dumps(m,indent=2)+'\n')
print(f'PASS: {len(blob)} template bytes, {len(reached)} reachable instructions; local-only branches, internal-base loads/stores, no cache/register writes or external calls; SRAM/static owners and exact embedded bytes verified')
