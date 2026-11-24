#!/usr/bin/env python3
"""Check the real cache publisher's address aliases, alignment and ordering."""
from pathlib import Path
import subprocess
import sys
import tempfile
source=Path(sys.argv[1]).read_text()
source='\n'.join(l for l in source.splitlines() if not l.startswith('#include'))
shim=r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
typedef uint32_t u32;
#define __iomem
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) ((~0U>>(31-(h))) & (~0U<<(l)))
#define DEFINE_RAW_SPINLOCK(n) int n
#define round_down(v,a) ((v)&~((unsigned long)(a)-1))
#define round_up(v,a) (((v)+(a)-1)&~((unsigned long)(a)-1))
#define mb() ((void)0)
#define panic(...) abort()
static unsigned locked,ops;
static u32 regs[64],addresses[2],sizes[2];
#define raw_spin_lock_irqsave(p,f) do { (void)(p); (f)=0; assert(!locked);locked=1; } while(0)
#define raw_spin_unlock_irqrestore(p,f) do { (void)(p);(void)(f);assert(locked);locked=0;}while(0)
static u32 fake_read(void *p) { return regs[((uintptr_t)p-0x600c4000UL)/4]; }
static void fake_write(u32 v,void *p) {
 unsigned off=(uintptr_t)p-0x600c4000UL; assert(locked);
 regs[off/4]=v;
 if (off==0x28 || off==0x88) {
  assert(ops<2 && off==(ops?0x88:0x28));
  assert(v&(ops?1:2));
  addresses[ops]=regs[(off+4)/4];sizes[ops]=regs[(off+8)/4]&0x7fffff;
  regs[off/4]=ops?2:8; ops++;
 }
}
#define readl(p) fake_read((void *)(p))
#define writel(v,p) fake_write(v,(void *)(p))
#define readl_poll_timeout_atomic(p,v,c,d,t) ((v)=readl(p),(c)?0:-1)
'''
tests=r'''
int main(void) {
 regs[0]=1<<3; regs[0x60/4]=1<<3; /* 32-byte data/instruction lines */
 esp32_sync_exec_cache(0x3d800003,0x3d800021);
 assert(!locked && ops==2 && addresses[0]==0x3d800000 && addresses[1]==0x43800000);
 assert(sizes[0]==2 && sizes[1]==2);
 ops=0;
 esp32_sync_exec_cache(0x43801001,0x43801010);
 assert(ops==2 && addresses[0]==0x3d801000 && addresses[1]==0x43801000);
 assert(sizes[0]==1 && sizes[1]==1);
 ops=0;
 esp32_sync_exec_cache(0x42140000,0x42150000);
 esp32_sync_exec_cache(0x3d800000,0x3d800000);
 esp32_sync_exec_cache(0x3d7fffff,0x3d800100);
 esp32_sync_exec_cache(0x3dffff00,0x3e000001);
 assert(ops==0 && !locked);
 regs[0]=0; regs[0x60/4]=1<<3; /* different line sizes */
 esp32_sync_exec_cache(0x3d800010,0x3d800030);
 assert(ops==2 && addresses[0]==0x3d800010 && addresses[1]==0x43800000);
 assert(sizes[0]==2 && sizes[1]==2);
 puts("PASS: D writeback precedes I invalidate, both aliases, line bounds, flash exclusion and shared lock");
}
'''
with tempfile.TemporaryDirectory(prefix='s3-exec-cache-') as tmp:
 binary=str(Path(tmp)/'cache-test')
 subprocess.run(['cc','-std=gnu17','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-x','c','-','-o',binary],input=shim+source+tests,text=True,check=True)
 subprocess.run([binary],check=True)
