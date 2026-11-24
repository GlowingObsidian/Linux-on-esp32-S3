#!/usr/bin/env bash
# Build in the existing reproduction container; never run or flash the board.
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
export XTENSA_GNU_CONFIG=${COREMARK_XTENSA_CONFIG:-$XTENSA_GNU_CONFIG}
coremark_out=${COREMARK_OUT:-$repo_dir/build-output/coremark-smp}
archive=$coremark_out/coremark-1.01.tar.gz
mkdir -p "$coremark_out"
if [[ ! -f $archive ]]; then
    curl --fail --location --output "$archive" \
        https://codeload.github.com/eembc/coremark/tar.gz/refs/tags/v1.01
fi
printf '%s  %s\n' \
    99c5a6d63af85a281b4e4d6ccb522c446653c435dfec9455ad73ef9e71f28bde \
    "$archive" | sha256sum -c -
tar -xzf "$archive" -C "$coremark_out"
src=$coremark_out/coremark-1.01
# Only the Linux portability layer changes: monotonic wall time, small
# pthread stacks, checked thread errors, and CPU affinity for the SMP run.
python3 "$programs_dir/coremark-port.py" "$src"
base_flags='-O2 -mfdpic -mauto-litpools -fPIC -ffunction-sections -fdata-sections -D_GNU_SOURCE'
link_flags='-Wl,--gc-sections,-z,stack-size=65536 -lrt'
for mode in single smp; do
    mkdir -p "$coremark_out/$mode"
    parallel_flags='-DMULTITHREAD=1'
    libs=$link_flags
    if [[ $mode = smp ]]; then
        parallel_flags='-DMULTITHREAD=2 -DUSE_PTHREAD=1 -pthread'
        libs="$libs -pthread"
    fi
    make -C "$src" link PORT_DIR=linux EXE= \
        OPATH="$coremark_out/$mode/" CC="$CC" \
        PORT_CFLAGS="$base_flags" XCFLAGS="$parallel_flags" \
        LFLAGS_END="$libs" ITERATIONS=0
    "$prefix-strip" -o "$coremark_out/coremark-$mode" \
        "$coremark_out/$mode/coremark"
    "$prefix-readelf" -h -l -d "$coremark_out/coremark-$mode" \
        > "$coremark_out/coremark-$mode.elf.txt"
    "$prefix-size" "$coremark_out/coremark-$mode"
done
sha256sum "$coremark_out"/coremark-{single,smp} > "$coremark_out/SHA256SUMS"
echo "CoreMark single-thread and two-thread FDPIC binaries ready; board tests pending."
