#!/usr/bin/env python3
"""IP notifications belong to a device identity, independent of its name."""
from pathlib import Path
import subprocess,tempfile
r=Path(__file__).resolve().parent.parent
s=(r/'build-output/wifi-smp-audit/current/esp_cfg80211.c').read_text()
p=s.index('static int esp_inetaddr_event(');b=s.index('{',p)+1;d=1
while d:d+=(s[b]=='{')-(s[b]=='}');b+=1
shim=r'''
#include <stdint.h>
#include <stddef.h>
#include <assert.h>
#include <stdio.h>
#define container_of(p,t,m) ((t*)((char*)(p)-offsetof(t,m)))
#define NOTIFY_DONE 0
#define ESP_STA_IF 0
#define NETDEV_UP 1
#define NETDEV_DOWN 2
#define esp_info(...) ((void)0)
struct notifier_block {int unused;};
struct net_device {const char *name;};
struct in_device {struct net_device *dev;};
struct in_ifaddr {struct in_device *ifa_dev;uint32_t ifa_local;};
struct esp_wifi_device {struct notifier_block nb;struct net_device *ndev;int if_type;};
static unsigned calls;static uint32_t ip;
static void cmd_set_ip_address(struct esp_wifi_device *p,uint32_t addr){calls++;ip=addr;}
'''
main=r'''
int main(void){
 struct net_device own={"wlan0"},foreign={"wlan0"};
 struct esp_wifi_device priv={.ndev=&own};struct in_device dev={&own};
 struct in_ifaddr addr={&dev,0x1234};
 esp_inetaddr_event(&priv.nb,NETDEV_UP,&addr);assert(calls==1 && ip==0x1234);
 own.name="renamed-by-user";esp_inetaddr_event(&priv.nb,NETDEV_UP,&addr);assert(calls==2);
 dev.dev=&foreign;esp_inetaddr_event(&priv.nb,NETDEV_UP,&addr);assert(calls==2);
 esp_inetaddr_event(&priv.nb,NETDEV_UP,NULL);addr.ifa_dev=NULL;
 esp_inetaddr_event(&priv.nb,NETDEV_UP,&addr);assert(calls==2);
 addr.ifa_dev=&dev;dev.dev=&own;esp_inetaddr_event(&priv.nb,NETDEV_DOWN,&addr);assert(calls==3 && !ip);
 puts("PASS: renamed station IP notifications, foreign interface isolation, missing event data and disconnect");
}
'''
with tempfile.TemporaryDirectory() as temp:
    src=Path(temp)/'netdev.c';out=Path(temp)/'netdev';src.write_text(shim+s[p:b]+main)
    subprocess.run(['cc','-std=gnu11','-fsanitize=address,undefined',str(src),'-o',str(out)],check=True)
    subprocess.run([str(out)],check=True)
