#!/bin/sh
# Complete the first mount after the manual NOR scratch test.
# BusyBox hush on this board rejects set -eu; check each operation explicitly.
guard_dev=/sys/bus/platform/devices/3fcc8000.flash-guard
[ "$(cat "$guard_dev/rw_enable")" = 1 ] || {
    echo 'Run the verified manual scratch test and enable rw first.' >&2
    exit 1
}
[ "$#" -eq 0 ] || { echo 'Usage: flash-state-enable' >&2; exit 1; }

mount -t jffs2 -o rw,noatime mtd:etc /etc || exit 1
if ! mount -t jffs2 -o rw,noatime mtd:home /home; then
    umount /etc
    exit 1
fi

printf '%s\n' flash-nor-v1 > /etc/.flash-rw-verified.new || exit 1
sync || exit 1
mv /etc/.flash-rw-verified.new /etc/.flash-rw-verified || exit 1
sync || exit 1
echo 'Persistent flash etc/home enabled. Next boot uses them without SD.'
