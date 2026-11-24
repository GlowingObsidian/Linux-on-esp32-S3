#!/usr/bin/env bash
# Compile with the preserved FDPIC toolchain. Board execution stays manual.
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
platform_out=${PLATFORM_TEST_OUT:-$repo_dir/build-output/platform-stability-2026-11-19}
mkdir -p "$platform_out"
"$CC" $CFLAGS -std=gnu17 -Wall -Wextra -Werror -pthread \
    "$programs_dir/smp-stress.c" $LDFLAGS $LIBS -pthread -lrt \
    -o "$platform_out/smp-stress.debug"
"$prefix-strip" -o "$platform_out/smp-stress" "$platform_out/smp-stress.debug"
# iperf2 needs C++; this cached toolchain supplies C only. Use iperf3 peers.
PLATFORM_TEST_OUT="$platform_out" bash "$programs_dir/build-iperf3.sh"
for executable in smp-stress iperf3; do
    "$prefix-readelf" -h -l -d "$platform_out/$executable" > "$platform_out/$executable.elf.txt"
done
sha256sum "$platform_out/smp-stress" "$platform_out/iperf3" > "$platform_out/programs.sha256"
echo 'SMP stress and iperf3 binaries built; board validation pending.'
