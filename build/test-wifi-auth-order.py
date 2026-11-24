#!/usr/bin/env python3
"""Verify actual readiness hook gates DHCP on authorization, failure and timeout."""
from pathlib import Path
import os,subprocess,tempfile
repo=Path(__file__).resolve().parents[1]
source=(repo/'new-files/board/espressif/esp32s3/rootfs_overlay/etc/wifi-wait-auth').read_text()
interfaces=(repo/'new-files/board/espressif/esp32s3/rootfs_overlay/etc/network/interfaces').read_text()
assert 'pre-up /etc/wifi-wait-auth wlan0' in interfaces
assert 'post-up wpa_supplicant' not in interfaces
assert 'post-down killall -q wpa_supplicant' in interfaces
assert 'pre-down killall -q wpa_supplicant' not in interfaces
for mode in ['connected','wrong-key','timeout','disconnect']:
 with tempfile.TemporaryDirectory() as temp:
  p=Path(temp);log=p/'wpa.log';trace=p/'trace';bin=p/'bin';bin.mkdir()
  script=source.replace('CONF=/etc/wpa_supplicant.conf','CONF='+str(p/'conf')).replace('LOG=/run/wpa_supplicant.log','LOG='+str(log)).replace('WAIT=45','WAIT=3')
  hook=p/'hook';hook.write_text(script)
  def command(name,body):
   f=bin/name;f.write_text('#!/bin/sh\n'+body+'\n');f.chmod(0o755)
  command('ip',f'echo ip >>"{trace}"')
  event={'connected':'wlan0: CTRL-EVENT-CONNECTED - Connection to aa:bb:cc:dd:ee:ff completed','wrong-key':'wlan0: CTRL-EVENT-SSID-TEMP-DISABLED reason=WRONG_KEY','timeout':'','disconnect':'wlan0: CTRL-EVENT-CONNECTED - Connection to aa:bb:cc:dd:ee:ff completed\nwlan0: CTRL-EVENT-DISCONNECTED bssid=aa:bb:cc:dd:ee:ff'}[mode]
  command('wpa_supplicant',f'echo wpa >>"{trace}"\ncat >"{log}" <<\'EVENT\'\n{event}\nEVENT')
  command('sleep',f'echo wait >>"{trace}"')
  env=dict(os.environ,PATH=str(bin)+':'+os.environ['PATH'])
  r=subprocess.run(['/bin/sh','-c','/bin/sh "$1" wlan0 && echo dhcp >>"$2"','sh',str(hook),str(trace)],env=env,capture_output=True,text=True,timeout=5)
  lines=trace.read_text().splitlines()
  assert lines[:2]==['ip','wpa'],lines
  assert ('dhcp' in lines)==(mode=='connected'),(mode,lines,r.stderr)
  if mode!='connected':assert r.returncode!=0,(mode,r.returncode)
  print('PASS',mode,': DHCP gated by supplicant authorization')
