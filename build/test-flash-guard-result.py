#!/usr/bin/env python3
"""Replay the board's failure through the actual sysfs test handler.

Hardware entry is stubbed; the handler's errno, SRAM checks and validation
are compiled directly from source. The assembly has separate linked tests.
"""
from pathlib import Path
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parent.parent
source = (root / 'experiments/flash-guard/guard-test.c').read_text()
start = source.index('static ssize_t test_store(')
end = source.index('{', start) + 1
depth = 1
while depth:
    depth += (source[end] == '{') - (source[end] == '}')
    end += 1
handler = source[start:end]
handler = handler.replace('asm volatile("memw\\n\\tisync" ::: "memory");', '(void)0;')
assert 'asm volatile' not in handler
device = re.search(r'struct fg_device \{.*?\n\};', source, re.S)[0]
live = source[source.index('static void fg_read_errors('):source.index('static DEVICE_ATTR_RO(cache_live);')]
prefix = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
typedef uint8_t u8;
typedef uint32_t u32;
#define __iomem
#define __force
#define GFP_KERNEL 0
#include "esp32s3-flash-guard-layout.h"
struct mutex { int unused; };
struct device { void *data; };
struct device_attribute { int unused; };
'''
stubs = r'''
static struct fg_device hw;
static struct device dev;
static int injected, locks, entries;
static bool alloc_fail;
static int selected;
enum { OK, BOARD_FLAG, RECURRENT_FLAG, TIMEOUT, NEW_ERROR, BAD_PC,
       BAD_CANARY, BAD_ACK, MIXED_ERROR, COUNTER_BEFORE, COUNTER_AFTER,
       COUNTER_ACK, COUNTER_MMU, COUNTER_OCCUPY, COUNTER_UNKNOWN, COUNTER_CPU };
