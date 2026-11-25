#!/usr/bin/env python3
"""Build SD-independent XIP root and uncompressed etc/home JFFS2 seeds."""
from pathlib import Path
import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import tempfile

r = Path(__file__).resolve().parent.parent
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--source', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--root-only', action='store_true', help='Repack rootfs without creating persistent partition seeds')
p.add_argument('--profile', choices=('nano', 'generic'), default='nano')
p.add_argument('--tools', type=Path, default=r / 'experiments/mmu-poc/out/programs/default-tools',
               help='Directory populated by build-default-tools.sh')
a = p.parse_args()
host = Path('/work/refs/esp32-linux-build/build/build-buildroot-arduino_nano_esp32/host')
table = r / 'new-files/esp-hosted/network_adapter/partition_table.esp32s3.16m8r'
with table.open() as rows:
    rootfs_offset, rootfs_size = next(
        tuple(int(value, 0) for value in row[3:5])
        for row in csv.reader(rows, skipinitialspace=True) if row and row[0] == 'rootfs')
home_offset = rootfs_offset + rootfs_size
a.output.mkdir(parents=True, exist_ok=True)

def run(*args):
    subprocess.run(list(map(str, args)), check=True)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

with tempfile.TemporaryDirectory(prefix='flash-root-', dir=r / 'experiments/mmu-poc/out') as tmp:
    tmp = Path(tmp)
    tree = tmp / 'root'
    run(host / 'bin/cramfsck', '-x', tree, a.source)
    bash_sha, busybox_sha = sha(tree / 'bin/bash'), sha(tree / 'bin/busybox')
    run('python3', r / 'build/install-default-tools.py', tree, a.tools,
        r / 'new-files/board/espressif/esp32s3/rootfs_overlay')
    # Retire wrappers inherited from the SD image before the final repack.
    for name in ('sbin/hybrid-init', 'sbin/flash-init', 'usr/sbin/flash-state-enable'):
        (tree / name).unlink(missing_ok=True)
    init_hook = tree / 'sbin/flash-state-init'
    shutil.copyfile(r / 'build/flash-init.sh', init_hook)
    init_hook.chmod(0o755)
    if a.profile == 'generic':
        (tree / 'etc/hostname').write_text('esp32s3\n')
        (tree / 'etc/issue').write_text('Welcome to ESP32-S3 Linux\n')
        (tree / 'usr/bin/espctl').unlink(missing_ok=True)
        version = tree / 'etc/esp-image-version'
        if version.exists():
            lines = version.read_text().splitlines()
            version.write_text('\n'.join('BOARD=Espressif ESP32-S3 N16R8' if line.startswith('BOARD=') else line
                                          for line in lines) + '\n')
    # gpio-leds owns the Nano RGB pins; the boot hook retires persisted copies.
    (tree / 'etc/init.d/S04led').unlink(missing_ok=True)
    inittab = tree / 'etc/inittab'
    init_lines = inittab.read_text().splitlines(keepends=True)
    init_lines = [line for line in init_lines if '/sbin/flash-state-init' not in line]
    inittab.write_text('::sysinit:/sbin/flash-state-init\n' + ''.join(init_lines))
    (tree / 'etc/fstab').write_text('''/dev/root / cramfs ro,noauto 0 0
proc /proc proc defaults 0 0
devpts /dev/pts devpts defaults,gid=5,mode=620,ptmxmode=0666 0 0
sysfs /sys sysfs defaults 0 0
tmpfs /run tmpfs mode=0755,nosuid,nodev 0 0
tmpfs /dev/shm tmpfs mode=1777,nosuid,nodev 0 0
mtd:etc /etc jffs2 noauto,noatime 0 0
mtd:home /home jffs2 noauto,noatime 0 0
''')
    assert not (tree / 'etc/.flash-rw-verified').exists()
    image = a.output / 'rootfs.cramfs'
    run(host / 'bin/mkcramfs', '-X', '-q', tree, image)
    assert image.stat().st_size <= rootfs_size, "rootfs exceeds its flash partition"
    run(host / 'bin/cramfsck', image)
    check = tmp / 'check'
    run(host / 'bin/cramfsck', '-x', check, image)
    assert sha(check / 'bin/bash') == bash_sha
    assert sha(check / 'bin/busybox') == busybox_sha
    assert (check / 'sbin/init').is_symlink()
    assert not any((check / 'home/root').iterdir())
    for name in ('coremark-single', 'coremark-smp', 'iperf3', 'fastfetch'):
        assert sha(check / 'usr/bin' / name) == sha(a.tools / name)
    assert (check / 'usr/bin/coremark').readlink() == Path('coremark-smp')
    assert (check / 'sbin/flash-state-init').read_bytes() == (r / 'build/flash-init.sh').read_bytes()
    assert (check / 'etc/inittab').read_text().count('::sysinit:/sbin/flash-state-init\n') == 1
    assert not (check / 'sbin/hybrid-init').exists()
    assert not (check / 'sbin/flash-init').exists()
    assert not (check / 'usr/sbin/flash-state-enable').exists()

    partitions = []
    seed_layout = [] if a.root_only else [('etc', 0xd0000, 0x70000), ('home', home_offset, 0x1000000 - home_offset)]
    for name, offset, size in seed_layout:
        source = check / name
        source.mkdir(exist_ok=True)
        target = a.output / (name + '.jffs2')
        run(host / 'sbin/mkfs.jffs2', '--little-endian', '--eraseblock=65536',
            '--compression-mode=none', '--root=' + str(source),
            '--pad=' + str(size), '--output=' + str(target))
        assert target.stat().st_size == size
        partitions.append(dict(name=name, offset=hex(offset), size=size,
                               erase_size=65536, sha256=sha(target)))
    manifest = dict(profile='flash-xip-etc-home-native-nor', board_profile=a.profile,
                    rootfs_sha256=sha(image), rootfs_bytes=image.stat().st_size,
                    source_rootfs_sha256=sha(a.source), bash_sha256=bash_sha,
                    busybox_sha256=busybox_sha, sd_required=False,
                    init='/sbin/init', state_mount_hook='/sbin/flash-state-init',
                    writable_dirs=['/etc', '/home'], automatic_flash_format=False,
                    automatic_writable_state=True, partitions=partitions,
                    hardware_verified=False)
    (a.output / 'flash-rootfs.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))
