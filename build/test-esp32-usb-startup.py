#!/usr/bin/env python3
"""Exercise the actual DWC2 enable path with inherited USB hardware state."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

source = Path(sys.argv[1]).read_text()
match = re.search(r'^static int __dwc2_lowlevel_hw_enable\([^;{}]*\)\s*\{', source, re.M)
assert match
end = source.index('{', match.start()) + 1
depth = 1
while depth:
    depth += (source[end] == '{') - (source[end] == '}')
    end += 1
function = source[match.start():end]
shim = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
struct device { void *of_node; };
struct platform_device { struct device dev; };
struct platform_data { int (*phy_init)(struct platform_device*,int); int phy_type; };
struct dwc2_hsotg {
 struct device *dev; struct platform_data *plat;
 void *clk,*utmi_clk,*uphy,*phy,*reset;
 int supplies[2]; bool ll_hw_enabled;
};
static char events[64];static unsigned nr;
static bool s3,clock_on,reset_on,pulsed,require_pulse;
static char fail;
static void event(char c){events[nr++]=c;events[nr]=0;}
static struct platform_device *to_platform_device(struct device*d){return (void*)d;}
static bool of_device_is_compatible(void*n,const char*s){(void)n;assert(!strcmp(s,"esp,esp32s3-usb"));return s3;}
static int regulator_bulk_enable(unsigned n,int*s){(void)n;(void)s;event('r');return fail=='r'?-EIO:0;}
static void regulator_bulk_disable(unsigned n,int*s){(void)n;(void)s;event('R');}
static int clk_prepare_enable(void*c){event(c==(void*)1?'u':'c');if(fail==events[nr-1])return -EIO;if(c==(void*)2)clock_on=true;return 0;}
static void clk_disable_unprepare(void*c){event(c==(void*)1?'U':'C');if(c==(void*)2)clock_on=false;}
static int reset_control_assert(void*r){assert(r && clock_on);event('a');if(fail=='a')return -EIO;reset_on=true;return 0;}
static int reset_control_deassert(void*r){assert(r && clock_on && reset_on);event('d');if(fail=='d')return -EIO;reset_on=false;pulsed=true;return 0;}
static int phy_init(void*p){(void)p;event('i');assert(clock_on && !reset_on);assert(!require_pulse || pulsed);return fail=='i'?-EIO:0;}
static int phy_power_on(void*p){(void)p;event('p');return fail=='p'?-EIO:0;}
static void phy_exit(void*p){(void)p;event('x');}
static int usb_phy_init(void*p){return phy_init(p);}
'''
main = r'''
static void setup(struct dwc2_hsotg*h){
 nr=0;events[0]=0;fail=0;clock_on=false;reset_on=false;pulsed=false;
 s3=true;require_pulse=true;h->ll_hw_enabled=false;h->uphy=NULL;
}
int main(void){
 struct platform_device d={0};struct dwc2_hsotg h={.dev=&d.dev,.clk=(void*)2,.utmi_clk=(void*)1,.reset=(void*)3,.phy=(void*)4};
 setup(&h);assert(!__dwc2_lowlevel_hw_enable(&h));assert(!strcmp(events,"rucadip"));
 setup(&h);s3=false;require_pulse=false;assert(!__dwc2_lowlevel_hw_enable(&h));assert(!strcmp(events,"rucip"));
 setup(&h);h.ll_hw_enabled=true;require_pulse=false;assert(!__dwc2_lowlevel_hw_enable(&h));assert(!strcmp(events,"rucip"));
 setup(&h);h.uphy=(void*)5;assert(!__dwc2_lowlevel_hw_enable(&h));assert(!strcmp(events,"rucadi"));
 setup(&h);fail='a';assert(__dwc2_lowlevel_hw_enable(&h)==-EIO);assert(!strcmp(events,"rucaCUR") && !clock_on);
 setup(&h);fail='d';assert(__dwc2_lowlevel_hw_enable(&h)==-EIO);assert(!strcmp(events,"rucadCUR") && !clock_on);
 setup(&h);fail='c';assert(__dwc2_lowlevel_hw_enable(&h)==-EIO);assert(!strcmp(events,"rucUR") && !pulsed);
 setup(&h);fail='u';assert(__dwc2_lowlevel_hw_enable(&h)==-EIO);assert(!strcmp(events,"ruR") && !pulsed);
 setup(&h);fail='i';assert(__dwc2_lowlevel_hw_enable(&h)==-EIO);assert(!strcmp(events,"rucadiCUR") && !clock_on);
 setup(&h);fail='p';assert(__dwc2_lowlevel_hw_enable(&h)==-EIO);assert(!strcmp(events,"rucadipxCUR") && !clock_on);
 puts("PASS: S3 clock/reset/PHY order, inherited state, resume preservation, other platforms and error cleanup");
}
'''
with tempfile.TemporaryDirectory(prefix='usb-startup-test-') as temp:
    path = Path(temp)
    c = path/'test.c'
    exe = path/'test'
    c.write_text(shim + function + main)
    subprocess.run(['cc', '-std=gnu11', '-fsanitize=address,undefined', str(c), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
