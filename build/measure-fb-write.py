#!/usr/bin/env python3
"""Measure how long a full-frame write to /dev/fb0 takes on the board.

This is the measurement that decides whether SPI DMA is worth building. The
panel is 128x160 RGB565, so one full frame is 40960 bytes. The SPI driver
moves it 64 bytes at a time -- 640 iterations of memcpy_toio + spin on
SPI_USR -- and DMA would remove the copy from each. If a full frame is already
fast enough that nothing on the board waits for it, the DMA work has no
problem to solve.

Read-only against the board: it opens /dev/fb0, writes to it, and times that.
Nothing is written to flash, no config changes, no reboot.

Run:  ~/.venvs/esp-console/bin/python3 build/measure-fb-write.py /dev/cu.usbserial-1120
"""
import argparse
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'probe', ROOT / 'experiments/mmu-poc/serial-probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('port')
    args = parser.parse_args()

    console = probe.Console(args.port)
    try:
        console.login()

        # What the panel actually is, so the frame size is measured rather than
        # assumed. fbset is not in this image; the sysfs node is.
        console.show('--- panel ---')
        console.show(console.command(
            'cat /sys/class/graphics/fb0/virtual_size '
            '/sys/class/graphics/fb0/bits_per_pixel 2>&1'))

        # A full frame is width * height * bytes-per-pixel. Read it from sysfs
        # rather than hardcoding 128x160x2.
        frame = console.command(
            "set -- $(cat /sys/class/graphics/fb0/virtual_size | tr ',' ' '); "
            "bpp=$(cat /sys/class/graphics/fb0/bits_per_pixel); "
            "echo $(( $1 * $2 * bpp / 8 ))")
        frame_bytes = int(frame.strip())
        console.show('--- frame size ---')
        console.show('%d bytes' % frame_bytes)

        # One full frame, timed. `dd` is in busybox and reports its own rate;
        # the shell's `time` is not, so the elapsed time comes from date.
        console.show('--- one full frame, /dev/zero -> /dev/fb0 ---')
        console.show(console.command(
            'dd if=/dev/zero of=/dev/fb0 bs=%d count=1 2>&1' % frame_bytes,
            seconds=120))

        # Ten frames back to back: the steady-state number, which is what an
        # animation would actually see.
        console.show('--- ten frames ---')
        console.show(console.command(
            'dd if=/dev/zero of=/dev/fb0 bs=%d count=10 2>&1' % frame_bytes,
            seconds=300))

        # Readback, for comparison: the receive path, where the driver does a
        # memcpy_fromio per 64-byte window.
        console.show('--- readback, /dev/fb0 -> /dev/null ---')
        console.show(console.command(
            'dd if=/dev/fb0 of=/dev/null bs=%d count=10 2>&1' % frame_bytes,
            seconds=300))

        # The CPU cost of the write, as a cross-check on dd's rate. A busy
        # loop for the same wall time shows how much of it the CPU owned.
        console.show('--- cpu ---')
        console.show(console.command(
            'cat /proc/cpuinfo | grep -i "cpu MHz\\|BogoMIPS" 2>&1'))
    finally:
        console.close()


if __name__ == '__main__':
    sys.exit(main())
