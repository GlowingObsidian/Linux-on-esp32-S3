#!/usr/bin/env python3
"""Exercise native management/keys using real wire structs and host scan decoder.

The vendor calls are captured, not emulated. This proves the bridge's data,
length and ownership contracts, not that the radio completes association.
"""
from pathlib import Path
import subprocess
import tempfile

r = Path(__file__).resolve().parent.parent
s = (r / 'build-output/wifi-smp-audit/current/esp_blob.c').read_text()
h = (r / 'kernel-driver-esp32-ng/adapter.h').read_text()
host = (r / 'build-output/wifi-smp-audit/current/esp_cmd.c').read_text()

def fun(src, name):
    p = src.index(name + '(')
    a = src.rfind('\n', 0, p) + 1
    b = src.index('{', p) + 1
    depth = 1
    while depth:
        depth += (src[b] == '{') - (src[b] == '}')
        b += 1
    return src[a:b] + '\n'

def structure(name):
    p = h.index('struct ' + name + ' {')
    return h[p:h.index('} __packed;', p) + len('} __packed;')] + '\n'

shim = r'''
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <stddef.h>
#include <stdio.h>
#include <errno.h>
#include <assert.h>
typedef uint8_t u8; typedef uint16_t u16; typedef uint32_t u32;
typedef uint64_t u64;
#define __packed __attribute__((packed))
#define MAX_SSID_LEN 32
#define MAC_ADDR_LEN 6
#define MAX_KEY_LEN 32
#define MAX_SEQ_LEN 10
#define BIT(n) (1U << (n))
#define cpu_to_le16(v) (v)
#define cpu_to_le32(v) (v)
#define cpu_to_le64(v) (v)
#define le16_to_cpu(v) (v)
#define le32_to_cpu(v) (v)
#define le64_to_cpu(v) (v)
#define GFP_ATOMIC 0
#define GFP_KERNEL 0
#define CONFIG_ESP32_WIFI_BLOB 1
#define EVENT_SCAN_RESULT 1
#define EVENT_AUTH_RX 4
#define EVENT_ASSOC_RX 5
#define PACKET_TYPE_EAPOL 9
#define CFG80211_BSS_FTYPE_UNKNOWN 0
#define CFG80211_BSS_FTYPE_BEACON 1
#define CFG80211_BSS_FTYPE_PRESP 2
#define NL80211_BAND_2GHZ 0
#define IEEE80211_STYPE_BEACON 0x80
#define IEEE80211_STYPE_PROBE_RESP 0x50
#define IEEE80211_CHAN_DISABLED 1
#define ESP_MARK_SCAN_DONE(p, aborted) ((void)0)
#define esp_err(...) ((void)0)
#define esp_info(...) ((void)0)
#define spin_lock_irqsave(lock, flags) ((void)0)
#define spin_unlock_irqrestore(lock, flags) ((void)0)
static int blob_station_lock;
static u8 blob_station_bssid[6];
static bool blob_station_bssid_valid;
static int hw, *g_blob = &hw;
static bool fail_alloc;
static void *kzalloc(size_t n, int flags) { return fail_alloc ? NULL : calloc(1,n); }
#define kfree free
static u16 get_unaligned_le16(const void *p) { u16 v; memcpy(&v,p,2); return v; }
static u32 get_unaligned_le32(const void *p) { u32 v; memcpy(&v,p,4); return v; }
static void put_unaligned_le16(u16 v, void *p) { memcpy(p,&v,2); }
static u8 event_copy[4096], eapol_copy[2048];
static unsigned event_len, eapol_len;
static int blob_post_event(void *hw, const void *p, unsigned n) {
 assert(n <= sizeof(event_copy)); memcpy(event_copy,p,n); event_len=n; return 0;
}
static int blob_queue_packet(void *hw, unsigned type, const void *p, unsigned n) {
 assert(type==PACKET_TYPE_EAPOL && n<=sizeof(eapol_copy));
 memcpy(eapol_copy,p,n); eapol_len=n; return 0;
}
static const u8 own[6]={0x34,0x85,0x18,0x7b,0x81,0xb8};
static int osi_read_mac(u8 *mac,int type) { memcpy(mac,own,6); return 0; }
struct ieee80211_hdr_3addr { u16 frame_control,duration_id; u8 addr1[6],addr2[6],addr3[6]; u16 seq_ctrl; } __packed;
struct beacon_probe_fixed_params { u64 timestamp; u16 beacon_interval,cap_info; } __packed;
struct cfg80211_bss { int unused; };
struct ieee80211_channel { int flags; };
static struct ieee80211_channel channel;
struct esp_adapter { void *wiphy; };
struct esp_wifi_device { struct esp_adapter *adapter; bool waiting_for_scan_done; int wait_for_scan_completion; void *ndev,*bss,*assoc_req_ie; unsigned assoc_req_ie_len; int rssi; };
static unsigned assoc_reports,ports_opened;
#define CFG80211_RX_ASSOC_RESP(...) (assoc_reports++)
static void esp_port_open(struct esp_wifi_device *priv) {ports_opened++;}
static void wake_up_interruptible(void *p) {}
static int ieee80211_channel_to_frequency(int c,int b) {return c;}
static struct ieee80211_channel *ieee80211_get_channel(void *w,int c) {return c==6?&channel:NULL;}
static void cfg80211_put_bss(void *w,struct cfg80211_bss *b) {}
static unsigned bss_reports,reported_ie_len;
static u8 reported_ies[2304],reported_bssid[6];
static int reported_signal;
static u16 reported_interval,reported_cap;
static u64 reported_timestamp;
static struct cfg80211_bss *capture_bss(void *w,void *c,int type,const u8 *mac,
 u64 ts,u16 cap,u16 interval,const u8 *ies,unsigned len,int signal,int flags) {
 assert(len<=sizeof(reported_ies)); memcpy(reported_ies,ies,len);
 memcpy(reported_bssid,mac,6);reported_ie_len=len;reported_signal=signal;
 reported_interval=interval;reported_cap=cap;reported_timestamp=ts;bss_reports++;
 return NULL;
}
#define CFG80211_INFORM_BSS capture_bss
static unsigned api_calls,auth_done;
static int api_result;
static unsigned registered_mask;
static blob_wifi_config_t captured_config;
static int esp_wifi_register_mgmt_frame_internal(unsigned mask,unsigned vendor) {
 api_calls++; registered_mask=mask; return api_result;
}
static int esp_wifi_set_config(int ifx,const blob_wifi_config_t *c) {
 assert(ifx==0);api_calls++;captured_config=*c;return api_result;
}
static int esp_wifi_connect(void) {api_calls++;return api_result;}
static unsigned key_index,key_set_tx,key_seq_len,key_size,key_flags,key_alg;
static u8 key_data[32],key_seq[10],key_mac[6],igtk_copy[40];
static int esp_wifi_set_sta_key_internal(int alg,u8 *addr,int index,int tx,
 u8 *seq,size_t sn,u8 *key,size_t kn,int flags) {
 api_calls++; key_alg=alg;key_index=index;key_set_tx=tx;key_seq_len=sn;key_size=kn;key_flags=flags;
 assert(sn<=sizeof(key_seq) && kn<=sizeof(key_data));
 memcpy(key_mac,addr,6);if(sn)memcpy(key_seq,seq,sn);if(kn)memcpy(key_data,key,kn);
 return api_result;
}
static int esp_wifi_set_igtk_internal(u8 ifx,const void *key) {
 api_calls++;memcpy(igtk_copy,key,40);return api_result;
}
static bool esp_wifi_auth_done_internal(void) {auth_done++;return true;}
'''

