#!/usr/bin/env python3
"""Execute the actual disassembled SRAM guard under a bounded MMIO/race model.

This checks instruction paths and absolute addresses, not physical Xtensa
windows, cache timing or hardware execution. Those remain board gates.
"""
from pathlib import Path
import argparse,json,random,re
p=argparse.ArgumentParser();p.add_argument('disassembly',type=Path);a=p.parse_args()
ins={}
for line in a.disassembly.read_text().splitlines():
    m=re.match(r'\s*([0-9a-f]+):\s+([0-9a-f]{4,8})\s+(\S+)\s*(.*)',line)
    if m:ins[int(m[1],16)]=(len(m[2])//2,m[3],m[4])
lo=min(ins);ins={pc-lo:x for pc,x in ins.items()}
frame_bytes=int(ins[0][2].split(',')[1],0)
assert frame_bytes in (32,64)
has_native=any(op=='callx0' for _,op,_ in ins.values())
root=Path(__file__).resolve().parent.parent
h=(root/'experiments/flash-guard/esp32s3-flash-guard-layout.h').read_text()
f={k:int(v,0) for k,v in re.findall(r'^#define (FGC_\w+) (0x[0-9a-f]+|\d+)\s*$',h,re.M)}
BASE=0x3fcc8000;CTRL=BASE+0xe00;MMIO=0x600c4000
regs={'i':(0x60,0x64,0xa0,0x94,0x154),'d':(0,4,0x4c,0x40,0x150)}
class Model:
    def __init__(self,fault='',auto=True,freeze_mode=0,route=-1,nmi=-1,ack=False,native=False,native_result=0,global_status=0,after_status=0):
        self.fault=fault;self.words={};self.mm={0xe4:global_status,0xf0:0,0xfc:0};self.clock=0xffff0000
        self.after_status=after_status
        self.cpus=[];self.writes=[];self.status_writes=[];self.route=route;self.nmi=nmi;self.frozen=set()
        self.words[CTRL+f['FGC_ACK_REQUEST']]=int(ack)
        self.native=native;self.native_result=native_result;self.native_calls=0
        if native:
            assert has_native
            self.words[CTRL+f['FGC_NOR_TASK']]=0x3fcc7f00
            self.words[CTRL+f['FGC_NOR_ENTRY']]=0x403b70bc
        for c,(cr,bus,aut,pre,fr) in regs.items():
            self.mm.update({cr:0x101,bus:0,aut:0x108|(4 if auto else 0),pre:2,fr:freeze_mode})
        if fault=='active_freeze':self.mm[0x154]|=1
        if fault=='buffer_busy':self.mm[0xa0]|=512
        if fault=='prior_error':self.mm[0xe4]|=1
        if fault in ['cpu0_ibus_write','cpu0_ibus_recurs']:self.mm[0xf0]=2
        if fault=='cpu0_other_error':self.mm[0xf0]=4
        if fault=='cpu0_mixed_error':self.mm[0xf0]=6
        if fault=='cpu1_ibus_write':self.mm[0xfc]=2
        self.original=self.mm.copy()
    def stage(self):return self.words.get(CTRL+f['FGC_STAGE'],0)
    def cache_accessible(self):return all((self.mm[cr]&1) and not(self.mm[bus]&3) for cr,bus,*_ in regs.values())
    def read(self,addr,cpu):
        if BASE<=addr<BASE+0x2000 or self.native and 0x3fcc7f00<=addr<0x3fcc8000:return self.words.get(addr,0)
        if any(b<=addr<b+99*4 for b in [0x600c2000,0x600c2800]):
            return 14 if addr==self.route else 6
        assert addr>=MMIO and addr-MMIO in set(self.mm)|{0x130},hex(addr)
        o=addr-MMIO
        if o==0xe4 and self.stage()>=3:
            self.mm[o]|=self.after_status
        if o==0x130:
            result=0
            for c,(cr,_,_,_,_) in regs.items():
                idle=not(self.fault==c+'.idle' and not(self.mm[cr]&1) and self.stage()<4)
                if self.fault==c+'.restore_idle' and self.stage()==4:idle=False
                result|=(1 if idle else 0)<<(0 if c=='i' else 12)
            return result
        v=self.mm[o]
        for c,(cr,bus,aut,pre,fr) in regs.items():
            if o==aut:
                busy=(self.fault==c+'.auto' and bool(v&512) and self.stage()<4) or (self.fault==c+'.auto_permanent' and self.stage()>=2)
                return (v&~8)|(0 if busy else 8)
            if o==pre:
                busy=self.fault==c+'.preload' and not(self.mm[cr]&1) and self.stage()<4
                return (v&~2)|(0 if busy else 2)
            if o==fr:
                done=bool(v&1) and self.fault!=c+'.freeze_on'
                if self.fault==c+'.freeze_off' and c in self.frozen:done=True
                return (v&~4)|(4 if done else 0)
        return v
    def write(self,addr,value,cpu):
        value&=0xffffffff
        if BASE<=addr<BASE+0x2000:
            if addr==CTRL+f['FGC_FINAL']:
                assert cpu==0,'peer wrote final release'
                if self.writes:
                    assert self.cache_accessible(),'release with cache/bus disabled'
                    for cr,bus,aut,pre,fr in regs.values():
                        for o,mask in [(cr,0xffffffff),(bus,0xffffffff),(aut,0xfffffff7),(fr,0xfffffffb)]:
                            assert (self.read(MMIO+o,cpu)^self.original[o])&mask==0,('bad release',hex(o))
                        assert not(self.read(MMIO+fr,cpu)&4),'release with freeze busy'
            self.words[addr]=value;return
        o=addr-MMIO
        allowed={o for cr,bus,aut,pre,fr in regs.values() for o in [cr,bus,aut,fr]}|{0xec}
        assert o in allowed,('unexpected MMIO write',hex(addr))
        assert cpu==0 and self.words.get(CTRL+4)==1,'mutation without internal peer'
        assert len(self.cpus)>1 and not self.cpus[1].done,'mutated after peer returned'
        if o==0xec:
            assert value==2 and self.words.get(CTRL+f['FGC_ACK_REQUEST'])==1,'unexpected status clear'
            assert self.words.get(CTRL+f['FGC_ERRORS_BEFORE']+4)==2,'unrecorded status clear'
            assert self.stage()==1 and not self.writes,'status clear outside preflight'
            self.status_writes.append((addr,value))
            if self.fault!='cpu0_ibus_recurs':self.mm[0xf0]&=~value
            return
        self.writes.append((addr,value))
        for c,(cr,bus,aut,pre,fr) in regs.items():
            if o==fr and value&1:self.frozen.add(c)
            if o==cr and self.stage()==4 and self.fault==c+'.readback':value^=0x100
        self.mm[o]=value
class CPU:
    def __init__(self,m,cpu):
        self.m=m;self.id=cpu;self.pc=0;self.r=[0]*16;self.done=False
        self.oldsp=0x3d500000+cpu*0x4000;self.r[0]=0x42222222;self.r[1]=self.oldsp
        self.r[2]=CTRL;self.r[3]=BASE+0x1800+cpu*0x800;self.r[4]=cpu;self.r[5]=12000000
    def step(self):
        if self.done:return
        assert self.pc in ins,('unreachable padding/bad PC',hex(self.pc))
        size,op,args=ins[self.pc];parts=[s.strip() for s in args.split(',')];nxt=self.pc+size
        def rr(s):return self.r[int(s[1:])]
        def imm(s):return int(s,0)
        def put(s,v):self.r[int(s[1:])]=v&0xffffffff
        def target(s):return int(s.split()[0],16)-lo
        if op=='entry':put(parts[0],rr(parts[0])-imm(parts[1]))
        elif op=='or':put(parts[0],rr(parts[1])|rr(parts[2]))
        elif op=='and':put(parts[0],rr(parts[1])&rr(parts[2]))
        elif op=='xor':put(parts[0],rr(parts[1])^rr(parts[2]))
        elif op in ['add','sub']:put(parts[0],rr(parts[1])+(rr(parts[2]) if op=='add' else -rr(parts[2])))
        elif op=='addi':put(parts[0],rr(parts[1])+imm(parts[2]))
        elif op=='slli':put(parts[0],rr(parts[1])<<imm(parts[2]))
        elif op=='extui':put(parts[0],(rr(parts[1])>>imm(parts[2]))&((1<<imm(parts[3]))-1))
        elif op=='movi':put(parts[0],imm(parts[1]))
        elif op=='l32i':put(parts[0],self.m.read((rr(parts[1])+imm(parts[2]))&0xffffffff,self.id))
        elif op=='s32i':self.m.write((rr(parts[1])+imm(parts[2]))&0xffffffff,rr(parts[0]),self.id)
        elif op=='call0':self.r[0]=0x403b8100+self.pc+size;nxt=target(parts[0])
        elif op=='callx0':
            assert self.id==0 and self.m.native and rr(parts[0])==0x403b70bc
            assert self.r[2]==0x3fcc7f00 and not self.m.cache_accessible()
            assert self.m.words[CTRL+f['FGC_PEER_OFF']]==1 and not self.m.cpus[1].done
            self.m.native_calls+=1
            self.m.words[0x3fcc7f00+16]=self.m.native_result
            # Exercise the call0 boundary with all caller-saved registers clobbered.
            for reg in [0,*range(2,12)]:self.r[reg]=0xdead0000+reg
        elif op=='j':nxt=target(parts[0])
        elif op in ['beq','bne','bltu']:
            x,y=rr(parts[0]),rr(parts[1]);take=x==y if op=='beq' else x!=y if op=='bne' else x<y
            if take:nxt=target(parts[2])
        elif op in ['bnez','beqz']:
            if bool(rr(parts[0]))==(op=='bnez'):nxt=target(parts[1])
        elif op in ['bbci','bbsi']:
            if bool(rr(parts[0])&(1<<imm(parts[1])))==(op=='bbsi'):nxt=target(parts[2])
        elif op=='rsr.ccount':put(parts[0],self.m.clock)
        elif op=='rsr.intenable':put(parts[0],0)
        elif op=='rsr.interrupt':put(parts[0],0x4000 if self.m.nmi==self.id else 0)
        elif op=='memw':pass
        elif op=='retw':
            assert self.m.cache_accessible(),'returned while cache disabled'
            assert self.r[1]==self.oldsp-frame_bytes and self.r[0]==0x42222222,'SP/return corrupted'
            self.done=True
        else:raise AssertionError(('unsupported instruction',op))
        self.pc=nxt;self.m.clock=(self.m.clock+500)&0xffffffff

def run(name,fault='',expected=0,auto=True,mode=0,route=-1,nmi=-1,peer=True,quantum=1,ack=False,native=False,native_result=0,global_status=0,after_status=0):
    m=Model(fault,auto,mode,route,nmi,ack,native,native_result,global_status,after_status);m.cpus=[CPU(m,0),CPU(m,1)]
    for rounds in range(40000):
        for cpu in m.cpus[:2 if peer else 1]:
            for _ in range(quantum):cpu.step()
        if all(c.done for c in m.cpus[:2 if peer else 1]):break
        if m.stage()==7:break
    if expected=='fatal':
        assert m.stage()==7 and not any(c.done for c in m.cpus),name
        assert not m.words.get(CTRL+f['FGC_FINAL']),name
    else:
        assert all(c.done for c in m.cpus[:2 if peer else 1]),name
        assert all(c.r[2]==expected for c in m.cpus[:2 if peer else 1]),(name,[c.r[2] for c in m.cpus])
        if expected==0:
            assert m.words.get(CTRL+f['FGC_PEER_OFF'])==1 and m.words[CTRL+f['FGC_STATE_OFF']]==0x1001
            assert m.words[CTRL+f['FGC_HOLD']]>=4800,name
            assert m.words[CTRL+f['FGC_SNAPSHOTS']]==7,name
        if not peer or expected==5 and not m.native_calls:assert not m.writes,name
        if fault.startswith('cpu0_ibus_'):
            assert m.words[CTRL+f['FGC_ERRORS_BEFORE']+4]==2,'prior flag was lost'
            assert len(m.status_writes)==int(ack),'status acknowledgement count'
            if expected==0:assert m.words[CTRL+f['FGC_ERRORS_AFTER']+4]==0
        if expected==5 and m.stage()==1:assert m.words[CTRL+f['FGC_SNAPSHOTS']]==1
    if global_status:
        assert m.words[CTRL+f['FGC_ERRORS_BEFORE']]==global_status,'raw status was masked in diagnostics'
    if after_status and expected==0:
        assert m.words[CTRL+f['FGC_ERRORS_AFTER']]&after_status==after_status
    if native:assert m.native_calls==int(expected in (0,5) and not fault and not(global_status&~0xf80)),name
    return {'case':name,'result':expected,'mmio_writes':len(m.writes),'status_writes':len(m.status_writes),'native_calls':m.native_calls}
results=[]
for q in [1,3,11]:
    for auto in [False,True]:results.append(run(f'normal-q{q}-auto{auto}',auto=auto,mode=2,quantum=q))
results.append(run('missing-peer',expected=3,peer=False))
for nmi in [0,1]:results.append(run(f'pending-nmi-{nmi}',expected=5,nmi=nmi))
for route in [0x600c2000,0x600c2800+98*4]:results.append(run(f'route-nmi-{route:x}',expected=5,route=route))
for fault in ['active_freeze','buffer_busy','prior_error']:results.append(run(fault,fault,5))
results.append(run('hardware-cpu0-flag-strict','cpu0_ibus_write',5))
results.append(run('hardware-cpu0-flag-acknowledged','cpu0_ibus_write',ack=True))
results.append(run('cpu0-flag-recurs-after-ack','cpu0_ibus_recurs',5,ack=True))
results.append(run('ack-mode-clean-entry',ack=True))
for status in [1<<bit for bit in range(7,12)]+[0xf80]:
    results.append(run(f'counter-before-{status:x}',global_status=status))
    results.append(run(f'counter-after-{status:x}',after_status=status))
results.append(run('counter-with-cpu0-ack','cpu0_ibus_write',ack=True,global_status=0xf80,after_status=0xf80))
for fault_bit in [1<<bit for bit in range(7)]+[0x1000]:
    results.append(run(f'counter-mixed-real-fault-{fault_bit:x}',expected=5,global_status=0xf80|fault_bit))
for fault in ['prior_error','cpu0_other_error','cpu0_mixed_error','cpu1_ibus_write']:
    results.append(run('ack-mode-rejects-'+fault,fault,5,ack=True))
for c in ['i','d']:
    for fault in ['auto','preload','freeze_on','idle']:results.append(run(c+'.'+fault,c+'.'+fault,3))
    for fault in ['auto_permanent','freeze_off','restore_idle','readback']:results.append(run(c+'.'+fault,c+'.'+fault,'fatal'))
# With the master absent, peer timeout must request abort but never return.
m=Model();m.cpus=[CPU(m,0),CPU(m,1)]
for _ in range(40000):m.cpus[1].step()
assert not m.cpus[1].done and m.words.get(CTRL+f['FGC_ABORT'])==1 and not m.writes
results.append({'case':'master-absent-peer-remains-internal','result':'parked','mmio_writes':0})
if has_native:
    for q in (1,3,11):
        results.append(run(f'native-call0-clobbers-q{q}',native=True,quantum=q))
        results.append(run(f'native-refusal-restores-q{q}',native=True,native_result=5,expected=5,quantum=q))
    results.append(run('native-precondition-no-call','prior_error',5,native=True))
    results.append(run('native-counter-wrap',native=True,global_status=0xf80,after_status=0xf80))
    results.append(run('native-counter-mmu-refused',native=True,expected=5,global_status=0xf80|0x20))
(a.disassembly.parent/'cache-fault-tests.json').write_text(json.dumps({'cases':results,'passed':len(results),'scope':'actual disassembled instruction simulation; windows/cache timing/hardware not emulated'},indent=2)+'\n')
print(f'PASS: {len(results)} actual-instruction scenarios: absolute MMIO, both CPUs, IRQ preflight, wraparound, partial-cache timeouts, restoration failures and peer timeout safety')
