# The platform, end to end

How this image is put together and how it gets onto the board, from the ROM
up. Written for whoever picks this up next — including whoever comes back to
it months later and has to re-derive the boot chain from scratch.

Everything here was read out of the tree or the running board, not recalled.
Where a fact has a single authority, the file is named.

## The board

Arduino Nano ESP32. One chip: **ESP32-S3, N16R8** — 16 MB flash, 8 MB octal
PSRAM, two Xtensa LX7 cores.

Two cores is not two computers. There is one chip, and the two cores do not
share an operating system.

| | core 0 | core 1 |
|---|---|---|
| runs | ESP-IDF / FreeRTOS | Linux 7.2.4, NOMMU |
| owns | the WiFi radio, flash writes, the RSA block | your shell, every process |
| built from | `new-files/esp-hosted/` + `patches/02-firmware-network-adapter.patch` | `patches/linux/` (61 patches) |

Core 0 is the hardware driver. Core 1 is the computer. They talk over shared
memory and software interrupts (`esp32-wifi-shmem`, `drivers/misc/esp32-ipc`).
Linux never touches the WiFi registers; it asks core 0 to.

This split is the reason several things look odd. WiFi dies if core 0's
firmware dies. Flash writes go through core 0, so the flash cache is a
cross-core object (see `DEVELOPMENT.md` incident 15). The temperature sensor
is shared silicon nothing on the Linux side can arbitrate.

## The boot chain

This is the part that is not obvious from any single file, and the part worth
re-deriving first if something stops booting.

```
ROM bootloader            in silicon, cannot be changed
  reads the partition table at 0x8000
  loads bootloader.bin at 0x0
    │
    ├─ bootloader.bin     ESP-IDF 2nd stage, 17,792 B, built as part of the
    │                     network_adapter project at
    │                     $NA/build/bootloader/bootloader.bin
    │  picks partition "factory" (type app, 0x00), verifies the header and
    │  checksum, maps it, jumps
    │
    ├─ network_adapter.bin   runs on core 0, app_main()
    │  ...
    │  linux_boot()
    │    xTaskCreatePinnedToCore(linux_task, ..., core 1)
    │    map_flash_and_go()
    │      init_linux_mmap()        maps PSRAM and flash into the MMU
    │      find_partition("linux")  0x42000000 + 0x140000
    │      parse_cmdline()          reads /etc/cmdline out of the jffs2
    │      builds bp_tag[] in RAM
    │      asm("mov a2, %1 ; jx %0")
    │
    └─ xipImage            Linux, executing in place from flash
```

**Core 0's firmware jumps to Linux. The IDF bootloader does not.**

The IDF bootloader only looks at partitions of type `app` (0x00). `linux`,
`etc`, `rootfs` and `home` are type `0x40` with subtypes `0x0`/`0x1` — a
custom namespace it ignores entirely. So the jump is core 0's job, and it
happens in `linux_boot.c`:

```c
asm volatile ("mov a2, %1 ; jx %0" :: "r"(linux), "r"(tag) : "a2");
```

`a2` is the Xtensa ABI's first argument register, and it is the kernel's
entry contract: `arch/xtensa/kernel/head.S:36` says "a2 contains either 0 or a
pointer to a list of boot parameters." `head.S:58` saves it with
`wsr a2, excsave1`, `head.S:275` puts it back into `abi_arg0`, and
`init_arch(bp_tag_t *bp_start)` walks it.

### The boot parameter list

`map_flash_and_go()` builds this array on the stack of core 0's task:

```c
struct bp_tag tag[N_TAGS] = {
    [0]         = {.id = BP_TAG_FIRST},
    [1]         = {.id = BP_TAG_LAST, .size = CMDLINE_MAX},
    [N_TAGS - 1]= {.id = BP_TAG_LAST},
};
```

`tag[1]` is a placeholder that `parse_cmdline()` overwrites with
`BP_TAG_COMMAND_LINE` if `CONFIG_LINUX_COMMAND_LINE` is on and the `etc`
partition holds a readable `cmdline` inode. It is on
(`sdkconfig.defaults.esp32s3.16m8r:436`). So the kernel command line is not
compiled into the device tree in the shipping image — it is read off the
board's own jffs2 at boot.

The kernel side handles all of this already, upstream, with no Linux-side
work needed:

