#!/usr/bin/env python3
"""Test real SRAM control allocation, queue ABI and failure cleanup, isolated from DMA."""
from pathlib import Path
import ast,subprocess,sys,tempfile
repo=Path(__file__).resolve().parent.parent
s=Path(sys.argv[1]).read_text()
tree=ast.parse((repo/'build/test-wifi-internal-memory.py').read_text())
shim=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name)and t.id=='shim'for t in n.targets))
def struct(n):
 a=s.index('struct '+n+' {');return s[a:s.index('};',a)+2]+'\n'
def fun(n):
 p=s.index(n+'(');a=s.rfind('\n',0,p)+1;b=s.index('{',p)+1;d=1
 while d:d+=(s[b]=='{')-(s[b]=='}');b+=1
 return s[a:b]+'\n'
a=s.index('static struct gen_pool *osi_internal_pool;');b=s.index('/* A zero-byte kmalloc',a)
code=shim+s[a:b]+'''
typedef unsigned char u8; typedef uint32_t u32;
typedef int spinlock_t; typedef struct {int initialized;} wait_queue_head_t;
#define spin_lock_init(x) (*(x)=0)
#define init_waitqueue_head(x) ((x)->initialized=1)
'''+struct('osi_queue')+struct('osi_wifi_queue')
code+='''
struct task_struct { char comm[16]; };
static struct task_struct task={.comm="test"};
static struct task_struct *current=&task;
struct mutex { int initialized; };
struct semaphore { unsigned count; };
#define mutex_init(p) ((p)->initialized=1)
#define sema_init(p,n) ((p)->count=(n))
#define pr_debug(...) ((void)0)
#define pr_err(...) ((void)0)
#define osi_handle_bad(p) false
#define osi_handle_warn(...) ((void)0)
'''+struct('osi_mutex')+struct('osi_sem')+struct('osi_evtgrp')
for n in ['osi_mutex_create','osi_mutex_delete','osi_semphr_create','osi_semphr_delete','osi_spin_lock_create','osi_spin_lock_delete','osi_event_group_create','osi_event_group_delete']:
 code+=fun(n)
for n in ['osi_queue_create_common','osi_queue_create','osi_queue_delete','osi_wifi_create_queue','osi_wifi_delete_queue']:
 code+=fun(n)
code+=r'''
static struct gen_pool control;
int main(void){
 osi_internal_pool=&pool;osi_control_pool=&control;
 size_t dma_free=gen_pool_avail(&pool),all=gen_pool_avail(&control);
 struct osi_mutex*m=osi_mutex_create();assert(m&&osi_is_control(m)&&m->lock.initialized);
 size_t held=gen_pool_avail(&control);m->owner=&task;osi_mutex_delete(m);assert(gen_pool_avail(&control)==held);m->owner=NULL;osi_mutex_delete(m);
 struct osi_sem*sem=osi_semphr_create(7,3);assert(sem&&osi_is_control(sem)&&sem->sem.count==3&&sem->max==7);osi_semphr_delete(sem);
 void*lock=osi_spin_lock_create();assert(lock&&osi_is_control(lock));osi_spin_lock_delete(lock);
 struct osi_evtgrp*g=osi_event_group_create();assert(g&&osi_is_control(g)&&g->wait.initialized);osi_event_group_delete(g);
 assert(gen_pool_avail(&control)==all);

 void *w=osi_wifi_create_queue(200,8);assert(w && osi_is_control(w));
 struct osi_queue*q=*(void**)w;
 assert(osi_is_control(q) && osi_is_control(q->buf));
 assert(q->wait.initialized && q->len==200 && q->item_size==8);
 for(unsigned i=0;i<1600;i++)assert(!q->buf[i]);
 assert(!((uintptr_t)q->buf%32));
 size_t live=gen_pool_avail(&control);
 assert(!osi_wifi_create_queue(1000,8));assert(gen_pool_avail(&control)==live);
 assert(!osi_control_zalloc(SIZE_MAX));assert(!osi_control_calloc(SIZE_MAX,2));
 assert(gen_pool_avail(&control)==live);
 osi_wifi_delete_queue(w);assert(gen_pool_avail(&control)==all);
 void*slots[128];unsigned n=0;void*p;
 while((p=osi_control_zalloc(1))){assert(n<128);slots[n++]=p;}
 assert(n && !gen_pool_avail(&control));
 for(unsigned i=0;i<n;i++)osi_control_free(slots[i]);
 assert(gen_pool_avail(&control)==all && gen_pool_avail(&pool)==dma_free);
 osi_control_free(NULL);osi_wifi_delete_queue(NULL);
 puts("PASS real SRAM controls: separate DMA capacity, queue ABI/storage/zeroing, alignment, exhaustion, overflow and failed-create cleanup");
}
'''
with tempfile.TemporaryDirectory()as d:
 p=Path(d);(p/'test.c').write_text(code)
 subprocess.run(['cc','-std=gnu11','-g','-fsanitize=address,undefined',str(p/'test.c'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True)
