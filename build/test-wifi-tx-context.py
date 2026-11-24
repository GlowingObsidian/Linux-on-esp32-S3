#!/usr/bin/env python3
"""Actual atomic transport -> worker dispatch, queue bounds and shutdown.

The old synchronous write fails when the fake blocking radio API asserts
process context. The deferred write preserves ownership without sleeping.
"""
from pathlib import Path
import subprocess, tempfile, sys
r = Path(__file__).resolve().parent.parent
s = Path(sys.argv[1] if len(sys.argv)>1 else r/'build-output/wifi-smp-audit/current/esp_blob.c').read_text()
def fun(name):
    p=s.index(name+'(');a=s.rfind('\n',0,p)+1;b=s.index('{',p)+1;depth=1
    while depth:
        depth+=(s[b]=='{')-(s[b]=='}');b+=1
    return s[a:b]+'\n'
shim=r'''
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <stddef.h>
#include <assert.h>
#include <string.h>
#include <errno.h>
#include <stdio.h>
typedef uint16_t u16; typedef uint8_t u8;
#define READ_ONCE(x) (x)
#define PACKET_TYPE_COMMAND_REQUEST 1
#define CMD_RESPONSE_FAIL 2
#define le16_to_cpu(v) (v)
#define BLOB_TX_QUEUE_LIMIT 16
#define BLOB_TX_DATA_LIMIT 8
#define BLOB_TX_DATA_LOW 4
#define ESP_STA_NW_IF 0
#define pr_err(...) ((void)0)
#define pr_warn_ratelimited(...) ((void)0)
#define container_of(p,t,m) ((t*)((char*)(p)-offsetof(t,m)))
struct net_device {bool running,stopped;};
struct esp_wifi_device {struct net_device *ndev;bool port_open,stop_data;};
struct esp_adapter {struct esp_wifi_device *priv[1];};
struct work_struct {int unused;};
struct sk_buff {unsigned char *data,*allocation;unsigned len;struct sk_buff *next;};
struct sk_buff_head {int lock;struct sk_buff *head,*tail;unsigned len;};
struct esp_wifi_blob {struct esp_adapter adapter;struct sk_buff_head tx_q;struct work_struct tx_work;bool tx_stopping;unsigned tx_data_pending;bool tx_paused;int tx_wait;};
struct esp_payload_header {u16 offset,len;uint8_t packet_type;};
struct command_header {uint8_t cmd_code;};
static struct esp_wifi_blob hw,*g_blob=&hw;
static unsigned atomic_depth,scheduled,freed,commands,frames,replies;
static unsigned history[64];
static unsigned tx_failures, tx_error, waits, stop_on_wait, check_payload;
static unsigned wifi_tx_calls, wifi_tx_no_mem, wifi_tx_accepted, wifi_tx_failed;
static unsigned wifi_tx_done,wifi_tx_done_failed,wifi_tx_queue_full,wakeups;
static unsigned long jiffies;
#define atomic_read(p) (*(p))
#define msecs_to_jiffies(n) (n)
#define time_after_eq(a,b) ((long)((a)-(b))>=0)
#define wait_event_timeout(w,cond,n) ({assert(!atomic_depth);waits++;jiffies+=(n);if(stop_on_wait)g_blob->tx_stopping=true; (cond)?1:0;})
static void wake_up(void *q){wakeups++;}
static bool netif_running(struct net_device *d){return d->running;}
static bool netif_queue_stopped(struct net_device *d){return d->stopped;}
static void netif_stop_queue(struct net_device *d){assert(atomic_depth);d->stopped=true;}
static void netif_wake_queue(struct net_device *d){assert(atomic_depth);d->stopped=false;}
#define atomic_inc(p) (++*(p))
static void *system_long_wq;
static void blob_tx_work(struct work_struct *work);
#define spin_lock_irqsave(l,f) do{f=atomic_depth;atomic_depth++;}while(0)
#define spin_unlock_irqrestore(l,f) do{assert(atomic_depth==f+1);atomic_depth=f;}while(0)
static unsigned skb_queue_len(struct sk_buff_head *q){return q->len;}
static void __skb_queue_tail(struct sk_buff_head *q,struct sk_buff *s){
 assert(atomic_depth);s->next=NULL;if(q->tail)q->tail->next=s;else q->head=s;q->tail=s;q->len++;
}
static struct sk_buff *skb_dequeue(struct sk_buff_head *q){
 struct sk_buff *s=q->head;if(s){q->head=s->next;if(!q->head)q->tail=NULL;q->len--;}return s;
}
static void dev_kfree_skb_any(struct sk_buff *s){free(s->allocation);free(s);freed++;}
#define dev_kfree_skb dev_kfree_skb_any
static void skb_queue_purge(struct sk_buff_head *q){struct sk_buff*s;while((s=skb_dequeue(q)))dev_kfree_skb_any(s);}
static void skb_pull(struct sk_buff *s,unsigned n){s->data+=n;s->len-=n;}
static bool queue_work_on(unsigned cpu,void *q,struct work_struct *work){assert(cpu==0);scheduled=1;return true;}
static void run(void){assert(!atomic_depth);while(scheduled){scheduled=0;blob_tx_work(&hw.tx_work);}}
static void flush_work(struct work_struct *w){run();}
static bool blob_handle_command(void *p,struct command_header*c,unsigned len){
 assert(!atomic_depth);history[commands++]=c->cmd_code;return c->cmd_code==1;
}
static void blob_post_cmd_resp(void *p,int cmd,void *data,int n,int status){assert(!atomic_depth);assert(status==CMD_RESPONSE_FAIL);replies++;}
static int esp_wifi_internal_tx(void *p,void *data,unsigned n){
 assert(!atomic_depth);frames++;
 if(check_payload){assert(n==32-sizeof(struct esp_payload_header));assert(*(unsigned char*)data==42);}
 if(tx_error)return tx_error;
 if(tx_failures){tx_failures--;return 0x101;}
 return 0;
}
static void usleep_range(unsigned lo,unsigned hi){assert(!atomic_depth);assert(lo==1000 && hi==2000);waits++;if(stop_on_wait)hw.tx_stopping=true;}
static struct sk_buff *packet(unsigned n,int type,int cmd){
 struct sk_buff*s=calloc(1,sizeof(*s));s->allocation=s->data=calloc(1,n);s->len=n;
 if(n>=sizeof(struct esp_payload_header)){
  struct esp_payload_header*h=(void*)s->data;h->offset=sizeof(*h);h->len=n-sizeof(*h);h->packet_type=type;
  if(h->len)s->data[h->offset]=cmd;
 }return s;
}
'''
main=r'''
int main(void){
 atomic_depth=2; /* The actual host command worker holds two BH locks. */
 assert(!blob_write_packet(&hw.adapter,packet(32,1,1)));
 assert(!commands && !frames && !freed && atomic_depth==2);
 assert(!blob_write_packet(&hw.adapter,packet(32,1,99)) && !commands);
 atomic_depth=0;run();assert(commands==2 && history[0]==1 && history[1]==99 && replies==1 && freed==2);
 atomic_depth=1;assert(!blob_write_packet(&hw.adapter,packet(32,0,0)) && !frames);
 atomic_depth=0;run();assert(frames==1 && freed==3);
 unsigned before=freed;
 for(unsigned i=0;i<BLOB_TX_QUEUE_LIMIT;i++)assert(!blob_write_packet(&hw.adapter,packet(32,0,0)));
 assert(blob_write_packet(&hw.adapter,packet(32,0,0))==-ENOBUFS && freed==before+1);
 assert(hw.tx_q.len==BLOB_TX_QUEUE_LIMIT);run();assert(hw.tx_q.len==0 && frames==17);
 assert(blob_write_packet(&hw.adapter,packet(1,0,0))==-EINVAL);
 assert(blob_write_packet(&hw.adapter,packet(sizeof(struct esp_payload_header),1,0))==-EINVAL);
 struct sk_buff *bad=packet(32,1,1);((struct esp_payload_header*)bad->data)->len=100;
 assert(blob_write_packet(&hw.adapter,bad)==-EINVAL);
 unsigned saved_frames=frames,saved_freed=freed,saved_commands=commands;
 tx_failures=3;waits=0;check_payload=1;
 assert(!blob_write_packet(&hw.adapter,packet(32,0,42)));run();
 assert(frames==saved_frames+4 && waits==3 && freed==saved_freed+1 && commands==saved_commands);
 check_payload=0;saved_frames=frames;saved_freed=freed;tx_error=0x101;waits=0;
 assert(!blob_write_packet(&hw.adapter,packet(32,0,0)));run();
 assert(frames==saved_frames+17 && waits==16 && freed==saved_freed+1);
 saved_frames=frames;saved_freed=freed;tx_error=0x102;waits=0;
 assert(!blob_write_packet(&hw.adapter,packet(32,0,0)));run();
 assert(frames==saved_frames+1 && waits==0 && freed==saved_freed+1);
 tx_error=0;tx_failures=99;waits=0;stop_on_wait=1;saved_frames=frames;saved_freed=freed;
 assert(!blob_write_packet(&hw.adapter,packet(32,0,0)));run();
 assert(frames==saved_frames+1 && waits==1 && freed==saved_freed+1 && hw.tx_stopping);
 hw.tx_stopping=false;stop_on_wait=tx_failures=0;
 g_blob=NULL;assert(blob_write_packet(&hw.adapter,packet(32,0,0))==-ENODEV);g_blob=&hw;
 assert(!blob_write_packet(&hw.adapter,packet(32,1,1)));
 blob_tx_shutdown(&hw);assert(hw.tx_stopping && hw.tx_q.len==0 && commands==3);
 assert(blob_write_packet(&hw.adapter,packet(32,0,0))==-ESHUTDOWN);
 blob_tx_shutdown(&hw);assert(!atomic_depth && !scheduled);
 puts("PASS: command/data writes under atomic callers never invoke blocking radio APIs; CPU0 worker, FIFO order, bounded queue, exact packet ownership, malformed input and shutdown/drain");
}
'''
if 'static void blob_tx_flow_locked(' in s:
    main=main.replace('atomic_depth=2; /*', 'struct net_device ndev={.running=true};struct esp_wifi_device priv={.ndev=&ndev,.port_open=true};hw.adapter.priv[0]=&priv;\n atomic_depth=2; /*')
    main=main.replace('i<BLOB_TX_QUEUE_LIMIT','i<BLOB_TX_DATA_LIMIT')
    main=main.replace('assert(hw.tx_q.len==BLOB_TX_QUEUE_LIMIT);run();assert(hw.tx_q.len==0 && frames==17);',
        'assert(hw.tx_q.len==BLOB_TX_DATA_LIMIT && ndev.stopped && hw.tx_paused);\n'
        ' unsigned command_before=commands;\n'
        ' for(unsigned i=0;i<BLOB_TX_QUEUE_LIMIT-BLOB_TX_DATA_LIMIT;i++)assert(!blob_write_packet(&hw.adapter,packet(32,1,1)));\n'
        ' assert(blob_write_packet(&hw.adapter,packet(32,1,1))==-ENOBUFS);\n'
        ' run();assert(!hw.tx_q.len && !hw.tx_data_pending && !ndev.stopped && !hw.tx_paused && frames==9);\n'
        ' assert(commands==command_before+8);')
    main=main.replace('commands==3','commands==11')
    main=main.replace('frames==saved_frames+17 && waits==16', 'frames==saved_frames+126 && waits==125')
    main=main.replace('puts("PASS:',
        'unsigned done_before=wifi_tx_done;blob_tx_done(1,NULL,NULL,false);assert(wifi_tx_done==done_before);\n'
        ' blob_tx_done(0,NULL,NULL,true);blob_tx_done(0,NULL,NULL,false);\n'
        ' assert(wifi_tx_done==done_before+2 && wifi_tx_done_failed==1);\n'
        ' ndev.stopped=true;hw.tx_paused=true;priv.port_open=false;atomic_depth=1;blob_tx_flow_locked(&hw);atomic_depth=0;assert(ndev.stopped);\n'
        ' puts("PASS: flow stop/wake, command reserve, completion notifications, bounded stall and no teardown wake;')
    code=shim+fun('blob_tx_flow_locked')+fun('blob_tx_done')+fun('blob_process_packet')+fun('blob_tx_work')+fun('blob_write_packet')+fun('blob_tx_shutdown')+main
