#!/usr/bin/env python3
"""Run actual response producer and host MAC decoder across the wire boundary."""
from pathlib import Path
import subprocess,tempfile,sys
r=Path(__file__).resolve().parent.parent
s=Path(sys.argv[1] if len(sys.argv)>1 else r/'build-output/wifi-smp-audit/current/esp_blob.c').read_text()
h=(r/'kernel-driver-esp32-ng/adapter.h').read_text();decoder=(r/'kernel-driver-esp32-ng/esp_cmd.c').read_text()
def fun(src,n):
 p=src.index(n+'(');a=src.rfind('\n',0,p)+1;b=src.index('{',p)+1;d=1
 while d:d+=(src[b]=='{')-(src[b]=='}');b+=1
 return src[a:b]
def structure(n):
 p=h.index('struct '+n+' {');b=h.index('} __packed;',p)+len('} __packed;');return h[p:b]
shim=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <errno.h>
typedef uint8_t u8;typedef uint16_t u16;
#define __packed __attribute__((packed))
#define MAC_ADDR_LEN 6
#define ESP_STA_IF 0
#define ESP_STA_IF_NUM 0
#define PRIO_Q_HIGH 0
#define SKB_DATA_ADDR_ALIGNMENT 4
#define PACKET_TYPE_COMMAND_RESPONSE 3
#define CMD_INIT_INTERFACE 1
#define CMD_GET_MAC 3
#define CMD_RESPONSE_SUCCESS 2
#define cpu_to_le16(v) (v)
#define pr_info(...) ((void)0)
#define pr_debug(...) ((void)0)
#define pr_err(...) ((void)0)
#define esp_info(...) ((void)0)
#define esp_err(...) ((void)0)
struct esp_payload_header {u8 if_type,if_num,flags,packet_type;u16 len,offset;};
struct sk_buff {u8*data,*allocation;unsigned len,capacity;};
struct esp_wifi_blob {int rx_q[1],adapter;};
struct esp_wifi_device {u8 mac_address[6];};
struct command_node {struct sk_buff*resp_skb;};
static struct sk_buff*rx;static bool fail_alloc;
static struct sk_buff*dev_alloc_skb(unsigned n){if(fail_alloc)return NULL;struct sk_buff*s=calloc(1,sizeof(*s));s->allocation=s->data=calloc(1,n);s->capacity=n;return s;}
static void skb_reserve(struct sk_buff*s,unsigned n){s->data+=n;s->capacity-=n;}
static void*skb_put(struct sk_buff*s,unsigned n){assert(s->len+n<=s->capacity);void*p=s->data+s->len;s->len+=n;return p;}
static void skb_put_data(struct sk_buff*s,const void*p,unsigned n){memcpy(skb_put(s,n),p,n);}
static void skb_queue_tail(void*q,struct sk_buff*s){assert(!rx);rx=s;}
static void esp_process_new_packet_intr(void*a){}
static const u8 expected[6]={0x34,0x85,0x18,0x7b,0x81,0xb8};
static int esp_wifi_get_mac(int ifx,u8*mac){assert(ifx==0);memcpy(mac,expected,6);return 0;}
'''
main=r'''
static void release(void){free(rx->allocation);free(rx);rx=NULL;}
int main(void){
 struct esp_wifi_blob hw={0};struct esp_wifi_device priv={0};
 assert(blob_maybe_answer_cmd(&hw,CMD_GET_MAC));assert(rx);
 struct esp_payload_header*payload=(void*)rx->data;
 assert(payload->packet_type==PACKET_TYPE_COMMAND_RESPONSE);
 assert(payload->len==sizeof(struct cmd_config_mac_address));
 rx->data+=payload->offset;rx->len-=payload->offset;
 struct command_node node={.resp_skb=rx};assert(!decode_mac_addr(&priv,&node));
 assert(!memcmp(priv.mac_address,expected,6));release();
 assert(blob_maybe_answer_cmd(&hw,CMD_INIT_INTERFACE));assert(((struct esp_payload_header*)rx->data)->len==sizeof(struct command_header));release();
 fail_alloc=true;assert(!blob_maybe_answer_cmd(&hw,CMD_GET_MAC) && !rx);
 puts("PASS: real response producer -> real host decoder yields factory MAC, single header, correct response length and allocation-failure path");
}
'''
code=shim+structure('command_header')+structure('cmd_config_mac_address')+fun(s,'blob_post_cmd_resp')+fun(s,'blob_maybe_answer_cmd')+fun(decoder,'decode_mac_addr')+main
with tempfile.TemporaryDirectory() as d:
 src=Path(d)/'test.c';out=Path(d)/'test';src.write_text(code)
 subprocess.run(['cc','-std=gnu11','-fsanitize=address,undefined',str(src),'-o',str(out)],check=True)
 subprocess.run([str(out)],check=True)
