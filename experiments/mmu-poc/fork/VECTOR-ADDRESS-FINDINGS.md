# Vector address mismatch, and the stale partition limits around it

## The failure

`build/package-final.py:82`:

    AssertionError: Firmware/kernel vector mismatch

`package-final.py` compares `space_for_vectors` in the firmware ELF against
`CONFIG_VECTORS_ADDR` in `experiments/mmu-poc/out/linux-fork/.config`.

    firmware ELF   space_for_vectors   0x4037b000
    fork .config   CONFIG_VECTORS_ADDR 0x4037c000

## What `space_for_vectors` is

`new-files/esp-hosted/network_adapter/main/linux_boot.c:57`:

    static char IRAM_ATTR space_for_vectors[4096] __attribute__((aligned(4096)));

A real 4 KiB reservation in the firmware's own IRAM, aligned to a page, that
core 0 hands to Linux. `nm -S` on the ELF confirms size `0x1000`:

    4037b000 00001000 t space_for_vectors

The firmware's own vector table is elsewhere — `.iram0.vectors` at
`0x40374000`, `_vector_table = 0x40374000`. So `space_for_vectors` is a hole
the firmware deliberately leaves, not the firmware's own vectors.

That hole is carved out of `.iram0.text`, which spans
`0x40375000 + 0x104db = 0x403854db`. The symbols either side of the hole:

    4037a3a8 t esp_wifi_tx_done_cb      <- last before
    4037b000 t space_for_vectors        <- the 4 KiB hole
    4037c000 t spiflash_start_default   <- first after

**`0x4037c000` is inside live firmware code.** Linking the kernel there puts
Linux's vector table on top of `spiflash_start_default`, `esp_cpu_stall`,
`esp_cpu_compare_and_set` and friends — the core 0 StoreProhibited panic the
config comment warns about.

## Why the two disagreed

`make-images.sh:130-200` has a realign step. It reads `space_for_vectors` from
the firmware ELF, compares against `CONFIG_VECTORS_ADDR` in
`new-files/board/espressif/esp32s3/nano_esp32_linux.config`, and when they
differ it rewrites the board config, rewrites
`buildroot/build/linux-7.2.4/.config`, and runs `linux-rebuild`.

It never fired. Both `nano_esp32_linux.config` and buildroot's `.config` read
`0x4037c000` — they agreed with each other and disagreed with the firmware, and
the realign only reconciles the kernel side against the firmware when the
kernel side is the one that moved. The `Kernel: arch/xtensa/boot/xipImage is
ready` line in the build log was the ordinary kernel build, not a realign.

The firmware ELF was rebuilt from inputs that had not changed
(`firmware-stamp.sh hash` matches `.local-firmware-inputs.sha256`), so
`0x4037b000` is what this tree's firmware genuinely produces. The
`0x4037c000` in the sdkconfig comment is stale — it was measured when NimBLE
was first dropped, and the adapter's IRAM use moved 4 KiB further down since.

## The shipped kernel is not the one make-images.sh realigns

Worth recording separately, because it is a trap:

    build/package-final.py:247
        shutil.copyfile(experiment / 'real-bins/xipImage-fork-quiet', out / 'xipImage')

The `xipImage` that ships comes from `experiments/mmu-poc/out/linux-fork`, a
tree `build-kernel.sh:21` makes with `cp -a` from buildroot's tree **once**, and
which nothing re-syncs afterwards. `make-images.sh`'s realign touches
buildroot's tree, so even when it does fire it fixes a tree whose `xipImage` is
never shipped. The two only agree because they started equal.

`build-kernel.sh:44` runs `olddefconfig` on the fork tree, which preserves
`CONFIG_VECTORS_ADDR` from the copy rather than re-reading the board config, so
a corrected board config does not reach the fork tree either.

## The fix applied

`CONFIG_VECTORS_ADDR=0x4037b000`, set in
`new-files/board/espressif/esp32s3/nano_esp32_linux.config` (the live value,
via `BR2_LINUX_KERNEL_CUSTOM_CONFIG_FILE`), in buildroot's
`build/linux-7.2.4/.config`, and in the sdkconfig comment block.

The kernel defconfig inside patch 0039
(`arch/xtensa/configs/arduino_nano_esp32_defconfig`) also carries
`CONFIG_VECTORS_ADDR=0x4037c000` and is left alone: nothing reads it. Buildroot
configures the kernel from `BR2_LINUX_KERNEL_CUSTOM_CONFIG_FILE`, and
`check-kernel-config.py` verifies against the board config.

## Stale partition limits — a separate bug, not fixed here

The partition recut in `0f92141` moved `rootfs` from `0x6b0000` to `0x630000`
and `linux` from `0x310000` to `0x390000`. The `0x6b0000` was not propagated:

    experiments/mmu-poc/make-test-images.sh:43
    experiments/mmu-poc/fork/real/make-image.sh:15
    experiments/mmu-poc/programs/make-cpython-image.sh:17
    experiments/mmu-poc/programs/compact-image.sh:12,16
    experiments/mmu-poc/programs/make-image.sh:24,28
    experiments/mmu-poc/programs/image-profiles.py:19
    experiments/mmu-poc/programs/make-neovim-image.sh:32,33
    experiments/mmu-poc/programs/make-shell-tools-image.sh:35,36
    ARCHITECTURE.md:402,414
    docs/platform.md:142,148

Each asserts a rootfs limit 512 KiB larger than the partition it is checked
against, so a rootfs between `0x630000` and `0x6b0000` passes every check and
then fails at flash time. `experiments/mmu-poc/fork/make-image.sh:18` was fixed
as part of this work; the rest are left for a separate pass.