elif 'static int blob_process_packet(' in s:
    code=shim+fun('blob_process_packet')+fun('blob_tx_work')+fun('blob_write_packet')+fun('blob_tx_shutdown')+main
else:
    # Run the same context assertion on the pre-fix transport.
    code=shim+fun('blob_write_packet')+'\nint main(void){atomic_depth=2;blob_write_packet(&hw.adapter,packet(32,1,1));}\nstatic void blob_tx_work(struct work_struct*w){}\n'
if 'wifi_time_record(' in s:
    timing_shim = '''
typedef uint64_t u64;
#define WIFI_TIME_TX_API 0
#define WIFI_TIME_TX_RETRY 1
static u64 wifi_time_start(void) { return 0; }
static void wifi_time_record(int kind, u64 start) {}
static void wifi_time_enqueue(struct sk_buff *skb) {}
#define wifi_time_dequeue(skb, ...) ((void)(skb))
'''
    code = code.replace(shim, shim + timing_shim, 1)
if 'wifi_tx_cpu' in s:
    code=code.replace('static bool queue_work_on(', 'static unsigned wifi_tx_cpu, hires_waits; static bool wifi_retry_hires;\n#define ms_to_ktime(v) (v)\n#define wait_event_hrtimeout(w,c,t) (hires_waits++,wait_event_timeout(w,c,t))\nstatic bool queue_work_on(',1)
    code=code.replace('assert(cpu==0);scheduled=1;', 'assert(cpu==wifi_tx_cpu);scheduled=1;')
    code=code.replace('puts("PASS:', 'wifi_tx_cpu=1;wifi_retry_hires=true;stop_on_wait=false;tx_error=0;tx_failures=1;g_blob=&hw;hw.tx_stopping=false;hw.tx_paused=false;priv.port_open=true;priv.stop_data=false;ndev.running=true;ndev.stopped=false;assert(!blob_write_packet(&hw.adapter,packet(32,0,42)));run();assert(hires_waits==1); puts("PASS: CPU1 and high-resolution retry;')
with tempfile.TemporaryDirectory() as temp:
    src=Path(temp)/'context.c';out=Path(temp)/'context';src.write_text(code)
    subprocess.run(['cc','-std=gnu11','-fsanitize=address,undefined',str(src),'-o',str(out)],check=True)
    subprocess.run([str(out)],check=True)