| | |
|---|---|
| `arch/xtensa/kernel/head.S:36,58,275` | the `a2` contract, save and restore |
| `arch/xtensa/kernel/setup.c:232` | `init_arch(bp_tag_t *bp_start)` |
| `arch/xtensa/kernel/setup.c:139` | `parse_bootparam()` — needs `BP_TAG_FIRST` … `BP_TAG_LAST` |
| `arch/xtensa/kernel/setup.c:121` | `parse_tag_fdt()` — a `BP_TAG_FDT` tag **replaces** `__dtb_start` |
| `arch/xtensa/kernel/setup.c:58` | `dtb_start = __dtb_start` — the built-in DTB is the fallback |
| `arch/xtensa/include/asm/bootparam.h` | the tag definitions |

Tags that exist: `BP_TAG_COMMAND_LINE` 0x1001, `BP_TAG_INITRD` 0x1002,
`BP_TAG_MEMORY` 0x1003, `BP_TAG_SERIAL_BAUDRATE` 0x1004,
`BP_TAG_SERIAL_PORT` 0x1005, `BP_TAG_FDT` 0x1006, `BP_TAG_FIRST` 0x7B0B,
`BP_TAG_LAST` 0x7E0B.

`BP_TAG_FDT` is defined in the firmware's own `bootparam.h` and **nothing
emits it**. That is the entire gap between what ships and a device tree
supplied at boot: build the tag, put the DTB's physical address in `data[0]`.
The kernel already knows what to do with it.

`CONFIG_PARSE_BOOTPARAM` is the switch that compiles `parse_bootparam()` in.
It is not named at all in `nano_esp32_linux.config`; the upstream esp32
defconfig in patch 0039 has it explicitly off. Turning it on is safe — a
kernel with it on still boots identically when `a2 == 0`, because the built-in
DTB stays as the fallback.

## Flash layout

Seven partitions. The authoritative source is the CSV,
`new-files/esp-hosted/network_adapter/partition_table.esp32s3.16m8r`; if
anything else disagrees with it, the CSV is right.

| partition | offset | size | contents |
|---|---|---|---|
| *(bootloader)* | `0x0` | — | `bootloader.bin` |
| *(table)* | `0x8000` | — | `partition-table.bin` |
| `nvs` | `0xa000` | 20 K | |
| `phy_init` | `0xf000` | 4 K | |
| `factory` | `0x10000` | 768 K | `network_adapter.bin` — core 0 |
| `etc` | `0xd0000` | 448 K | jffs2, writable, mounted at `/etc` |
| `linux` | `0x140000` | 3.8125 M | `xipImage` — the kernel, XIP |
| `rootfs` | `0x510000` | 6.1875 M | `rootfs.cramfs`, read-only `/` |
| `home` | `0xb40000` | 4.75 M | jffs2, writable, mounted at `/home` |

Two consequences worth stating plainly, because both have bitten:

- **The kernel derives the rootfs XIP address from the partition offset**
  (`0x42000000 + 0x510000`). Flashing `rootfs.cramfs` at the wrong offset
  panics with "Cannot open root device".
- **Moving `linux` means moving `CONFIG_KERNEL_LOAD_ADDRESS`** with it, or the
  board boot-loops in the bootloader with a Guru Meditation before Linux
  prints anything. `package-final.py` asserts the two agree and refuses to
  package otherwise.

`/` is read-only cramfs by design. Anything that persists lives under `/etc`
or `/home`, both jffs2, both mounted **over** the cramfs. A file baked into
the image under `/etc` or `/home` is therefore shadowed at runtime and only
shows up on a freshly flashed board.

## The build

Five stages, in a pinned Debian container, driven by
`build/container-build.sh`. Stage markers live in `stages/*.done` so a rerun
resumes instead of recompiling the toolchain.

| stage | what it produces |
|---|---|
| `toolchain` | crosstool-NG + `xtensa-dynconfig` + `config-esp32s3`. Hours. Depends only on `build/sources.lock`. |
| `espctl` | compiles `espctl.c` into `rootfs_overlay/usr/bin/` |
| `base-rootfs` | Buildroot → `rootfs.cramfs`, `etc.jffs2`, and the kernel tree |
| `firmware` | ESP-IDF → `network_adapter.bin`, `bootloader.bin` |
| `userspace` | the fork kernel → `xipImage`; bash, dash, make, micropython, socat, dtach |
| `package` | `package-final.py` → `artifacts/` and the 16 MB combined image |

