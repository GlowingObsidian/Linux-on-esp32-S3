#!/bin/sh
# Early BusyBox sysinit: only etc/home persistent, no SD wait or mount.
PATH=/sbin:/bin:/usr/sbin:/usr/bin
export PATH
guard_dev=/sys/bus/platform/devices/3fcc8000.flash-guard

[ -r /proc/mounts ] || mount -t proc proc /proc
[ -c /dev/console ] || mount -t devtmpfs devtmpfs /dev
mount -t sysfs sysfs /sys 2>/dev/null || :
mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /tmp
mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /var/log

# Confirm that both state filesystems can be read before permitting writes.
# The guard checks cache coordination and identifies the flash at every boot.
if [ -e "$guard_dev/rw_enable" ] && mount -t jffs2 -o ro,noatime mtd:etc /etc; then
    if mount -t jffs2 -o ro,noatime mtd:home /home &&
       echo 3 > "$guard_dev/test" && echo 4 > "$guard_dev/test" &&
       echo resume > "$guard_dev/rw_enable" &&
       mount -o remount,rw,noatime /etc &&
       mount -o remount,rw,noatime /home; then
        :
    else
        mount -o remount,ro /etc 2>/dev/null || :
        echo 0 > "$guard_dev/rw_enable"
        umount /home 2>/dev/null || :
        umount /etc 2>/dev/null || :
        echo 'flash: writable-state validation failed; recovery with read-only defaults'
    fi
else
    echo 'flash: writable partitions unavailable; using read-only root defaults'
fi

# Retire only this project's legacy RGB script after the LED driver owns the
# pins. A rename preserves rollback and runs once on persistent /etc.
if [ -d /sys/class/leds/nano:red ] && [ -f /etc/init.d/S04led ]; then
    legacy_led_sha=$(sha256sum /etc/init.d/S04led)
    if [ "${legacy_led_sha%% *}" = "aba49fb0ba0cf6c11a2288ec5d3007dee1edcf61dfbd5e446753c3f0d21b367c" ]; then
        mv /etc/init.d/S04led /etc/.S04led.pre-gpio-leds ||
            echo 'flash: could not retire the legacy RGB LED script' >&2
    else
        echo 'flash: modified legacy RGB LED script needs review' >&2
    fi
fi

if [ -x /usr/sbin/wifi-boot ]; then
    /usr/sbin/wifi-boot || echo 'flash: Wi-Fi setup incomplete; continuing boot'
fi
exit 0
