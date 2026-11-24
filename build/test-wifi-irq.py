#!/usr/bin/env python3
"""Run actual OSI IRQ transition callbacks, including enable-before-map."""
from pathlib import Path
import tempfile, subprocess, sys
repo=Path(__file__).resolve().parent.parent
s=Path(sys.argv[1] if len(sys.argv)>1 else repo/'build-output/wifi-smp-audit/current/esp_blob.c').read_text()
def fun(n):
 p=s.index(n+'(');a=s.rfind('\n',0,p)+1;b=s.index('{',p)+1;d=1
 while d:d+=(s[b]=='{')-(s[b]=='}');b+=1
 return s[a:b]
shim=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#define OSI_MAX_ISR 8
#define CONFIG_SMP 1
#define cpumask_of(cpu) (cpu)
static unsigned osi_intr_cpu[8];
static int irq_set_affinity(int irq,unsigned cpu){assert(cpu<2);return 0;}
#define BIT(n) (1U<<(n))
#define IRQF_ONESHOT 1
#define IRQF_NO_AUTOEN 2
#define pr_err(...) ((void)0)
#define pr_info(...) ((void)0)
#define pr_debug(...) ((void)0)
#define spin_lock_irqsave(l,f) ((void)(f=0))
#define spin_unlock_irqrestore(l,f) ((void)0)
struct osi_isr_slot {void(*fn)(void*);void*arg;int virq;bool mapped;};
static struct osi_isr_slot osi_isrs[8];static int osi_intr_source[8];
static unsigned osi_ints_enabled;static int osi_isr_lock;
static int depth, requested, disposed, map_result=10, request_result;
static int osi_isr_thunk(int irq,void*p){return 1;}
static int osi_map_irq(int n,int source){return map_result;}
static int request_threaded_irq(int irq,void*p,int(*fn)(int,void*),unsigned flags,const char*name,void*s){
 assert(flags==(IRQF_ONESHOT|IRQF_NO_AUTOEN));requested++;
 if(!request_result)depth=1;return request_result;
}
static void enable_irq(int irq){assert(depth==1);depth--;}
static void disable_irq_nosync(int irq){assert(depth==0);depth++;}
static void irq_dispose_mapping(int irq){disposed++;}
static void handler(void*p){}
'''
main=r'''
int main(void){
 osi_isrs[0].fn=handler;osi_intr_source[0]=3;
 osi_ints_on(1);assert(osi_ints_enabled==1 && !requested);
 osi_try_connect(0);assert(osi_isrs[0].mapped && requested==1 && depth==0);
 osi_ints_on(1);assert(depth==0);osi_ints_off(1);assert(depth==1);
 osi_ints_off(1);assert(depth==1);osi_ints_on(1);assert(depth==0);
 osi_try_connect(0);assert(requested==1);
 osi_isrs[1].fn=handler;osi_intr_source[1]=4;
 osi_try_connect(1);assert(depth==1);osi_ints_on(2);assert(depth==0);
 osi_isrs[3].fn=handler;osi_intr_source[3]=7;map_result=0;
 osi_try_connect(3);assert(requested==2 && !osi_isrs[3].mapped);
 map_result=11;request_result=-1;osi_try_connect(3);
 assert(disposed==1 && !osi_isrs[3].mapped);
 puts("PASS: enable-before-map, disable depth, repeated transitions, failed mapping and request cleanup");
}
'''
code=shim+'\n'.join(fun(n) for n in ['osi_try_connect','osi_ints_on','osi_ints_off'])+main
with tempfile.TemporaryDirectory() as d:
 src=Path(d)/'test.c';out=Path(d)/'test';src.write_text(code)
 subprocess.run(['cc','-std=gnu11','-fsanitize=address,undefined',str(src),'-o',str(out)],check=True)
 subprocess.run([str(out)],check=True)