`build/reproduce-macos.sh` is the macOS entry point and differs from
`build/reproduce.sh` in four ways that matter:

- it snapshots the **working tree**, not HEAD, so uncommitted edits build
- the work directory persists, so stages resume
- `--platform linux/amd64`, because the pinned base image digest is amd64
- `refs/` and `out/` live on Docker volumes, because APFS is case-insensitive
  and crosstool-NG refuses to build on such a filesystem — and because the
  kernel tree contains names differing only in case
  (`xt_DSCP.h`/`xt_dscp.h`), which no copy flag can fix

The hinge of the whole build is two lines in `patches/00-esp32-linux-build.patch`:

```sh
[ -x ../apply-local-changes.sh ] && ../apply-local-changes.sh buildroot
[ -x ../apply-local-changes.sh ] && ../apply-local-changes.sh esp-hosted
```

Without them the build silently produces the upstream project. The firmware
half commits locally, because the firmware's `CMakeLists.txt` runs
`git reset --hard` on every `cmake` invocation and only a commit survives it.

Kernel patches do **not** go through that script. They go through
`BR2_LINUX_KERNEL_PATCH` in the defconfig, applied in lexical order.

### What the packager refuses

`build/package-final.py` is the gate, and every check in it exists because
something shipped wrong once:

- firmware/kernel vector-address mismatch (`CONFIG_VECTORS_ADDR` vs
  `space_for_vectors` in the firmware ELF)
- `CONFIG_KERNEL_LOAD_ADDRESS` ≠ `0x42000000 + linux offset`
- `partition-table.bin` ≠ the CSV
- any image larger than its partition
- dropbear private host keys in the image
- `www-data` present
- a path in `retired-paths.txt` surviving in the cramfs
- `/bin/busybox` not setuid

Then `esptool merge_bin` assembles exactly 16 MiB, and the packager reads the
result back and asserts every partition sits at its offset with the right
bytes.

## Storage and filesystems

| mount | device | fs | writable |
|---|---|---|---|
| `/` | `mtd:rootfs` | cramfs | no |
| `/etc` | `mtd:etc` | jffs2 | yes |
| `/home` | `mtd:home` | jffs2 | yes |
| `/tmp`, `/run`, `/dev/shm` | — | tmpfs | RAM, uncapped |

`tmpfs` cannot be capped on this kernel: `SHMEM` depends on MMU, so what
answers to `tmpfs` is the tiny-shmem stub over ramfs, whose only mount option
is `mode`. A `size=` is rejected and the filesystem does not mount at all.
Keep large files on `/home`.

## The memory model

Linux runs **NOMMU**. There is no MMU in the Linux sense — no page tables, no
copy-on-write, no memory protection between processes.

Fork is provided by a software backend (`swap-banks.patch`, the default):

- a context switch **exchanges** resident contents with the incoming
  process's page set, rather than save/restore
- N processes sharing a region need N−1 page sets instead of N
- UP-only, no multithreaded fork, private memory per fork capped at 512 KiB
  (`fork_bank_max_bytes`)
- tunables are `module_param`s under `/sys/module/nommu/parameters/` — the
  module is `nommu`, not `nommu_bank`

**Copy-on-write is not possible on this chip and cannot be made possible.**
The TRM (§15.6) specifies that an unpermitted write is *dropped* and raises
an asynchronous interrupt — not a restartable fault. There is nothing to
catch, copy a page, and retry the store from.

`mmu-run` is separate: it loads constrained freestanding Xtensa payloads and
switches owned remap pages.

## The buses

| bus | pins | notes |
|---|---|---|
| I2C | A4 = GPIO11 (SDA), A5 = GPIO12 (SCL) | bit-banged, `compatible = "i2c-gpio"`; there is no ESP32 I2C controller driver in this kernel |
| SPI2 | D10 = GPIO21 (CS0), D11 = GPIO38 (COPI), D12 = GPIO47 (CIPO), D13 = GPIO48 (SCK) | ST7789 panel |
| UART0 | D0 = GPIO44 (RX), D1 = GPIO43 (TX) | the console, `console=ttyS0` |

The S3's FSPI IOMUX function is 4 and reaches only GPIO9–14, so the four SPI
pads go into their plain GPIO function and the FSPI signals are routed through
the **GPIO matrix**. The two muxes are not symmetric:

