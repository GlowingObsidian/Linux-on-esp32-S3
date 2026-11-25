#!/usr/bin/env python3
"""Verify generated firmware profiles and merge their full factory images."""
import argparse
import hashlib
from pathlib import Path
import re
import struct
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('artifacts', type=Path)
parser.add_argument('--esptool', default='esptool')
args = parser.parse_args()
directories = args.artifacts
release = directories / 'release'
release.mkdir(exist_ok=True)

profiles = (
    ('esp32s3-dio80', 'DIO', 80, 'esp32s3-n16r8-dio-80mhz-factory.bin'),
    ('nano-qio120', 'QIO', 120, 'arduino-nano-esp32-qio-120mhz-factory.bin'),
)
limits = {
    0: ('bootloader.bin', 0x8000),
    0x8000: ('partition-table.bin', 0x10000),
    0x10000: ('firmware.bin', 0xd0000),
    0xd0000: ('etc.jffs2', 0x140000),
    0x140000: ('xipImage', 0x510000),
    0x510000: ('rootfs.cramfs', 0xc00000),
    0xc00000: ('home.jffs2', 0x1000000),
}
checksums = []
for profile, mode, speed, name in profiles:
    folder = directories / profile
    cfg = (folder / 'sdkconfig.h').read_text()
    for key in (f'ESPTOOLPY_FLASHMODE_{mode}', f'ESPTOOLPY_FLASHFREQ_{speed}M',
                f'SPIRAM_SPEED_{speed}M', 'SPIRAM_MODE_OCT', 'ESPTOOLPY_FLASHSIZE_16MB'):
        assert re.search(rf'^#define CONFIG_{key} 1$', cfg, re.M), (profile, key)
    if speed == 120:
        assert re.search(r'^#define CONFIG_SPI_FLASH_HPM_ENABLE 1$', cfg, re.M), profile
    header_mode = 'DOUT' if speed == 120 else 'DIO'
    for image in ('bootloader.bin', 'firmware.bin'):
        info = subprocess.check_output([args.esptool, 'image-info', str(folder / image)], text=True)
        assert 'Flash freq: 80m' in info and 'Flash size: 16MB' in info, (profile, image)
        assert f'Flash mode: {header_mode}' in info, (profile, image, 'expected the ROM-compatible header')
    subprocess.run([sys.executable, str(Path(__file__).with_name('verify-firmware-linux-ownership.py')),
                    str(folder / 'vmlinux'), str(folder / 'firmware.elf')], check=True,
                    stdout=subprocess.DEVNULL)
    assert (folder / 'board.dtb').read_bytes() in (folder / 'xipImage').read_bytes(), profile
    table = (folder / 'partition-table.bin').read_bytes()
    partitions = [struct.unpack_from('<HBBII16sI', table, off) for off in (0, 32, 64)]
    assert [(entry[0], entry[3], entry[4]) for entry in partitions] == [
        (0x50aa, 0x10000, 0xc0000), (0x50aa, 0x140000, 0x3d0000), (0x50aa, 0x510000, 0x6f0000)]
    full = bytearray(b'\xff' * 0x1000000)
    for start, (file, end) in limits.items():
        data = (folder / file).read_bytes()
        assert 0 < len(data) <= end - start, (profile, file)
        full[start:start + len(data)] = data
    (release / name).write_bytes(full)
    checksums.append(f'{hashlib.sha256(full).hexdigest()}  {name}')
    print(f'PASS {profile}, generated timing, flash geometry, embedded DTB and SRAM ownership')
(release / 'SHA256SUMS').write_text('\n'.join(checksums) + '\n')
