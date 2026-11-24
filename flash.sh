#!/bin/sh
# Copyright (c) 2026 Paulneja. GPLv3, see LICENSE. https://github.com/paulneja/Linux-on-esp32-S3
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
IMG="$DIR/images"
PORT=""
ERASE=0
PARTS=0
ONLY_PART=""
BACKUP=""
BEFORE="default_reset"

# A single-partition write names its target by partition, not by file, because
# the file that belongs in a partition is a property of the layout and not of
# this script. `factory` is the ESP-IDF firmware and `linux` is the kernel; a
# change to either is a few seconds to flash against the full image's minute,
# and neither touches the rootfs.
part_image() {
	case "$1" in
	factory) echo network_adapter.bin ;;
	linux)   echo xipImage ;;
	rootfs)  echo rootfs.cramfs ;;
	*)       echo "" ;;
	esac
}

usage() {
	cat <<'EOF'
Usage: ./flash.sh [-p PORT] [--images DIR] [--parts] [--erase]
       ./flash.sh --part NAME [-p PORT] [--images DIR]
       ./flash.sh --backup DIR [-p PORT]

Requires Python 3 and esptool. Images come from images/ beside this script
unless --images points somewhere else, such as a build-output artifacts
directory.
  -p, --port PORT  Serial/COM adapter (otherwise autodetected)
  --images DIR     Read the images from DIR instead of images/
  --parts          Write separate images instead of the merged one
  --part NAME      Write one partition and nothing else: factory, linux or
                   rootfs, which hold network_adapter.bin, xipImage and
                   rootfs.cramfs respectively
  --erase          Erase the entire chip before writing; destroys all board data
  --before MODE    esptool reset mode before contact (default: default_reset)
  --backup DIR     Kept for compatibility; /etc and /home are no longer
                   separate partitions, so there is nothing to read
  -h, --help       Show this help

The default full-image write overwrites the whole chip, /etc and /home included.
All inputs are checked before device access. Partial updates also verify
the installed partition table before writing.

--part writes one partition and leaves every other byte of the board alone, so
/etc, /home and the rest are exactly as they were. Use it when one piece
changed: `--part factory` for the ESP-IDF firmware after a change to core 0,
`--part linux` for the kernel after a change to the device tree, a driver or a
patch under patches/linux, `--part rootfs` for userspace. Each is a few seconds
against the full image's minute. It cannot be combined with --erase or --parts,
and it refuses to run without a hand-entered download mode, because it is meant
for a board that is already booted and holding data worth keeping.

--before no_reset is for boards that cannot be reset into the bootloader from
their own USB port, such as the Arduino Nano ESP32: put the board in download
mode by hand (hold BOOT, tap RST, release BOOT), then flash with
--before no_reset, which stops esptool resetting it back out of that mode.
Leaving it at the default resets the board first, so the hand-entered download
mode is lost.

EOF
	exit "${1:-0}"
}

while [ $# -gt 0 ]; do
	case "$1" in
	-p|--port) PORT="${2:?-p needs a port}"; shift 2 ;;
	--images)  IMG="${2:?--images needs a directory}"; shift 2 ;;
	--erase)   ERASE=1; shift ;;
	--parts)   PARTS=1; shift ;;
	--part)    ONLY_PART="${2:?--part needs a partition name}"; shift 2 ;;
	--before)  BEFORE="${2:?--before needs a mode}"; shift 2 ;;
	--backup)  BACKUP="${2:?--backup needs a directory}"; shift 2 ;;
	-h|--help) usage 0 ;;
	*) echo "unknown option: $1" >&2; usage 1 >&2 ;;
	esac
done

# --part is the narrow, safe write: one partition, nothing else. The two flags
# it conflicts with are the two that destroy board data, so the combination is
# refused rather than resolved by precedence.
if [ -n "$ONLY_PART" ]; then
	[ "$ERASE" = 1 ] && { echo "error: --part and --erase cannot be combined" >&2; exit 1; }
	[ "$PARTS" = 1 ] && { echo "error: --part and --parts cannot be combined" >&2; exit 1; }
	[ -n "$BACKUP" ] && { echo "error: --part and --backup cannot be combined" >&2; exit 1; }
	# The name is checked here rather than at the write, so a typo is refused
	# before the board is touched at all.
	[ -n "$(part_image "$ONLY_PART")" ] || {
		echo "error: --part takes factory, linux or rootfs, not '$ONLY_PART'" >&2
		exit 1
	}
	# The whole point is to leave /etc and /home alone. esptool's default
	# reset would drop the hand-entered download mode before anything is
	# written, and the board cannot re-enter it from its own USB port.
	[ "$BEFORE" = "default_reset" ] && {
		echo "error: --part needs --before no_reset" >&2
		echo "       put the board in download mode by hand first: hold BOOT, tap RST, release BOOT" >&2
		exit 1
	}
fi

command -v python3 >/dev/null 2>&1 || { echo "error: Python 3 is required" >&2; exit 1; }
if command -v esptool.py >/dev/null 2>&1; then
	ESPTOOL="esptool.py"
elif command -v esptool >/dev/null 2>&1; then
	ESPTOOL="esptool"
