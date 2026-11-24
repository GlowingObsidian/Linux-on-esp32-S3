#!/usr/bin/env python3
"""Nano's Arduino qio_opi hardware contract, including the ROM header mode."""
from pathlib import Path
import sys

repo = Path(__file__).resolve().parent.parent
path = Path(sys.argv[1]) if len(sys.argv) > 1 else repo / 'new-files/esp-hosted/network_adapter/sdkconfig.defaults.esp32s3.16m8r'
settings = {}
for line in path.read_text().splitlines():
    if line.startswith('CONFIG_') and '=' in line:
        key, value = line.split('=', 1)
        settings[key] = value
required = {
    'CONFIG_ESPTOOLPY_FLASHMODE_QIO': 'y',
    'CONFIG_ESPTOOLPY_FLASHMODE': '"dio"',
    'CONFIG_ESPTOOLPY_FLASHFREQ_80M': 'y',
    'CONFIG_ESPTOOLPY_FLASHFREQ': '"80m"',
    'CONFIG_ESPTOOLPY_FLASHSIZE_16MB': 'y',
    'CONFIG_SPIRAM_MODE_OCT': 'y',
    'CONFIG_SPIRAM_SPEED_80M': 'y',
    'CONFIG_SPIRAM_SPEED': '80',
    'CONFIG_SPIRAM_TYPE_AUTO': 'y',
    'CONFIG_SPIRAM_CLK_IO': '30',
    'CONFIG_SPIRAM_CS_IO': '26',
    'CONFIG_SPIRAM_BOOT_INIT': 'y',
    'CONFIG_ESP32S3_INSTRUCTION_CACHE_32KB': 'y',
    'CONFIG_ESP32S3_INSTRUCTION_CACHE_SIZE': '0x8000',
}
for key, value in required.items():
    assert settings.get(key) == value, (path, key, settings.get(key), value)
assert settings.get('CONFIG_ESPTOOLPY_FLASHMODE_DIO') != 'y'
assert settings.get('CONFIG_SPIRAM_MODE_QUAD') != 'y'
assert settings.get('CONFIG_ESP32S3_INSTRUCTION_CACHE_16KB') != 'y'
if len(sys.argv) > 2:
    header = Path(sys.argv[2]).read_text().splitlines()
    definitions = {}
    for line in header:
        fields = line.split(maxsplit=2)
        if len(fields) == 3 and fields[0] == '#define':
            definitions[fields[1]] = fields[2]
    for key, value in required.items():
        expected = '1' if value == 'y' else value
        assert definitions.get(key) == expected, ('generated header', key, definitions.get(key), expected)
    assert definitions.get('CONFIG_ESPTOOLPY_FLASHMODE_DIO') not in ('1', 'y')
print('PASS Nano: QIO80 flash, ROM-compatible DIO header, OPI80 PSRAM, 32 KiB instruction cache')
