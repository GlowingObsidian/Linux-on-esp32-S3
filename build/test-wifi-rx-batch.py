#!/usr/bin/env python3
"""Verify bounded Linux RX handoff order with the actual worker and helpers."""
from pathlib import Path
import subprocess
import sys
import tempfile

source = Path(sys.argv[1]).read_text()
helpers = source[source.index('enum { ESP_RX_BATCH_MAX'):source.index('static void process_rx_packet')]
worker = source[source.index('static void esp_get_packets'):source.index('int esp_send_packet',source.index('static void esp_get_packets'))]
shim = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>

#define module_param_named(...)
#define MODULE_PARM_DESC(...)
#define KERNEL_VERSION(a,b,c) (((a)<<16)|((b)<<8)|(c))
#define LINUX_VERSION_CODE KERNEL_VERSION(7,2,4)
#define ESP_STA_IF 1
#define ESP_AP_IF 2
#define PACKET_TYPE_DATA 3
#define READ_ONCE(x) (x)

struct esp_payload_header { int if_type, packet_type; };
struct sk_buff { struct esp_payload_header *data; unsigned len, index; };
struct sk_buff_head { struct sk_buff *items[8]; unsigned len; };
struct esp_adapter;
struct ops { struct sk_buff *(*read)(struct esp_adapter *); };
struct esp_adapter { const struct ops *if_ops; };
static struct sk_buff frames[17];
static struct esp_payload_header headers[17];
static unsigned next, delivered, bh_count, bh, bucket, largest;

static void __skb_queue_head_init(struct sk_buff_head *q) { q->len=0; }
static bool skb_queue_empty(struct sk_buff_head *q) { return !q->len; }
static unsigned skb_queue_len(struct sk_buff_head *q) { return q->len; }
static struct sk_buff *__skb_dequeue(struct sk_buff_head *q) {
    if (!q->len) return NULL;
    struct sk_buff *skb=q->items[0];
    for (unsigned i=1;i<q->len;i++) q->items[i-1]=q->items[i];
    q->len--;
    return skb;
}
static void __skb_queue_tail(struct sk_buff_head *q, struct sk_buff *skb) {
    assert(q->len<8);
    q->items[q->len++]=skb;
}
static void local_bh_disable(void) { assert(!bh); bh=1; bucket=0; bh_count++; }
static void local_bh_enable(void) {
    assert(bh && bucket>0 && bucket<=8);
    if (bucket>largest) largest=bucket;
    bh=0;
}
static void NETIF_RX_NI(struct sk_buff *skb) {
    assert(skb->index==delivered++);
    bucket+=bh;
}
static struct sk_buff *read_next(struct esp_adapter *adapter) {
    (void)adapter;
    return next<17 ? &frames[next++] : NULL;
}
'''
mock = r'''
static void process_rx_packet(struct esp_adapter *adapter, struct sk_buff *skb,
                              struct sk_buff_head *batch) {
    (void)adapter;
    assert(!bh);
    if (!esp_rx_is_data(skb)) {
        assert(!batch && skb->index==delivered++);
    } else if (batch) {
        __skb_queue_tail(batch,skb);
    } else {
        NETIF_RX_NI(skb);
    }
}
'''
test = r'''
int main(void) {
    const struct ops operations={ read_next };
    struct esp_adapter adapter={ &operations };
    for (unsigned i=0;i<17;i++) {
        headers[i]=(struct esp_payload_header){ESP_STA_IF,PACKET_TYPE_DATA};
        frames[i]=(struct sk_buff){&headers[i],sizeof(headers[i]),i};
    }
    headers[3].packet_type=0;   /* Control in the middle of a partial batch. */
    headers[13].if_type=0;      /* Non-STA path also flushes preceding data. */
    frames[15].len=0;          /* Short transport buffer cannot be classified. */
    esp_get_packets(&adapter);
    assert(delivered==17 && bh_count==0);
    delivered=next=0;
    wifi_rx_batch=true;
    esp_get_packets(&adapter);
    assert(delivered==17 && bh_count==5 && largest==8 && !bh);
    puts("PASS disabled mode, bounded bursts, FIFO ordering, control and truncated frames");
}
'''
with tempfile.TemporaryDirectory() as d:
    path = Path(d)
    (path/'rx.c').write_text(shim+helpers+mock+worker+test)
    subprocess.run(['cc','-Wall','-Wextra','-fsanitize=address,undefined',
                    str(path/'rx.c'),'-o',str(path/'test')],check=True)
    subprocess.run([str(path/'test')],check=True)
