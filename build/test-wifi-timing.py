#!/usr/bin/env python3
"""Exercise real timing helpers: disabled path, skb ownership and 64-bit reporting."""
from pathlib import Path
import subprocess,sys,tempfile
s=Path(sys.argv[1]).read_text()
a=s.index('enum wifi_time_kind {');b=s.index('module_param_cb(timing,',a)
block=s[a:s.index('\n',b)+1]
shim=r'''
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <assert.h>
typedef uint64_t u64;
static u64 now=1000000000ULL;
#define READ_ONCE(x) (x)
#define DEFINE_SPINLOCK(n) int n
#define spin_lock_irqsave(lock,flags) do{(flags)=0;}while(0)
#define spin_unlock_irqrestore(lock,flags) ((void)0)
#define module_param_named(...)
#define module_param_cb(...)
#define BUILD_BUG_ON(x) _Static_assert(!(x), "cb overlap")
#define PAGE_SIZE 4096
#define USEC_PER_SEC 1000000
#define HZ 100
#define scnprintf snprintf
struct kernel_param {int unused;};
struct kernel_param_ops {int (*get)(char *,const struct kernel_param *);};
struct esp_skb_cb {void *priv;};
struct sk_buff {unsigned char cb[48];};
static u64 ktime_get_ns(void){return now;}
static u64 div_u64(u64 a,unsigned b){return a/b;}
'''
main=r'''
int main(void){
 struct sk_buff skb;char output[4096];memset(&skb,0xa5,sizeof(skb));
 wifi_time_enqueue(&skb);wifi_time_dequeue(&skb, WIFI_TIME_TX_QUEUE);
 assert(!wifi_times[WIFI_TIME_TX_QUEUE].count);
 for(unsigned i=0;i<40;i++)assert(skb.cb[i]==0xa5);
 wifi_timing_enabled=true;wifi_time_enqueue(&skb);
 now+=12500000;wifi_time_dequeue(&skb, WIFI_TIME_TX_QUEUE);
 assert(wifi_times[WIFI_TIME_TX_QUEUE].count==1);
 assert(wifi_times[WIFI_TIME_TX_QUEUE].total_us==12500);
 assert(wifi_times[WIFI_TIME_TX_QUEUE].ge_1ms==1);
 assert(wifi_times[WIFI_TIME_TX_QUEUE].ge_10ms==1);
 assert(wifi_times[WIFI_TIME_TX_QUEUE].ge_100ms==0);
 wifi_time_enqueue(&skb);
 now+=2000000;wifi_time_dequeue(&skb, WIFI_TIME_RX_QUEUE);
 assert(wifi_times[WIFI_TIME_RX_QUEUE].total_us==2000);
 assert(wifi_times[WIFI_TIME_TX_QUEUE].total_us==12500);
 u64 started=wifi_time_start();now+=5000000000000ULL;
 wifi_time_record(WIFI_TIME_TX_API,started);
 assert(wifi_times[WIFI_TIME_TX_API].total_us==5000000000ULL);
 wifi_timing_enabled=false;wifi_time_enqueue(&skb);wifi_time_dequeue(&skb, WIFI_TIME_TX_QUEUE);
 assert(wifi_times[WIFI_TIME_TX_QUEUE].count==1);
 int n=wifi_timing_ops.get(output,NULL);
 assert(n>0 && n<(int)sizeof(output));
 assert(strstr(output,"enabled=0 jiffy_us=10000"));
 assert(strstr(output,"tx_api count=1 total_us=5000000000 max_us=5000000000 ge_1ms=1 ge_10ms=1 ge_100ms=1"));
 for(unsigned i=0;i<40;i++)assert(skb.cb[i]==0xa5);
 puts("PASS disabled path, queue timestamp isolation, thresholds and 64-bit timing report");
}
'''
with tempfile.TemporaryDirectory() as temp:
 p=Path(temp);(p/'timing.c').write_text(shim+block+main)
 subprocess.run(['cc','-std=gnu11','-fsanitize=address,undefined',str(p/'timing.c'),'-o',str(p/'timing')],check=True)
 subprocess.run([str(p/'timing')],check=True)
