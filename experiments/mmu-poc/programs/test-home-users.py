#!/usr/bin/env python3
import struct
import sys
from pathlib import Path
import subprocess
import tempfile

here = Path(__file__).resolve().parent
repo = here.parents[2]
host = repo.parent / 'refs/esp32-linux-build/build/build-buildroot-arduino_nano_esp32/host'
image = Path(sys.argv[1]) if len(sys.argv) > 1 else here.parent / 'out/programs/rootfs-home-users-ready.cramfs'
data = image.read_bytes()
assert struct.unpack_from('<I', data)[0] == 0x28CD3D45
inode = struct.unpack_from('<III', data, 64)
for part in ('bin', 'busybox'):
    offset = (inode[2] >> 6) * 4
    end = offset + (inode[1] & 0xFFFFFF)
    while offset < end:
        child = struct.unpack_from('<III', data, offset)
        length = (child[2] & 63) * 4
        name = data[offset+12:offset+12+length].rstrip(b'\0').decode()
        if name == part:
            inode = child
            break
        offset += 12 + length
    else:
        raise AssertionError('Missing packed inode: ' + part)
assert inode[0] >> 16 == 0
assert inode[0] & 0o7777 == 0o4755
with tempfile.TemporaryDirectory(prefix='home-users-check-') as tmp:
    tmp = Path(tmp)
    tree = tmp / 'tree'
    subprocess.run([host / 'bin/cramfsck', '-x', tree, image], check=True)
    assert not (tree / 'www').exists()
    assert (tree / 'bin/sh').readlink() == Path('busybox')
    assert (tree / 'usr/bin/dtach').is_file()
    assert (tree / 'bin/stat').readlink() == Path('busybox')
    assert not (tree / 'usr/bin/sudo').exists()
    assert not (tree / 'usr/bin/doas').exists()
    assert (tree / 'usr/share/esp32-home/README.txt').read_text().splitlines()[0] == 'Hi, Welcome to Linux on esp32-S3 !!!'
    # This image ships no web server: no seed to unpack, no script to enable it,
    # and no www-data for it to have run as. The account is the part that is
    # invisible from a booting board -- nothing logs in as it to be missed.
    assert not (tree / 'usr/share/esp32-home/www.tar.gz').exists()
    assert not (tree / 'usr/sbin/web-server').exists()
    for name in ('etc/passwd', 'etc/group'):
        text = (tree / name).read_text()
        assert 'www-data' not in text, f'www-data still in {name}:\n' + text
    print('PASS: /www removed; no web seed, no web-server, no www-data; welcome README; SUID BusyBox; dtach; /bin/sh unchanged')
