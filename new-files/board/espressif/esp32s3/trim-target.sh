#! /bin/sh
# Copyright (c) 2026 Paulneja. GPLv3, see LICENSE. https://github.com/paulneja/Linux-on-esp32-S3

set -e

# Nothing NEEDs these: readelf -d over every ELF in the image lists
# libncurses for nano and bash, and libnl-3 and libnl-genl-3 for iw and
# wpa_supplicant, and none of them names these four. buildroot's own
# trim-libs.sh already does the same for libnl-nf, libnl-route and libnl-xfrm.
rm -f "$1/usr/lib/"libform* "$1/usr/lib/"libmenu* "$1/usr/lib/"libpanel*
rm -f "$1/usr/lib/"libnl-idiag*

# Buildroot's esp32s3 board directory carries four dropbear private host keys
# and they are tracked in a public repository, so every board built from it
# would answer SSH with the same key -- and ship it world readable. inetd runs
# dropbear with -R, which generates a key per board into the writable /etc on
# the first connection, so the baked-in ones are only harmful.
rm -f "$1/etc/dropbear/"*_host_key

# /var/log is a symlink into the 1777 /tmp in buildroot's skeleton, so syslogd
# writes authentication records where any local user can read them. /run is
# 0755 and is mounted before syslogd starts.
if [ -L "$1/var/log" ]; then
	rm -f "$1/var/log"
	ln -s ../run/log "$1/var/log"
fi

# swapon/swapoff cannot work: CONFIG_SWAP depends on an MMU and this kernel is
# NOMMU, so both inittab lines are a guaranteed failing fork+exec per boot.
#
# The third line this used to manage is the USB Serial/JTAG getty. It is gone
# for a different reason: the only USB PHY on this SoC now belongs to the OTG
# controller, so ttyGS3 is not there to respawn a getty on, and usb-console --
# the command that uncommented the placeholder -- is retired with it. Deleting
# any ttyGS3 line rather than only the placeholder also cleans an inittab that
# came down from an earlier image with it already uncommented.
if [ -f "$1/etc/inittab" ]; then
	sed -i '/^::sysinit:\/sbin\/swapon /d; /^::shutdown:\/sbin\/swapoff /d; /ttyGS3::/d' \
		"$1/etc/inittab"
fi

# The serial getty is named explicitly rather than left on buildroot's
# `console` alias. `console` is a kernel alias for the *last* console= on the
# command line, and patch 0067 appends console=tty1 to the one that was
# already there -- so `console` stopped meaning ttyS0 and buildroot's
# `console::respawn:/sbin/getty -L  console 0 vt100` silently moved the serial
# getty onto the panel. The board then had two gettys on tty1 and none on
# ttyS0: the serial console took no input at all, and the two on the panel
# fought over the terminal. Pinning the device here means the serial getty
# survives any future change to what console= points at.
#
# 115200 is the rate the kernel command line sets for ttyS0. getty does not
# need it -- the line is already configured -- but naming it keeps the line
# readable as the serial one.
if [ -f "$1/etc/inittab" ]; then
	sed -i 's#^console::respawn:/sbin/getty -L  console 0 vt100.*#ttyS0::respawn:/sbin/getty -L ttyS0 115200 vt100#' \
		"$1/etc/inittab"
fi

