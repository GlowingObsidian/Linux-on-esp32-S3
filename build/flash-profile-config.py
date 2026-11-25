#!/usr/bin/env python3
"""Select flash and matching PSRAM clocks for the two 16 MiB factory images."""
import argparse
from pathlib import Path
import re

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('profile', choices=('esp32s3-dio80', 'nano-qio120'))
parser.add_argument('base', type=Path)
parser.add_argument('output', type=Path)
parser.add_argument('--partition-table', type=Path)
args = parser.parse_args()
config = args.base.read_text()
qio = args.profile == 'nano-qio120'
speed = '120' if qio else '80'
settings = {
    'CONFIG_ESPTOOLPY_FLASHMODE_QIO': 'y' if qio else None,
    'CONFIG_ESPTOOLPY_FLASHMODE_DIO': None if qio else 'y',
    'CONFIG_ESPTOOLPY_FLASHMODE': '"dout"' if qio else '"dio"',
    'CONFIG_FLASHMODE_QIO': 'y' if qio else None,
    'CONFIG_FLASHMODE_DIO': None if qio else 'y',
    'CONFIG_ESPTOOLPY_FLASHFREQ_80M': None if qio else 'y',
    'CONFIG_ESPTOOLPY_FLASHFREQ_120M': 'y' if qio else None,
    'CONFIG_ESPTOOLPY_FLASHFREQ': '"80m"',
    'CONFIG_SPIRAM_SPEED_80M': None if qio else 'y',
    'CONFIG_SPIRAM_SPEED_120M': 'y' if qio else None,
    'CONFIG_SPIRAM_SPEED': speed,
    'CONFIG_ESP_WIFI_NVS_ENABLED': None,
    'CONFIG_ESP32_WIFI_NVS_ENABLED': None,
}
if qio:
    settings['CONFIG_IDF_EXPERIMENTAL_FEATURES'] = 'y'
if args.partition_table:
    settings['CONFIG_PARTITION_TABLE_CUSTOM_FILENAME'] = f'"{args.partition_table.resolve()}"'

for key, value in settings.items():
    entry = f'# {key} is not set' if value is None else f'{key}={value}'
    config, count = re.subn(rf'^(?:{key}=.*|# {key} is not set)$',
                            entry, config, flags=re.M)
    if not count:
        config = config.rstrip('\n') + '\n' + entry + '\n'
args.output.write_text(config)