elif python3 -c 'import esptool' >/dev/null 2>&1; then
	ESPTOOL="python3 -m esptool"
else
	echo "error: esptool not found. Install it with:" >&2
	echo "    pip install esptool" >&2
	echo "or activate your ESP-IDF environment (. \$IDF_PATH/export.sh)." >&2
	exit 1
fi

if [ -z "$PORT" ]; then
	for p in /dev/ttyACM0 /dev/ttyACM1 /dev/ttyUSB0 /dev/ttyUSB1; do
		[ -e "$p" ] && { PORT="$p"; break; }
	done
	[ -n "$PORT" ] || { echo "error: no board found; pass -p /dev/ttyXXX" >&2; exit 1; }
	echo "Using port $PORT (override with -p)"
fi


# Reading the two writable partitions off the board. No image is involved, so
# this runs before the preflight and leaves without touching anything else.
if [ -n "$BACKUP" ]; then
	mkdir -p "$BACKUP"
	# /etc and /home are no longer separate partitions -- both live in the
	# read-only cramfs rootfs -- so there is nothing here to read off the board.
	# The option is kept so the usage text and any script calling it still
	# work; it now reports that there is nothing to back up.
	echo "==> --backup: nothing to do"
	echo "    /etc and /home are part of the read-only rootfs now, not partitions"
	echo "    of their own. Reflash the rootfs, or copy files off the running board."
	exit 0
fi

CSV="$DIR/new-files/esp-hosted/network_adapter/partition_table.esp32s3.16m8r"
OFFSETS=$(python3 - "$IMG" "$CSV" "$PARTS" "$ERASE" "$ONLY_PART" <<'PY'
import csv
from pathlib import Path
import struct
import sys

image_dir, table = map(Path, sys.argv[1:3])
parts_mode, erase, only_part = sys.argv[3:6]
flash_size = 16 * 1024 * 1024

# The file that belongs in each partition. Kept in step with part_image() in
# the shell above by hand: this half cannot call a shell function, and the two
# disagreeing would mean validating one file and writing another.
PART_IMAGE = {'factory': 'network_adapter.bin', 'linux': 'xipImage',
              'rootfs': 'rootfs.cramfs'}

def check_image(name, limit, exact=False):
    path = image_dir / name
    with path.open('rb') as stream:
        stream.seek(0, 2)
        size = stream.tell()
    if size <= 0 or size > limit or (exact and size != limit):
        raise ValueError(f'{path}: invalid size {size}; expected '
                         + (str(limit) if exact else f'1..{limit}') + ' bytes')
    return path

def read_table():
    parts = {}
    with table.open() as stream:
        for row in csv.reader(stream):
            if not row or not row[0].strip() or row[0].lstrip().startswith('#'):
                continue
            name = row[0].strip()
            if len(row) < 5 or name in parts:
                raise ValueError(f'{table}: malformed or duplicate partition {name}')
            offset, size = (int(field.strip(), 0) for field in row[3:5])
            if offset < 0x9000 or size <= 0 or (offset | size) % 4096:
                raise ValueError(f'{name}: invalid partition offset or size')
            if offset + size > flash_size:
                raise ValueError(f'{name}: partition exceeds 16 MiB flash')
            parts[name] = (offset, size)
    return parts

def check_binary_table(parts):
    binary = check_image('partition-table.bin', 0x1000).read_bytes()
    installed = {}
    for pos in range(0, len(binary) - 31, 32):
        magic, _, _, offset, size, label, _ = struct.unpack_from('<HBBII16sI', binary, pos)
        if magic != 0x50AA:
            break
        name = label.split(b'\0', 1)[0].decode('ascii')
        if name in installed:
            raise ValueError(f'partition-table.bin: duplicate partition {name}')
        installed[name] = (offset, size)
    if installed != parts:
        raise ValueError('partition-table.bin does not match CSV partition names/offsets/sizes')

try:
    if parts_mode == '0' and not only_part:
        check_image('linux-esp32s3-native-full.bin', flash_size, exact=True)
    elif only_part:
        # Only this partition's offset is needed, but the table is still parsed
        # and validated in full below -- an offset read out of a table that does
        # not match the board is exactly the mistake worth catching here.
        parts = read_table()
        if only_part not in parts:
            raise ValueError(f'{table}: no {only_part} partition')
        check_binary_table(parts)
        check_image(PART_IMAGE[only_part], parts[only_part][1])
        print(str(parts[only_part][0]))
    else:
        parts = read_table()
        end = 0x9000
        for name, (offset, size) in sorted(parts.items(), key=lambda item: item[1][0]):
            if offset < end:
                raise ValueError(f'{name}: overlapping partitions')
            end = offset + size
        required = ('factory', 'linux', 'rootfs')
        for name in required:
            if name not in parts:
                raise ValueError(f'{table}: missing {name} partition')
        check_image('bootloader.bin', 0x8000)
        check_binary_table(parts)
        for name, filename in zip(required, ('network_adapter.bin',
                                             'xipImage', 'rootfs.cramfs')):
            check_image(filename, parts[name][1])
        print(' '.join(str(parts[name][0]) for name in required))
except (OSError, ValueError, struct.error) as error:
    sys.exit(f'error: preflight failed: {error}; no board data was changed')
PY
)