- an **output** is selected at the *pad's* register `GPIO_FUNCn_OUT_SEL_CFG`,
  taking the *signal* index (`FSPICLK_OUT` 101, `FSPID_OUT` 103,
  `FSPICS0_OUT` 110)
- an **input** is selected at the *signal's* register `GPIO_FUNCn_IN_SEL_CFG`,
  taking the *GPIO number* (CIPO on GPIO47 feeds `FSPIQ_IN` 102)

**Known open risk:** `spi-esp32.c` drives chip select itself through
`SPI_MISC_REG` and never consults `cs-gpios`. If the peripheral's output
enable does not survive the matrix detour, CS sits wrong and the panel is
silent with no error anywhere. The fallback is a few lines in
`esp32_spi_set_cs` to drive a GPIO when `cs-gpios` is present — which is also
the only way a second SPI device (an SD card) becomes possible.

## The panel

240×240 ST7789 on SPI2 CS0, `drivers/staging/fbtft/fb_st7789v.c`, as both
console and canvas. Four non-obvious node properties, all in
`docs/st7789-panel-findings.md`:

- `buswidth = <8>` — without it probe fails with "buswidth is not set"
- `width = <240>` and `height = <240>` as **bare names**, not `fbtft,width`,
  because `fbtft_properties_read()` uses `device_property_read_u32(dev,
  "width", …)`
- `fb_st7789v.c`'s own struct says 240×320 (the controller's column count), so
  without the overrides the framebuffer is 80 rows too tall
- SPI **mode 3** via `spi-cpol` + `spi-cpha`; mode 0 leaves it blank

No `led-gpios` and no `cs-gpios` — the module has neither pad. Backlight tied
to VCC, CS tied low. `CONFIG_VT` and `CONFIG_FRAMEBUFFER_CONSOLE` are on;
`console=ttyS0` and `console=tty1` both on the cmdline. Font `FONT_6x8` =
40 columns × 30 rows. 240×240 RGB565 = 115,200 bytes.

**The serial console is the default console and stays that way.** The panel is
an addition to it, never a replacement.

## The USB port

The port is a **host**, not a console. One USB PHY on the die, two peripherals
that can drive it (the OTG controller and USB-Serial/JTAG), and it comes up
belonging to Serial/JTAG. Giving it to OTG is what costs the image `ttyGS3`.

`drivers/phy/phy-esp32s3-usb.c` is the PHY driver. Two `reg` windows —
`RTC_CNTL USB_CONF` at `0x60008120` and `USB_WRAP OTG_CONF` at `0x60039000` —
and four bits: `pad_enable`, `phy_sel = 0`, `sw_hw_usb_phy_sel`,
`sw_usb_phy_sel`.

**It writes registers, never eFuses.** `EFUSE_DIS_USB_OTG_DOWNLOAD_MODE` is
already burned on modules made before date code 2219 and would make the ROM's
USB download mode unavailable for good. A register write is undone by a chip
reset, which is the only reason the board stays flashable over USB. **Every
write is read-modify-write** — both registers have bits set in their reset
values.

The host runs in **PIO, not DMA**, because Linux's RAM is the 8 MB octal PSRAM
at `0x3d800000` and the USB DMA master is understood to reach internal SRAM
only. `dwc2_set_esp32s3_params()` in `drivers/usb/dwc2/params.c` records that
this is the one line to revisit if that turns out to be wrong. The FIFO split
there (rx 152 / nptx 16 / ptx 32 = 200) is ESP-IDF's own RX-biased table.

Storage is the one class driver the port carries: `usb-storage` → SCSI disk →
`/dev/sda`. FAT only, manual mount, no auto-mount. **The port powers
nothing** — no VBUS regulator exists anywhere in the device tree — so a
bus-powered stick needs a powered hub.

## Getting it onto the board

```sh
./flash.sh -p /dev/cu.usbmodem101 --images build-output/reproduce.macos/artifacts --before no_reset
```

**The board cannot enter download mode on its own from its USB port.** It must
be put there by hand: hold BOOT, tap RST, release BOOT. Hence `--before
no_reset` on every flash.

`flash.sh --backup` writes `etc.jffs2.bak` and `home.jffs2.bak`, which hold
account password hashes and WiFi credentials. **Keep them private. Never
commit them.** `.gitignore` carries the rule.

