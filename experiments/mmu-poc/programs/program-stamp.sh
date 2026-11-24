#!/usr/bin/env bash
# Copyright (c) 2026 Paulneja. GPLv3, see LICENSE. https://github.com/paulneja/Linux-on-esp32-S3
#
# Per-program fingerprints for the `userspace` stage.
#
# The stage is one long shell script: micropython, then dash, make, bash, socat,
# netcat, dtach, the process tools, then the image. It has a single `.done`
# marker, so a change to any one program re-runs all of them. Most of that is
# wasted work -- the trees are already incremental, and the only reason a
# rebuild costs anything is the `make clean` and `./configure` at the top of
# each script, which throw the objects away and then redo them.
#
# So each program gets a fingerprint of what it is built from: its own build
# script, the patch it applies, the source tree's own version, and the shared
# inputs that change what any of them link against. When the fingerprint
# matches, the program is already built and its build script is skipped
# entirely -- not run and found to be a no-op, skipped, so nothing rewrites an
# mtime.
#
# `--print NAME` lists the files a program's fingerprint covers, which is how
# to check that a dependency was not forgotten.
#
# Usage:
#   program-stamp.sh hash NAME     print the fingerprint
#   program-stamp.sh check NAME    exit 0 if built from it, 1 otherwise
#   program-stamp.sh write NAME    record it (after a successful build)
#   program-stamp.sh files NAME    print the files it covers
set -euo pipefail

task_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# programs/ -> mmu-poc/ -> experiments/ -> repo root: three, not two.
repo_dir=$(cd -- "$task_dir/../../.." && pwd)
experiment_dir=$(cd -- "$task_dir/.." && pwd)
programs_out="$experiment_dir/out/programs"
build_dir="$repo_dir/../refs/esp32-linux-build/build"
stamp_dir="$programs_out/.stamps"

# The fork library is what every one of these links against, and it is built
# from the fork patch set and the kernel config. A change to any of those
# changes the ABI these programs were linked to, so all of them are stale.
shared_inputs() {
    printf '%s\n' "$experiment_dir/programs/env.sh"
    printf '%s\n' "$repo_dir/new-files/board/espressif/esp32s3/nano_esp32_linux.config"
    printf '%s\n' "$experiment_dir/fork/fork-compat.c"
    printf '%s\n' "$experiment_dir/fork/build-test.sh"
    find "$experiment_dir/fork" -maxdepth 1 -name '*.patch' -type f | LC_ALL=C sort
}

# What a program is built from, beyond the shared inputs. Each entry is the
# build script, the patches it applies, and anything else it reads. The source
# tarball is deliberately not listed: it never changes, and hashing 10 MB per
# program per build is the kind of cost this file exists to remove. The
# version is pinned in the script, which is hashed.
program_files() {
    local name=$1
    case "$name" in
    micropython)
        printf '%s\n' "$task_dir/build-micropython.sh" "$task_dir/micropython.patch"
        find "$task_dir/modules" -type f | LC_ALL=C sort
        # The interpreter is built against the fork header, like the rest.
        printf '%s\n' "$experiment_dir/fork/real/fork-decl.h"
        ;;
    dash)
        printf '%s\n' "$experiment_dir/fork/real/build.sh" "$experiment_dir/fork/real/dash.patch"
        printf '%s\n' "$experiment_dir/fork/real/fork-decl.h"
        ;;
    make)
        printf '%s\n' "$experiment_dir/fork/real/build.sh" "$experiment_dir/fork/real/make.patch"
        printf '%s\n' "$experiment_dir/fork/real/fork-decl.h"
        ;;
    bash)
        printf '%s\n' "$task_dir/build-bash.sh" "$task_dir/shell-tools.sha256"
        for p in bash-fork.patch bash-help-cross.patch bash-pid-cache.patch bash-vfork.patch; do
            printf '%s\n' "$task_dir/$p"
        done
        printf '%s\n' "$task_dir/test-bash-pid-cache.py"
        ;;
    socat)
        printf '%s\n' "$task_dir/build-socat.sh" "$task_dir/socat-fork.patch"
        printf '%s\n' "$task_dir/shell-tools.sha256"
        ;;
    netcat)
        printf '%s\n' "$task_dir/build-netcat.sh"
        for p in busybox-nc.patch busybox-login.patch busybox-users.patch busybox-stat.patch; do
            printf '%s\n' "$task_dir/$p"
        done
        ;;
    dtach)
        printf '%s\n' "$task_dir/build-dtach.sh" "$task_dir/dtach-vfork.patch"
        printf '%s\n' "$experiment_dir/fork/real/fork-decl.h"
        ;;
    atfork)
        printf '%s\n' "$task_dir/build-atfork-test.sh"
        ;;
    process-tools)
        printf '%s\n' "$task_dir/build-process-tools.sh"
        ;;
    lscpu)
        printf '%s\n' "$task_dir/build-lscpu.sh"
        printf '%s\n' "$experiment_dir/fork/real/fork-decl.h"
        ;;
    pthread-smp-test)
        printf '%s\n' "$task_dir/build-pthread-smp-test.sh" "$task_dir/pthread-smp-test.c"
        ;;
    *)
        echo "program-stamp.sh: unknown program '$name'" >&2
        return 2
        ;;
    esac
}

