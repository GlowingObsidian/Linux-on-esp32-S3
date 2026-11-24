#!/usr/bin/env python3
"""Remember radio state and join saved credentials after bounded radio setup."""
from pathlib import Path
import os, subprocess, tempfile, hashlib
r=Path(__file__).resolve().parent.parent
script=r/'new-files/board/espressif/esp32s3/rootfs_overlay/usr/sbin/wifi-boot'
with tempfile.TemporaryDirectory() as temp:
    root=Path(temp)
    for d in ['etc/network','etc/default','proc','sys/class/net','sys/module/esp32_wifi/parameters','bin']:
        (root/d).mkdir(parents=True,exist_ok=True)
    config=root/'etc/network/interfaces';enable=root/'sys/module/esp32_wifi/parameters/enable'
    credentials=root/'etc/wpa_supplicant.conf'
    credentials.write_text('network={\n ssid="retained"\n psk="test-only-secret"\n}\n');credentials.chmod(0o600)
    original=hashlib.sha256(credentials.read_bytes()).hexdigest()
    sleep=root/'bin/sleep';sleep.write_text('#!/bin/sh\n[ "$TEST_READY" != yes ] || mkdir -p "$WIFI_ROOT/sys/class/net/wlan0"\n');sleep.chmod(0o755)
    (root/'proc/cmdline').write_text('console=ttyS0\n')
    env=dict(os.environ,WIFI_ROOT=str(root),PATH=str(root/'bin')+':'+os.environ['PATH'],TEST_READY='yes')
    def run():
        return subprocess.run(['/bin/sh',str(script)],env=env,text=True,capture_output=True,timeout=10)
    config.write_text('auto lo espsta0 eth0\niface espsta0 inet dhcp\n post-up wpa_supplicant -i espsta0 -c /etc/wpa_supplicant.conf -B\n# espsta0_suffix stays\n')
    config.chmod(0o640);enable.write_text('0\n')
    result=run();assert result.returncode==0,result.stderr
    text=config.read_text();assert 'auto lo eth0' in text and 'iface wlan0 inet dhcp' in text and '-i wlan0 ' in text
    assert 'espsta0_suffix' in text and text.count('auto wlan0\n')==1
    assert enable.read_text()=='1\n' and config.stat().st_mode&0o777==0o640
    assert 'iface espsta0' in (root/'etc/network/interfaces.pre-wlan0').read_text()
    before=config.read_bytes();assert run().returncode==0 and config.read_bytes()==before
    assert hashlib.sha256(credentials.read_bytes()).hexdigest()==original and credentials.stat().st_mode&0o777==0o600
    config.write_text('auto wlan0\nallow-hotplug wlan0\niface wlan0 inet dhcp\n')
    assert run().returncode==0 and config.read_text()=='iface wlan0 inet dhcp\nauto wlan0\n'
    (root/'etc/default/wifi').write_text('WIFI_RADIO_ENABLE=0\n');enable.write_text('0\n')
    assert run().returncode==0 and enable.read_text()=='0\n'
    assert 'auto wlan0' not in config.read_text()
    credentials.unlink()
    (root/'etc/default/wifi').write_text('WIFI_RADIO_ENABLE=1\n')
    assert run().returncode==0 and 'auto wlan0' not in config.read_text()
    (root/'etc/default/wifi').unlink();(root/'proc/cmdline').write_text('esp32_wifi.enable=0 console=ttyS0\n')
    assert run().returncode==0 and enable.read_text()=='0\n'
    (root/'proc/cmdline').write_text('console=ttyS0\n');(root/'sys/class/net/wlan0').rmdir();env['TEST_READY']='no'
    result=run();assert result.returncode==1 and 'continuing boot for recovery' in result.stderr
    # The boot helper must not run clients or bring the link up.
    code=script.read_text()
    assert not any(x in code for x in ['ifup ', 'udhcpc ', 'ip link set ', '\nwpa_supplicant '])
print('PASS: interface migration, saved-network autojoin, unsaved picker state, credentials/mode preserved, idempotence, remembered off, delayed readiness, recovery disable and bounded timeout')
