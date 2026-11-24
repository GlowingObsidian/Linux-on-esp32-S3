#!/usr/bin/env python3
"""Exercise the actual callback's stack/IRQ boundary and error propagation.

Host stubs replace only hardware register operations; the callback logic and
argument calculations come from its actual source. Linked Xtensa ordering and
assembly safety are verified separately.
"""
from pathlib import Path
import re, tempfile, subprocess
r = Path(__file__).resolve().parent.parent
s = (r/'experiments/flash-guard/guard-test.c').read_text()
p = s.index('static int fg_cpu(')
b = s.index('{', p) + 1
depth = 1
while depth:
    depth += (s[b] == '{') - (s[b] == '}')
    b += 1
callback = s[p:b]
callback = re.sub(r'asm volatile\("rsr %0, ps" : "=a" \(ps\)\);', 'ps = test_ps;', callback)
callback = re.sub(r'asm volatile\("rsr %0, ps" : "=a" \(after_ps\)\);', 'after_ps = test_ps;', callback)
callback = re.sub(r'asm volatile\("rsr %0, windowstart" : "=a" \(windows\)\);', 'windows = 0x40;', callback)
assert 'asm volatile' not in callback
code = r'''
#include <stdint.h>
#include <stdlib.h>
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdio.h>
typedef uint32_t u32;
#define __iomem
#define __force
#include "esp32s3-flash-guard-layout.h"
struct fg_device {void *sram;unsigned long iram;u32 mode;};
static unsigned test_cpu,calls,mask_calls,spills,restore_calls;
static int assembly_result;
static bool masked, returned, corrupt_ps;
static u32 test_ps=0x40003;
static struct fg_device hw;
#define raw_smp_processor_id() test_cpu
#define writel(v,p) (*(volatile u32 *)(p)=(v))
static u32 fg_mask_all(void){assert(!masked);masked=true;mask_calls++;return 0x180044;}
static void spill_registers(void){assert(masked);spills++;}
static void fg_restore_all(u32 old){assert(masked && returned && old==0x180044);masked=false;restore_calls++;}
static int fake_assembly(struct fg_control *control,void *sp,unsigned n,u32 timeout){
 calls++;assert((void*)control==hw.sram+FG_CONTROL && n==test_cpu);
 assert(sp==hw.sram+FG_STACK(test_cpu)+FG_STACK_BYTES);
 if(hw.mode!=1){assert(masked && spills==mask_calls);assert(timeout==(hw.mode==4?1250000000U:FG_RENDEZVOUS_CYCLES));
   assert(control->irq_saved[n]==0x180044 && control->ps[n]==test_ps && control->windows[n]==0x40);}
 else{assert(!masked && timeout==120000000);}
 returned=true;if(corrupt_ps)test_ps^=0x10;return assembly_result;
}
''' + callback + r'''
static void setup(unsigned mode){hw.mode=mode;hw.iram=(uintptr_t)fake_assembly-(mode!=1?FG_CACHE_CODE:0);returned=false;}
int main(void){
 hw.sram=calloc(1,FG_BYTES);test_cpu=0;setup(1);assert(fg_cpu(&hw)==0 && calls==1);
 test_cpu=1;setup(1);assert(fg_cpu(&hw)==0 && calls==2);
 for(test_cpu=0;test_cpu<2;test_cpu++)for(unsigned mode=2;mode<=4;mode++){
  setup(mode);assembly_result=0;assert(fg_cpu(&hw)==0 && !masked);
  setup(mode);assembly_result=3;assert(fg_cpu(&hw)==-ETIMEDOUT && !masked);
  setup(mode);assembly_result=5;assert(fg_cpu(&hw)==-EBUSY && !masked);
  setup(mode);assembly_result=4;assert(fg_cpu(&hw)==-EIO && !masked);
 }
 assembly_result=0;test_cpu=0;setup(2);corrupt_ps=true;
 assert(fg_cpu(&hw)==-EIO && !masked);corrupt_ps=false;test_ps=0x40003;
 unsigned before=calls;test_cpu=2;setup(2);assert(fg_cpu(&hw)==-EINVAL && calls==before);
 assert(mask_calls==spills && mask_calls==restore_calls);
 free(hw.sram);puts("PASS: actual callback arguments, both modes/CPU stacks, complete IRQ mask before spill, IRQ restoration on success/error, negative errno and PS corruption checks");
}
'''
with tempfile.TemporaryDirectory() as temp:
    src=Path(temp)/'guard.c';out=Path(temp)/'guard';src.write_text(code)
    subprocess.run(['cc','-std=gnu11','-fsanitize=address,undefined','-I',str(r/'experiments/flash-guard'),str(src),'-o',str(out)],check=True)
    subprocess.run([str(out)],check=True)
