#!/usr/bin/env python3
"""Exercise first-boot seeding, persistence, and missing-card fallback in a fixture."""
from pathlib import Path
import os
import re
import subprocess
import tempfile

script = Path(__file__).with_name('hybrid-init.sh').read_text()
with tempfile.TemporaryDirectory(prefix='hybrid-init-test-') as tmp:
    root = Path(tmp) / 'root'
    fake = Path(tmp) / 'commands'
    fake.mkdir()
    for name in ('proc','sys','dev','sbin','data','etc','home','root','var','tmp','opt','usr/local'):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / 'proc/mounts').write_text('fixture\n')
    (root / 'dev/console').touch()
    (root / 'dev/mmcblk0p1').touch()
    (root / 'data/.esp32-sd-profile').write_text('journaled-sd-v1\n')
    # A reset during the old first-boot copy must not block a new attempt.
    (root / 'data/.linux-state.new.1').mkdir()
    (root / 'etc/hostname').write_text('flash-default\n')
    (root / 'home/user-file').write_text('seed\n')
    (root / 'sbin/init').write_text('#!/bin/sh\necho INIT_REACHED\n')
    (root / 'sbin/init').chmod(0o755)
    for name in ('mount', 'umount', 'sleep', 'sync'):
        path = fake / name
        path.write_text('#!/bin/sh\nexit 0\n')
        path.chmod(0o755)
    transformed = script.replace('PATH=/sbin:/bin:/usr/sbin:/usr/bin', '__TEST_PATH__')
    # Redirect literal absolute paths and the loop's dynamic /$dir to fixture.
    transformed = re.sub(r'(?<![\w/])/(proc|sys|dev|sbin|data|etc|home|root|var|tmp|opt|usr)(?=[/\s";]|$)',
                         lambda m: str(root) + '/' + m[1], transformed)
    transformed = transformed.replace('"/$dir', f'"{root}/$dir')
    transformed = transformed.replace('[ -b ', '[ -f ').replace('[ ! -b ', '[ ! -f ')
    transformed = transformed.replace('[ -c ', '[ -f ')
    transformed = transformed.replace('__TEST_PATH__', f'PATH={fake}:/usr/bin:/bin')
    runner = Path(tmp) / 'init.sh'
    runner.write_text(transformed)
    def run():
        return subprocess.run(['/bin/sh', str(runner)], check=True,
                              capture_output=True, text=True).stdout
    first = run()
    state = root / 'data/.linux-state'
    assert 'INIT_REACHED' in first and 'flash XIP system' in first
    assert (state / 'etc/hostname').read_text() == 'flash-default\n'
    assert (state / 'home/user-file').read_text() == 'seed\n'
    assert (state / 'tmp').stat().st_mode & 0o7777 == 0o1777
    (state / 'etc/hostname').write_text('persistent-change\n')
    (root / 'etc/hostname').write_text('updated-flash-default\n')
    assert 'INIT_REACHED' in run()
    assert (state / 'etc/hostname').read_text() == 'persistent-change\n'
    (root / 'data/.esp32-sd-profile').unlink()
    incompatible = run()
    assert 'journaled SD data image required' in incompatible and 'INIT_REACHED' in incompatible
    (root / 'dev/mmcblk0p1').unlink()
    missing = run()
    assert 'SD unavailable' in missing and 'INIT_REACHED' in missing
print('PASS: state seeded once, SD changes preserved, tmp mode, bind path and flash fallback')
