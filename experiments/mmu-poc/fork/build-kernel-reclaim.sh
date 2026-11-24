#!/usr/bin/env bash
# Copyright (c) 2026 Paulneja. GPLv3, see LICENSE. https://github.com/paulneja/Linux-on-esp32-S3
set -euo pipefail
task_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$task_dir/../programs/env.sh"
kernel_dir="$experiment_dir/out/linux-fork"
applied() { patch -d "$kernel_dir" --force --dry-run -R -p1 < "$task_dir/$1" >/dev/null 2>&1; }

# Nothing below is free. Stripping and re-applying a patch rewrites the file it
# touches even when the content is identical, and `make` reads mtime, so a
# no-op patch run still rebuilds nommu.o and relinks the image. That is the
# whole cost this guard removes: when the fingerprint of everything that
# decides the kernel's contents matches what the tree was last built from,
# there is nothing to do and the tree is left exactly as it was found.
#
# The fingerprint covers the board config, buildroot's `.config`, every
# patches/linux patch and every fork patch -- so a change to any of them, and
# only those, gets a rebuild. See kernel-stamp.sh. FORK_FORCE_KERNEL=1 skips
# the guard for the case where the tree is suspect rather than the inputs.
if [[ "${FORK_FORCE_KERNEL:-0}" != 1 ]] &&
   bash "$task_dir/kernel-stamp.sh" check 2>/dev/null &&
   [[ -f "$experiment_dir/out/real-bins/xipImage-fork-quiet" ]]; then
    python3 "$task_dir/check-kernel-config.py" --require-fork \
        "$repo_dir/new-files/board/espressif/esp32s3/nano_esp32_linux.config" \
        "$kernel_dir/.config"
    echo "kernel: inputs unchanged, out/linux-fork left as built"
    sha256sum "$experiment_dir/out/real-bins/xipImage-fork-quiet"
    exit 0
fi
if [[ "${FORK_FORCE_KERNEL:-0}" == 1 ]]; then
    echo "kernel: FORK_FORCE_KERNEL=1, rebuilding regardless of the fingerprint"
fi
# Dependencies come off first. swap-banks and switch-latency rewrite the
# code the base patches touch, so on a warm tree that still carries them the
# base patches' own checks below would fail on changed context before this
# script ever reached the line that removes them. Strip them in reverse
# order; the block near the end puts them back when FORK_SWAP_BANKS=1.
if applied sd-exec.patch; then
    patch -d "$kernel_dir" --force --batch -R -p1 < "$task_dir/sd-exec.patch"
fi
if applied smp-banks.patch; then
    patch -d "$kernel_dir" --force --batch -R -p1 < "$task_dir/smp-banks.patch"
fi
if applied bank-budget.patch; then
    patch -d "$kernel_dir" --force --batch -R -p1 < "$task_dir/bank-budget.patch"
fi
if applied mmu-swap.patch; then
    patch -d "$kernel_dir" --force --batch -R -p1 < "$task_dir/mmu-swap.patch"
fi
if applied switch-irqs-on.patch; then
    patch -d "$kernel_dir" --force --batch -R -p1 < "$task_dir/switch-irqs-on.patch"
fi
if applied switch-latency.patch; then
    patch -d "$kernel_dir" --force --batch -R -p1 < "$task_dir/switch-latency.patch"
fi
if applied swap-banks.patch; then
    patch -d "$kernel_dir" --force --batch -R -p1 < "$task_dir/swap-banks.patch"
fi
if applied quiet-trace.patch; then
    patch -d "$kernel_dir" --force --batch -R -p1 < "$task_dir/quiet-trace.patch"
fi
if ! patch -d "$kernel_dir" --force --dry-run -R -p1 < "$task_dir/reclaim.patch" >/dev/null 2>&1; then
    FORK_CONFIGURE_ONLY=1 bash "$task_dir/build-kernel.sh"
    if ! applied real/scheduler.patch; then
        patch -d "$kernel_dir" --forward --batch --dry-run -p1 < "$task_dir/real/scheduler.patch"
        patch -d "$kernel_dir" --forward --batch -p1 < "$task_dir/real/scheduler.patch"
    fi
    patch -d "$kernel_dir" --forward --batch --dry-run -p1 < "$task_dir/reclaim.patch"
    patch -d "$kernel_dir" --forward --batch -p1 < "$task_dir/reclaim.patch"
