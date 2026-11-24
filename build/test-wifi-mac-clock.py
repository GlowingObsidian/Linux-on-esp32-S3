#!/usr/bin/env python3
"""Test actual MAC, clock and reset callbacks against known S3 values."""
from pathlib import Path
import subprocess,tempfile,sys
r=Path(__file__).resolve().parent.parent
s=Path(sys.argv[1] if len(sys.argv)>1 else r/'build-output/wifi-smp-audit/current/esp_blob.c').read_text()
def fun(n):
 p=s.index(n+'(');a=s.rfind('\n',0,p)+1;b=s.index('{',p)+1;d=1
 while d:d+=(s[b]=='{')-(s[b]=='}');b+=1
 return s[a:b]
shim=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
typedef uint32_t u32;typedef uint8_t u8;
#define __iomem
#define BIT(n) (1U<<(n))
#define pr_warn(...) ((void)0)
#define pr_info(...) ((void)0)
#define pr_debug(...) ((void)0)
#define udelay(n) ((void)0)
static void*efuse_base;static u32 lo=0x187b81b8,hi=0x3485,clock_reg=0x80008020,reset_reg=0x1000;
static u32 writes[8];static unsigned nwrite;
static void*ioremap(unsigned a,unsigned n){assert(a==0x60007000);return(void*)(uintptr_t)a;}
static u32 readl(void*p){switch((uintptr_t)p){
case 0x60007044:return lo;case 0x60007048:return hi;
case 0x60026014:return clock_reg;case 0x60026018:return reset_reg;
default:assert(0);return 0;}}
static void writel(u32 v,void*p){writes[nwrite++]=v;switch((uintptr_t)p){
case 0x60026014:clock_reg=v;break;case 0x60026018:reset_reg=v;break;default:assert(0);}}
'''
const='\n'.join(x for x in s.splitlines() if x.startswith(('#define ESP32_EFUSE_BASE','#define EFUSE_RD_MAC','#define ESP32_SYSTEM_BASE','#define SYSTEM_WIFI_','#define WIFI_COMMON_CLK_MASK','#define MAC_RST','#define ESP32_SYSTEM_REG')))
main=r'''
int main(void){
 unsigned char mac[6];const unsigned char base[6]={0x34,0x85,0x18,0x7b,0x81,0xb8};
 for(unsigned type=0;type<4;type++){assert(!osi_read_mac(mac,type));assert(!memcmp(mac,base,5));assert(mac[5]==base[5]+type);}
 assert(!osi_read_mac(mac,5) && !memcmp(mac,base,6));
 assert(osi_read_mac(NULL,0));lo=hi=0;assert(osi_read_mac(mac,0));
 lo=hi=~0U;assert(osi_read_mac(mac,0));
 osi_wifi_clock_enable();assert(clock_reg==(0x80008020|0x78078f));
 nwrite=0;osi_wifi_reset_mac();assert(nwrite==2 && writes[0]==0x1004 && writes[1]==0x1000);
 puts("PASS: exact factory MAC, derived addresses, invalid eFuse, S3 reset bit2 and preserved clock bits");
}
'''
with tempfile.TemporaryDirectory() as d:
 src=Path(d)/'test.c';out=Path(d)/'test';src.write_text(shim+const+'\n'+''.join(fun(n) for n in ['osi_read_mac','osi_wifi_clock_enable','osi_wifi_reset_mac'])+main)
 subprocess.run(['cc','-std=gnu11','-fsanitize=address,undefined',str(src),'-o',str(out)],check=True)
 subprocess.run([str(out)],check=True)