if [ -n "$ONLY_PART" ]; then
	set -- "$OFFSETS" "$IMG/$(part_image "$ONLY_PART")"
	echo "Warning: --part $ONLY_PART writes the $ONLY_PART partition only."
	echo "         Every other byte of the board is left as it is."
elif [ "$PARTS" = 1 ]; then
	# shellcheck disable=SC2086
	set -- $OFFSETS
	OFF_APP=$1; OFF_LINUX=$2; OFF_ROOTFS=$3
	set -- 0x0 "$IMG/bootloader.bin" 0x8000 "$IMG/partition-table.bin" \
		"$OFF_APP" "$IMG/network_adapter.bin" \
		"$OFF_LINUX" "$IMG/xipImage" "$OFF_ROOTFS" "$IMG/rootfs.cramfs"
	if [ "$ERASE" = 1 ]; then
		echo "Warning: --parts --erase resets the whole chip; all board data will be lost."
	else
		echo "Warning: --parts overwrites the kernel and the rootfs."
		echo "         /etc and /home live in the rootfs now, so they are replaced too."
	fi
else
	set -- 0x0 "$IMG/linux-esp32s3-native-full.bin"
	echo "Warning: the full image overwrites everything, including /etc and /home."
fi

# images/ now ships a SHA256SUMS; check it when the directory has one, so a
# truncated download or a half-written artifact is caught before the erase.
# GNU coreutils spells the tool sha256sum; macOS (and anything carrying perl's
# digest module) spells it `shasum -a 256`. Both verify with -c. `--quiet` is
# GNU-only, so the output is captured and the per-file lines indented instead.
# The status has to be read from the command, not the pipeline: this file is
# POSIX sh, so there is no pipefail to catch a failure on the left of a pipe.
if [ -f "$IMG/SHA256SUMS" ]; then
	if command -v sha256sum >/dev/null 2>&1; then
		SHA="sha256sum"
	elif command -v shasum >/dev/null 2>&1; then
		SHA="shasum -a 256"
	else
		SHA=""
	fi
	if [ -z "$SHA" ]; then
		echo "warning: no sha256sum or shasum here, so $IMG/SHA256SUMS was NOT checked" >&2
	else
		echo "==> Verifying checksums in $IMG"
		# shellcheck disable=SC2086
		sums=$( cd "$IMG" && $SHA -c SHA256SUMS ) || {
			printf '%s\n' "$sums" >&2
			echo "error: checksum mismatch in $IMG; no board data was changed" >&2
			exit 1
		}
		printf '%s\n' "$sums" | sed 's/^/    /'
	fi
fi

# Layout changes require a clean install. A partial write must verify the
# actual chip layout first, because a larger rootfs can overlap old /home.
if [ -n "$ONLY_PART" ] || { [ "$PARTS" = 1 ] && [ "$ERASE" = 0 ]; }; then
	echo "==> Checking the board's partition table"
	# shellcheck disable=SC2086
	if ! $ESPTOOL --chip esp32s3 -p "$PORT" -b 460800 --before "$BEFORE" \
		--after no_reset verify_flash 0x8000 "$IMG/partition-table.bin"; then
		echo "error: cannot verify the installed layout; no flash was written" >&2
		exit 1
	fi
	BEFORE=no_reset
fi

if [ "$ERASE" = 1 ]; then
	echo "==> Erasing the whole chip (this wipes /home too)"
	# --before matters here too: erase_flash would otherwise reset the board
	# first and drop a hand-entered download mode before erasing anything.
	# shellcheck disable=SC2086
	$ESPTOOL --chip esp32s3 -p "$PORT" -b 460800 --before "$BEFORE" --after hard_reset erase_flash
fi

echo "==> Flashing validated images"
# shellcheck disable=SC2086
$ESPTOOL --chip esp32s3 -p "$PORT" -b 460800 --before "$BEFORE" --after hard_reset \
	write_flash --flash_mode dio --flash_size 16MB --flash_freq 80m "$@"

cat <<'EOF'

Done. If the board does not come up, press RST on it. The reset esptool does
after a write pulses RTS, and a board that does not wire RTS to EN on its own
USB port -- the Arduino Nano ESP32 does not -- is left sitting in download mode
until something resets it by hand. That is the same board the note above about
--before no_reset is for.

Open the serial console at 115200 baud, e.g.:

    screen /dev/ttyACM0 115200      (or: picocom -b 115200 /dev/ttyACM0)
    macOS: screen /dev/cu.usbserial-* 115200

Log in as root / arduino. Nothing listens on the network: the board is
console-only until you run `remote-login ssh`,
and `remote-login off` closes it again.
No WiFi is configured on a fresh flash. The boot prints "Starting network
(background): OK" either way — it comes up behind the login prompt, and what
it actually did is in /var/log/network.log. Connect with:

    wifi connect "YOUR SSID" "YOUR PASSWORD"

Then `remote-login ssh`, and ssh in from your LAN, or keep using the serial
console.
EOF
