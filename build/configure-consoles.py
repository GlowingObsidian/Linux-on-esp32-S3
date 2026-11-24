#!/usr/bin/env python3
"""Keep BusyBox init's tty IDs aligned with the consoles getty opens."""
from pathlib import Path
import sys

CONSOLES = {'console', 'ttyS0', 'tty1', '/dev/console', '/dev/ttyS0', '/dev/tty1'}
ENTRIES = ('ttyS0::respawn:/sbin/getty -L ttyS0 115200 vt100',
           'tty1::respawn:/sbin/getty -L tty1 0 linux')


def normalize(text, *, display=True):
    lines = []
    for line in text.splitlines():
        fields = line.split(':', 3)
        if len(fields) == 4 and fields[2] == 'respawn':
            words = fields[3].split()
            if words and Path(words[0]).name == 'getty':
                if fields[0] in CONSOLES or CONSOLES.intersection(words[1:]):
                    continue
        lines.append(line)
    return '\n'.join([*lines, *(ENTRIES if display else ENTRIES[:1])]) + '\n'


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial-only', action='store_true')
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    args.path.write_text(normalize(args.path.read_text(), display=not args.serial_only))
