#!/usr/bin/env python3
"""Replay the hardware log's two source registrations and check Linux fwspec."""
from pathlib import Path
import subprocess,tempfile,sys
r=Path(__file__).resolve().parent.parent
s=Path(sys.argv[1] if len(sys.argv)>1 else r/'build-output/wifi-smp-audit/current/esp_blob.c').read_text()
def fun(n):
 p=s.index(n+'(');a=s.rfind('\n',0,p)+1;b=s.index('{',p)+1;d=1
 while d:d+=(s[b]=='{')-(s[b]=='}');b+=1
 return s[a:b]
shim=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <errno.h>
typedef int32_t int32_t;
#define OSI_MAX_ISR 8
#define OSI_MATRIX_SOURCES 128
#define nr_cpu_ids 2
#define cpu_online(cpu) ((cpu)<2)
#define pr_info(...) ((void)0)
#define pr_debug(...) ((void)0)
#define pr_warn(...) ((void)0)
#define pr_err(...) ((void)0)
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define set_bit(n,p) ((p)[(n)/64]|=1UL<<((n)%64))
#define clear_bit(n,p) ((p)[(n)/64]&=~(1UL<<((n)%64)))
#define test_bit(n,p) (!!((p)[(n)/64]&(1UL<<((n)%64))))
static bool bitmap_empty(unsigned long*p,unsigned n){return !p[0]&&!p[1];}
static unsigned find_first_bit(unsigned long*p,unsigned n){for(unsigned i=0;i<n;i++)if(test_bit(i,p))return i;return n;}
#define for_each_set_bit(i,p,n) for(i=0;i<(n);i++)if(test_bit(i,p))
static unsigned long osi_intr_sources[8][2];static unsigned osi_intr_cpu[8];
static int osi_intr_source[8]={-1,-1,-1,-1,-1,-1,-1,-1},osi_intr_prio[8];
static struct{bool mapped;}osi_isrs[8];
static void osi_try_connect(int n){}
struct irq_fwspec{void*fwnode;unsigned param_count,param[16];};
static struct irq_fwspec saved;
static struct{void*intc_np;}blob={(void*)1};
static __typeof__(blob)*g_blob=&blob;
#define of_fwnode_handle(p) (p)
static int irq_create_fwspec_mapping(struct irq_fwspec*p){saved=*p;return 8;}
'''
main=r'''
int main(void){
 osi_set_intr(0,2,0,1);osi_set_intr(0,0,0,1);
 assert(osi_intr_source[0]==0 && test_bit(2,osi_intr_sources[0]));
 assert(osi_map_irq(0,osi_intr_source[0])==8);
 assert(saved.param_count==4 && saved.param[0]==0 && saved.param[1]==0 && saved.param[2]==0 && saved.param[3]==2);
 osi_set_intr(0,2,0,1);osi_map_irq(0,osi_intr_source[0]);assert(saved.param_count==4);
 osi_clear_intr(2,0);osi_map_irq(0,osi_intr_source[0]);assert(saved.param_count==3);
 osi_set_intr(0,2,0,1);osi_isrs[0].mapped=true;osi_set_intr(0,4,0,1);
 assert(!test_bit(4,osi_intr_sources[0]));osi_clear_intr(2,0);assert(test_bit(2,osi_intr_sources[0]));
 puts("PASS: BB row2 + MAC row0 retained, no duplicate source, pre-map clear and live-group guard");
}
'''
with tempfile.TemporaryDirectory() as d:
 src=Path(d)/'test.c';out=Path(d)/'test';src.write_text(shim+''.join(fun(n) for n in ['osi_map_irq','osi_set_intr','osi_clear_intr'])+main)
 subprocess.run(['cc','-std=gnu11','-fsanitize=address,undefined',str(src),'-o',str(out)],check=True)
 subprocess.run([str(out)],check=True)
