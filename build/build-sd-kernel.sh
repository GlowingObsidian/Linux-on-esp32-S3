#!/usr/bin/env bash
# Incremental SD-rootfs POC build; run in the cached reproduction container.
set -euo pipefail
r=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
b=${ESP32_BUILD_CACHE:-/work/refs/esp32-linux-build/build}
k=$r/experiments/mmu-poc/out/linux-fork
profile=$r/new-files/board/espressif/esp32s3/sd-rootfs
art=${SD_ARTIFACTS:-$r/build-output/sd-rootfs-poc}
prefix=$b/crosstool-NG/builds/xtensa-esp32s3-linux-uclibcfdpic/bin/xtensa-esp32s3-linux-uclibcfdpic-
export XTENSA_GNU_CONFIG=$b/xtensa-dynconfig/esp32s3.so
export KBUILD_BUILD_USER=builder KBUILD_BUILD_HOST=esp32-repro
export KBUILD_BUILD_TIMESTAMP='Sat Sep 5 00:00:00 UTC 2026' KBUILD_BUILD_VERSION=1
mode=${SD_TRANSFER_MODE:-dma}
root_mode=${SD_ROOT_MODE:-sd}
case "$mode" in
    dma) dtb=arduino-nano-esp32-sd ;;
    pio) dtb=arduino-nano-esp32-sd-pio ;;
    *) echo "SD_TRANSFER_MODE must be dma or pio" >&2; exit 1 ;;
esac
case "$root_mode" in
    sd) ;;
    hybrid)
        [[ "$mode" = pio ]] || { echo 'Hybrid baseline requires PIO' >&2; exit 1; }
        dtb=arduino-nano-esp32-hybrid-pio ;;
    *) echo 'SD_ROOT_MODE must be sd or hybrid' >&2; exit 1 ;;
esac
mkdir -p "$art"
# Save the prior configuration and image before selecting the SD profile.
if [[ ! -f "$art/pre-sd.config" ]]; then
    cp "$k/.config" "$art/pre-sd.config"
    cp "$k/arch/xtensa/boot/xipImage" "$art/pre-sd.xipImage"
fi
python3 "$r/experiments/mmu-poc/fork/check-kernel-config.py" --require-fork \
    "$r/new-files/board/espressif/esp32s3/nano_esp32_linux.config" "$art/pre-sd.config"
patches=(
 "$r/new-files/board/espressif/esp32s3/patches/linux/0081-spi-esp32-prepare-safe-SD-transfers.patch"
 "$r/new-files/board/espressif/esp32s3/patches/linux/0082-spi-esp32s3-add-internal-SRAM-GDMA-transfers.patch"
 "$r/new-files/board/espressif/esp32s3/patches/linux/0083-xtensa-esp32s3-publish-RAM-executables-to-I-bus.patch"
 "$r/new-files/board/espressif/esp32s3/patches/linux/0084-spi-esp32s3-complete-GDMA-master-initialization.patch"
 "$r/new-files/board/espressif/esp32s3/patches/linux/0085-spi-esp32s3-restore-PIO-FIFO-state-and-recover-from-DMA-errors.patch"
 "$r/new-files/board/espressif/esp32s3/patches/linux/0086-spi-esp32-allow-an-isolated-PIO-baseline.patch"
 "$r/new-files/board/espressif/esp32s3/patches/linux/0087-mm-nommu-reclaim-duplicate-executable-file-cache.patch"
 "$r/new-files/board/espressif/esp32s3/patches/linux/0088-spi-esp32s3-validate-single-TX-descriptor-completion.patch"
 "$r/new-files/board/espressif/esp32s3/patches/linux/0089-spi-esp32s3-clear-FIFO-errors-and-enable-SRAM-bursts.patch"
 "$r/experiments/mmu-poc/fork/sd-exec.patch"
)
# Reconstruct and verify in scratch; preserve mtimes of unchanged headers.
python3 "$r/build/apply-kernel-stack.py" "$k" "${patches[@]}"
cp "$profile/arduino-nano-esp32-sd.dts" "$profile/arduino-nano-esp32-sd-pio.dts" "$k/arch/xtensa/boot/dts/"
cp "$profile/arduino-nano-esp32-hybrid-pio.dts" "$k/arch/xtensa/boot/dts/"
cat "$profile/kernel.fragment" > "$art/kernel.profile"
if [[ "$root_mode" = hybrid ]]; then
    cat >> "$art/kernel.profile" <<'EOF'
CONFIG_EXT4_FS=y
# CONFIG_EXT4_FS_POSIX_ACL is not set
# CONFIG_EXT4_FS_SECURITY is not set
EOF
fi
printf 'CONFIG_BUILTIN_DTB_NAME="%s"\n' "$dtb" >> "$art/kernel.profile"
cat "$r/new-files/board/espressif/esp32s3/nano_esp32_linux.config" \
    "$art/kernel.profile" > "$art/kernel.seed"
(cd "$k" && scripts/kconfig/merge_config.sh -m "$art/pre-sd.config" "$art/kernel.profile")
make -C "$k" ARCH=xtensa CROSS_COMPILE="$prefix" olddefconfig
python3 "$r/experiments/mmu-poc/fork/check-kernel-config.py" --require-fork "$art/kernel.seed" "$k/.config"
cp "$k/.config" "$art/kernel.config"
make -C "$k" -j"${JOBS:-6}" ARCH=xtensa CROSS_COMPILE="$prefix" xipImage
[[ $(wc -c < "$k/arch/xtensa/boot/xipImage") -le $((0x3d0000)) ]]
cp "$k/arch/xtensa/boot/xipImage" "$art/linux.bin"
cp "$k/arch/xtensa/boot/dts/$dtb.dtb" "$art/board.dtb"
sha256sum "${patches[@]}" "$profile/arduino-nano-esp32-sd.dts" "$profile/arduino-nano-esp32-sd-pio.dts" "$profile/arduino-nano-esp32-hybrid-pio.dts" "$art/kernel.profile" > "$art/kernel-inputs.sha256"
printf '%s\n' "$mode" > "$art/transfer-mode"
printf '%s\n' "$root_mode" > "$art/root-mode"
sha256sum "$art/linux.bin" "$art/board.dtb"
