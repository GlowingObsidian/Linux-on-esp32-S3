#!/usr/bin/env python3
"""Exercise the actual flash init scripts with temporary files and mount stubs."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import tempfile

r=Path(__file__).resolve().parent.parent
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--output',type=Path,default=r/'build-output/flash-rw-research/nor-candidate/flash-init-tests.json')
p.add_argument('--shell',default='sh',help='Shell executable, including target-configured host BusyBox hush')
a=p.parse_args()
out=a.output
cases=[]
def run(script,name,marker=False,driver=True,fail='',rw='0',args=(), led=''):
    with tempfile.TemporaryDirectory(prefix='flash-init-test-') as tmp:
        root=Path(tmp)
        for directory in ('etc','home','sys/guard','dev','proc','sbin','usr/sbin','var/log','tmp'):
            (root/directory).mkdir(parents=True,exist_ok=True)
        (root/'proc/mounts').write_text('root / cramfs ro 0 0\n')
        if led:
            (root/'sys/leds/nano:red').mkdir(parents=True)
            (root/'etc/init.d').mkdir(parents=True)
            legacy=(r/'new-files/board/espressif/esp32s3/rootfs_overlay/etc/init.d/S04led').read_bytes()
            (root/'etc/init.d/S04led').write_bytes(legacy if led=='original' else b'# user custom LED script\n')
        if driver:(root/'sys/guard/rw_enable').write_text(rw+'\n')
        if marker:(root/'etc/.flash-rw-verified').write_text('flash-nor-v1\n')
        if fail=='guard':(root/'sys/guard/test').mkdir()
        if fail=='marker-write':(root/'etc/.flash-rw-verified.new').mkdir()
        for path,label in (('sbin/init','INIT'),('usr/sbin/wifi-boot','WIFI')):
            f=root/path;f.write_text('#!/bin/sh\necho '+label+' >> "'+tmp+'/trace"\n');f.chmod(0o755)
        source=(r/'build'/script).read_text()
        source=source.replace('PATH=/sbin:/bin:/usr/sbin:/usr/bin','PATH=/usr/bin:/bin')
        # Rewrite only absolute filesystem tokens; all command logic is retained.
        import re
        source=re.sub(r'(?<![A-Za-z0-9_])/(etc|home|proc|dev|sbin|usr|var|tmp|data)(?=[/\s";]|$)',lambda m:tmp+'/'+m[1],source)
        source=source.replace('/sys/bus/platform/devices/3fcc8000.flash-guard',tmp+'/sys/guard')
        source=source.replace('/sys/class/leds',str(root/'sys/leds'))
        source=re.sub(r'^PATH=.*$', 'PATH=/usr/bin:/bin', source, flags=re.M)
        stubs='''
sha256sum() {
    if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$@";
    else command sha256sum "$@"; fi
}
mount() {
    printf 'mount %s\n' "$*" >> "$TRACE"
    case "$*" in
        *remount,rw*) [ "$FAIL" != remount ] || return 1 ;;
        *mtd:home*) [ "$FAIL" != home ] || return 1 ;;
        *mtd:etc*) [ "$FAIL" != etc ] || return 1 ;;
    esac
}
umount() { printf 'umount %s\n' "$*" >> "$TRACE"; }
sync() { echo sync >> "$TRACE"; [ "$FAIL" != sync ]; }
'''
        f=root/'test.sh';f.write_text(stubs+source)
        env=dict(os.environ,TRACE=tmp+'/trace',FAIL=fail)
        result=subprocess.run([a.shell,str(f),*args],env=env,capture_output=True,text=True)
        trace=(root/'trace').read_text() if (root/'trace').exists() else ''
        state=(root/'sys/guard/rw_enable').read_text().strip() if driver else ''
        if script=='flash-init.sh':
            assert result.returncode==0 and 'WIFI' in trace and 'INIT' not in trace,(name,result.stderr)
            assert 'mmcblk' not in trace and 'ext4' not in trace
            if marker and driver and not fail:
                assert 'mtd:home' in trace and 'remount,rw' in trace and state=='resume'
                assert (root/'sys/guard/test').read_text().strip()=='4'
                assert 'persistent writable etc/home' not in result.stdout
                if led=='original':
                    assert not (root/'etc/init.d/S04led').exists()
                    assert (root/'etc/.S04led.pre-gpio-leds').read_bytes()==legacy
                if led=='custom':
                    assert (root/'etc/init.d/S04led').exists()
                    assert not (root/'etc/.S04led.pre-gpio-leds').exists()
                    assert 'modified legacy' in result.stderr
            else:
                assert state!='resume' and 'persistent writable etc/home' not in result.stdout
                if marker and fail in ('home','remount','guard'):assert state=='0'
                if not marker:assert not (root/'sys/guard/test').exists()
        elif rw!='1':
            assert result.returncode!=0 and 'mount ' not in trace
        elif args:
            assert result.returncode!=0 and 'mount ' not in trace
        elif fail=='home':
            assert result.returncode!=0 and 'umount ' in trace
            assert not (root/'etc/.flash-rw-verified').exists()
        elif fail in ('etc','marker-write','sync'):
            assert result.returncode!=0 and not (root/'etc/.flash-rw-verified').exists()
            if fail=='etc':assert 'mtd:home' not in trace
        else:
            assert result.returncode==0 and 'mtd:etc' in trace and 'mtd:home' in trace
            assert (root/'etc/.flash-rw-verified').read_text()=='flash-nor-v1\n'
        cases.append(dict(case=name,exit=result.returncode))

run('flash-init.sh','first-boot-locked')
run('flash-init.sh','marker-resumes',marker=True)
run('flash-init.sh','retire-legacy-led',marker=True,led='original')
run('flash-init.sh','preserve-custom-led',marker=True,led='custom')
run('flash-init.sh','missing-driver',driver=False)
for fault in ('etc','home','remount','guard'):
    run('flash-init.sh','boot-failure-'+fault,marker=True,fail=fault)
run('flash-state-enable.sh','enable-refuses-locked')
run('flash-state-enable.sh','enable-after-scratch',rw='1')
run('flash-state-enable.sh','enable-home-failure',rw='1',fail='home')
for fault in ('etc','marker-write','sync'):
    run('flash-state-enable.sh','enable-failure-'+fault,rw='1',fail=fault)
run('flash-state-enable.sh','enable-rejects-sd-import',rw='1',args=('--import-sd',))
out.parent.mkdir(parents=True,exist_ok=True)
out.write_text(json.dumps(dict(passed=len(cases),cases=cases),indent=2)+'\n')
print(f'PASS: {len(cases)} actual-script scenarios: locked first boot, marker resume, no SD mount, failed guard/mount recovery, and explicit state activation')