main = r'''
int main(void) {
 const u8 ap[6]={2,3,4,5,6,7};
 u8 beacon[]={0x11,0x22,0x33,0x44,0,0,0,0,100,0,0x31,4, 0,3,'a','p','1',3,1,6};
 struct esp_adapter adapter={0};struct esp_wifi_device priv={.adapter=&adapter};
 assert(!blob_scan_rx_mgmt(8,beacon,sizeof(beacon),(u8*)ap,(u32)-43,6,123));
 assert(event_len==sizeof(struct scan_event)+sizeof(beacon));
 struct scan_event *scan=(void*)event_copy;
 assert(scan->header.len==event_len-sizeof(scan->header));
 process_scan_result_event(&priv,scan);
 assert(bss_reports==1 && reported_signal==-4300 && reported_interval==100 && reported_cap==0x431);
 assert(reported_timestamp==0x44332211 && !memcmp(reported_bssid,ap,6));
 assert(reported_ie_len==sizeof(beacon)-12 && !memcmp(reported_ies,beacon+12,reported_ie_len));
 assert(blob_scan_rx_mgmt(8,beacon,11,(u8*)ap,0,6,0)==-EINVAL);
 assert(blob_scan_rx_mgmt(8,beacon,2305,(u8*)ap,0,6,0)==-EINVAL);
 assert(blob_scan_rx_mgmt(8,beacon,12,(u8*)ap,0,0,0)==-EINVAL);
 fail_alloc=true;assert(blob_scan_rx_mgmt(8,beacon,12,(u8*)ap,0,6,0)==-ENOMEM);fail_alloc=false;
 memcpy(blob_station_bssid,ap,6);blob_station_bssid_valid=true;
 u8 auth[]={0,0,2,0,0,0};
 assert(!blob_station_rx_mgmt(11,auth,6,(u8*)ap,(u32)-30,6,4));
 struct auth_event *ae=(void*)event_copy;struct ieee80211_hdr_3addr *mh=(void*)ae->frame;
 assert(ae->frame_len==30 && event_len==sizeof(*ae)+30 && ae->header.event_code==EVENT_AUTH_RX);
 assert(mh->frame_control==0xb0 && !memcmp(mh->addr1,own,6) && !memcmp(mh->addr2,ap,6));
 assert(!memcmp(ae->frame+24,auth,6));
 assert(!blob_station_rx_mgmt(1,auth,6,(u8*)ap,(u32)-30,6,4));
 struct assoc_event *as=(void*)event_copy;
 assert(as->frame_len==30 && event_len==sizeof(*as)+30 && as->header.event_code==EVENT_ASSOC_RX);
 assert(!memcmp(as->frame+24,auth,6));
 as->frame[26]=0;process_assoc_event(&priv,as);assert(assoc_reports==1 && ports_opened==1);
 as->frame[26]=17;process_assoc_event(&priv,as);
 assert(assoc_reports==2 && ports_opened==1); /* notify rejection; don't open carrier */
 as->frame_len=25;process_assoc_event(&priv,as);assert(assoc_reports==2 && ports_opened==1);
 assert(blob_station_rx_mgmt(11,auth,5,(u8*)ap,0,6,0)==-EINVAL);
 assert(blob_station_rx_mgmt(11,auth,6,(u8*)own,0,6,0)==-EINVAL);
 u8 eapol[]={2,3,0,0};
 assert(!blob_station_eapol((u8*)ap,eapol,4));
 assert(eapol_len==4 && !memcmp(eapol_copy,eapol,4));
 eapol[0]=9;assert(eapol_copy[0]==2); /* copied; borrowed caller buffer remains owned */
 assert(blob_station_eapol((u8*)own,eapol,4)==-EINVAL);
 assert(blob_station_eapol((u8*)ap,eapol,3)==-EINVAL);
 assert(blob_station_eapol((u8*)ap,eapol,2049)==-EINVAL);
 struct cmd_sta_auth request={.channel=6};
 memcpy(request.ssid,"real network",12);memcpy(request.bssid,ap,6);
 api_calls=0;assert(!blob_station_auth(&request,sizeof(request)) && api_calls==3);
 assert(!memcmp(captured_config.sta.ssid,request.ssid,32) && captured_config.sta.bssid_set);
 assert(!memcmp(captured_config.sta.bssid,ap,6) && captured_config.sta.channel==6);
 assert(!captured_config.sta.password[0]);
 request.pad[0]=1;assert(!blob_station_auth(&request,sizeof(request)));
 assert(!memcmp(captured_config.sta.password,"12345678",8) && captured_config.sta.threshold.authmode==3);
 api_calls=0;assert(blob_station_auth(&request,sizeof(request)-1)==-EOPNOTSUPP && !api_calls);
 request.auth_type=3;assert(blob_station_auth(&request,sizeof(request))==-EOPNOTSUPP && !api_calls);
 request.auth_type=0;request.channel=0;assert(blob_station_auth(&request,sizeof(request))==-EOPNOTSUPP && !api_calls);
 request.channel=6;api_result=-EIO;assert(blob_station_auth(&request,sizeof(request))==-EIO && api_calls==1);api_result=0;
 struct wifi_sec_key key={.algo=3,.index=0,.len=16};
 memcpy(key.mac_addr,ap,6);memset(key.data,0x7a,16);key.pad[0]=1;
 assert(!blob_station_key(&key,false) && key_alg==3 && key_index==0 && key_set_tx==1);
 assert(key_size==16 && key_seq_len==0 && !memcmp(key_data,key.data,16) && !auth_done);
 assert(key_flags==(BIT(5)|BIT(2)|BIT(3)));
 key.index=1;key.pad[0]=0;key.seq_len=6;memset(key.seq,0x31,6);
 assert(!blob_station_key(&key,false) && key_flags==(BIT(4)|BIT(2)) && auth_done==1);
 assert(!memcmp(key_seq,key.seq,6));
 assert(!blob_station_key(&key,true) && !key_alg && !key_size && !key_seq_len);
 key.index=4;key.algo=7;assert(!blob_station_key(&key,false));
 assert(igtk_copy[0]==4 && !memcmp(igtk_copy+2,key.seq,6) && !memcmp(igtk_copy+8,key.data,16));
 api_calls=0;key.index=6;assert(blob_station_key(&key,false)==-EINVAL && !api_calls);
 key.index=1;key.algo=2;assert(blob_station_key(&key,false)==-EOPNOTSUPP && !api_calls);
 key.algo=3;key.len=33;assert(blob_station_key(&key,false)==-EINVAL && !api_calls);
 key.len=16;key.seq_len=11;assert(blob_station_key(&key,false)==-EINVAL && !api_calls);
 const u8 rsn[]={48,20,1,0,0,15,172,4,1,0,0,15,172,4,1,0,0,15,172,2,0xc0,0};
 blob_wifi_wpa_ie_t parsed;
 assert(!blob_parse_security_ie(rsn,sizeof(rsn),&parsed));
 assert(parsed.proto==BIT(1) && parsed.group_cipher==BIT(3) && parsed.pairwise_cipher==BIT(3));
 assert(parsed.key_mgmt==BIT(1) && parsed.capabilities==0xc0);
 for(unsigned n=0;n<sizeof(rsn);n++) assert(blob_parse_security_ie(rsn,n,&parsed)==-EINVAL);
 u8 bad[sizeof(rsn)];memcpy(bad,rsn,sizeof(bad));bad[8]=255;
 assert(blob_parse_security_ie(bad,sizeof(bad),&parsed)==-EINVAL);
 puts("PASS: actual scan producer -> host decoder preserves real IEs/BSSID/RSSI; auth/assoc wire frames; borrowed EAPOL ownership; open/WPA2 config; CCMP/IGTK and rejection paths; bounded RSN parser");
}
'''

abi = (r / 'build-output/wifi-smp-audit/current/esp_blob_radio_abi.h').read_text()
# Types must precede the mock APIs but follow stdint/stdbool.
shim = shim.replace('static unsigned api_calls,auth_done;', abi + '\nstatic unsigned api_calls,auth_done;')
structures = ''.join(structure(n) for n in ['command_header', 'event_header', 'scan_event', 'auth_event', 'assoc_event', 'cmd_sta_auth', 'wifi_sec_key'])
functions = ''.join(fun(s, n) for n in ['blob_cipher_selector', 'blob_akm_selector', 'blob_parse_security_ie', 'blob_scan_rx_mgmt', 'blob_station_eapol', 'blob_station_rx_mgmt', 'blob_station_auth', 'blob_station_key'])
functions += fun(host, 'process_scan_result_event')
functions += fun(host, 'process_assoc_event')
with tempfile.TemporaryDirectory() as temp:
    src = Path(temp) / 'native.c'
    out = Path(temp) / 'native'
    src.write_text(shim + structures + functions + main)
    subprocess.run(['cc', '-std=gnu11', '-fsanitize=address,undefined', str(src), '-o', str(out)], check=True)
    subprocess.run([str(out)], check=True)
