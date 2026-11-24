#!/usr/bin/env bash
# Copyright (c) 2026 Paulneja. GPLv3, see LICENSE. https://github.com/paulneja/Linux-on-esp32-S3
#
# Fingerprint of everything that decides what the `firmware` stage produces:
# network_adapter.bin and bootloader.bin, and therefore whether the ESP-IDF
# sequence needs to run at all.
#
# Why this exists: the stage is the most expensive one after the toolchain, and
# it re-runs in full whenever it runs. `idf_tools.py install` downloads the
# Xtensa toolchain for the S3 and builds a Python environment for it, then
# `idf.py build` compiles the whole network_adapter from scratch. None of that
# is incremental across runs, because every `docker run` is a fresh container
# and $HOME -- where the IDF tools live -- is not on a mounted volume.
#
# The stage's own guard is the `.done` marker in build/container-build.sh, and
# that marker only covers "this work directory has already run this stage". It
# cannot see that the inputs moved, and it cannot see that they did not: a
# `--fresh` run, or a work directory whose stages/ was cleared, pays the whole
# download and build again for a firmware that is already correct.
#
# So the rule is the same as the kernel's and the programs': hash the inputs,
# and do nothing when the hash matches what the binary on disk was built from.
# The hash covers
#
#   - the firmware patch applied on top of esp-hosted, which is what makes this
#     project's network_adapter different from upstream's
#   - the partition table and the sdkconfig defaults the build copies in
#   - the network_adapter sources themselves, which the patch does not touch
#     but which a hand edit would
#   - the ESP-IDF revision, because a different IDF is a different binary
#   - make-images.sh, which is what turns the build output into images/
#
# and nothing else. A change to the kernel, the rootfs or a userspace program
# does not move it, and must not: those are separate stages with their own
# fingerprints.
#
# Usage:
#   firmware-stamp.sh hash    print the fingerprint
#   firmware-stamp.sh check   exit 0 if the images were built from it, 1 otherwise
#   firmware-stamp.sh write   record the fingerprint (after a successful build)
#   firmware-stamp.sh files   print the files the fingerprint covers, one per line
set -euo pipefail

task_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd -- "$task_dir/../../.." && pwd)
build_dir="$repo_dir/../refs/esp32-linux-build/build"
driver_dir="$build_dir/esp-hosted/esp_hosted_ng/esp/esp_driver"
adapter_dir="$driver_dir/network_adapter"
stamp="$build_dir/esp-hosted/.local-firmware-inputs.sha256"

# sources.lock is where the IDF revision is pinned. Sourced rather than parsed
# so the fingerprint cannot disagree with what the build actually checks out.
set -a
source "$repo_dir/build/sources.lock"
set +a

fingerprint_files() {
    printf '%s\n' "$repo_dir/new-files/esp-hosted/network_adapter/partition_table.esp32s3.16m8r"
    printf '%s\n' "$repo_dir/new-files/esp-hosted/network_adapter/sdkconfig.defaults.esp32s3.16m8r"
    printf '%s\n' "$repo_dir/patches/02-firmware-network-adapter.patch"
    printf '%s\n' "$repo_dir/make-images.sh"
    printf '%s\n' "$repo_dir/build/test-nano-memory-config.py"
    printf '%s\n' "$repo_dir/build/patch-firmware-vector-reservation.py"
    printf '%s\n' "$repo_dir/build/sources.lock"
    # The sources the patch is applied to. Listed by path, sorted, so a rename
    # reads as a change and a build directory appearing does not.
    find "$adapter_dir/main" -type f | LC_ALL=C sort
    # Anything else tracked at the top of network_adapter that the build reads:
    # the CMakeLists, the Kconfig, the other partition tables. Not `build/`,
    # which is the output, and not `sdkconfig`, which idf.py rewrites.
    find "$adapter_dir" -maxdepth 1 -type f \
        -not -name 'sdkconfig' -not -name 'sdkconfig.old' | LC_ALL=C sort
}

# The path is hashed alongside the content: a source file renamed changes what
# the build compiles, and a patch renamed changes whether it applies at all.
hash_files() {
    local f
    while IFS= read -r f; do
        if [[ -f "$f" ]]; then
            printf '%s  ' "${f#"$repo_dir"/}"
            sha256sum < "$f" | cut -d' ' -f1
        else
            printf '%s  MISSING\n' "${f#"$repo_dir"/}"
        fi
    done < <(fingerprint_files)
    # The IDF revision is a value, not a file, so it gets its own line. Without
    # it a bump to ESP_IDF_REV would keep the previous binary and report
    # success, which is the one failure this file exists to prevent.
    printf 'ESP_IDF_REV  %s\n' "$ESP_IDF_REV"
}

case "${1:-check}" in
hash)
    hash_files | sha256sum | cut -d' ' -f1
    ;;
files)
    fingerprint_files
    ;;
check)
    want=$(hash_files | sha256sum | cut -d' ' -f1)
    have=$(cat "$stamp" 2>/dev/null || true)
    if [[ "$want" == "$have" ]]; then
        exit 0
    fi
    if [[ -z "$have" ]]; then
        echo "no firmware fingerprint recorded; the firmware has never been stamped" >&2
    else
        echo "firmware inputs moved ($have -> $want)" >&2
    fi
    exit 1
    ;;
write)
    mkdir -p "$(dirname -- "$stamp")"
    hash_files | sha256sum | cut -d' ' -f1 > "$stamp"
    echo "firmware fingerprint recorded: $(cat "$stamp")"
    ;;
*)
    echo "usage: firmware-stamp.sh [hash|check|write|files]" >&2
    exit 2
    ;;
esac
