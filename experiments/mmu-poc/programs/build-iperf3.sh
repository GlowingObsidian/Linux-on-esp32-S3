#!/usr/bin/env bash
# Build a matching iperf3 client/server using the preserved C-only toolchain.
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
export XTENSA_GNU_CONFIG=${IPERF_XTENSA_CONFIG:-$XTENSA_GNU_CONFIG}
network_out=${PLATFORM_TEST_OUT:-$repo_dir/build-output/platform-stability-2026-11-19}
archive=$network_out/iperf-3.17.1.tar.gz
printf '%s  %s\n' 84404ca8431b595e86c473d8f23d8bb102810001f15feaf610effd3b318788aa "$archive" | sha256sum -c -
tar -xzf "$archive" -C "$network_out"
cat > "$network_out/iperf3-nommu.h" <<'HEADER'
#include <sys/types.h>
#include <unistd.h>
#include <pthread.h>
/* libfork supplies fork() on this NOMMU uClibc build. */
extern pid_t fork(void);
/* Bound each benchmark stream's stack within the S3's 8 MiB RAM. */
static inline int esp32_iperf_thread_attr_init(pthread_attr_t *attributes)
{
    int error = pthread_attr_init(attributes);
    if (error)
        return error;
    error = pthread_attr_setstacksize(attributes, 65536);
    if (error)
        pthread_attr_destroy(attributes);
    return error;
}
#define pthread_attr_init esp32_iperf_thread_attr_init
HEADER
if [[ -n ${IPERF_RUNTIME_DIR:-} ]]; then
    CFLAGS="$CFLAGS -fno-inline-atomics"
    LDFLAGS="$LDFLAGS -L$IPERF_RUNTIME_DIR"
    LIBS="$LIBS -latomic"
fi
export CFLAGS="$CFLAGS -D_GNU_SOURCE -include $network_out/iperf3-nommu.h"
export LIBS="$LIBS -pthread -lrt -lm"
mkdir -p "$network_out/iperf3-build"
cd "$network_out/iperf3-build"
"$network_out/iperf-3.17.1/configure" --host=xtensa-esp32s3-linux-uclibcfdpic \
    --disable-shared --enable-static --without-openssl --without-sctp
make -j"${JOBS:-6}"
"$prefix-strip" -o "$network_out/iperf3" src/iperf3
"$prefix-readelf" -h -l -d "$network_out/iperf3" > "$network_out/iperf3.elf.txt"
sha256sum "$network_out/iperf3" > "$network_out/iperf3.sha256"
printf '%s\n' 'SMP stress and iperf3 built; board runs remain manual.'