# A getty on tty1, so the panel carries a login prompt and not only kernel
# messages. buildroot's skeleton inittab has no tty1 line -- it ships the
# serial getty and a handful of commented-out placeholders -- and this image
# has no /etc/inittab of its own, so the line is added here rather than
# carried in the overlay. Nothing in the sed above removes it.
#
# The leading :: is the id field, left empty the way buildroot's own serial
# line leaves it. -L is there because the panel cannot be typed at: without
# it getty would wait for carrier detect on a device that will never raise
# it, and the prompt would never appear.
#
# The terminal type is `linux`, not the `vt100` the serial getty uses, and
# that is the difference between a coloured panel and a monochrome one. `ls`
# is busybox with FEATURE_LS_COLOR_IS_DEFAULT=y, so it emits SGR colour codes
# regardless of TERM -- but TERM is what tells the *reader* what those codes
# mean, and the reader here is fbcon on the board itself. vt100's terminfo
# entry has no colour capabilities, so busybox's own `ls` short-circuits to
# no colour at all and the panel comes up monochrome. `linux` is the console
# entry: it has colour, and fbcon's 16-slot palette maps onto the ST7789's
# RGB565 framebuffer, which was confirmed on the board by printing a colour
# bar to the panel.
#
# This matters only for the panel. The serial getty keeps vt100 because the
# reader there is a terminal emulator on the far end of the cable, which
# carries its own colour settings and does not consult the board's TERM.
#
# The line is deleted and rewritten rather than appended when absent, and that
# is a fix, not a style choice. The append-if-absent form this used to have --
# `grep -q ... || echo ... >>` -- is idempotent against itself and helpless
# against a line that differs: target/ is add-only, so an earlier build's
# `::respawn:/sbin/getty -L tty1 0 vt100` was still there, the grep matched
# it, and the `linux` this script writes was never appended. The image then
# shipped vt100 on the panel while this file said linux, and the two disagreed
# silently for two builds. Confirmed on the board: `echo $TERM` on tty1
# printed vt100. Deleting first makes this line the script's to state, so the
# value here is the value that ships.
#
# The id field is `tty1`, and that is load-bearing, not decoration. busybox
# init treats an EMPTY id as "use init's own tty" -- open_stdio_to_tty() in
# busybox's init.c only opens the device when the name is non-empty, and
# otherwise the child keeps init's stdio, which is /dev/console. /dev/console
# is the LAST `console=` on the kernel command line, and that is ttyS0. So an
# empty id here does not mean "no tty", it means "ttyS0" -- and this line
# would put a SECOND getty on the serial port, where it fights the ttyS0 getty
# above for every keystroke and neither one completes a login line. The
# symptom is a login prompt that accepts no input.
#
# That is what this line did from the commit that added the panel console
# until now. It was invisible because no build in between reached a login
# prompt -- they panicked on the rootfs or reset on the watchdog first.
#
# Naming tty1 makes init open /dev/tty1 and hand that to the getty, which is
# what the panel needs and what the serial port needs it not to be.
if [ -f "$1/etc/inittab" ]; then
	# Both spellings are deleted, so this is idempotent against an inittab
	# that already carries either: the empty-id form this used to write, and
	# the tty1-id form it writes now. target/ is add-only, so a stale line
	# from an earlier build survives into the next one otherwise.
	sed -i -e '/^::respawn:\/sbin\/getty -L tty1 /d' \
	       -e '/^tty1::respawn:\/sbin\/getty -L tty1 /d' "$1/etc/inittab"
	echo 'tty1::respawn:/sbin/getty -L tty1 0 linux' >> "$1/etc/inittab"
fi

# www-data exists only to run busybox httpd, which this image does not build
# (CONFIG_HTTPD is off) and nothing else references. It arrives from buildroot's
# skeleton /etc/passwd and /etc/group rather than from anything here, so it has
# to be removed here. A UID that no process can assume is still a UID a file
# could be chowned to, and an owner that cannot log in cannot be audited.
for file in etc/passwd etc/group etc/shadow; do
	[ -f "$1/$file" ] || continue
	sed -i '/^www-data:/d' "$1/$file"
done
for file in etc/passwd etc/group etc/shadow; do
	[ -f "$1/$file" ] || continue
	if grep -q '^www-data:' "$1/$file"; then
		echo "trim-target: www-data survived in $file" >&2
		exit 1
	fi
done

# Paths this image used to carry and no longer does. build/package-final.py
# already drops these from the tree it packs, which is why they do not ship --
# but target/ is add-only, the same way every stage of the cramfs chain is, so
# a file a package stopped installing or this project stopped copying sits here
# forever. That is how usr/share/esp32-cron survived two builds after cron was
# removed: it was not in retired-paths.txt yet, so nothing removed it, and the
# stale copy in target/ was packed into the image. Deleting them here as well
# enforces the removal where the leftovers are made rather than only at the end
# of the chain, and it means a path has to be *on* the list to be gone from
# target/ -- so a leftover nothing knows about cannot reach the image.
#
# The list is the same file package-final.py reads, so the two cannot disagree
# about what is retired. It sits beside this script -- apply-local-changes.sh
# rsyncs new-files/board/ into buildroot/board/ -- so find it from this
# script's own directory. $1 is the target directory and is unrelated to where
# the board files live.
board=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$board/retired-paths.txt" ]; then
	while IFS= read -r line; do
		path=$(printf '%s' "$line" | sed 's/#.*//; s/^[[:space:]]*//; s/[[:space:]]*$//; s#^/##; s#/$##')
		[ -n "$path" ] || continue
		rm -rf "$1/$path"
	done < "$board/retired-paths.txt"
fi
