#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
original="$build_dir/build-buildroot-arduino_nano_esp32/build/busybox-1.36.1"
source_dir="$programs_out/busybox-netcat"
variant=${OPT_VARIANT:-baseline}
extra=()
output=busybox-with-netcat
case "$variant" in
    baseline) ;;
    oz|lto)
        source_dir="$programs_out/busybox-$variant"
        flags=-Oz
        if [[ "$variant" == lto ]]; then flags+=' -flto'; fi
        extra=("CONFIG_EXTRA_CFLAGS=$flags" "CONFIG_EXTRA_LDFLAGS=$flags -Wl,-z,stack-size=16384")
        output=busybox-$variant.bin ;;
    *) exit 2 ;;
esac
# The copy is skipped when the tree is already there, so a tree made before a
# busybox.config change keeps the old config and this build ships the old
# applets -- the config edit never reaches the binary, and nothing says so.
# So the tree is checked against buildroot's, which is configured from
# busybox.config and is therefore the thing this copy claims to derive from.
#
# The check cannot be a comparison of the two .config files. `make oldconfig`
# below rewrites $source_dir/.config in place, and it does so on every run --
# so after the first successful build the two files differ by construction,
# and a comparison would abort every later run with "this tree is stale" about
# a tree that is current. What survives oldconfig is the *seed* the tree was
# configured from, so that is what is stamped and compared: a copy of
# buildroot's .config, taken at the moment the tree is made, and rewritten
# whenever the tree is re-copied. A busybox.config change moves buildroot's
# .config, the stamps stop matching, and the tree is rebuilt from the new one
# instead of being rejected.
stamp="$source_dir/.netcat-seed-config"
if [[ ! -d "$source_dir" ]]; then
    cp -a --reflink=auto "$original" "$source_dir"
    cp -a --reflink=auto "$original/.config" "$stamp"
elif [[ ! -f "$stamp" ]]; then
    # A tree from before this stamp existed. Its .config has been through
    # oldconfig, so it cannot be compared; treat it as current and stamp it,
    # rather than delete a tree that may be perfectly good.
    cp -a --reflink=auto "$original/.config" "$stamp"
elif ! cmp -s "$original/.config" "$stamp"; then
    echo "build-netcat.sh: buildroot's busybox config has changed since" >&2
    echo "  $source_dir was copied, so that tree carries the old applets." >&2
    echo "Re-copying it from $original" >&2
    rm -rf "$source_dir"
    cp -a --reflink=auto "$original" "$source_dir"
    cp -a --reflink=auto "$original/.config" "$stamp"
fi
apply_program_patch "$source_dir" busybox-nc.patch
apply_program_patch "$source_dir" busybox-login.patch
apply_program_patch "$source_dir" busybox-users.patch
apply_program_patch "$source_dir" busybox-stat.patch
unset CFLAGS CXXFLAGS CPPFLAGS LDFLAGS LIBS
make -C "$source_dir" ARCH=xtensa CROSS_COMPILE="$host_dir/bin/xtensa-esp32s3-linux-uclibcfdpic-" \
    HOSTCC='cc -std=gnu17' oldconfig < /dev/null
make -C "$source_dir" -j"${JOBS:-4}" ARCH=xtensa \
    CROSS_COMPILE="$host_dir/bin/xtensa-esp32s3-linux-uclibcfdpic-" HOSTCC='cc -std=gnu17' "${extra[@]}"
install -m 755 "$source_dir/busybox_unstripped" "$programs_out/$output.debug"
"$prefix-strip" --strip-unneeded -R .xt.prop -R .xt.lit \
    -o "$programs_out/$output" "$source_dir/busybox_unstripped"
"$prefix-size" "$programs_out/$output"
