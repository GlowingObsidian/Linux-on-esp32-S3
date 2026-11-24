#!/usr/bin/env python3
"""Actual netdev TX must not read an skb after transport takes ownership."""
from pathlib import Path
import subprocess
import sys
import tempfile

source = Path(sys.argv[1]).read_text()
pos = source.index('process_tx_packet(')
start = source.rfind('\n', 0, pos) + 1
end = source.index('{', pos) + 1
depth = 1
while depth:
    depth += (source[end] == '{') - (source[end] == '}')
    end += 1
shim = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8; typedef uint16_t u16;
struct net_device {bool stopped;};
struct esp_adapter {unsigned capabilities;};
struct esp_wifi_device {struct net_device *ndev;struct esp_adapter *adapter;bool stop_data;u8 if_type,if_num;struct {unsigned tx_errors,tx_packets,tx_bytes,tx_dropped;} stats;};
struct esp_skb_cb {struct esp_wifi_device *priv;};
struct sk_buff {unsigned char *data,*base;unsigned len,headroom;unsigned char cb[48];};
struct esp_payload_header {u8 if_type,if_num;u16 len,offset;u8 packet_type,checksum;};
#define NETDEV_TX_OK 0
#define NETDEV_TX_BUSY 1
#define SKB_DATA_ADDR_ALIGNMENT 4
#define PACKET_TYPE_DATA 0
#define ESP_CHECKSUM_ENABLED 1
#define cpu_to_le16(v) (v)
#define IS_ALIGNED(p,n) (!((p)%n))
#define esp_info(...) ((void)0)
#define esp_err(...) ((void)0)
static bool host_sleep;
static int transport_result;
static unsigned frees,submissions;
static unsigned skb_headroom(struct sk_buff*s){return s->headroom;}
static int skb_linearize(struct sk_buff*s){return 0;}
static struct sk_buff *esp_alloc_skb(unsigned n){
 struct sk_buff*s=calloc(1,sizeof(*s));s->base=s->data=calloc(1,n);return s;
}
static void dev_kfree_skb(struct sk_buff*s){free(s->base);free(s);frees++;}
#define dev_kfree_skb_any dev_kfree_skb
static bool netif_queue_stopped(const struct net_device*d){return d->stopped;}
static void skb_copy_from_linear_data(struct sk_buff*s,void*p,unsigned n){memcpy(p,s->data,n);}
static void skb_put(struct sk_buff*s,unsigned n){s->len+=n;}
static void skb_push(struct sk_buff*s,unsigned n){assert(s->headroom>=n);s->data-=n;s->len+=n;s->headroom-=n;}
static unsigned compute_checksum(void*p,unsigned n){return 0;}
static int esp_send_packet(struct esp_adapter*a,struct sk_buff*s){
 struct esp_payload_header*h=(void*)s->data;
 assert(h->len==64 && s->len==h->offset+64);submissions++;
 dev_kfree_skb(s); /* Legal immediate completion, even on another CPU. */
 return transport_result;
}
'''
main = r'''
static struct sk_buff *packet(struct esp_wifi_device*p,bool headroom){
 struct sk_buff*s=esp_alloc_skb(128);s->len=64;
 if(headroom){s->data+=32;s->headroom=32;}
 ((struct esp_skb_cb*)s->cb)->priv=p;return s;
}
int main(void){
 struct net_device dev={0};struct esp_adapter adapter={0};
 struct esp_wifi_device priv={.ndev=&dev,.adapter=&adapter};
 for(unsigned i=0;i<2;i++)assert(process_tx_packet(packet(&priv,i))==NETDEV_TX_OK);
 assert(priv.stats.tx_packets==2 && priv.stats.tx_bytes==128 && frees==3 && submissions==2);
 transport_result=-ENOBUFS;assert(process_tx_packet(packet(&priv,true))==NETDEV_TX_OK);
 assert(priv.stats.tx_errors==1 && priv.stats.tx_packets==2 && frees==4);
 dev.stopped=true;struct sk_buff*s=packet(&priv,true);
 assert(process_tx_packet(s)==NETDEV_TX_BUSY && frees==4 && s->len==64);
 dev_kfree_skb(s);dev.stopped=false;priv.stop_data=true;
 assert(process_tx_packet(packet(&priv,true))==NETDEV_TX_OK && frees==6 && priv.stats.tx_dropped==1);
 puts("PASS: immediate transport free with/without skb reallocation; payload byte accounting; error ownership; BUSY preserves original skb");
}
'''
with tempfile.TemporaryDirectory() as temp:
    src = Path(temp) / 'ownership.c'
    exe = Path(temp) / 'ownership'
    src.write_text(shim + source[start:end] + main)
    subprocess.run(['cc', '-std=gnu11', '-fsanitize=address,undefined', str(src), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
