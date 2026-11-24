#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
"$CC" $CFLAGS -std=gnu17 -Wall -Wextra -Werror -pthread \
    "$programs_dir/pthread-smp-test.c" $LDFLAGS -pthread \
    -o "$programs_out/pthread-smp-test.debug"
"$prefix-strip" -o "$programs_out/pthread-smp-test" "$programs_out/pthread-smp-test.debug"
"$prefix-readelf" -d "$programs_out/pthread-smp-test" | sed -n '/NEEDED/p'
