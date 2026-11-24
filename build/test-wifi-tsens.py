#!/usr/bin/env python3
"""Replay hwmon/PHY TSENS sharing, including failed conversions and analog I/O."""
from pathlib import Path
import re, subprocess, tempfile, sys
r=Path(__file__).resolve().parent.parent
c=r/'build-output/wifi-smp-audit/current'
s=Path(sys.argv[1] if len(sys.argv)>1 else c/'esp32s3-tsens.c').read_text()
phy=(c/'esp_blob_phy.c').read_text()
def fun(src,n):
    m=re.search(r'^[^\n;]*\b'+re.escape(n)+r'\([^;{}]*\)\s*\{',src,re.M);assert m,n
    a=m.start();b=src.index('{',a)+1;d=1
    while d:d+=(src[b]=='{')-(src[b]=='}');b+=1
    return src[a:b]+'\n'
constants='\n'.join(l for l in s.splitlines() if l.startswith('#define '))
constants+='\n'+'\n'.join(l for l in phy.splitlines() if l.startswith('#define SENS_'))
shim=r'''
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <stdio.h>
#include <errno.h>
#include <assert.h>
#include <pthread.h>
typedef uint8_t u8;typedef uint32_t u32;
#define BIT(n) (1U<<(n))
#define IS_REACHABLE(x) 1
#define CONFIG_ESP32_WIFI_BLOB 1
static pthread_mutex_t lock=PTHREAD_MUTEX_INITIALIZER;
static _Thread_local unsigned depth;
static unsigned reads,writes,fail_read,fail_write,warnings;
static bool timeout;
static u8 dac;
static u32 sens[128],ana[128];static void *sens_base=sens;
struct esp32s3_tsens {void *sens,*ana;};
static u32 phy_enter_critical(void){assert(!depth);pthread_mutex_lock(&lock);depth=1;return 0;}
static void phy_exit_critical(u32 flags){assert(depth==1);depth=0;pthread_mutex_unlock(&lock);}
static void udelay(unsigned n){assert(depth==1);}
static void usleep_range(unsigned a,unsigned b){assert(!depth);}
#define pr_warn(...) (warnings++)
'''
mmio=r'''
static u32 readl(const void *p){
 u32 v=*(const u32*)p;
 if(p==(void*)((char*)sens+SENS_TSENS_CTRL)){
  v&=~(TSENS_READY|TSENS_OUT);
  v|=145;
  if(!timeout && (v&TSENS_DUMP_OUT) && (v&TSENS_POWER_UP) &&
     (sens[SENS_PERI_CLK_GATE/4]&PERI_TSENS_CLK_EN))v|=TSENS_READY;
 }
 return v;
}
static void writel(u32 v,void *p){
 if(p==(void*)((char*)sens+SENS_TSENS_CTRL))v&=~(TSENS_READY|TSENS_OUT);
 *(u32*)p=v;
}
static u32 sens_read(u32 off){return readl((char*)sens+off);}
static void sens_set_bits(u32 off,u32 bits){writel(sens_read(off)|bits,(char*)sens+off);}
static void sens_clr_bits(u32 off,u32 bits){writel(sens_read(off)&~bits,(char*)sens+off);}
static int tsens_ana_read(struct esp32s3_tsens *ts,u8 b,u8 a,u8 *v){
 assert(depth==1);if(++reads==fail_read)return -EIO;*v=dac;return 0;
}
static int tsens_ana_write(struct esp32s3_tsens *ts,u8 b,u8 a,u8 v){
 assert(depth==1);if(++writes==fail_write)return -EIO;dac=v;return 0;
}
static int tsens_ana_write_bits(struct esp32s3_tsens *ts,u8 b,u8 a,u8 mask,u8 v){
 assert(depth==1);if(++writes==fail_write)return -EIO;dac=(dac&~mask)|(v&mask);return 0;
}
'''
main=r'''
static struct esp32s3_tsens ts={sens,ana};
static void setup(bool on){
 reads=writes=fail_read=fail_write=warnings=0;timeout=false;dac=0xa7;
 sens[SENS_TSENS_CTRL/4]=on?(TSENS_POWER_UP|TSENS_POWER_UP_FORCE|(13<<14)):0;
 sens[SENS_TSENS_CTRL2/4]=on?TSENS_XPD_FORCE:0;
 sens[SENS_PERI_CLK_GATE/4]=BIT(30)|(on?PERI_TSENS_CLK_EN:0);
 ana[ANA_CONFIG/4]=0x12345678;ana[ANA_CONFIG2/4]=0x23456789;
}
static void measure(bool on,int error){
 u32 ctrl=sens[SENS_TSENS_CTRL/4],ctrl2=sens[SENS_TSENS_CTRL2/4],clk=sens[SENS_PERI_CLK_GATE/4];
 u32 a=ana[ANA_CONFIG/4],a2=ana[ANA_CONFIG2/4];int raw=-1;
 assert(esp32s3_tsens_measure(&ts,&raw)==error);
 assert(!depth && sens[SENS_TSENS_CTRL/4]==ctrl && sens[SENS_TSENS_CTRL2/4]==ctrl2);
 assert(sens[SENS_PERI_CLK_GATE/4]==clk && ana[ANA_CONFIG/4]==a && ana[ANA_CONFIG2/4]==a2);
 assert(dac==0xa7);if(!error)assert(raw==145);else assert(raw==-1);
 if(on){timeout=false;assert(phy_get_tsens_value()==145 && !warnings);}
}
static void *worker(void *arg){
 for(unsigned n=0;n<200;n++){
  if(arg){int raw;assert(!esp32s3_tsens_measure(&ts,&raw) && raw==145);}
  else assert(phy_get_tsens_value()==145);
 }return NULL;
}
int main(void){
 setup(false);measure(false,0);setup(true);measure(true,0);
 setup(true);timeout=true;measure(true,-ETIMEDOUT);
 setup(true);fail_read=1;measure(true,-EIO);
 setup(true);fail_write=1;measure(true,-EIO);
 setup(true);pthread_t a,b;pthread_create(&a,NULL,worker,(void*)1);pthread_create(&b,NULL,worker,NULL);
 pthread_join(a,NULL);pthread_join(b,NULL);assert(!warnings && dac==0xa7);
 puts("PASS: actual hwmon preserves PHY sensor power/clock/divider/DAC/analog state on success and failures; concurrent hwmon/PHY reads serialize without sleeping under the PHY lock");
}
'''
code=shim+constants+'\n'+mmio+fun(s,'tsens_power')+fun(s,'esp32s3_tsens_measure')+fun(phy,'phy_get_tsens_value')+main
with tempfile.TemporaryDirectory() as temp:
    src=Path(temp)/'tsens.c';out=Path(temp)/'tsens';src.write_text(code)
    subprocess.run(['cc','-std=gnu11','-pthread','-fsanitize=address,undefined',str(src),'-o',str(out)],check=True)
    subprocess.run([str(out)],check=True)