#define dev_get_drvdata(d) ((d)->data)
#define readl(p) (*(volatile u32 *)(p))
#define writel(v,p) (*(volatile u32 *)(p)=(v))
#define memset_io(p,v,n) memset((p),(v),(n))
#define sysfs_emit(b,...) snprintf((b),4096,__VA_ARGS__)
#define dev_warn(d,...) ((void)0)
#define dev_info(d,...) ((void)0)
static int kstrtouint(const char *s,unsigned base,unsigned *v) {
    char *end; unsigned long n=strtoul(s,&end,base);
    if (!*s || (*end && *end!='\n') || n>UINT32_MAX) return -EINVAL;
    *v=n; return 0;
}
static void mutex_lock(struct mutex *m) { assert(!locks); locks++; }
static void mutex_unlock(struct mutex *m) { assert(locks==1); locks--; }
static void *kmalloc(size_t n,int flags) { return alloc_fail ? NULL : malloc(n); }
static void kfree(void *p) { free(p); }
static void cpus_read_lock(void) {}
static void cpus_read_unlock(void) {}
static int num_online_cpus(void) { return 2; }
static bool cpu_online(unsigned cpu) { return cpu<2; }
static void *cpu_online_mask;
static int fg_cpu(void *arg) { return 0; }
static int stop_machine_cpuslocked(int (*entry)(void *),void *arg,void *mask) {
    struct fg_control *c=hw.sram+FG_CONTROL;
    assert(arg==&hw && entry==fg_cpu); entries++;
    bool cache=hw.mode!=1;
    assert(c->ack_request==(hw.mode==3));
    for(unsigned cpu=0;cpu<2;cpu++) {
        c->ready[cpu]=1;
        c->sp[cpu]=FG_BASE+FG_STACK(cpu)+FG_STACK_BYTES-32;
        c->pc[cpu]=FG_BASE+FG_ALIAS+(cache?FG_CACHE_CODE:0)+4;
    }
    if(!cache) return 0;
    c->stage=5; c->attempt=1; c->mutated=3; c->peer_off=1;
    c->suspended[2]=3; c->suspended[3]=3;
    c->state_off=0x1001; c->hold_cycles=FG_HOLD_CYCLES; c->snapshots=7;
    if(selected==COUNTER_BEFORE || selected==COUNTER_ACK)
        c->errors_before[0]=FG_CACHE_COUNTER_STATUS_MASK;
    if(selected==COUNTER_AFTER) c->errors_after[0]=FG_CACHE_COUNTER_STATUS_MASK;
    if(selected==COUNTER_ACK) c->errors_ack[0]=FG_CACHE_COUNTER_STATUS_MASK;
    if(selected==COUNTER_MMU) c->errors_after[0]=FG_CACHE_COUNTER_STATUS_MASK|0x20;
    if(selected==COUNTER_OCCUPY) c->errors_after[0]=FG_CACHE_COUNTER_STATUS_MASK|0x40;
    if(selected==COUNTER_UNKNOWN) c->errors_after[0]=FG_CACHE_COUNTER_STATUS_MASK|0x1000;
    if(selected==COUNTER_CPU) c->errors_after[1]=0x80;
    if(selected==BOARD_FLAG || selected==RECURRENT_FLAG || selected==MIXED_ERROR || selected==COUNTER_ACK) {
        c->errors_before[1]=selected==MIXED_ERROR?6:2;
        if(hw.mode==3 && selected!=MIXED_ERROR) {
            c->acknowledged=2;
            if(selected!=RECURRENT_FLAG) return 0;
            c->errors_ack[1]=2;
        }
        c->stage=1; c->attempt=0; c->mutated=0;
        c->peer_off=0; c->state_off=0; c->hold_cycles=0; c->snapshots=1;
        memset(c->suspended,0,sizeof(c->suspended));
        c->result[0]=c->result[1]=5; return -EBUSY;
    }
    if(selected==TIMEOUT) {
        c->stage=1; c->attempt=0; c->snapshots=1;
        c->ready[1]=0; c->result[0]=c->result[1]=3; return -ETIMEDOUT;
    }
    if(selected==NEW_ERROR) c->errors_after[1]=2;
    if(selected==BAD_PC) c->pc[1]=0x42000000;
    if(selected==BAD_CANARY) writel(0,hw.sram+FG_STACK(0));
    if(selected==BAD_ACK) { c->acknowledged=4; c->errors_before[1]=4; }
    return injected;
}
'''
main = r'''
static void check(unsigned mode,int scenario,int expected,bool internal) {
    selected=scenario; char text[8]; snprintf(text,sizeof(text),"%u",mode);
    ssize_t ret=test_store(&dev,NULL,text,strlen(text));
    assert(ret==(expected?expected:(int)strlen(text)));
    assert(hw.last_result==expected && !locks);
    assert(hw.checked[0]==internal);
    if(scenario!=TIMEOUT && scenario!=BAD_PC && scenario!=BAD_CANARY)
        assert(hw.checked[1]==internal);
}
int main(void) {
    hw.sram=calloc(1,FG_BYTES); hw.iram=FG_BASE+FG_ALIAS;
    hw.code_bytes=162; hw.cache_bytes=2600; dev.data=&hw;
    u32 registers[0x158 / 4] = {0}, saved[0x158 / 4];
    char diagnostic[4096];
    hw.cache=registers;
    registers[0xe4 / 4]=0x20;
    registers[0x120 / 4]=0x94000;
    registers[0x124 / 4]=0x42832e54;
    memcpy(saved,registers,sizeof(saved));
    assert(cache_live_show(&dev,NULL,diagnostic)>0 && !locks && !entries);
    assert(strstr(diagnostic,"errors=00000020,00000000,00000000"));
    assert(strstr(diagnostic,"mmu_fault_content=00094000 mmu_fault_vaddr=42832e54"));
    assert(!memcmp(saved,registers,sizeof(saved)));
    registers[0xe4 / 4]=registers[0x120 / 4]=registers[0x124 / 4]=0;
    assert(cache_live_show(&dev,NULL,diagnostic)>0 && !locks && !entries);
    assert(strstr(diagnostic,"mmu_fault_content=00000000 mmu_fault_vaddr=00000000"));
    check(1,OK,0,true);
    check(2,OK,0,true);
    check(2,COUNTER_BEFORE,0,true);
    check(2,COUNTER_AFTER,0,true);
    check(3,COUNTER_ACK,0,true);
    check(2,COUNTER_MMU,-EIO,true);
    check(2,COUNTER_OCCUPY,-EIO,true);
    check(2,COUNTER_UNKNOWN,-EIO,true);
    check(2,COUNTER_CPU,-EIO,true);
    check(2,BOARD_FLAG,-EBUSY,true);
    struct fg_control *c=hw.sram+FG_CONTROL;
    assert(c->errors_before[1]==2 && c->attempt==0 && hw.psram_ok);
    assert(c->snapshots==1 && c->errors_after[1]==0);
    check(3,BOARD_FLAG,0,true);
    assert(c->acknowledged==2 && c->errors_before[1]==2);
    check(3,RECURRENT_FLAG,-EBUSY,true);
    check(3,MIXED_ERROR,-EBUSY,true);
    assert(c->acknowledged==0 && c->errors_before[1]==6);
    check(2,TIMEOUT,-ETIMEDOUT,true);
    assert(!hw.checked[1]);
    check(2,NEW_ERROR,-EIO,true);
    check(3,NEW_ERROR,-EIO,true);
    check(3,BAD_ACK,-EIO,true);
    check(2,BAD_PC,-EIO,true);
    assert(!hw.checked[1]);
    check(2,BAD_CANARY,-EIO,false);
    assert(hw.checked[1]);
    alloc_fail=true;
    check(3,OK,-ENOMEM,false);
    assert(!c->stage && !c->snapshots && !hw.checked[1]);
    int before=entries;
    assert(test_store(&dev,NULL,"4",1)==-EINVAL && entries==before);
    free(hw.sram);
    puts("PASS: actual sysfs handler preserves hardware EBUSY/timeout, separates SRAM checks, rejects new/unapproved errors, preserves prior flags and clears stale snapshots on allocation failure");
}
'''
with tempfile.TemporaryDirectory(prefix='flash-guard-result-') as temp:
    cfile = Path(temp) / 'result.c'
    binary = Path(temp) / 'result'
    cfile.write_text(prefix + device + stubs + live + handler + main)
    subprocess.run(['cc', '-std=gnu11', '-fsanitize=address,undefined',
                    '-I', str(root/'experiments/flash-guard'), str(cfile),
                    '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
