#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"

# Use the same util-linux release/hash as the pinned Buildroot. Its broad
# 'basic set' is MMU-only, so build just lscpu, which does not require fork.
version=2.39.3
archive="util-linux-$version.tar.xz"
expected=7b6605e48d1a49f43cc4b4cfc59f313d0dd5402fa40b96810bd572e167dfed0f
cache="$build_dir/buildroot/dl/util-linux/$archive"
if [[ ! -f "$cache" ]]; then
    cache="$programs_out/$archive"
    if [[ ! -f "$cache" ]]; then
        curl -fL --retry 2 "https://www.kernel.org/pub/linux/utils/util-linux/v2.39/$archive" -o "$cache"
    fi
fi
printf '%s  %s\n' "$expected" "$cache" | sha256sum -c -
src="$programs_out/util-linux-$version"
if [[ ! -f "$src/configure" ]]; then
    tar -xJf "$cache" -C "$programs_out"
fi
mkdir -p "$programs_out/lscpu-build"
cd "$programs_out/lscpu-build"
# Static internal libraries avoid adding another target shared-library ABI.
# The executable still uses the existing dynamic uClibc.
# libcommon also compiles an unused fork-based canonicalization helper.
# NOMMU uClibc omits that declaration; the prototype lets it compile, and
# function-section GC discards it from lscpu (which does not call it).
export CFLAGS="$CFLAGS -Os -include $experiment_dir/fork/real/fork-decl.h"
LIBS= "$src/configure" \
    --host=xtensa-linux --build="$(sh "$src/config/config.guess")" \
    --prefix=/usr --disable-all-programs --enable-lscpu \
    --enable-libsmartcols --disable-libblkid --disable-libmount \
    --disable-libfdisk --disable-libuuid --disable-shared --enable-static \
    --disable-nls --without-python --without-systemd --without-udev \
    --without-selinux --without-audit --without-cap-ng
make -j"${JOBS:-4}" lscpu
"$prefix-strip" -o "$programs_out/lscpu" lscpu
"$prefix-size" "$programs_out/lscpu"
"$prefix-readelf" -d "$programs_out/lscpu" | sed -n '/NEEDED/p'
