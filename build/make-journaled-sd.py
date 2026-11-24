#!/usr/bin/env python3
"""Build a small-state journaled SD image; never access a physical disk."""
from pathlib import Path
import argparse
import hashlib
import json
import struct
import subprocess
import tempfile

root = Path(__file__).resolve().parent.parent
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
a.output.mkdir(parents=True, exist_ok=True)
scratch = root / 'experiments/mmu-poc/out'
def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)
def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()
image_bytes = 896 * 1024 * 1024
offset = 1024 * 1024
with tempfile.TemporaryDirectory(prefix='journaled-sd-', dir=scratch) as temp:
    w = Path(temp)
    tree = w / 'tree'
    tree.mkdir()
    (tree / '.esp32-sd-profile').write_text('journaled-sd-v1\n')
    filesystem = w / 'data.ext4'
    with filesystem.open('wb') as f:
        f.truncate(image_bytes - offset)
    # Small journal; no 64-bit blocks, encryption, casefold or fast-commit.
    # Checksummed metadata makes damaged SPI/card metadata detectable.
    features = 'none,has_journal,extents,filetype,sparse_super,large_file,extra_isize,metadata_csum'
    run('mke2fs', '-q', '-F', '-t', 'ext4', '-b', '4096', '-I', '256',
        '-m', '0', '-J', 'size=8', '-O', features,
        '-E', 'lazy_itable_init=0,lazy_journal_init=0',
        '-L', 'linux-data', '-d', tree, filesystem)
    run('e2fsck', '-f', '-n', filesystem)
    info = run('dumpe2fs', '-h', filesystem, capture_output=True, text=True).stdout
    assert 'has_journal' in info and 'metadata_csum' in info
    marker = run('debugfs', '-R', 'cat /.esp32-sd-profile', filesystem,
                 capture_output=True, text=True).stdout
    assert marker.strip() == 'journaled-sd-v1'
    with filesystem.open('rb') as f:
        f.seek(1024)
        sb = f.read(1024)
    assert struct.unpack_from('<H', sb, 56)[0] == 0xef53
    assert struct.unpack_from('<H', sb, 58)[0] & 1  # clean initial filesystem
    assert struct.unpack_from('<I', sb, 92)[0] & 4  # has_journal
    assert struct.unpack_from('<I', sb, 224)[0] == 8  # journal inode
    image = a.output / 'sd-data.img'
    with image.open('wb') as f:
        f.truncate(image_bytes)
        mbr = bytearray(512)
        mbr[446:462] = struct.pack('<B3sB3sII', 0, b'\xfe\xff\xff', 0x83,
                                  b'\xfe\xff\xff', offset // 512,
                                  (image_bytes - offset) // 512)
        mbr[510:512] = b'\x55\xaa'
        f.write(mbr)
    run('dd', f'if={filesystem}', f'of={image}', 'bs=1M', 'seek=1',
        'conv=notrunc,sparse', 'status=none')
    # Check the finished image's journal flag and UUID as well as the staging fs.
    with image.open('rb') as f:
        assert f.read(512) == mbr
        f.seek(offset + 1024)
        assert f.read(1024) == sb
    report = dict(profile='journaled-sd-v1', filesystem='ext4',
                  journal_mib=8, metadata_checksums=True,
                  image_bytes=image_bytes, partition_offset=offset,
                  sd_image_sha256=digest(image), filesystem_uuid=sb[104:120].hex(),
                  hardware_verified=False)
    (a.output / 'sd-data.json').write_text(json.dumps(report, indent=2) + '\n')
    (a.output / 'filesystem-check.txt').write_text(info)
    print(json.dumps(report, indent=2))
