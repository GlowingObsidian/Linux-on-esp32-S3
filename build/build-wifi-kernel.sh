#!/usr/bin/env bash
# Incremental Linux-hosted Wi-Fi build on the preserved hybrid SMP baseline.
# Run in the cached reproduction container; no toolchain or SD/rootfs rebuild.
set -euo pipefail
r=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
b=${ESP32_BUILD_CACHE:-/work/refs/esp32-linux-build/build}
k=$r/experiments/mmu-poc/out/linux-fork
art=${WIFI_ARTIFACTS:-$r/build-output/wifi-smp-audit/build}
base=$r/build-output/hybrid-journaled-sd
prefix=$b/crosstool-NG/builds/xtensa-esp32s3-linux-uclibcfdpic/bin/xtensa-esp32s3-linux-uclibcfdpic-
export XTENSA_GNU_CONFIG=$b/xtensa-dynconfig/esp32s3.so
export KBUILD_BUILD_USER=builder KBUILD_BUILD_HOST=esp32-repro
if [[ -z ${KBUILD_BUILD_TIMESTAMP:-} ]]; then
    source_epoch=${SOURCE_DATE_EPOCH:-$(git -C "$r" show -s --format=%ct HEAD)}
    KBUILD_BUILD_TIMESTAMP=$(LC_ALL=C date -u -d "@$source_epoch")
fi
export KBUILD_BUILD_TIMESTAMP KBUILD_BUILD_VERSION=1
mkdir -p "$art"
patches=("$r"/new-files/board/espressif/esp32s3/patches/linux/009[2-9]-*.patch "$r"/new-files/board/espressif/esp32s3/patches/linux/010[0-9]-*.patch "$r"/new-files/board/espressif/esp32s3/patches/linux/011[0-9]-*.patch "$r"/new-files/board/espressif/esp32s3/patches/linux/012[0-9]-*.patch)
patches+=("$r"/new-files/board/espressif/esp32s3/patches/linux/013[0-9]-*.patch)
patches+=("$r"/new-files/board/espressif/esp32s3/patches/linux/014[0-8]-*.patch)
patches+=("$r"/new-files/board/espressif/esp32s3/patches/linux/0150-net-wireless-esp32-batch-Linux-RX-handoffs.patch)
[[ ${#patches[@]} = 58 ]]
dtb=arduino-nano-esp32-hybrid-wifi
if [[ ${FLASH_GUARD_TEST:-0} = 1 ]]; then
    dtb=arduino-nano-esp32-guard-test
    if [[ ${FLASH_NOR:-0} = 1 ]]; then
        dtb=arduino-nano-esp32-flash-rw
    fi
    cp "$r/experiments/flash-guard/$dtb.dts" "$k/arch/xtensa/boot/dts/"
fi
python3 "$r/build/apply-kernel-stack.py" "$k" "${patches[@]}"
cp "$r/new-files/board/espressif/esp32s3/sd-rootfs/arduino-nano-esp32-hybrid-wifi.dts" "$k/arch/xtensa/boot/dts/"
cat > "$art/wifi.fragment" <<'CONFIG'
CONFIG_ESP32_WIFI_BLOB=y
# CONFIG_ESP32_WIFI_SHMEM is not set
CONFIG_GENERIC_ALLOCATOR=y
CONFIG
printf 'CONFIG_BUILTIN_DTB_NAME="%s"\n' "$dtb" >> "$art/wifi.fragment"
if [[ ${FLASH_GUARD_TEST:-0} = 1 ]]; then
    cat >> "$art/wifi.fragment" <<'CONFIG'
CONFIG_MTD_ESP32_FLASH_GUARD_TEST=y
CONFIG
fi
if [[ ${FLASH_NOR:-0} = 1 ]]; then
    [[ ${FLASH_GUARD_TEST:-0} = 1 ]]
    cat >> "$art/wifi.fragment" <<'CONFIG'
CONFIG_MTD_ESP32S3_NOR=y
CONFIG_NEW_LEDS=y
CONFIG_LEDS_CLASS=y
CONFIG_LEDS_GPIO=y
# CONFIG_MMC is not set
# CONFIG_MMC_BLOCK is not set
# CONFIG_MMC_BLOCK_MINORS is not set
# CONFIG_MMC_SPI is not set
# CONFIG_CRC7 is not set
# CONFIG_CRC_ITU_T is not set
CONFIG
fi
cat "$base/kernel.config" "$art/wifi.fragment" > "$art/kernel.seed"
(cd "$k" && scripts/kconfig/merge_config.sh -m "$base/kernel.config" "$art/wifi.fragment")
components=$b/esp-hosted/esp_hosted_ng/esp/esp_driver/esp-idf/components
wifi_driver=$k/drivers/net/wireless/espressif/esp32-ng
ASAN_OPTIONS=detect_leaks=0 python3 "$r/build/test-wifi-rx.py" "$wifi_driver/esp_blob.c"
ASAN_OPTIONS=detect_leaks=0 python3 "$r/build/test-wifi-rx-batch.py" "$wifi_driver/main.c"
ASAN_OPTIONS=detect_leaks=0 python3 "$r/build/test-wifi-timing.py" "$k/drivers/net/wireless/espressif/esp32-ng/esp_blob.c"
ASAN_OPTIONS=detect_leaks=0 python3 "$r/build/test-wifi-cache-config.py"     "$k/drivers/net/wireless/espressif/esp32-ng/esp_blob.c"     "$components/esp_wifi/include/esp_wifi.h"
make -C "$k" ARCH=xtensa CROSS_COMPILE="$prefix" olddefconfig
ASAN_OPTIONS=detect_leaks=0 python3 "$r/build/test-iomap-split-pool.py" \
    "$k/fs/iomap/ioend.c" "$k/block/bio.c"
ASAN_OPTIONS=detect_leaks=0 python3 "$r/build/test-nommu-statm.py" \
    "$k/fs/proc/task_nommu.c"
python3 "$r/experiments/mmu-poc/fork/check-kernel-config.py" --require-fork "$art/kernel.seed" "$k/.config"
cp "$k/.config" "$art/kernel.config"
make -C "$k" -j"${JOBS:-6}" ARCH=xtensa CROSS_COMPILE="$prefix" ESP32_IDF_COMPONENTS="$components" xipImage
cp "$k/arch/xtensa/boot/xipImage" "$art/linux.bin"
cp "$k/vmlinux" "$k/System.map" "$art/"
cp "$k/arch/xtensa/boot/dts/$dtb.dtb" "$art/board.dtb"
sha256sum "${patches[@]}" "$art/kernel.config" > "$art/kernel-inputs.sha256"
sha256sum "$art/linux.bin" "$art/board.dtb" "$art/vmlinux" > "$art/SHA256SUMS"
# A linker success is not approval to overlap the existing XIP rootfs.
if [[ $(wc -c < "$art/linux.bin") -gt $((0x3d0000)) ]]; then
    echo 'NOT FLASHABLE: Wi-Fi kernel exceeds current Linux partition.' >&2
    exit 2
fi
printf '%s\n' 'Build complete; hardware, SRAM and flash-layout gates still required.'
