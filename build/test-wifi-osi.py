#!/usr/bin/env python3
"""Exercise actual queue ABI and task creation callbacks under ASan/UBSan.

Mocks kernel scheduling/allocation, not radio hardware. The blob-style load of
wrapper[0] and wake-time affinity/handle checks catch the historical defects.
"""
from pathlib import Path
import subprocess, tempfile, sys
repo = Path(__file__).resolve().parent.parent
s = Path(sys.argv[1] if len(sys.argv)>1 else repo/'build-output/wifi-smp-audit/current/esp_blob.c').read_text()
def fun(name):
    pos=s.index(name+'('); start=s.rfind('\n',0,pos)+1
    brace=s.index('{',pos); end=brace+1; depth=1
    while depth:
        depth+=(s[end]=='{')-(s[end]=='}');end+=1
    return s[start:end]
def struct(name):
    start=s.index('struct '+name+' {');return s[start:s.index('};',start)+2]
shim=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
typedef uint8_t u8; typedef uint32_t u32;
typedef int spinlock_t; typedef struct {int initialized;} wait_queue_head_t;
#define GFP_KERNEL 0
#define spin_lock_init(x) (*(x)=0)
#define init_waitqueue_head(x) ((x)->initialized=1)
#define osi_note(x) ((void)0)
static unsigned allocs, calls, fail_at;
static void *kzalloc(size_t n,int flags) {
    if (++calls==fail_at) return NULL;
    void *p=calloc(1,n);if(p)allocs++;return p;
}
static void *kcalloc(size_t n,size_t size,int flags) {
    if (size && n>UINT32_MAX/size) return NULL;
    return kzalloc(n*size,flags);
}
static void kfree(void*p){if(p){assert(allocs);allocs--;free(p);}}
/* Non-IRAM profile uses the same allocator; pool behavior is tested separately. */
static void *osi_control_zalloc(size_t n){return kzalloc(n,GFP_KERNEL);}
static void *osi_control_calloc(size_t n,size_t z){return kcalloc(n,z,GFP_KERNEL);}
static void osi_control_free(void*p){kfree(p);}
struct task_struct {unsigned cpu; int refs;};
#define nr_cpu_ids 2
static bool online[2]={true,true}, create_fail;
#define cpu_online(c) online[c]
#define IS_ERR(p) ((p)==NULL)
static void **published;static unsigned wanted_cpu, wakes;
static int osi_task_trampoline(void*p){return 0;}
static struct task_struct *kthread_create(int(*fn)(void*),void*p,const char*fmt,const char*name){
    if(create_fail)return NULL;
    struct task_struct*t=kzalloc(sizeof(*t),0);if(t)t->cpu=~0U;return t;
}
static void get_task_struct(struct task_struct*t){t->refs++;}
static void set_user_nice(struct task_struct*t,int n){}
static void kthread_bind(struct task_struct*t,unsigned cpu){assert(cpu<2);t->cpu=cpu;}
static void wake_up_process(struct task_struct*t){
    assert(published && *published);assert(t->cpu==wanted_cpu);wakes++;
}
#define strscpy(dst,src,n) snprintf(dst,n,"%s",src)
'''
main=r'''
int main(void){
    assert(offsetof(struct osi_wifi_queue,handle)==0);
    assert(!osi_wifi_create_queue(0,8));assert(!osi_wifi_create_queue(2,-1));
    for(unsigned i=1;i<=3;i++){
        calls=0;fail_at=i;assert(!osi_wifi_create_queue(200,8));assert(!allocs);
    }
    fail_at=0;
    void *opaque=osi_wifi_create_queue(200,8);assert(opaque);
    /* Exactly the dereference in pp_create_task, before passing to OSI. */
    struct osi_queue *raw=*(void**)opaque;
    assert(raw && raw->buf && raw->len==200 && raw->item_size==8);
    assert(raw->wait.initialized && !((struct osi_wifi_queue*)opaque)->storage);
    osi_wifi_delete_queue(opaque);assert(!allocs);
    raw=osi_queue_create(4,8);assert(raw && raw->len==4);
    osi_queue_delete(raw);osi_wifi_delete_queue(NULL);assert(!allocs);
    assert(!osi_queue_create(UINT32_MAX,8));assert(!allocs);
    for(unsigned cpu=0;cpu<3;cpu++){
        void *handle=NULL;published=&handle;wanted_cpu=cpu<2?cpu:~0U;
        assert(osi_task_create_pinned_to_core(NULL,"test",2048,NULL,1,&handle,wanted_cpu)==1);
        struct osi_task*t=handle;assert(t->tsk->refs==1);
        kfree(t->tsk);kfree(t);assert(!allocs);
    }
    void *handle=NULL;published=&handle;wanted_cpu=~0U;
    assert(osi_task_create(NULL,"unbound",2048,NULL,0,&handle)==1);
    struct osi_task*t=handle;kfree(t->tsk);kfree(t);assert(!allocs);
    online[1]=false;assert(!osi_task_create_pinned_to_core(NULL,"offline",1,NULL,0,&handle,1));
    online[1]=true;create_fail=true;handle=NULL;
    assert(!osi_task_create_pinned_to_core(NULL,"failure",1,NULL,0,&handle,0));
    assert(!handle && !allocs && wakes==4);
    puts("PASS: blob queue ABI, allocation failure cleanup, overflow, CPU0/CPU1 affinity and handle publication");
}
'''
code=shim+'\n'+struct('osi_queue')+'\n'+struct('osi_wifi_queue')+'\n'+struct('osi_task')+'\n'
for n in ['osi_queue_create_common','osi_queue_create','osi_queue_delete','osi_wifi_create_queue','osi_wifi_delete_queue','osi_task_create_common','osi_task_create','osi_task_create_pinned_to_core']:
    code+=fun(n)+'\n'
with tempfile.TemporaryDirectory() as d:
    src=Path(d)/'test.c';exe=Path(d)/'test';src.write_text(code+main)
    subprocess.run(['cc','-std=gnu11','-g','-fsanitize=address,undefined',str(src),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True)
