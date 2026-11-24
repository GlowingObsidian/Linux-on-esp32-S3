#!/usr/bin/env python3
"""Build flash XIP root with an early writable-SD state mount; never flash."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile

r = Path(__file__).resolve().parent.parent
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--source', type=Path, required=True)
a = p.parse_args()
host = Path('/work/refs/esp32-linux-build/build/build-buildroot-arduino_nano_esp32/host/bin')
scratch = r / 'experiments/mmu-poc/out'
a.output.mkdir(parents=True, exist_ok=True)
def run(*args):
    subprocess.run(list(map(str, args)), check=True)
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
with tempfile.TemporaryDirectory(prefix='hybrid-root-', dir=scratch) as t:
    tree = Path(t) / 'root'
    run(host / 'cramfsck', '-x', tree, a.source)
    run('python3', r / 'build/configure-consoles.py', '--serial-only', tree / 'etc/inittab')
    for name in ('data', 'etc', 'home', 'root', 'var', 'tmp', 'opt', 'usr/local'):
        path = tree / name
        # Keep symlink aliases in the original filesystem unchanged.
        if not path.is_symlink():
            path.mkdir(parents=True, exist_ok=True)
    bash_sha = sha(tree / 'bin/bash')
    busybox_sha = sha(tree / 'bin/busybox')
    logdir = tree / 'var/log'
    if logdir.is_symlink():
        logdir.unlink()
    logdir.mkdir(exist_ok=True)
    shutil.copyfile(r / 'build/hybrid-init.sh', tree / 'sbin/hybrid-init')
    (tree / 'sbin/hybrid-init').chmod(0o755)
    overlay = r / 'new-files/board/espressif/esp32s3/rootfs_overlay'
    for name in ('usr/bin/wifi', 'usr/sbin/wifi-boot',
                 'etc/network/interfaces', 'etc/default/wifi'):
        dest = tree / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(overlay / name, dest)
        dest.chmod(0o755 if name.startswith('usr/') else 0o644)
    inittab = tree / 'etc/inittab'
    inittab.write_text(''.join(line for line in inittab.read_text().splitlines(True)
                               if 'mount -o remount,rw /' not in line))
    (tree / 'etc/fstab').write_text('''/dev/root / cramfs ro,noauto 0 0
proc /proc proc defaults 0 0
devpts /dev/pts devpts defaults,gid=5,mode=620,ptmxmode=0666 0 0
sysfs /sys sysfs defaults 0 0
tmpfs /run tmpfs mode=0755,nosuid,nodev 0 0
tmpfs /dev/shm tmpfs mode=1777,nosuid,nodev 0 0
''')
    # Runtime rcK remains the known working shutdown sequence, including sync
    # and umount -a. No flash MTD write/erase path is introduced.
    assert sha(tree / 'bin/bash') == bash_sha
    assert sha(tree / 'bin/busybox') == busybox_sha
    image = a.output / 'rootfs.cramfs'
    run(host / 'mkcramfs', '-X', '-q', tree, image)
    assert image.stat().st_size <= 0x6f0000
    run(host / 'cramfsck', image)
    verify = Path(t) / 'verify'
    run(host / 'cramfsck', '-x', verify, image)
    assert sha(verify / 'bin/bash') == bash_sha
    assert sha(verify / 'bin/busybox') == busybox_sha
    assert (verify / 'sbin/hybrid-init').read_bytes() == (r / 'build/hybrid-init.sh').read_bytes()
    for name in ('usr/bin/wifi', 'usr/sbin/wifi-boot',
                 'etc/network/interfaces', 'etc/default/wifi'):
        assert (verify / name).read_bytes() == (overlay / name).read_bytes()
    assert (verify / 'sbin/init').is_symlink()
    assert 'tmpfs /tmp' not in (verify / 'etc/fstab').read_text()
    manifest = dict(profile='flash-xip-with-writable-sd-directories',
                    rootfs_sha256=sha(image), rootfs_bytes=image.stat().st_size,
                    source_rootfs_sha256=sha(a.source), bash_sha256=bash_sha,
                    busybox_sha256=busybox_sha, flash_writes_enabled=False,
                    transfer_mode='pio', hardware_verified=False,
                    sd_filesystem='ext4', journal_required=True, logs_in_ram=True,
                    writable_dirs=['/etc','/home','/root','/var','/tmp','/opt','/usr/local'])
    (a.output / 'hybrid-rootfs.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))
