#!/bin/sh
# Kernel PID 1: executable files stay on flash; persistent state lives on SD.
PATH=/sbin:/bin:/usr/sbin:/usr/bin
export PATH
start_init() {
    if [ -x /usr/sbin/wifi-boot ]; then
        /usr/sbin/wifi-boot || echo 'hybrid: Wi-Fi setup incomplete; continuing boot'
    fi
    exec /sbin/init
}
[ -r /proc/mounts ] || mount -t proc proc /proc
[ -c /dev/console ] || mount -t devtmpfs devtmpfs /dev
mount -t sysfs sysfs /sys 2>/dev/null || :
fallback() {
    mount -t tmpfs -o mode=1777 tmpfs /tmp
    mount -t tmpfs -o mode=0755 tmpfs /var/log
}

n=0
while [ ! -b /dev/mmcblk0p1 ] && [ "$n" -lt 30 ]; do
    sleep 1
    n=$((n+1))
done
if [ -b /dev/mmcblk0p1 ] && mount -t ext4 -o rw,noatime,data=ordered,errors=remount-ro /dev/mmcblk0p1 /data; then
    if [ "$(cat /data/.esp32-sd-profile 2>/dev/null)" != journaled-sd-v1 ]; then
        echo 'hybrid: journaled SD data image required; leaving existing card unused'
        umount /data
        fallback
        start_init
    fi
    state=/data/.linux-state
    if [ ! -e "$state" ]; then
        stage=$(mktemp -d /data/.linux-state.new.XXXXXX)
        if [ -n "$stage" ]; then
            ok=yes
            for dir in etc home root var tmp opt; do
                mkdir "$stage/$dir" || ok=no
                cp -a "/$dir/." "$stage/$dir/" || ok=no
            done
            mkdir "$stage/usr-local" || ok=no
            cp -a /usr/local/. "$stage/usr-local/" || ok=no
            chmod 1777 "$stage/tmp" || ok=no
            printf '%s\n' hybrid-xip-v1 > "$stage/.profile" || ok=no
            if [ "$ok" = yes ]; then
                sync
                mv "$stage" "$state" || ok=no
                sync
            fi
        fi
    fi
    if [ -r "$state/.profile" ] && [ "$(cat "$state/.profile")" = hybrid-xip-v1 ]; then
        ok=yes
        for dir in etc home root var tmp opt; do
            mount --bind "$state/$dir" "/$dir" || ok=no
        done
        mount --bind "$state/usr-local" /usr/local || ok=no
        # Frequent logs must not create continuous SD metadata writes.
        mount -t tmpfs -o mode=0755 tmpfs /var/log
        if [ "$ok" = yes ]; then
            echo 'hybrid: flash XIP system; writable SD etc/home/root/var/tmp/opt/usr-local'
        else
            echo 'hybrid: some SD bind mounts failed; inspect /proc/mounts'
        fi
    else
        echo 'hybrid: SD state initialization failed; retaining flash defaults'
        fallback
    fi
else
    echo 'hybrid: SD unavailable; booting flash defaults without persistent writes'
    fallback
fi
start_init