`--parts --erase` destroys everything in `/home` and `/etc` on the board.

## Verifying it

Three tools drive the board over the serial console. None of them flashes the
kernel or the rootfs.

| tool | what it does |
|---|---|
| `build/test-board.py PORT ARTIFACTS --output DIR` | the suite; ties itself to the exact image by reading the kernel and rootfs partitions back and hashing them |
| `build/extra-board-tests.py PORT [DIR]` | what a person does and the suite does not: a detached session, `passwd` and a fresh login, a reboot that keeps `/home`, jffs2 written and read back, the shell under fork load |
| `build/soak-boot.py PORT ARTIFACTS --output DIR --rounds N` | the factory-boot fault rate; each round rewrites `/etc` and `/home`, holds the board in reset until the port is listening, watches the boot, then logs in and reads `dmesg` and the taint flags back |

A `quiet` console shows KERN_ERR and worse only, so the console alone cannot
decide a round. `soak-boot.py` reads the ring buffer and the taint flags back,
and a round is PASS only on positive evidence.

`build/test-ssh-pty.py` and `build/test-network-services.py` drive the board
over the network instead, and need `paramiko` in addition to `pyserial`.

The fork backend is built by
`experiments/mmu-poc/fork/build-kernel-reclaim.sh` with the page-set exchange
on; `FORK_SWAP_BANKS=0` builds the copying model.

## Where the pieces live

```
patches/                      diffs against the three upstream clones
  00-esp32-linux-build.patch  the two apply-local-changes.sh calls (the hinge)
  02-firmware-network-adapter.patch
  03-buildroot-tracked-changes.patch
new-files/
  toplevel/apply-local-changes.sh
  configs/                    the Buildroot defconfig
  board/espressif/esp32s3/
    nano_esp32_linux.config   the kernel config
    busybox.config
    patches/linux/            61 kernel patches, applied in lexical order
    rootfs_overlay/           everything that lands in the image's filesystem
    espctl/                   the board's own userspace program (compiled, not committed)
    fbtest/                   panel programs
  esp-hosted/network_adapter/
    partition_table.esp32s3.16m8r   the authoritative partition table
    sdkconfig.defaults.esp32s3.16m8r
kernel-driver-esp32-ng/       readable reference copy of the WiFi driver
experiments/mmu-poc/          the fork backend, mmu-run, userspace programs
build/                        the pipeline, the packager and the test tools
images/                       the released 0.10.0 binaries
```

The repo is a **delta**, not a full source tree. Everything under `patches/`
is a diff against a pinned upstream clone; everything under `new-files/` is
copied in whole.

## Traps, collected

Each of these cost real time once. They are here so they cost it once.

- **`CONFIG_VECTORS_ADDR` must match `space_for_vectors` in the firmware ELF.**
  Linux writes its exception vectors over firmware memory. If they disagree
  the board dies the instant it executes userspace — "Boot stops right at
  `Run /sbin/init`". The address is chosen by the firmware's linker so it
  moves whenever the firmware's size changes. `make-images.sh` detects and
  realigns it; `package-final.py` refuses a mismatch.
- **One flash cache, two cores.** ESP-IDF's post-write invalidation asks its
  own MMU accounting whether it knows the page, and it never knows a partition
  Linux mapped for itself. Nothing invalidated the flash cache after a jffs2
  write, so jffs2 read stale pages back. The fix is
  `Cache_Invalidate_Addr(0x42000000 + addr, size)` in the firmware's
  `linux_flash.c` — the ROM call, which takes a virtual address and does not
  ask who mapped it. `DEVELOPMENT.md` incident 15.
- **`find /sys` costs 1620 kB of slab, permanently.** `SLUB_TINY` sets
  `KMALLOC_RECLAIM = KMALLOC_NORMAL`, so `SReclaimable` reads 0 and dentry and
  inode objects are freed only by the superblock shrinker. Documented in
  `ARCHITECTURE.md` and `TROUBLESHOOTING.md`.
- **Opening `/dev/cu.usbmodem101` resets the board.** Do not touch it while
  anyone is logged in.
- **The RSA driver's wait loop** has no timeout in the original; patch 06 gave
  it 100 ms. Without it the boot stops dead with nothing printed.
- **The default login password is `arduino`** and nothing prompts a change.
  Run `passwd` before the board is reachable from a network you do not
  control. `remote-login off` closes the way in.
