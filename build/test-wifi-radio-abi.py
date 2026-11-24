#!/usr/bin/env python3
"""Cross-compile layout assertions against the exact pinned IDF public headers."""
from pathlib import Path
import os,re,subprocess,tempfile
r=Path(__file__).resolve().parent.parent
b=Path(os.environ.get('ESP32_BUILD_CACHE','/work/refs/esp32-linux-build/build'))
components=b/'esp-hosted/esp_hosted_ng/esp/esp_driver/esp-idf/components'
h=r/'build-output/wifi-smp-audit/current/esp_blob_radio_abi.h'
d=(r/'build-output/wifi-smp-audit/base/idf-esp_wifi_driver.h').read_text()
a=d.rfind('typedef ',0,d.index('} wifi_wpa_ie_t;'));z=d.index('} wifi_wpa_ie_t;')+len('} wifi_wpa_ie_t;')
wp=d[a:z];a=d.index('struct wpa_funcs {');z=d.index('\n};',a)+3;wp+=d[a:z]
code='''#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#define _ESP_WIFI_TYPES_PRIVATE_H
#define ESP_IF_WIFI_STA 0
#define ESP_IF_WIFI_AP 1
#define ESP_IF_WIFI_NAN 2
#define ESP_EVENT_DECLARE_BASE(n) extern const char *n
#define CONFIG_IDF_TARGET_ESP32S3 1
'''
code+=f'#include "{components}/esp_wifi/include/esp_wifi_types.h"\n'+wp+f'\n#include "{h}"\n'
names=['wifi_country_t','wifi_active_scan_time_t','wifi_scan_time_t','wifi_scan_config_t','wifi_he_ap_info_t','wifi_ap_record_t','wifi_scan_threshold_t','wifi_pmf_config_t','wifi_ap_config_t','wifi_sta_config_t','wifi_nan_config_t','wifi_config_t','wifi_wpa_ie_t']
for n in names:code+=f'_Static_assert(sizeof({n})==sizeof(blob_{n}), "{n} size");\n'
for n,fields in {'wifi_scan_config_t':['ssid','bssid','channel','show_hidden','scan_type','scan_time','home_chan_dwell_time'],'wifi_ap_record_t':['bssid','ssid','primary','second','rssi','authmode','pairwise_cipher','group_cipher','ant','country','he_ap'],'wifi_sta_config_t':['ssid','password','scan_method','bssid_set','bssid','channel','listen_interval','sort_method','threshold','pmf_cfg','sae_pwe_h2e','sae_pk_mode','failure_retry_cnt','sae_h2e_identifier']}.items():
 for f in fields:code+=f'_Static_assert(offsetof({n},{f})==offsetof(blob_{n},{f}),"{n}.{f}");\n'
code+='''_Static_assert(sizeof(struct wpa_funcs)==sizeof(struct blob_wpa_funcs),"WPA table size");
_Static_assert(offsetof(struct wpa_funcs,wpa_sta_rx_mgmt)==offsetof(struct blob_wpa_funcs,wpa_sta_rx_mgmt),"WPA RX slot");
_Static_assert(sizeof(void*)==4,"must validate 32-bit Xtensa ABI");
'''
cc=b/'crosstool-NG/builds/xtensa-esp32s3-linux-uclibcfdpic/bin/xtensa-esp32s3-linux-uclibcfdpic-gcc'
env=os.environ.copy();env['XTENSA_GNU_CONFIG']=str(b/'xtensa-dynconfig/esp32s3.so')
with tempfile.TemporaryDirectory() as temp:
 src=Path(temp)/'abi.c';obj=Path(temp)/'abi.o';src.write_text(code)
 subprocess.run([str(cc),'-std=gnu11','-ffreestanding','-I'+str(components/'esp_wifi/include'),'-c',str(src),'-o',str(obj)],check=True,env=env)
print('PASS: pinned IDF scan/config/AP/WPA layouts and offsets match under the actual 32-bit Xtensa compiler')
