#!/usr/bin/env python3
"""Run actual OSI queue code with blocked producers and consumers.

The pthread shim implements Linux's condition/wakeup contract. A producer must
resume when receive makes space, without waiting for its two-second deadline.
"""
from pathlib import Path
import subprocess
import sys
import tempfile

repo = Path(__file__).resolve().parent.parent
source = Path(sys.argv[1]).read_text()

def function(name):
    pos = source.index(name + '(')
    start = source.rfind('\n', 0, pos) + 1
    end = source.index('{', pos) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end] + '\n'

start = source.index('struct osi_queue {')
queue = source[start:source.index('};', start) + 2]
shim = r'''
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
typedef uint8_t u8; typedef uint32_t u32;
typedef pthread_mutex_t spinlock_t;
typedef struct {pthread_mutex_t lock; pthread_cond_t cond; unsigned waiters;} wait_queue_head_t;
#define GFP_KERNEL 0
#define kzalloc(n,f) calloc(1,n)
#define kcalloc(n,s,f) calloc(n,s)
#define kfree free
#define osi_control_zalloc(n) calloc(1,n)
#define osi_control_calloc(n,s) calloc(n,s)
#define osi_control_free free
#define spin_lock_init(p) pthread_mutex_init(p,NULL)
#define spin_lock_irqsave(p,f) do {(f)=0; pthread_mutex_lock(p);} while(0)
#define spin_unlock_irqrestore(p,f) pthread_mutex_unlock(p)
#define init_waitqueue_head(p) do {pthread_mutex_init(&(p)->lock,NULL); pthread_cond_init(&(p)->cond,NULL);} while(0)
#define pr_debug(...) ((void)0)
#define osi_handle_bad(p) false
#define osi_handle_warn(...) ((void)0)
#define OSI_FUNCS_TIME_BLOCKING UINT32_MAX
#define MAX_SCHEDULE_TIMEOUT 100000L
#define OSI_TICKS_TO_JIFFIES(t) (t)
static struct timespec deadline(unsigned ms) {
 struct timespec t; clock_gettime(CLOCK_REALTIME,&t);
 t.tv_sec+=ms/1000; t.tv_nsec+=(ms%1000)*1000000L;
 if(t.tv_nsec>=1000000000L){t.tv_sec++;t.tv_nsec-=1000000000L;}
 return t;
}
static void wake_up(wait_queue_head_t *w) {
 pthread_mutex_lock(&w->lock);pthread_cond_broadcast(&w->cond);pthread_mutex_unlock(&w->lock);
}
#define wait_event_killable_timeout(w,condition,ticks) ({ \
 long result=1; struct timespec end=deadline(ticks); \
 pthread_mutex_lock(&(w).lock); (w).waiters++; pthread_cond_broadcast(&(w).cond); \
 while(!(condition)){if(pthread_cond_timedwait(&(w).cond,&(w).lock,&end)==ETIMEDOUT){result=0;break;}} \
 (w).waiters--;pthread_mutex_unlock(&(w).lock);result; })
'''
main = r'''
static struct osi_queue *q;
static pthread_mutex_t finished_lock=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t finished_cond=PTHREAD_COND_INITIALIZER;
static bool finished;
static int result;
static uint32_t value=22, received;
static void *producer(void *p) {
 result=osi_queue_send_wait(q,&value,2000,(uintptr_t)p);
 pthread_mutex_lock(&finished_lock);finished=true;pthread_cond_signal(&finished_cond);pthread_mutex_unlock(&finished_lock);return NULL;
}
static void *consumer(void *p) {
 result=osi_queue_recv(q,&received,2000);
 pthread_mutex_lock(&finished_lock);finished=true;pthread_cond_signal(&finished_cond);pthread_mutex_unlock(&finished_lock);return NULL;
}
static void await_blocked(void) {
 struct timespec end=deadline(1000);
 pthread_mutex_lock(&q->wait.lock);
 while(!q->wait.waiters)assert(pthread_cond_timedwait(&q->wait.cond,&q->wait.lock,&end)!=ETIMEDOUT);
 pthread_mutex_unlock(&q->wait.lock);
}
static void await_finished(void) {
 struct timespec end=deadline(1000);
 pthread_mutex_lock(&finished_lock);
 while(!finished)assert(pthread_cond_timedwait(&finished_cond,&finished_lock,&end)!=ETIMEDOUT);
 pthread_mutex_unlock(&finished_lock);
}
int main(void) {
 q=osi_queue_create_common(1,sizeof(value));assert(q);
 for(unsigned front=0;front<2;front++){
  uint32_t first=11;assert(!osi_queue_put(q,&first,false));finished=false;
  pthread_t thread;assert(!pthread_create(&thread,NULL,producer,(void*)(uintptr_t)front));
  await_blocked();assert(osi_queue_recv(q,&received,0)==1 && received==11);
  await_finished();pthread_join(thread,NULL);assert(result==1);
  assert(osi_queue_recv(q,&received,0)==1 && received==22);
 }
 finished=false;pthread_t thread;assert(!pthread_create(&thread,NULL,consumer,NULL));
 await_blocked();assert(!osi_queue_put(q,&value,false));await_finished();pthread_join(thread,NULL);
 assert(result==1 && received==22);
 assert(!osi_queue_recv(q,&received,0));
 pthread_mutex_destroy(&q->lock);pthread_mutex_destroy(&q->wait.lock);pthread_cond_destroy(&q->wait.cond);
 free(q->buf);free(q);
 puts("PASS: full-queue producers (front/back) wake on receive, empty-queue consumer wakes on send; no deadline polling");
}
'''
code = shim + queue + '\n'
for name in ['osi_queue_create_common', 'osi_queue_put', 'osi_queue_send_wait', 'osi_queue_recv']:
    code += function(name)
with tempfile.TemporaryDirectory() as temp:
    src = Path(temp) / 'queue.c'
    exe = Path(temp) / 'queue'
    src.write_text(code + main)
    subprocess.run(['cc', '-std=gnu11', '-pthread', '-fsanitize=address,undefined', str(src), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True, timeout=10)
