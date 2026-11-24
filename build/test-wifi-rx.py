#!/usr/bin/env python3
"""Call the actual three-argument RX callback and verify copied data/ownership."""
from pathlib import Path
import subprocess,tempfile,sys
r=Path(__file__).resolve().parent.parent
s=Path(sys.argv[1] if len(sys.argv)>1 else r/'build-output/wifi-smp-audit/current/esp_blob.c').read_text()
def fun(n):
 p=s.index(n+'(');a=s.rfind('\n',0,p)+1;b=s.index('{',p)+1;d=1
 while d:d+=(s[b]=='{')-(s[b]=='}');b+=1
 return s[a:b]
shim=r'''
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
#include <errno.h>
#include <stdio.h>
typedef uint64_t u64;
static unsigned wifi_rx_alloc_failed, wifi_rx_rejected;
#define atomic_inc(p) (++*(p))
#define WIFI_TIME_RX_COPY 0
#define wifi_time_record(kind, start) ((void)(start))
#define wifi_time_start() 0
#define wifi_time_enqueue(skb) ((void)(skb))
typedef uint8_t u8;typedef uint16_t u16;
#define ETH_HLEN 14
#define ESP_STA_IF 0
#define PRIO_Q_HIGH 0
#define SKB_DATA_ADDR_ALIGNMENT 4
#define PACKET_TYPE_DATA 1
#define cpu_to_le16(v) (v)
struct esp_payload_header{u8 if_type,if_num,flags,packet_type;u16 len,offset;};
struct sk_buff{u8*data,*base;unsigned len,capacity;};
struct esp_wifi_blob{int rx_q[1],adapter;};
static struct esp_wifi_blob hw,*g_blob=&hw;
static struct sk_buff*rx;static bool fail_alloc;static unsigned freed;
static struct sk_buff*dev_alloc_skb(unsigned n){if(fail_alloc)return NULL;struct sk_buff*s=calloc(1,sizeof(*s));s->base=s->data=malloc(n);s->capacity=n;return s;}
static void skb_reserve(struct sk_buff*s,unsigned n){s->data+=n;s->capacity-=n;}
static void*skb_put(struct sk_buff*s,unsigned n){assert(s->len+n<=s->capacity);void*p=s->data+s->len;s->len+=n;return p;}
static void skb_put_data(struct sk_buff*s,const void*p,unsigned n){memcpy(skb_put(s,n),p,n);}
static void skb_queue_tail(void*q,struct sk_buff*s){assert(!rx);rx=s;}
static void esp_process_new_packet_intr(void*a){}
static void esp_wifi_internal_free_rx_buffer(void*p){freed++;free(p);}
static void*buffer(unsigned n){void*p=malloc(n);memset(p,0x5a,n);return p;}
'''
main=r'''
int main(void){
 int(*typed)(void*,uint16_t,void*)=blob_rx_cb;
 void*p=buffer(64);assert(!typed(p,64,p) && freed==1 && rx);
 struct esp_payload_header*h=(void*)rx->data;
 assert(h->packet_type==PACKET_TYPE_DATA && h->len==64);
 for(unsigned i=0;i<64;i++)assert(rx->data[h->offset+i]==0x5a);
 free(rx->base);free(rx);rx=NULL;
 fail_alloc=true;p=buffer(64);assert(!typed(p,64,p) && freed==2 && !rx);
 fail_alloc=false;g_blob=NULL;p=buffer(64);assert(!typed(p,64,p) && freed==3 && !rx);
 g_blob=&hw;p=buffer(1);assert(!typed(p,1,p) && freed==4 && !rx);
 p=buffer(1);assert(!typed(p,65535,p) && freed==5 && !rx);
 assert(!typed(NULL,0,NULL) && freed==5);
#ifdef CHECK_RX_COUNTERS
 assert(wifi_rx_alloc_failed==1 && wifi_rx_rejected==4);
#endif
 puts("PASS: actual three-argument RX ABI, data copy before buffer release, exactly-once frees and allocation/drop paths");
}
'''
counter_check = '#define CHECK_RX_COUNTERS\n' if 'atomic_inc(&wifi_rx_alloc_failed)' in s else ''
with tempfile.TemporaryDirectory() as d:
 src=Path(d)/'test.c';out=Path(d)/'test';src.write_text(counter_check+shim+fun('blob_queue_packet')+fun('blob_rx_cb')+main)
 subprocess.run(['cc','-std=gnu11','-fsanitize=address,undefined',str(src),'-o',str(out)],check=True)
 subprocess.run([str(out)],check=True)
