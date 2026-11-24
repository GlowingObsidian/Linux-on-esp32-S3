#!/usr/bin/env python3
"""Verify exact linked SRAM cache code and its constrained control flow."""
from pathlib import Path
import argparse,hashlib,json,re,struct,subprocess
p=argparse.ArgumentParser();p.add_argument('artifacts',type=Path);a=p.parse_args();b=a.artifacts
r=Path(__file__).resolve().parent.parent
layout=(r/'experiments/flash-guard/esp32s3-flash-guard-layout.h').read_text()
control_bytes=int(re.search(r'^#define FGC_BYTES (\d+)',layout,re.M)[1])
subprocess.run(['python3',str(r/'build/verify-flash-guard.py'),str(b)],check=True)
symbols={v[2]:int(v[0],16) for line in (b/'System.map').read_text().splitlines() if len(v:=line.split())>=3}
low=symbols['esp32_flash_cache_template_start'];high=symbols['esp32_flash_cache_template_end']
assert 0<high-low<=0xe00-0x100
instructions={}
for line in (b/'cache-template.dis').read_text().splitlines():
    m=re.match(r'\s*([0-9a-f]+):\s+([0-9a-f]{4,8})\s+(\S+)\s*(.*)',line)
    if m and low<=int(m[1],16)<high:instructions[int(m[1],16)]=(len(m[2])//2,m[3],m[4])
allowed={'entry','or','addi','s32i','slli','add','call0','rsr.ccount','rsr.intenable','rsr.interrupt','extui','bnez','movi','j','l32i','beq','bne','sub','bltu','bbsi','bbci','and','beqz','xor','retw','memw'}
branches={'call0','j','bnez','beqz','beq','bne','bltu','bbci','bbsi'}
def reachable(start):
    todo=[start];seen=set()
    while todo:
        pc=todo.pop()
        if pc in seen:continue
        assert pc in instructions,('bad PC/padding',hex(pc));seen.add(pc)
        size,op,args=instructions[pc];assert op in allowed,(hex(pc),op)
        if op=='entry':assert pc==low
        if op in {'s32i','l32i'}:
            parts=[x.strip() for x in args.split(',')];base=parts[1];off=int(parts[2],0)
            assert base in {'a1','a2','a3','a9','a14'},(op,args)
            if base=='a1':assert 0<=off<32
            if base in {'a2','a14'}:assert 0<=off<control_bytes
            if base=='a9':assert op=='l32i' and off==0
            if base=='a3':
                assert off in {0,4,0x60,0x64,0x4c,0xa0,0x40,0x94,0x150,0x154,0x130,0xe4,0xec,0xf0,0xfc}
                if op=='s32i':assert off in {0,4,0x60,0x64,0x4c,0xa0,0x150,0x154,0xec}
                if off==0xec:assert op=='s32i','W1C status register must never be read'
        if op=='retw':continue
        if op in branches:
            target=int(re.search(r'\b([0-9a-f]+)\s+<',args)[1],16);assert low<=target<high
            todo.append(target)
            if op in {'call0','j'}:continue
        todo.append(pc+size)
    return seen
seen=reachable(low)
assert sum(instructions[pc][1]=='call0' for pc in seen)==1
assert sum(instructions[pc][1]=='retw' for pc in seen)==1
# The dispatch on CPU index leads to a peer path with no MMIO stores or final publication.
dispatch=next(pc for pc,(size,op,args) in instructions.items() if op=='beqz' and args.startswith('a4,'))
next_pc=dispatch+instructions[dispatch][0];assert instructions[next_pc][1]=='j'
peer=int(re.search(r'\b([0-9a-f]+)\s+<',instructions[next_pc][2])[1],16)
for pc in reachable(peer):
    size,op,args=instructions[pc]
    if op=='s32i':
        parts=[x.strip() for x in args.split(',')]
        assert parts[1]!='a3' and not(parts[1]=='a2' and int(parts[2],0)==8),'peer releases or mutates caches'
# Prove the C-to-SRAM boundary in the final callback, including compiler output.
callback=(b/'guard-callback.dis').read_text()
mask=re.search(r'wsr\.intenable\s+a8',callback);spill=re.search(r'\bcall8\s+',callback);window=callback.index('rsr.windowstart');entry=callback.index('callx8')
assert mask and spill and mask.start()<spill.start()<window<entry
assert not re.search(r'\bcall(?:0|4|8|12|x0|x4|x8|x12)\b',callback[window:entry])
assert re.search(r'wsr\.intenable\s+a6',callback[entry:])
# The source bytes copied into SRAM must be the actual bytes in this image.
elf=(b/'vmlinux').read_bytes();ph=struct.unpack_from('<I',elf,28)[0];psz,n=struct.unpack_from('<HH',elf,42);blob=None
for i in range(n):
    typ,off,va,pa,fs,ms,flags,align=struct.unpack_from('<8I',elf,ph+i*psz)
    if typ==1 and va<=low and high<=va+fs:blob=elf[off+low-va:off+high-va]
assert blob is not None and (b/'linux.bin').read_bytes()[low-0x42140000:high-0x42140000]==blob
subprocess.run(['python3',str(r/'build/test-cache-guard.py'),str(b/'cache-template.dis')],check=True)
subprocess.run(['python3',str(r/'build/test-flash-guard.py')],check=True)
subprocess.run(['python3',str(r/'build/test-flash-guard-result.py')],check=True)
scenario_count=json.loads((b/'cache-fault-tests.json').read_text())['passed']
m=json.loads((b/'integration-manifest.json').read_text())
m.update(profile='flash-guard-tag-preserving-cache-test',source_patch_count=28,cache_changes_enabled=True,
    flash_writes_enabled=False,flashed=False,hardware_verified=False,cache_hardware_verified=False,
    cache_template_bytes=len(blob),cache_template_sha256=hashlib.sha256(blob).hexdigest(),
    cache_reachable_instructions_checked=len(seen),cache_fault_scenarios_passed=scenario_count,
    control_bytes=control_bytes,cache_live_read_only=True,
    manual_status_acknowledgement='mode 3 only; CPU0 IBUS_WR_ICACHE 0x02 only; preserve original flag and require clean readback',
    control_offset='0xe00',cache_code_offset='0x100',guard_sram={'start':'0x3fcc8000','end':'0x3fcca000','bytes':8192,'runtime_verified':False},
    prior_sram_hardware_gate_passed=True,rootfs_changed=False,
    limitations=['Manual mode1 repeats SRAM test; strict mode2 suspends/resumes caches; mode3 also acknowledges only the recorded CPU0 0x02 status latch. No mode sends flash commands.',
      'Static and instruction-model checks are not hardware proof; physical register-window/cache timing and this exact SRAM layout require board validation.',
      'A PSRAM value-survival probe does not establish dirty-line residency at guard entry.',
      'Irrecoverable preload/autoload/freeze/restore failures keep both CPUs in SRAM until user reset; unsafe return is forbidden.',
      'Native flash erase/write, JFFS2 mounts and SD-free boot remain unimplemented.'])
(b/'integration-manifest.json').write_text(json.dumps(m,indent=2)+'\n')
print(f'PASS: exact linked {len(blob)}-byte cache template, {len(seen)} reachable instructions, constrained SRAM/MMIO, CPU0-only release, complete IRQ boundary and {scenario_count} fault scenarios')
