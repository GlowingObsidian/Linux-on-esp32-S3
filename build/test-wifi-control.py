#!/usr/bin/env python3
"""Verify native control messages never leak into the radio data TX API."""
from pathlib import Path
import tempfile,subprocess,sys
repo=Path(__file__).resolve().parent.parent
s=Path(sys.argv[1] if len(sys.argv)>1 else repo/'build-output/wifi-smp-audit/current/esp_blob.c').read_text()
p=s.index('static int blob_process_packet(');b=s.index('{',p)+1;d=1
while d:d+=(s[b]=='{')-(s[b]=='}');b+=1
shim=r'''
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
typedef uint16_t u16;
struct esp_adapter{int unused;};
struct sk_buff{unsigned char*data;unsigned len;unsigned char*alloc;};
struct esp_payload_header{uint16_t offset,len;uint8_t packet_type;};
struct command_header{uint8_t cmd_code;};
#define PACKET_TYPE_COMMAND_REQUEST 1
#define CMD_RESPONSE_FAIL 2
#define le16_to_cpu(x) (x)
#define pr_err(...) ((void)0)
#define pr_warn_ratelimited(...) ((void)0)
static unsigned tx,replies,freed;static int dummy;static void*g_blob=&dummy;
static void dev_kfree_skb(struct sk_buff*s){free(s->alloc);free(s);freed++;}
static bool blob_handle_command(void*hw,struct command_header*c,unsigned n){return c->cmd_code==1;}
static void blob_post_cmd_resp(void*hw,int cmd,void*p,int n,int status){assert(status==CMD_RESPONSE_FAIL);replies++;}
static int esp_wifi_internal_tx(void*hw,void*p,unsigned n){tx++;return 0;}
static void skb_pull(struct sk_buff*s,unsigned n){s->data+=n;s->len-=n;}
static struct sk_buff*packet(unsigned size,int type,int cmd){
 struct sk_buff*s=calloc(1,sizeof(*s));s->alloc=calloc(1,size);s->data=s->alloc;s->len=size;
 if(size>=sizeof(struct esp_payload_header)){
  struct esp_payload_header*h=(void*)s->data;h->offset=sizeof(*h);h->len=size-sizeof(*h);h->packet_type=type;
  if(h->len)s->data[h->offset]=cmd;
 }return s;
}
'''
main=r'''
int main(void){
 assert(blob_process_packet(NULL,packet(16,1,1))==0 && !tx && !replies);
 assert(blob_process_packet(NULL,packet(16,1,99))==0 && !tx && replies==1);
 assert(blob_process_packet(NULL,packet(16,0,0))==0 && tx==1);
 assert(blob_process_packet(NULL,packet(1,0,0))==-EINVAL);
 assert(blob_process_packet(NULL,packet(sizeof(struct esp_payload_header),1,0))==-EINVAL);
 struct sk_buff*s=packet(16,1,1);((struct esp_payload_header*)s->data)->len=100;
 assert(blob_process_packet(NULL,s)==-EINVAL);
 assert(tx==1 && freed==6);puts("PASS: unsupported and malformed commands cannot reach Ethernet TX");
}
'''
with tempfile.TemporaryDirectory() as d:
 src=Path(d)/'test.c';out=Path(d)/'test';src.write_text(shim+s[p:b]+main)
 subprocess.run(['cc','-std=gnu11','-fsanitize=address,undefined',str(src),'-o',str(out)],check=True)
 subprocess.run([str(out)],check=True)