fi
patch -d "$kernel_dir" --forward --batch --dry-run -p1 < "$task_dir/quiet-trace.patch"
patch -d "$kernel_dir" --forward --batch -p1 < "$task_dir/quiet-trace.patch"
# The page-set exchange is the default; FORK_SWAP_BANKS=0 builds the copying
# model. It goes last because it rewrites what the patches above build up;
# switch-latency and switch-irqs-on edit the exchange's nommu_bank_switch(),
# so they ride along.
if [ "${FORK_SWAP_BANKS:-1}" = 1 ]; then
    patch -d "$kernel_dir" --forward --batch --dry-run -p1 < "$task_dir/swap-banks.patch"
    patch -d "$kernel_dir" --forward --batch -p1 < "$task_dir/swap-banks.patch"
    patch -d "$kernel_dir" --forward --batch --dry-run -p1 < "$task_dir/switch-latency.patch"
    patch -d "$kernel_dir" --forward --batch -p1 < "$task_dir/switch-latency.patch"
    patch -d "$kernel_dir" --forward --batch --dry-run -p1 < "$task_dir/switch-irqs-on.patch"
    patch -d "$kernel_dir" --forward --batch -p1 < "$task_dir/switch-irqs-on.patch"
    patch -d "$kernel_dir" --forward --batch --dry-run -p1 < "$task_dir/mmu-swap.patch"
    patch -d "$kernel_dir" --forward --batch -p1 < "$task_dir/mmu-swap.patch"
    patch -d "$kernel_dir" --forward --batch --dry-run -p1 < "$task_dir/bank-budget.patch"
    patch -d "$kernel_dir" --forward --batch -p1 < "$task_dir/bank-budget.patch"
    patch -d "$kernel_dir" --forward --batch --dry-run -p1 < "$task_dir/smp-banks.patch"
    patch -d "$kernel_dir" --forward --batch -p1 < "$task_dir/smp-banks.patch"
    patch -d "$kernel_dir" --forward --batch --dry-run -p1 < "$task_dir/sd-exec.patch"
    patch -d "$kernel_dir" --forward --batch -p1 < "$task_dir/sd-exec.patch"
fi
"$kernel_dir/scripts/config" --file "$kernel_dir/.config" \
    --enable XTENSA_NOMMU_FORK --set-str LOCALVERSION '-forkbank'
make -C "$kernel_dir" ARCH=xtensa CROSS_COMPILE="$prefix-" olddefconfig
python3 "$task_dir/check-kernel-config.py" --require-fork \
    "$repo_dir/new-files/board/espressif/esp32s3/nano_esp32_linux.config" \
    "$kernel_dir/.config"
# The Wi-Fi blob's archives are linked into the kernel by the esp32-ng driver
# patch, whose Makefile hunk names them as $(srctree)/../../../esp-hosted from
# the kernel tree. That resolves correctly from buildroot's tree, which sits at
# build/linux-7.2.4 under the build driver, but not from this copy: out/linux-fork
# sits at experiments/mmu-poc/out/linux-fork, so the same relative path lands on
# experiments/esp-hosted, which does not exist.
#
# The link has to be at the path make actually resolves, and make resolves it
# against its own working directory, because srctree is "." here -- the kernel's
# top-level Makefile sets abs_srctree to $(realpath $(dir $(this-makefile))),
# which collapses to "." when the tree is its own source root. So the three
# levels up are counted from $kernel_dir, not from the repository root.
ln -sfn "$build_dir/esp-hosted" "$kernel_dir/../../../esp-hosted"
# The link is the whole point of the line above, and a silent failure here shows
# up three minutes later as make refusing a prerequisite it cannot see. Prove it
# resolves to the archive before the kernel build starts.
test -f "$kernel_dir/../../../esp-hosted/esp_hosted_ng/esp/esp_driver/esp-idf/components/esp_wifi/lib/esp32s3/libnet80211.a" || {
    echo "build-kernel-reclaim.sh: the esp-hosted link does not resolve:" >&2
    echo "  $kernel_dir/../../../esp-hosted -> $(readlink -f "$kernel_dir/../../../esp-hosted" 2>&1)" >&2
    exit 1
}
make -C "$kernel_dir" -j"${JOBS:-8}" ARCH=xtensa CROSS_COMPILE="$prefix-" xipImage \
    2>&1 | tee "$experiment_dir/out/kernel-quiet-details.log"
test "$(wc -c < "$kernel_dir/arch/xtensa/boot/xipImage")" -le $((0x3d0000))
cp "$kernel_dir/arch/xtensa/boot/xipImage" "$experiment_dir/out/real-bins/xipImage-fork-quiet"
sha256sum "$experiment_dir/out/real-bins/xipImage-fork-quiet"
# Recorded only after the image exists, so a build that dies halfway leaves no
# stamp and the next run does the work again rather than trusting a tree that
# was never finished.
bash "$task_dir/kernel-stamp.sh" write