# The unpacked source tree, which is the one thing the lists above cannot name
# in advance: the build scripts apply their patches to it in place, so its
# content is the script plus the patch plus whatever a hand edit left behind.
# Hashing it is what closes that gap -- without it, a tree edited by hand and
# then skipped would keep the edit and never rebuild.
#
# It is affordable. Measured on this tree: 22 files for dtach, 278 for socat,
# 1761 for bash, 8426 for micropython, and hashing all four takes about 1.3 s
# against builds that take minutes. Object files and the build directories are
# excluded, so the cost does not grow as the tree is built.
program_tree() {
    case "$1" in
    micropython) printf '%s\n' "$programs_out/micropython" ;;
    dash) printf '%s\n' "$programs_out/real-bins/dash-0.5.12" ;;
    make) printf '%s\n' "$programs_out/real-bins/make-4.4.1" ;;
    bash) printf '%s\n' "$programs_out/bash-5.2.37" ;;
    socat) printf '%s\n' "$programs_out/socat-1.8.1.3" ;;
    netcat) printf '%s\n' "$programs_out/busybox-netcat" ;;
    dtach) printf '%s\n' "$programs_out/dtach-b027c27b2439081064d07a86883c8e0b20a183c9" ;;
    lscpu) printf '%s\n' "$programs_out/util-linux-2.39.3" ;;
    *) return 0 ;;
    esac
}

fingerprint_files() {
    local name=$1
    shared_inputs
    program_files "$name"
}

hash_files() {
    local f
    while IFS= read -r f; do
        if [[ -f "$f" ]]; then
            printf '%s  ' "${f#"$repo_dir"/}"
            sha256sum < "$f" | cut -d' ' -f1
        else
            printf '%s  MISSING\n' "${f#"$repo_dir"/}"
        fi
    done < <(fingerprint_files "$1")
    # One line for the whole tree rather than one per file: the tree is
    # thousands of files and the per-file lines would dwarf the rest of the
    # fingerprint. Excludes what a build writes, so it is stable across runs.
    local tree
    tree=$(program_tree "$1")
    if [[ -n "$tree" && -d "$tree" ]]; then
        printf '%s  ' "${tree#"$repo_dir"/} (tree)"
        find "$tree" -type f \
            -not -path '*/build-*/*' -not -name '*.o' -not -name '*.a' \
            -not -name '*.so' -not -name '*.elf' -not -name '*.d' \
            -not -name 'Makefile' -not -name 'config.status' -not -name 'config.log' \
            -not -name '*.pyc' -not -path '*/__pycache__/*' \
            -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1
    fi
}

name=${2:-}
if [[ -z "$name" ]]; then
    echo "usage: program-stamp.sh [hash|check|write|files] NAME" >&2
    exit 2
fi
stamp="$stamp_dir/$name.sha256"

case "${1:-check}" in
hash)
    hash_files "$name" | sha256sum | cut -d' ' -f1
    ;;
files)
    fingerprint_files "$name"
    ;;
check)
    want=$(hash_files "$name" | sha256sum | cut -d' ' -f1)
    have=$(cat "$stamp" 2>/dev/null || true)
    [[ "$want" == "$have" ]] && exit 0
    if [[ -z "$have" ]]; then
        echo "program $name: never stamped, building" >&2
    else
        echo "program $name: inputs moved ($have -> $want), rebuilding" >&2
    fi
    exit 1
    ;;
write)
    mkdir -p "$stamp_dir"
    hash_files "$name" | sha256sum | cut -d' ' -f1 > "$stamp"
    ;;
*)
    echo "usage: program-stamp.sh [hash|check|write|files] NAME" >&2
    exit 2
    ;;
esac
