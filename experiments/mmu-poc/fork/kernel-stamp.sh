#!/usr/bin/env bash
# Copyright (c) 2026 Paulneja. GPLv3, see LICENSE. https://github.com/paulneja/Linux-on-esp32-S3
#
# Fingerprint of everything that decides what `out/linux-fork` contains, and
# therefore whether a kernel rebuild is needed at all.
#
# Why this exists: the kernel is the long pole of every build. The fork tree is
# a 1.8 GB copy of buildroot's configured kernel with eight patches on top, and
# `make` inside it is incremental *if* the tree is left alone. The old path
# could not leave it alone. `build-kernel-reclaim.sh` strips and re-applies its
# patches on every run, which rewrites `mm/nommu-bank.inc` whether or not its
# content changed, and `make` then rebuilds nommu.o and relinks the image. A
# patch that is already applied is a no-op in content but not in mtime, and
# mtime is what make reads.
#
# So the rule is: hash the inputs, and do nothing at all when the hash matches
# what the tree was last built from. The hash covers
#
#   - buildroot's configured kernel: `.config` and the board config it came
#     from, because the copy inherits both
#   - `patches/linux/*.patch`, which buildroot applies to that tree
#   - every patch in this directory, the fork patch set
#   - the two scripts that decide the config and the build flags
#
# and nothing else. A change to a userspace program, the rootfs, the firmware
# or the packaging does not move it, and must not: those are separate stages
# with their own fingerprints.
#
# Usage:
#   kernel-stamp.sh hash    print the fingerprint
#   kernel-stamp.sh check   exit 0 if out/linux-fork was built from it, 1 otherwise
#   kernel-stamp.sh write   record the fingerprint (after a successful build)
#   kernel-stamp.sh files   print the files the fingerprint covers, one per line
set -euo pipefail

task_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd -- "$task_dir/../../.." && pwd)
experiment_dir=$(cd -- "$task_dir/.." && pwd)
build_dir="$repo_dir/../refs/esp32-linux-build/build"
source_dir="$build_dir/build-buildroot-arduino_nano_esp32/build/linux-7.2.4"
kernel_dir="$experiment_dir/out/linux-fork"
stamp="$kernel_dir/.fork-kernel-inputs.sha256"

# The fork patch set, in application order. build-kernel-reclaim.sh owns this
# order; keep the two in step. A patch added there and not here would be
# invisible to the fingerprint and a stale kernel would ship.
fork_patches=(
    kernel.patch
    reclaim.patch
    quiet-trace.patch
    swap-banks.patch
    switch-latency.patch
    switch-irqs-on.patch
    mmu-swap.patch
    bank-budget.patch
    smp-banks.patch
    sd-exec.patch
    real/scheduler.patch
)
fork_scripts=(
    build-kernel.sh
    build-kernel-reclaim.sh
    check-kernel-config.py
)

fingerprint_files() {
    printf '%s\n' "$repo_dir/new-files/board/espressif/esp32s3/nano_esp32_linux.config"
    printf '%s\n' "$source_dir/.config"
    # patches/linux is applied to the buildroot tree the copy is made from.
    # Sorted, so a rename does not read as a change.
    find "$repo_dir/new-files/board/espressif/esp32s3/patches/linux" \
        -maxdepth 1 -name '*.patch' -type f | LC_ALL=C sort
    for p in "${fork_patches[@]}"; do printf '%s\n' "$task_dir/$p"; done
    for s in "${fork_scripts[@]}"; do printf '%s\n' "$task_dir/$s"; done
}

# The path is hashed alongside the content: a patch renamed to a different
# number changes the order buildroot applies it in, and order matters.
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
        echo "no kernel fingerprint recorded; the fork tree has never been stamped" >&2
    else
        echo "kernel inputs moved ($have -> $want)" >&2
    fi
    exit 1
    ;;
write)
    mkdir -p "$kernel_dir"
    hash_files | sha256sum | cut -d' ' -f1 > "$stamp"
    echo "kernel fingerprint recorded: $(cat "$stamp")"
    ;;
*)
    echo "usage: kernel-stamp.sh [hash|check|write|files]" >&2
    exit 2
    ;;
esac
