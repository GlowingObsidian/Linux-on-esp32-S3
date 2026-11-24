#!/usr/bin/env python3
"""Run the linked call0 NOR instructions against a bounded SPI1/flash model.

This verifies actual image instructions, absolute MMIO, SRAM/literal accesses,
wire commands, allowlists, cache alias invalidation and refusal/park paths.
Electrical timing, cache silicon and physical flash remain hardware gates.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import struct

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('artifacts', type=Path)
b = p.parse_args().artifacts
symbols = {v[2]: int(v[0], 16) for line in (b/'System.map').read_text().splitlines()
           if len(v := line.split()) >= 3}
start = symbols['esp32s3_nor_blob_start']
entry = symbols['esp32s3_nor_entry']
end = symbols['esp32s3_nor_blob_end']
elf = (b/'vmlinux').read_bytes()
ph = struct.unpack_from('<I', elf, 28)[0]
psz, count = struct.unpack_from('<HH', elf, 42)
blob = None
for n in range(count):
    typ, off, va, pa, fs, ms, flags, align = struct.unpack_from('<8I', elf, ph+n*psz)
    if typ == 1 and va <= start and end <= va+fs:
        blob = elf[off+start-va:off+end-va]
assert blob and 0 < len(blob) <= 0xf00
assert (b/'linux.bin').read_bytes()[start-0x42140000:end-0x42140000] == blob
ins = {}
for line in (b/'nor-template.dis').read_text().splitlines():
    m = re.match(r'\s*([0-9a-f]+):\s+([0-9a-f]{4,8})\s+(\S+)\s*(.*)', line)
    if m and entry <= int(m[1], 16) < end:
        ins[int(m[1], 16)] = (len(m[2])//2, m[3], m[4])

def target(s):
    return int(s.split()[0], 16)

allowed_ops = {'l32i', 's32i', 'addi', 'add', 'addmi', 'addx4', 'movi', 'mov',
               'l32r', 'extui', 'and', 'or', 'neg', 'sub', 'slli', 'srli',
               'sll', 'srl', 'ssl', 'ssr', 'memw', 'rsr.ccount', 'ret', 'j',
               'beqi', 'bnei', 'beqz', 'bnez', 'beq', 'bne', 'bltu', 'bgeu',
               'bltui', 'bbci', 'bbsi', 'bnall', 'bnone'}
todo, seen, literal_words = [entry], set(), set()
while todo:
    pc = todo.pop()
    if pc in seen:
        continue
    assert pc in ins, ('branch into padding', hex(pc))
    seen.add(pc)
    size, opcode, args = ins[pc]
    op = opcode.removesuffix('.n')
    assert op in allowed_ops, (hex(pc), opcode)
    parts = [s.strip() for s in args.split(',')]
    if op == 'l32r':
        literal = target(parts[1])
        assert start <= literal and literal+4 <= entry and literal % 4 == 0
        literal_words.add(literal)
    if op == 'ret':
        continue
    if op == 'j' or op.startswith('b'):
        dest = target(parts[-1])
        assert entry <= dest < end
        todo.append(dest)
        if op == 'j':
            continue
    todo.append(pc+size)

BASE, TASK, SPI1, SPI0, CACHE = 0x3fcc7000, 0x3fcc7f00, 0x60002000, 0x60003000, 0x600c4000
SAVE = [4, 8, *range(0x10, 0x2c, 4), 0x34, 0x3c, 0xa8, 0xe0]
U32 = 0xffffffff

class Model:
    def __init__(self, op=1, address=0, length=0, fault='', sr1=0, sr2=2,
                 jedec=0xef4018, ic_line=1, dc_line=1, timeout=1200000000, clock_gate=0):
        self.fault, self.op, self.address, self.length = fault, op, address, length
        self.clock, self.commands, self.syncs, self.writes = 0xffff0000, [], [], []
        self.ram = bytearray(0x3000)
        self.flash = {}
        self.mm = {SPI0: 0, SPI0+0x54: 0, SPI0+0xe0: 0, SPI0+8: 1 << 24,
                   SPI1: 0, SPI1+0x54: 0, SPI1+0x98: 0, SPI1+0xa0: 0,
                   SPI1+0xa4: 0, SPI1+0xe8: clock_gate, CACHE: 1 | dc_line << 3,
                   CACHE+0x60: 3 | ic_line << 3, CACHE+0x28: 0,
                   CACHE+0x88: 0, CACHE+0x2c: 0x3d123000, CACHE+0x30: 7,
                   CACHE+0x8c: 0x42140000, CACHE+0x90: 13}
        for off in SAVE:
            self.mm.setdefault(SPI1+off, 0x10000+off)
        self.mm[SPI1+0xe0] = 0
        for off in range(0x58, 0x98, 4):
            self.mm[SPI1+off] = 0x12345678
        self.original = {SPI1+off: self.mm[SPI1+off] for off in SAVE}
        self.original[SPI1+0xe8] = clock_gate
        self.initial_sync = {CACHE+off: self.mm[CACHE+off] for off in (0x2c, 0x30, 0x8c, 0x90)}
        self.sr1, self.sr2, self.busy, self.started = sr1, sr2, 0, False
        self.jedec = jedec
        self.wire_id = ((jedec >> 16) & 255) | (jedec & 0xff00) | ((jedec & 255) << 16)
        for i, value in enumerate((op, address, length, timeout, 0, jedec)):
            self.store(TASK+4*i, value)
        for i in range(16):
            self.store(TASK+48+4*i, 0xa5a50000+i)
        if op == 2:
            self.flash.update({address+i: (37*i) & 255 for i in range(length)})
        if fault == 'controller-active': self.mm[SPI1+0x54] = 1
        if fault == 'suspended-controller': self.mm[SPI1+0xa4] = 1
        if fault == 'flash-ddr': self.mm[SPI0+0xe0] = 1
        if fault == 'wide-command': self.mm[SPI0+8] |= 0x10
        if fault == 'dual-command': self.mm[SPI0+8] |= 1 << 7
        if fault == 'quad-command': self.mm[SPI0+8] |= 1 << 8
        if fault == 'cache-sync-active': self.mm[CACHE+0x28] = 1
        if fault == 'auto-resume': self.mm[SPI1+0xa0] = 1
        if fault == 'auto-waiti': self.mm[SPI1+0x98] = 1
        if fault == 'spi1-command-active': self.mm[SPI1] = 1 << 18
        if fault == 'spi0-command-active': self.mm[SPI0] = 1 << 31
        if fault == 'spi0-controller-active': self.mm[SPI0+0x54] = 4
        if fault == 'icache-sync-active': self.mm[CACHE+0x88] = 1
        if fault == 'unsupported-cache-line': self.mm[CACHE] = 1 | (3 << 3)

    def store(self, address, value):
        assert TASK <= address < TASK+120 or 0x3fcc9000 <= address < 0x3fcc9800
        struct.pack_into('<I', self.ram, address-BASE, value & U32)

    def read(self, address):
        if TASK <= address < TASK+120 or 0x3fcc9000 <= address < 0x3fcc9800:
            return struct.unpack_from('<I', self.ram, address-BASE)[0]
        assert address in self.mm, ('external/unknown load', hex(address))
        if address == SPI1 and self.started and self.fault == 'command-timeout':
            return 1 << 18
        if self.fault == 'restore-failure' and self.syncs and address == SPI1+0xe0:
            return self.mm[address] ^ 2
        return self.mm[address]

    def wire(self):
        assert self.mm[SPI1+8] == (1 << 18) | (1 << 19), 'single-line reference polarity'
        assert self.mm[SPI1+0x14] == 0x170b17
        assert self.mm[SPI1+0x34] & 0xc03 == 2
        command = self.mm[SPI1+0x20] & 0xffff
        assert self.mm[SPI1+0x20] >> 28 == 7
        addr = self.mm[SPI1+4]
        inp = (self.mm[SPI1+0x28]+1)//8
        out = (self.mm[SPI1+0x24]+1)//8
        phase = {0x05:(0,1,0), 0x35:(0,1,0), 0x9f:(0,3,0), 0x03:(24,self.length,0),
                 0x06:(0,0,0), 0x04:(0,0,0), 0x02:(24,0,self.length), 0xd8:(24,0,0)}
        assert command in phase, ('unexpected flash command', hex(command))
        bits, ri, wo = phase[command]
        assert (inp, out) == (ri, wo)
        assert self.mm[SPI1+0x1c] == ((bits-1) << 26 if bits else 0)
        expected_user = 0x800000c0 | (0x40000000 if bits else 0) | (0x10000000 if ri else 0) | (0x08000000 if wo else 0)
        assert self.mm[SPI1+0x18] == expected_user
        self.commands.append(command)
        if command == 0x05:
            if self.fault in ('initial-busy-timeout', 'flash-timeout') and (self.fault.startswith('initial') or self.busy):
                self.mm[SPI1+0x58] = self.sr1 | 1
            elif self.busy:
                self.busy -= 1
                self.mm[SPI1+0x58] = self.sr1 | 1
            else:
                self.mm[SPI1+0x58] = self.sr1
        elif command == 0x35: self.mm[SPI1+0x58] = self.sr2
        elif command == 0x9f: self.mm[SPI1+0x58] = self.wire_id
        elif command == 0x06:
            if self.fault != 'wren-refused': self.sr1 |= 2
        elif command == 0x04:
            if self.fault != 'wrdi-refused': self.sr1 &= ~2
            else: self.sr1 |= 2
        elif command == 0x03:
            assert addr == self.address
            for i in range((ri+3)//4):
                self.mm[SPI1+0x58+4*i] = sum(self.flash.get(addr+4*i+k,255) << (8*k) for k in range(4))
        else:
            assert self.sr1 & 3 == 2 and not self.sr1 & 0x7c and not self.sr2 & 0xc0
            assert addr == self.address
            assert 0xd0000 <= addr < 0x140000 or 0xc00000 <= addr < 0x1000000
            if command == 0xd8:
                assert addr & 0xffff == 0
                self.flash = {a:v for a,v in self.flash.items() if not addr <= a < addr+0x10000}
            else:
                assert (addr & 255)+wo <= 256
                for i in range(wo):
                    value = self.mm[SPI1+0x58+4*(i//4)] >> (8*(i%4)) & 255
                    self.flash[addr+i] = self.flash.get(addr+i,255) & value
            self.busy = 2
            self.sr1 &= ~2

    def write(self, address, value):
        value &= U32
        if TASK <= address < TASK+120 or 0x3fcc9000 <= address < 0x3fcc9800:
            return self.store(address,value)
        assert address in self.mm and address not in (SPI0, SPI0+8, SPI0+0x54, SPI0+0xe0, CACHE, CACHE+0x60), ('unexpected MMIO store',hex(address))
        self.writes.append((address,value))
        self.mm[address] = value
        if address == SPI1:
            command=self.mm[SPI1+0x20] & 0xffff
            assert value == (1 << 18) | ((1 << 17) if command in (0x02,0xd8) else 0), 'missing/wrong program-erase indication'
            self.started = True
            self.wire()
            self.mm[SPI1] = (1 << 17) if self.fault=='pe-clear-delayed' and command in (0x02,0xd8) else 0
        if address in (CACHE+0x28,CACHE+0x88) and value:
            assert value == 1, 'clean/writeback/global invalidation forbidden'
            a, lines = self.mm[address+4], self.mm[address+8]
            shift = 4+((self.mm[CACHE+0x60] >> 3) & 1) if address == CACHE+0x88 else 4+((self.mm[CACHE] >> 3) & 3)
            mask = (1 << shift)-1
            alias = 0x42000000 if address == CACHE+0x88 else 0x3c000000
            size = 0x10000 if self.op == 4 else self.length
            assert a == (alias+self.address) & ~mask
            assert lines == (size+(self.address & mask)+mask) >> shift
            assert alias <= a and a+(lines << shift) <= alias+0x1000000
            self.syncs.append((a,lines))
            self.mm[address] = 1 if self.fault == 'invalidate-timeout' else (2 if address == CACHE+0x88 else 8)

class CPU:
    def __init__(self, model):
        self.m, self.pc, self.r, self.sar = model, entry, [0]*16, 0
        self.r[0], self.r[1], self.r[2] = 0x42424242, 0x3fcc97c0, TASK
        for n in range(12,16): self.r[n] = 0xdead1000+n

    def run(self):
        for _ in range(250000):
            size, opcode, args = ins[self.pc]
            op = opcode.removesuffix('.n')
            parts = [s.strip() for s in args.split(',')]
            nxt = self.pc+size
            def rr(s): return self.r[int(s[1:])]
            def put(s,v): self.r[int(s[1:])] = v & U32
            def imm(s): return int(s,0)
            if op in ('mov','movi'): put(parts[0], rr(parts[1]) if op=='mov' else imm(parts[1]))
            elif op in ('addi','addmi'): put(parts[0], rr(parts[1])+imm(parts[2]))
            elif op in ('add','addx4','sub','and','or'):
                x,y = rr(parts[1]),rr(parts[2])
                put(parts[0], x+y if op=='add' else x*4+y if op=='addx4' else x-y if op=='sub' else x&y if op=='and' else x|y)
            elif op=='neg': put(parts[0],-rr(parts[1]))
            elif op=='extui': put(parts[0], rr(parts[1]) >> imm(parts[2]) & ((1 << imm(parts[3]))-1))
            elif op in ('slli','srli'): put(parts[0], rr(parts[1]) << imm(parts[2]) if op=='slli' else rr(parts[1]) >> imm(parts[2]))
            elif op in ('ssl','ssr'): self.sar = (32-rr(parts[0]) if op=='ssl' else rr(parts[0])) & 63
            elif op in ('sll','srl'): put(parts[0], rr(parts[1]) << (32-self.sar) if op=='sll' and self.sar<=32 else rr(parts[1]) >> self.sar if op=='srl' else 0)
            elif op=='l32r': put(parts[0], struct.unpack_from('<I',blob,target(parts[1])-start)[0])
            elif op=='l32i': put(parts[0], self.m.read((rr(parts[1])+imm(parts[2])) & U32))
            elif op=='s32i': self.m.write((rr(parts[1])+imm(parts[2])) & U32,rr(parts[0]))
            elif op=='rsr.ccount': put(parts[0],self.m.clock)
            elif op=='memw': pass
            elif op=='j':
                nxt=target(parts[0])
                # The only sanctioned permanent waits are memw/j SRAM park loops.
                if ins[nxt][1]=='memw' and nxt+ins[nxt][0]==self.pc:
                    assert self.m.read(TASK+16) in (3,6)
                    return 'parked'
            elif op.startswith('b'):
                x=rr(parts[0])
                y=imm(parts[1]) & U32 if op in ('beqi','bnei','bltui') else rr(parts[1]) if len(parts)>2 and op not in ('bbci','bbsi') else 0
                take = x==y if op in ('beq','beqi') else x!=y if op in ('bne','bnei') else x==0 if op=='beqz' else x!=0 if op=='bnez' else x<y if op in ('bltu','bltui') else x>=y if op=='bgeu' else (x&y)!=y if op=='bnall' else (x&y)==0 if op=='bnone' else bool(x & (1 << imm(parts[1])))==(op=='bbsi')
                if take: nxt=target(parts[-1])
            elif op=='ret':
                assert self.r[0]==0x42424242 and self.r[1]==0x3fcc97c0
                assert all(self.r[n]==0xdead1000+n for n in range(12,16)), 'call0 callee-saved register corruption'
                return self.m.read(TASK+16)
            else: raise AssertionError(op)
            self.pc=nxt
            self.m.clock=(self.m.clock+100000) & U32
        raise AssertionError('unbounded instruction path')

cases=[]
def check(name, expected=0, preflight=None, **kw):
    m=Model(**kw)
    result=CPU(m).run()
    assert result==expected,(name,result,expected)
    if preflight is not None:
        assert (m.read(TASK+112),m.read(TASK+116))==preflight,(name,'wrong rejected register')
        assert not m.commands and not m.writes,(name,'preflight changed hardware')
    if result!='parked' and m.read(TASK+44):
        assert all(m.mm[a]==v for a,v in m.original.items()),name
        assert all(m.mm[a]==v for a,v in m.initial_sync.items()),name
    if expected==0:
        assert m.read(TASK+112)==0 and m.read(TASK+116)==0
        assert m.read(TASK+44)==1
        assert len(m.syncs)==(2 if m.op in (3,4) else 0)
        if m.op==1: assert m.read(TASK+20)==m.jedec
        if m.op==3:
            assert bytes(m.flash[m.address+i] for i in range(m.length)) == bytes(m.ram[TASK-BASE+48:TASK-BASE+48+m.length])
        if m.op==2:
            assert bytes(m.ram[TASK-BASE+48:TASK-BASE+48+m.length]) == bytes((37*i)&255 for i in range(m.length))
    elif expected==5: assert 0x02 not in m.commands and 0xd8 not in m.commands and not m.syncs
    cases.append(dict(case=name,result=result,commands=len(m.commands),cache_syncs=len(m.syncs)))

check('jedec-read-only')
check('unsupported-jedec-identification',jedec=0xaa4018)
check('gd25q128-identification',jedec=0xc84018)
for op in (2,3,4):
    for address in (0xd0000,0xff0000):
        check(f'gd25q128-op{op}-{address:x}',op=op,address=address,length=64 if op!=4 else 0,jedec=0xc84018)
for sr1,sr2 in ((4,2),(0x40,2),(0,0x42),(0,0x82)):
    check(f'gd25q128-protected-{sr1:x}-{sr2:x}',5,op=3,address=0xff0000,length=64,sr1=sr1,sr2=sr2,jedec=0xc84018)
for op in (2,3,4):
    for addr in (0xd0000,0x130000,0xc00000,0xff0000):
        for dc in (0,1,2): check(f'op{op}-{addr:x}-d{dc}',op=op,address=addr+(1 if op!=4 else 0),length=63 if op!=4 else 0,dc_line=dc,ic_line=dc&1)
for addr,length in ((0,1),(0xcffff,1),(0x13ffff,2),(0x140000,1),(0xb3ffff,1),(0xffffff,2),(0xffffffff,64)):
    check(f'allowlist-{addr:x}-{length}',5,op=3,address=addr,length=length)
for length in (0,65,0xffffffff): check(f'bad-length-{length}',5,op=3,address=0xd0000,length=length)
check('page-crossing',5,op=3,address=0xd00f0,length=64)
check('unaligned-erase',5,op=4,address=0xff0001)
check('unsupported-chip-write',5,op=3,address=0xd0000,length=64,jedec=0xaa4018)
for op in (0,5): check(f'bad-op-{op}',5,op=op)
for timeout in (0,1200000001): check(f'bad-timeout-{timeout}',5,timeout=timeout)
for fault,reg in (('controller-active',SPI1+0x54),('suspended-controller',SPI1+0xa4),
                  ('flash-ddr',SPI0+0xe0),
                  ('wide-command',SPI0+8),('cache-sync-active',CACHE+0x28),
                  ('auto-resume',SPI1+0xa0),('auto-waiti',SPI1+0x98)):
    value=((1<<24)|0x10) if fault=='wide-command' else 1
    check(fault,5,fault=fault,preflight=(reg,value))
for gate in (0,1):
    for jedec in (0xc84018,0xef4018):
        for op in (1,2,3,4):
            check(f'register-clock-gate-{gate}-jedec{jedec:x}-op{op}',
                  op=op,address=0 if op==1 else 0xff0000,
                  length=64 if op in (2,3) else 0,clock_gate=gate,jedec=jedec)
for fault,reg,value in (('spi1-command-active',SPI1,1<<18),
                        ('spi0-command-active',SPI0,1<<31),
                        ('spi0-controller-active',SPI0+0x54,4),
                        ('icache-sync-active',CACHE+0x88,1),
                        ('unsupported-cache-line',CACHE,1|(3<<3))):
    check(fault,5,fault=fault,preflight=(reg,value))
for fault,bit in (('dual-command',7),('quad-command',8)):
    check(fault,5,fault=fault,preflight=(SPI0+8,(1<<24)|(1<<bit)))
check('user-completion-before-pe-clear',op=3,address=0xff0000,length=64,fault='pe-clear-delayed')
for sr1,sr2 in ((4,2),(0x40,2),(0,0x42),(0,0x82)):
    check(f'protected-or-suspended-{sr1:x}-{sr2:x}',5,op=3,address=0xff0000,length=64,sr1=sr1,sr2=sr2)
check('preexisting-wel',5,sr1=2)
check('wren-refused',5,op=3,address=0xff0000,length=64,fault='wren-refused')
for fault in ('command-timeout','initial-busy-timeout','flash-timeout','invalidate-timeout','restore-failure','wrdi-refused'):
    check(fault,'parked',op=3,address=0xff0000,length=64,fault=fault)
report=dict(passed=len(cases),reachable_instructions=len(seen),literal_words=len(literal_words),
            template_bytes=len(blob),template_sha256=hashlib.sha256(blob).hexdigest(),cases=cases,
            scope='Actual linked instruction model; hardware timing/cache/flash not emulated')
(b/'nor-fault-tests.json').write_text(json.dumps(report,indent=2)+'\n')
print(f'PASS: {len(cases)} linked NOR scenarios; {len(seen)} instructions, {len(literal_words)} SRAM literals; only SPI1 commands, allowlisted writes, two flash-only invalidations, ABI restore and fail-closed parks')
