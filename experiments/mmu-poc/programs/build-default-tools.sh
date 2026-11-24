#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"

out=${DEFAULT_TOOLS_OUT:-$programs_out/default-tools}
mkdir -p "$out/licenses"

fetch() {
    local sha=$1 url=$2 file=$3
    if [[ ! -f $file ]]; then
        curl -fL --retry 2 "$url" -o "$file.part"
        mv "$file.part" "$file"
    fi
    printf '%s  %s\n' "$sha" "$file" | sha256sum -c -
}

# Supply the complete GCC atomic ABI with the Nano's kernel-backed word
# operations. Disable compiler emission of native CAS in these programs.
native=$build_dir/xtensa-dynconfig
board=$build_dir/esp32s3
sed '/^#include <xtensa-dynconfig.h>$/a\
#undef XCHAL_HAVE_S32C1I\
#define XCHAL_HAVE_S32C1I 0
' "$native/xtensa-config.c" > "$out/xtensa-config.c"
gcc -shared -fPIC -O2 -DORIG -I"$native/include" -I"$board/binutils" \
    -I"$board/xtensa/config" "$out/xtensa-config.c" \
    "$board/binutils/xtensa-modules.c" "$board/gdb/xtensa-config.c" \
    "$board/gdb/xtensa-xtregs.c" -o "$out/xtensa-linux.so"
export XTENSA_GNU_CONFIG="$out/xtensa-linux.so"
ct=$build_dir/crosstool-NG/.build/xtensa-esp32s3-linux-uclibcfdpic
gcc_final=$ct/build/build-cc-gcc-final
triple=xtensa-esp32s3-linux-uclibcfdpic
atomic=$(mktemp -d "$out/atomic.XXXXXX")
trap 'rm -rf "$atomic"' EXIT
mkdir -p "$atomic/source" "$atomic/build/$triple"
cp -a "$ct/src/gcc/"{libatomic,include} "$atomic/source/"
cp -a "$gcc_final/$triple/libatomic" "$atomic/build/$triple/"
ln -s "$gcc_final/gcc" "$atomic/build/gcc"
python3 "$repo_dir/build/patch-psram-libatomic.py" \
    "$atomic/source/libatomic" "$programs_dir/esp32-atomics"
make -C "$atomic/build/$triple/libatomic" srcdir="$atomic/source/libatomic" \
    top_srcdir="$atomic/source/libatomic" clean
make -C "$atomic/build/$triple/libatomic" -j"${JOBS:-6}" CC="$CC" \
    CFLAGS='-Os -mauto-litpools -mfdpic -fno-inline-atomics -pthread' \
    srcdir="$atomic/source/libatomic" top_srcdir="$atomic/source/libatomic"
"$prefix-strip" --strip-unneeded -o "$out/libatomic.so.1" \
    "$atomic/build/$triple/libatomic/.libs/libatomic.so.1.2.0"
ln -sfn libatomic.so.1 "$out/libatomic.so"

COREMARK_OUT="$out" COREMARK_XTENSA_CONFIG="$XTENSA_GNU_CONFIG" \
    bash "$programs_dir/build-coremark.sh"
fetch 84404ca8431b595e86c473d8f23d8bb102810001f15feaf610effd3b318788aa \
    https://downloads.es.net/pub/iperf/iperf-3.17.1.tar.gz "$out/iperf-3.17.1.tar.gz"
PLATFORM_TEST_OUT="$out" IPERF_XTENSA_CONFIG="$XTENSA_GNU_CONFIG" IPERF_RUNTIME_DIR="$out" \
    bash "$programs_dir/build-iperf3.sh"

fetch e71d93de41e8e4b2554c36d2ae51758112a21ecf3a8c98605a2c689251e62987 \
    https://codeload.github.com/fastfetch-cli/fastfetch/tar.gz/refs/tags/2.71.0 "$out/fastfetch-2.71.0.tar.gz"
tar -xzf "$out/fastfetch-2.71.0.tar.gz" -C "$out"
fastfetch=$out/fastfetch-2.71.0
apply_program_patch "$fastfetch" fastfetch-xtensa.patch
cp "$programs_dir/fastfetch-compat/"ifaddrs.{c,h} "$fastfetch/"
cat >> "$fastfetch/CMakeLists.txt" <<'CMAKE'

target_sources(libfastfetch PRIVATE ifaddrs.c)
target_include_directories(libfastfetch PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")
CMAKE
# The Nano config uses text-only output. Keep the generic Linux artwork.
find "$fastfetch/src/logo/ascii" -name '*.txt' ! -name 'linux.txt' ! -name 'linux_small.txt' \
    -exec sh -c 'for path do printf "\\n" > "$path"; done' sh {} +
cat > "$out/fastfetch-toolchain.cmake" <<CMAKE
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR xtensa)
set(CMAKE_C_COMPILER "$CC")
set(CMAKE_AR "$AR")
set(CMAKE_RANLIB "$RANLIB")
set(CMAKE_C_FLAGS_INIT "$CFLAGS -fno-inline-atomics")
set(CMAKE_EXE_LINKER_FLAGS_INIT "-L$out -Wl,--gc-sections,-z,separate-code,-z,stack-size=131072 -latomic -lrt -lm")
CMAKE
modules=()
for directory in "$fastfetch/src/modules/"*; do
    name=${directory##*/}
    case "$name" in
        title|separator|os|host|kernel|uptime|shell|cpu|memory|swap|disk|localip|wifi|loadavg|processes|custom|colors|break|version)
            modules+=("-DMODULE_DISABLE_${name^^}=OFF") ;;
        *) modules+=("-DMODULE_DISABLE_${name^^}=ON") ;;
    esac
done
cmake -S "$fastfetch" -B "$out/fastfetch-build" \
    -DCMAKE_TOOLCHAIN_FILE="$out/fastfetch-toolchain.cmake" -DCMAKE_BUILD_TYPE=MinSizeRel \
    -DENABLE_LTO=OFF -DBUILD_FLASHFETCH=OFF -DSET_TWEAK=OFF \
    -DPKG_CONFIG_EXECUTABLE=/bin/false -DENABLE_IMAGE_LOGO=OFF \
    -DENABLE_WORDEXP=OFF -DENABLE_LUA=OFF -DENABLE_LIBZFS=OFF "${modules[@]}"
cmake --build "$out/fastfetch-build" --target fastfetch -j"${JOBS:-6}"
"$prefix-strip" --strip-unneeded -R .xt.prop -R .xt.lit \
    -o "$out/fastfetch" "$out/fastfetch-build/fastfetch"

for name in coremark-single coremark-smp iperf3 fastfetch-build/fastfetch libatomic.so.1; do
    # Fastfetch's unstripped .xt.prop distinguishes literal pools from code.
    "$prefix-objdump" -d "$out/$name" > "$out/${name##*/}.dis"
    if grep -qw s32c1i "$out/${name##*/}.dis"; then
        echo "Unexpected native PSRAM CAS in $name" >&2
        exit 1
    fi
done
cp "$out/coremark-1.01/LICENSE.md" "$out/licenses/CoreMark"
cp "$out/iperf-3.17.1/LICENSE" "$out/licenses/iperf3"
cp "$fastfetch/LICENSE" "$out/licenses/Fastfetch"
cp "$ct/src/gcc/COPYING3" "$out/licenses/libatomic-GPL-3.0"
cp "$ct/src/gcc/COPYING.RUNTIME" "$out/licenses/libatomic-runtime-exception"
