#!/usr/bin/env python3
"""Exercise actual PHY callbacks and SMP critical sections, without hardware."""
from pathlib import Path
import subprocess,tempfile,sys,re
r=Path(__file__).resolve().parent.parent
s=Path(sys.argv[1] if len(sys.argv)>1 else r/'build-output/wifi-smp-audit/current/esp_blob_phy.c').read_text()
def fun(n):
 m=re.search(r'^[^\n;]*\b'+re.escape(n)+r'\([^;{}]*\)\s*\{',s,re.M);assert m,n
 a=m.start();b=s.index('{',a)+1;d=1
 while d:d+=(s[b]=='{')-(s[b]=='}');b+=1
 return s[a:b]
# Compare every init-data byte against pinned IDF, not another hand-built array.
h=(r/'build-output/wifi-smp-audit/base/idf-phy_init_data.h').read_text()
body=h.split('static const esp_phy_init_data_t phy_init_data= { {',1)[1].split('} };',1)[0]
body=re.sub(r'LIMIT\(CONFIG_ESP_PHY_MAX_TX_POWER \* 4, 0, (0x[0-9a-f]+)\)',lambda m:str(min(80,int(m[1],16))),body)
expected=[int(x.strip(),0) for x in body.split(',') if x.strip()]
expected += [0]*(128-len(expected))
arr=s[s.index('static const u8 phy_init_data['):];arr=arr[:arr.index('};')+2]
actual=[int(x,16) for x in re.findall(r'0x[0-9a-f]+',arr)]
assert actual==expected and len(actual)==128,'PHY init data differs from pinned IDF'
shim=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <stdio.h>
#include <pthread.h>
#include <errno.h>
#include <stdatomic.h>
typedef uint32_t u32;typedef uint8_t u8;
#define BIT(n) (1U<<(n))
#define __aligned(x) __attribute__((aligned(x)))
#define NR_CPUS 2
#define READ_ONCE(x) __atomic_load_n(&(x),__ATOMIC_SEQ_CST)
#define WRITE_ONCE(x,v) __atomic_store_n(&(x),(v),__ATOMIC_SEQ_CST)
#define DEFINE_RAW_SPINLOCK(x) pthread_mutex_t x=PTHREAD_MUTEX_INITIALIZER
#define raw_spin_lock(x) pthread_mutex_lock(x)
#define raw_spin_unlock(x) pthread_mutex_unlock(x)
static _Thread_local unsigned this_cpu,preempt_depth,irq_off;
#define preempt_disable() (++preempt_depth)
#define preempt_enable() (assert(preempt_depth),--preempt_depth)
#define raw_smp_processor_id() this_cpu
#define local_irq_save(f) do{f=irq_off;irq_off=1;}while(0)
#define local_irq_restore(f) (irq_off=(f))
#define DEFINE_MUTEX(x) pthread_mutex_t x=PTHREAD_MUTEX_INITIALIZER
#define mutex_lock pthread_mutex_lock
#define mutex_unlock pthread_mutex_unlock
static pthread_mutex_t phy_lock=PTHREAD_MUTEX_INITIALIZER;
#define spin_lock_irqsave(l,f) do{f=0;pthread_mutex_lock(l);}while(0)
#define spin_unlock_irqrestore(l,f) pthread_mutex_unlock(l)
#define pr_info(...) ((void)0)
#define pr_debug(...) ((void)0)
#define IS_ENABLED(x) (x)
#ifndef CONFIG_PHY_ESP32S3_USB
#define CONFIG_PHY_ESP32S3_USB 0
#endif
static u32 regs[128];static bool sar_adc_on,pwdet_on,tsens_on,phy_calibrated;
static u32 sens_read(u32 off){assert(off%4==0 && off<sizeof(regs));return regs[off/4];}
static void sens_write(u32 off,u32 v){assert(off%4==0 && off<sizeof(regs));regs[off/4]=v;}
static void sens_set_bits(u32 off,u32 b){sens_write(off,sens_read(off)|b);}
#define PHY_RF_CAL_FULL 2
static u8 phy_rf_cal_data[1904];static DEFINE_MUTEX(phy_init_lock);
static void*g_phyFuns;static unsigned cal_calls;static int cal_ret;static bool missing_table;
static unsigned usb_calls;static bool usb_armed;
static void phy_bbpll_en_usb(bool en){assert(en);usb_armed=en;usb_calls++;}
static int register_chipv7_phy(const void*data,void*cal,int mode){
 assert(usb_armed==!!CONFIG_PHY_ESP32S3_USB);
 assert(usb_calls==(CONFIG_PHY_ESP32S3_USB?cal_calls+1:0));usb_armed=false;
 assert(data && cal==phy_rf_cal_data && mode==2);cal_calls++;
 g_phyFuns=missing_table?NULL:(void*)1;return cal_ret;
}
'''
const='\n'.join(x for x in s.splitlines() if x.startswith('#define SENS_') or x.startswith('#define PHY_INIT_DATA_LEN'))
a=s.index('static DEFINE_RAW_SPINLOCK(phy_critical_lock)');b=s.index('uint32_t phy_enter_critical',a)
code=shim+const+'\n'+arr+'\n'+s[a:b]+'\nuint32_t phy_enter_critical(void);\nvoid phy_exit_critical(uint32_t level);\n'
for n in ['phy_update_sar_power','set_xpd_sar','phy_set_pwdet_power','phy_set_tsens_power','phy_enter_critical','phy_exit_critical','esp32_wifi_phy_enable']:code+=fun(n)+'\n'
main=r'''
static unsigned count;static atomic_int inside;
static void*worker(void*p){this_cpu=(uintptr_t)p;
 for(unsigned i=0;i<10000;i++){
  u32 outer=phy_enter_critical();assert(atomic_fetch_add(&inside,1)==0);
  u32 inner=phy_enter_critical();count++;phy_exit_critical(inner);
  assert(irq_off && preempt_depth);assert(atomic_fetch_sub(&inside,1)==1);
  phy_exit_critical(outer);assert(!irq_off && !preempt_depth);
 }return NULL;
}
int main(void){
 regs[0]=0xabcdef01;regs[0x3c/4]=0x12345678;
 set_xpd_sar(true);assert((regs[0x3c/4]>>29&3)==3);
 phy_set_pwdet_power(true);set_xpd_sar(false);assert((regs[0x3c/4]>>29&3)==3);
 phy_set_pwdet_power(false);assert((regs[0x3c/4]>>29&3)==0);
 assert(regs[0]==0xabcdef01 && (regs[0x104/4]&BIT(30)));
 phy_set_tsens_power(true);assert((regs[0x50/4]&(BIT(22)|BIT(23)))==(BIT(22)|BIT(23)));
 assert((regs[0x54/4]>>12&3)==1 && (regs[0x104/4]&BIT(29)));
 phy_set_tsens_power(false);assert(!(regs[0x50/4]&(BIT(22)|BIT(23))) && !(regs[0x54/4]>>12&3));
 missing_table=true;assert(esp32_wifi_phy_enable()==-EIO && !phy_calibrated);
 missing_table=false;cal_ret=-7;assert(esp32_wifi_phy_enable()==-7 && !phy_calibrated);
 cal_ret=0;assert(!esp32_wifi_phy_enable() && phy_calibrated);
 assert(!esp32_wifi_phy_enable() && cal_calls==3);
 assert(usb_calls==(CONFIG_PHY_ESP32S3_USB?3:0));
 pthread_t a,b;pthread_create(&a,NULL,worker,(void*)0);pthread_create(&b,NULL,worker,(void*)1);
 pthread_join(a,NULL);pthread_join(b,NULL);assert(count==20000);
 puts("PASS: USB preservation precedes calibration/retries; exact IDF data, full calibration, S3 power and cross-CPU critical sections");
}
'''
with tempfile.TemporaryDirectory() as d:
 src=Path(d)/'test.c';exe=Path(d)/'test';src.write_text(code+main)
 for usb_enabled in (0,1):
  subprocess.run(['cc','-std=gnu11','-pthread','-fsanitize=address,undefined',f'-DCONFIG_PHY_ESP32S3_USB={usb_enabled}',str(src),'-o',str(exe)],check=True)
  subprocess.run([str(exe)],check=True)
