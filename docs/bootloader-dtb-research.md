# External DTB and U-Boot feasibility

2026-11-17. Research only; existing bootloader/firmware not modified.

The current board's full merged tree is archived at
`build-output/wifi-smp-audit/wlan0-user-control/current-device-tree.dts`.
It was decompiled from the exact flashed DTB, not reconstructed from includes.

## Findings

Upstream U-Boot was inspected at
`83b1ec64ede9998566e6a82b22848c20a33f4aff`. Its Xtensa target menu contains
XTFPGA and QEMU Xtensa Virt. That provides architecture support but no existing
ESP32-S3 target in that menu. A basic S3 U-Boot needs a board/SoC port and boot
packaging, flash/PSRAM/cache/clock/console setup, correct Xtensa core overlay,
and the Linux handoff/vector reservation contract. It is not a defconfig swap.

Its `arch/xtensa/lib/bootm.c` already emits BP_TAG_FDT, and the configured
Linux kernel's `arch/xtensa/kernel/setup.c:parse_tag_fdt` parses that tag.
Thus an external DTB can be supplied using an existing Xtensa boot protocol.
This feature does not inherently require U-Boot.

The smaller first implementation is to extend the existing IDF-based handoff:
load a validated external DTB, keep it alive/reserved through early Linux
initialization, and put its address into BP_TAG_FDT before entering Linux.
Keep the compiled DTB as recovery fallback. A working flash `/etc/board.dtb`
would be a convenient storage location, or a separate read-only DTB slot could
decouple the mechanism from JFFS2. Neither is implemented in the present image.

The actual removed firmware excerpt visibly loaded `/etc/cmdline`. Older
repository prose also mentions `/etc/board.dtb`; verify its exact original
reader/entry path before claiming that code can simply be switched back on.
The early tiny-JFFS2 reader's compression limitations matter if it supplies
the DTB: a compressed JFFS2 node cannot be treated as raw FDT data.

## Required validation

- Check FDT magic, bounded totalsize, version/structure and storage read errors.
- Reserve a DTB buffer outside live kernel data/stack/XIP ranges; validate its
  lifetime across data relocation, memblock initialization and tree unflattening.
- Preserve the boot memory map, Linux flash entry, vectors, CPU startup and
  required peripheral ownership. An external tree can still describe them wrong.
- Verify missing/corrupt DTB fallback, bootargs override behavior and ability
  to recover without a working writable filesystem.
- Any changed handoff ELF requires rechecking `space_for_vectors`, kernel
  CONFIG_VECTORS_ADDR and every occupied SRAM/I-bus alias. Do not flash a
  newly linked firmware alongside the old kernel without those checks.

Recommendation: implement external FDT loading in the existing handoff after
the writable-flash path is stable; assess a complete U-Boot port separately.
This meets the requested independent-device-tree goal with less new startup
code, without ruling out U-Boot later.

## Primary sources

- https://docs.u-boot.org/en/stable/arch/xtensa.html
- https://github.com/u-boot/u-boot/blob/83b1ec64ede9998566e6a82b22848c20a33f4aff/arch/xtensa/Kconfig
- https://github.com/u-boot/u-boot/blob/83b1ec64ede9998566e6a82b22848c20a33f4aff/arch/xtensa/lib/bootm.c

Pinned upstream source snapshots are in `build-output/flash-rw-research/uboot/`.
