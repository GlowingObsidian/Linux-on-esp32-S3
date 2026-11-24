#!/usr/bin/env python3
"""Exercise real timer lifecycle callbacks with deterministic timer/work mocks."""
from pathlib import Path
import tempfile,subprocess,sys
r=Path(__file__).resolve().parent.parent
s=Path(sys.argv[1] if len(sys.argv)>1 else r/'build-output/wifi-smp-audit/current/esp_blob.c').read_text()
a=s.index('struct osi_timer {');b=s.index('static int64_t osi_esp_timer_get_time',a)
shim=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdlib.h>
#include <stdio.h>
typedef uint64_t u64;typedef int64_t ktime_t;typedef unsigned refcount_t;
#define container_of(p,t,m) ((t*)((char*)(p)-offsetof(t,m)))
struct list_head{struct list_head*next,*prev;};
#define LIST_HEAD(n) struct list_head n={&n,&n}
#define list_for_each_entry(p,h,m) for(p=container_of((h)->next,__typeof__(*p),m);&p->m!=(h);p=container_of(p->m.next,__typeof__(*p),m))
static void list_add(struct list_head*n,struct list_head*h){n->next=h->next;n->prev=h;h->next->prev=n;h->next=n;}
static void list_del(struct list_head*n){n->prev->next=n->next;n->next->prev=n->prev;}
#define list_first_entry_or_null(h,t,m) ((h)->next==(h)?NULL:container_of((h)->next,t,m))
#define DEFINE_SPINLOCK(n) int n
#define spin_lock_irqsave(l,f) ((void)(f=0))
#define spin_unlock_irqrestore(l,f) ((void)0)
#define GFP_ATOMIC 0
#define pr_err(...) ((void)0)
#define NSEC_PER_MSEC 1000000ULL
#define NSEC_PER_USEC 1000ULL
#define CLOCK_MONOTONIC 1
#define HRTIMER_MODE_REL 0
#define ns_to_ktime(ns) (ns)
struct work_struct{void(*fn)(struct work_struct*);bool pending;};
struct workqueue_struct{int unused;};
#define INIT_WORK(w,f) do{(w)->fn=f;(w)->pending=false;}while(0)
static bool queue_work_on(unsigned cpu,struct workqueue_struct*q,struct work_struct*w){assert(cpu==0 && q);if(w->pending)return false;w->pending=true;return true;}
static bool cancel_work(struct work_struct*w){bool was=w->pending;w->pending=false;return was;}
static void destroy_workqueue(struct workqueue_struct*q){assert(q);}
enum hrtimer_restart{HRTIMER_NORESTART,HRTIMER_RESTART};
struct hrtimer{enum hrtimer_restart(*fn)(struct hrtimer*);bool armed;ktime_t period;};
static void hrtimer_setup(struct hrtimer*t,enum hrtimer_restart(*fn)(struct hrtimer*),int clock,int mode){t->fn=fn;}
static void hrtimer_start(struct hrtimer*t,ktime_t ns,int mode){t->armed=true;t->period=ns;}
static void hrtimer_forward_now(struct hrtimer*t,ktime_t ns){t->period=ns;}
static int hrtimer_try_to_cancel(struct hrtimer*t){t->armed=false;return 1;}
static int hrtimer_cancel(struct hrtimer*t){return hrtimer_try_to_cancel(t);}
static unsigned allocations;
static void*kzalloc(size_t n,int flags){void*p=calloc(1,n);if(p)allocations++;return p;}
static void kfree(void*p){assert(allocations);allocations--;free(p);}
static void refcount_set(unsigned*p,unsigned n){*p=n;}
static void refcount_inc(unsigned*p){assert(*p);(*p)++;}
static bool refcount_dec_and_test(unsigned*p){assert(*p);return --(*p)==0;}
'''
main=r'''
static unsigned calls;
static void cb(void*p){calls++;}
static void self_disarm(void*p){calls++;osi_timer_disarm(p);}
static void self_delete(void*p){calls++;osi_timer_done(p);}
static void fire(struct osi_timer*t){assert(t->timer.armed);if(t->timer.fn(&t->timer)==HRTIMER_NORESTART)t->timer.armed=false;}
static void run(struct osi_timer*t){assert(t->work.pending);t->work.pending=false;t->work.fn(&t->work);}
int main(void){
 struct workqueue_struct queue;osi_timer_wq=&queue;int key1,key2;
 osi_timer_setfn(&key1,cb,NULL);struct osi_timer*t=osi_timer_find(&key1);assert(t && allocations==1);
 osi_timer_arm_us(&key1,250,true);assert(t->timer.period==250000);
 fire(t);fire(t);assert(t->refs==2);run(t);assert(calls==1 && t->refs==1);
 fire(t);osi_timer_disarm(&key1);assert(!t->active && !t->timer.armed && !t->work.pending && t->refs==1);
 osi_timer_arm(&key1,UINT32_MAX,false);assert(t->timer.period==4294967295000000LL);
 osi_timer_done(&key1);assert(!allocations && !osi_timer_find(&key1));
 osi_timer_setfn(&key1,self_disarm,&key1);t=osi_timer_find(&key1);
 osi_timer_arm_us(&key1,1,true);fire(t);run(t);assert(calls==2 && !t->active && t->refs==1);
 osi_timer_done(&key1);assert(!allocations);
 osi_timer_setfn(&key1,self_delete,&key1);t=osi_timer_find(&key1);
 osi_timer_arm_us(&key1,1,false);fire(t);run(t);assert(calls==3 && !allocations);
 osi_timer_setfn(&key1,cb,NULL);t=osi_timer_find(&key1);
 osi_timer_arm_us(&key1,10,false);fire(t);osi_timer_done(&key1);assert(!allocations && calls==3);
 osi_timer_setfn(&key1,cb,NULL);osi_timer_setfn(&key2,cb,NULL);assert(allocations==2);
 osi_timer_arm_us(&key1,50,true);fire(osi_timer_find(&key1));
 osi_timers_shutdown(NULL);assert(!allocations && !osi_timer_wq && calls==3);
 puts("PASS: microsecond deadlines, wide arithmetic, coalescing, canceled work, callback self-disarm/delete and shutdown ownership");
}
'''
with tempfile.TemporaryDirectory() as d:
 src=Path(d)/'test.c';out=Path(d)/'test';src.write_text(shim+s[a:b]+main)
 subprocess.run(['cc','-std=gnu11','-fsanitize=address,undefined',str(src),'-o',str(out)],check=True)
 subprocess.run([str(out)],check=True)
