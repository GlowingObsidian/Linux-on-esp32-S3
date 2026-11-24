#!/bin/bash
# Copyright (c) 2026 Paulneja. GPLv3, see LICENSE. https://github.com/paulneja/Linux-on-esp32-S3
set -euo pipefail
cd "$(dirname "$0")"

has_changes() { [ -d "$1/patches" ] && [ -d "$1/new-files" ]; }

LOCAL=""
if [ -n "${LOCAL_CHANGES:-}" ]; then
	has_changes "$LOCAL_CHANGES" || {
		echo "error: LOCAL_CHANGES=$LOCAL_CHANGES has no patches/ and new-files/" >&2
		exit 1
	}
	LOCAL="$(cd "$LOCAL_CHANGES" && pwd)"
elif has_changes local-changes; then
	LOCAL="$(cd local-changes && pwd)"
else
	for d in ../* ../../*; do
		if has_changes "$d"; then LOCAL="$(cd "$d" && pwd)"; break; fi
	done
fi

if [ -z "$LOCAL" ]; then
	cat >&2 <<-EOF
	error: cannot find this project's patches/ and new-files/.
	Clone https://github.com/paulneja/Linux-on-esp32-S3 next to this tree, or
	point us at an existing checkout:
	    LOCAL_CHANGES=/path/to/Linux-on-esp32-S3 $0 ${1:-buildroot}
	EOF
	exit 1
fi
echo ">>> using local changes from: $LOCAL"

case "${1:-}" in
buildroot)
	[ -d build/buildroot ] || { echo "error: build/buildroot missing -- clone it first" >&2; exit 1; }
	echo ">>> buildroot: applying tracked-file changes"
	cd build/buildroot
	# The clone lives in a Docker volume that outlives a build, and the rsync
	# below only ever adds, so a file this project deletes from new-files/ stays
	# behind in the clone, gets copied into the rootfs, and the build reports
	# success. That is not hypothetical: usr/sbin/first-login was still in the
	# shipped image after the script was deleted, and etc/motd.tagline and
	# devkit_c1_16m_linux.config had both outlived their own removals the same
	# way. Putting the tree back to the pinned revision first is what makes a
	# deletion authoritative -- and it must be *every* tracked file, not just the
	# directories the rsync writes: this patch reaches past board/ into
	# linux/linux.mk and package/libcurl/libcurl.mk, so resetting only board/ and
	# configs/ leaves those patched and the patch then applies in neither
	# direction.
	#
	# Nothing generated lives in this tree -- build output goes to the O=
	# directory, which is a sibling -- so there is nothing here to lose. -x as
	# well as -d under board/ and configs/, the only places the patch creates
	# files, so patch debris (.orig/.rej) does not accumulate either.
	git checkout -- .
	git clean -fdxq board/ configs/
	# Now a clean tree every time, so this applies rather than "already applied".
	if git apply --check "$LOCAL/patches/03-buildroot-tracked-changes.patch" 2>/dev/null; then
		git apply "$LOCAL/patches/03-buildroot-tracked-changes.patch"
	elif git apply --check --reverse "$LOCAL/patches/03-buildroot-tracked-changes.patch" 2>/dev/null; then
		echo "    already applied -- skipping"
	else
		echo "error: 03-buildroot-tracked-changes.patch does not apply and is" >&2
		echo "       not already applied. Building on would silently produce an" >&2
		echo "       incomplete image. Details:" >&2
		git apply --check "$LOCAL/patches/03-buildroot-tracked-changes.patch" >&2 || true
		exit 1
	fi
	cd ../..
	echo ">>> buildroot: copying new files (board profile, espctl, init scripts)"
	# No rm for board/espressif/esp32s3/patches here any more: the clean above
	# already removed it along with everything else of ours.
	rsync -a "$LOCAL/new-files/board/" build/buildroot/board/
	rsync -a "$LOCAL/new-files/configs/" build/buildroot/configs/
	;;
esp-hosted)
	[ -d build/esp-hosted/esp_hosted_ng ] || { echo "error: build/esp-hosted/esp_hosted_ng missing -- clone it first" >&2; exit 1; }
	echo ">>> esp-hosted firmware: applying the firmware patch + committing locally"
	echo "    (esp_driver/CMakeLists.txt runs 'git reset --hard' on every cmake invocation;"
	echo "     a local commit is the only thing that survives that -- see DEVELOPMENT.md)"
	cd build/esp-hosted/esp_hosted_ng
	# The guard is the patch's own hash, not the presence of a local commit.
	# A work directory that persists across runs keeps the clone from the last
	# build, so "is there a local commit?" is true forever after the first
	# build -- and every later edit to the firmware patch would be silently
	# ignored, leaving a stale network_adapter.bin that reports success.
	# Recording what was applied is the only thing that can tell the two apart.
	fw_stamp=".esp-local-firmware.sha256"
	fw_sha=$(sha256sum "$LOCAL/patches/02-firmware-network-adapter.patch" \
		"$LOCAL/new-files/esp-hosted/network_adapter/partition_table.esp32s3.16m8r" \
		"$LOCAL/new-files/esp-hosted/network_adapter/sdkconfig.defaults.esp32s3.16m8r" \
		| sha256sum | cut -d' ' -f1)
	if [ "$(cat "$fw_stamp" 2>/dev/null || true)" = "$fw_sha" ]; then
		echo "    firmware patch unchanged since the last build -- skipping"
	else
		# The previous commit may already carry an older version of the patch,
		# so reset to the locked revision before applying the current one.
		# clone_locked allows local commits on top of the revision, and this
		# drops exactly those.
		if git log --oneline -1 | grep -q "local-only, never push"; then
			echo "    firmware patch changed; re-applying on a clean tree"
			git reset --hard "$(git rev-parse HEAD~1)"
		fi
		git apply "$LOCAL/patches/02-firmware-network-adapter.patch"
		cp "$LOCAL/new-files/esp-hosted/network_adapter/partition_table.esp32s3.16m8r" \
			"$LOCAL/new-files/esp-hosted/network_adapter/sdkconfig.defaults.esp32s3.16m8r" \
			esp/esp_driver/network_adapter/
		git add esp/esp_driver/network_adapter/main/ \
			esp/esp_driver/network_adapter/sdkconfig.defaults.esp32s3 \
			esp/esp_driver/network_adapter/partition_table.esp32s3.16m8r \
			esp/esp_driver/network_adapter/sdkconfig.defaults.esp32s3.16m8r
		# the host git name ended up in the fw hash once. nope
		env -u GIT_AUTHOR_NAME -u GIT_AUTHOR_EMAIL -u GIT_COMMITTER_NAME -u GIT_COMMITTER_EMAIL \
			git -c user.email="local@backup" -c user.name="local-backup" \
			commit -m "network_adapter: this project's firmware (local-only, never push)"
		printf '%s\n' "$fw_sha" > "$fw_stamp"
	fi

	;;
*)
	echo "usage: $0 buildroot|esp-hosted" >&2
	exit 1
	;;
esac
