# Architecture — how Linux and the WiFi firmware share one ESP32-S3

> The native dual-core layout was verified from a clean build for 0.6.
> The current branch adds a tested experimental fork backend and userspace;
> its new combined-image clean-build verification is tracked in
> [build/README.md](build/README.md). Do not conflate the two sets of evidence.

## The big picture: two cores, two operating systems, one chip

Unlike the emulated (RISC-V/Sv32) edition — where a single Xtensa core runs
an emulator that interprets a foreign RISC-V CPU in software — this edition
runs **real Linux compiled directly for the ESP32-S3's own Xtensa
instruction set**. No emulation, no interpretation. The tradeoff: the
ESP32-S3's WiFi radio is driven by a closed binary blob that only runs
under ESP-IDF/FreeRTOS, so Linux can't own it directly. The solution
(`esp-hosted`, upstream jcmvbkbc project) splits the two cores:

```
ESP32-S3 (single chip, two Xtensa LX7 cores)
│
├── Core 0 — ESP-IDF / FreeRTOS ("network_adapter" app)
│   └── Owns the WiFi radio (closed blob) directly.
│       Talks to Core 1 over shared memory (shmem IPC).
│       No Bluetooth: the stack was there only for phone provisioning,
│       which this image does not have, so it is compiled out (see below).
│
└── Core 1 — Linux 7.2.4 (real, native Xtensa binary)
    ├── esp32-ng driver (drivers/net/wireless/espressif/esp32-ng/)
    │   └── espsta0 — STA netdev, joins the home WiFi (wpa_supplicant).
    │                 STA only: the AP side was removed (see below).
    ├── BusyBox services + Bash user shell, nano, Dash and GNU Make
    ├── MicroPython, socat/nc, dtach sessions and process tools
    ├── wifi — interactive scan/connect helper for the STA uplink
    ├── espctl — GPIO/I2C control and a system readout, temperature included
    ├── i2c-gpio on A4/A5, SPI2 on D10-D13, ST7789 panel as /dev/fb0,
    │   and an hwmon driver for the SoC's own temperature sensor
    └── / (cramfs, read-only, XIP) + /etc and /home (jffs2, writable)
```

This is the AMP (asymmetric multiprocessing) architecture the emulated
edition's own planning docs flagged as "high risk, don't start there" —
except `esp-hosted` already built and maintains it upstream, which is why
this edition exists at all.

## Boot flow

1. ROM bootloader → 2nd-stage bootloader → `xipImage` (Linux kernel,
   executes in place from flash, not copied to RAM).
2. Kernel brings up `espsta0` (STA) → fires `CMD_INIT_INTERFACE` to
   Core 0's firmware → `esp_wifi_set_mode(WIFI_MODE_STA)` +
   `esp_wifi_start()`. The driver never creates an AP interface (see below),
   so the firmware stays in plain STA mode.
3. userspace: `S45inetd` (nothing until `remote-login` turns ssh on)
   → `wpa_supplicant` on `espsta0` (via `/etc/network/interfaces`, joining the
   WiFi set with the interactive `wifi` command or `wifi connect "SSID" "PASS"`).
   That step runs in the **background** so it does not hold the login prompt
   (see `S40network`); its output goes to `/var/log/network.log`. With no
   network chosen yet there is no `/etc/wpa_supplicant.conf`, so
   `wpa_supplicant` exits and that log records the failure — the expected state
   of a freshly flashed board, not a fault.
4. No WiFi is configured by default. The board joins a network only when
   someone with a console or an SSH session runs the `wifi` command,
   or writes `/etc/wpa_supplicant.conf` by hand and reboots. A freshly
   flashed board therefore has no network, which is the expected state, not
   a fault.

## Why there is no SoftAP (STA only)

Earlier versions created a second netdev (`espap0`) and let Core 0's firmware
run a SoftAP with a fixed SSID/password. It was removed because it never
worked and actively hurt: the closed WiFi blob beacons as **WEP** instead of
WPA2, so clients reject it, and — worse — bringing the AP up wedged the
firmware's wifi task so `CMD_SCAN_REQUEST` never returned, meaning the STA
could no longer scan or associate.

It was first disabled (the driver was hard-forced never to create `espap0`) and
has since been **removed outright**, on both sides. The board is **STA only**:
it joins an existing network, it does not host one.

- Kernel: the esp32-ng driver is back to its upstream form. Gone are the
  `.start_ap`/`.stop_ap` cfg80211 ops, `cmd_ap_start`/
  `cmd_ap_stop`, the `CMD_AP_*` and `EVENT_AP_*` protocol codes, the
  `esp_ap_password` and `ap_iface` module parameters, and `NL80211_IFTYPE_AP`
  in `wiphy->interface_modes`. `ESP_MAX_INTERFACE` is 1 again.
- Firmware: `cmd.c` is byte-identical to upstream again — `process_ap_start`/
  `process_ap_stop`, `configure_softap_fixed`, the AP half of `wpa_funcs`
  (`hostap_init`, `wpa_ap_*`, `hostap_sta_join`) and the `WIFI_EVENT_AP_*`
  handlers are all gone. WiFi start-up went back to upstream's model too: the
  event loop and `esp_wifi_start()` live in `process_init_interface()` behind
  the `!sta_init_flag` guard, rather than running once at boot — that
  restructuring only ever existed so the AP beacon would carry its RSN element.
- The vendored ESP-IDF is untouched again; it used to have `hostap_sta_join`
  un-`static`ed so the AP authenticator could link against it.
- The firmware is built with `CONFIG_ESP_WIFI_SOFTAP_SUPPORT=n`, so the SDK's
  own AP machinery is not compiled in either. That is a supported ESP-IDF
  configuration, not a hack: with the option off it provides an empty
  `net80211_softap_funcs_init()` that overrides a weak symbol in the closed
  WiFi library, which is built to run STA-only.

Restoring an AP would require both radio-side support and a validated userspace
configuration. The new fork backend removes one earlier userspace obstacle,
but does not restore the removed AP driver/firmware operations or prove that
hostapd fits and works. SoftAP remains unsupported.

## The buses are on the Nano's own pins

A Nano is expected to have I2C on A4/A5 and SPI on D10-D13, so that is where they
are. Both live in `arch/xtensa/boot/dts/arduino-nano-esp32.dts`; nothing about
either bus is compiled into a driver.

| Bus | Pin | GPIO | Notes |
|---|---|---|---|
| I2C (bit-banged) | A4 | 11 | SDA |
| | A5 | 12 | SCL |
| SPI2 | D10 | 21 | CS0 |
| | D11 | 38 | COPI (MOSI) |
| | D12 | 47 | CIPO (MISO) |
| | D13 | 48 | SCK; also the board LED |

There is no ESP32 I2C controller driver in this kernel, so `i2c0` is
`compatible = "i2c-gpio"`, a bit-banger that reads its own `sda-gpios`/`scl-gpios`
properties. Moving those two properties moved the bus; the driver is
pin-agnostic and was not touched.

SPI2 is the harder half. On the S3 the FSPI IO-MUX function is 4 and exists only
on GPIO9-14, so no IO-MUX setting can put the controller on GPIO21/38/47/48. The
four pads go into their plain GPIO function and the FSPI signals are routed to
them through the **GPIO matrix**, using the two pinctrl nodes `esp32s3.dtsi`
already declares for exactly this. The two muxes are not symmetric, and the
asymmetry is easy to get backwards:

- an **output** is selected at the *pad's* register, `GPIO_FUNCn_OUT_SEL_CFG`,
  and the value written is the *signal* index — `FSPICLK_OUT` 101, `FSPID_OUT`
  103, `FSPICS0_OUT` 110;
- an **input** is selected at the *signal's* register, `GPIO_FUNCn_IN_SEL_CFG`,
  and the value written is the *GPIO number* — CIPO on GPIO47 feeds `FSPIQ_IN`
  102.

Writing the bare signal index leaves `OUT_INV`/`OEN_SEL`/`OEN_INV` clear, so the
output enable stays with the peripheral, which is what a SPI bus wants.
`pinctrl-0` on `&spi2` spans three providers (IO-MUX for drive and pull, the two
matrix nodes for the routing) because a pinctrl state holds one setting per
pinctrl device; the driver core applies the default state before probe, so
`spi-esp32.c` needs no pinctrl awareness at all.

One open risk, named rather than discovered later: `spi-esp32.c` drives chip
select itself through `SPI_MISC_REG` and never consults `cs-gpios`. Routing
`FSPICS0_OUT` through the matrix should carry it, but if the peripheral's output
enable does not survive the detour then CS sits wrong and the panel is silent
with no error anywhere. The fallback is a few lines in `esp32_spi_set_cs` to
drive a GPIO when `cs-gpios` is present — which is also the only way a second SPI
device could ever be added.

D3 and D4 are free now that I2C has moved off them, and they are the panel's
control lines — `dc-gpios` and `reset-gpios`. D2 is free too but unused: this
module has no backlight pad to drive (see below).

## The panel is a framebuffer, not a console

`drivers/staging/fbtft/fb_st7789v.c` is already in the pinned kernel and already
describes this controller — this init sequence, this gamma — so the panel is a
device-tree node and some Kconfig, no new driver. It is bound under `&spi2` at
chip select 0.

The panel is a 240x240 ST7789. It was a 128x160 ST7735R first, and the driver
changed with the glass: `fb_st7735r.c` is now off, because nothing in the fbtft
Kconfig makes the two exclusive and a second driver would be dead code in
`xipImage` and a second `compatible` string that could bind the wrong panel.

Four things about the node are not obvious from the binding documentation and
were settled by reading the driver:

- the node needs `buswidth = <8>`. `fbtft_framebuffer_alloc()` merges the
  platform data's buswidth in and then rejects the device with "buswidth is not
  set" when it is 0, and `fb_st7789v.c` sets no default, so the missing property
  is a probe failure rather than a silent default;
- there is **no** `led-gpios` and **no** `cs-gpios`, because this module has
  neither pad. It is broken out as `SCL SDA RES DC` plus power: the backlight is
  tied to VCC on the board, so the light is on whenever the module is powered,
  and CS is tied low, so the panel is selected permanently. A `led-gpios` line
  would make `fbtft_properties_read()` register a backlight device that controls
  nothing, and a `cs-gpios` line would claim a pin the module does not have.
  `reg = <0>` stays regardless — it is what tells the SPI core which chip select
  to claim — and `spi-esp32.c` drives CS itself through `SPI_MISC_REG` without
  ever consulting `cs-gpios`, so the `FSPICS0_OUT` route on D10 is simply
  unconnected;
- `width = <240>` and `height = <240>` are required, and they are bare names, not
  `fbtft,width`. `fb_st7789v.c`'s own display struct says 240x320 — 320 is the
  controller's column count, not this module's, which is a 240x240 window onto
  it. fbtft takes the geometry from the driver and only lets the devicetree
  override it, so without these two the framebuffer comes up 80 rows too tall
  and the bottom of every frame is off the glass. The names are bare because
  `fbtft_properties_read()` reads them with
  `device_property_read_u32(dev, "width", …)`; a vendor prefix is read as absent
  and ignored without a warning;
- the panel is driven in **SPI mode 3**, written as `spi-cpol` and `spi-cpha`.
  Mode 0 leaves it blank: it latches on the wrong clock edge, so every command
  byte arrives shifted and nothing sent to it is understood. This is the mode
  the Adafruit sketch that brought the panel up passes to `tft.init()`, and
  `spi-esp32.c` implements it — CPOL sets `SPI_CK_IDLE_EDGE` in `SPI_MISC_REG`
  and CPHA sets `SPI_CK_OUT_EDGE` in `SPI_USER_REG`.

`CONFIG_VT` and `CONFIG_FRAMEBUFFER_CONSOLE` are **on**, so the panel carries a
console as well as being a canvas: `echo`, `cat` and anything else that writes
to a terminal lands on the screen, and a program can still open `/dev/fb0` and
draw. The serial console is not replaced — `console=ttyS0` and `console=tty1`
are both on the kernel command line, and the kernel prints to every console it
is given, so the boot log comes out of UART0 and the panel at once.

The font is `FONT_6x8`, which gives 40 columns by 30 rows on a 240x240 panel.
It was `FONT_MINI_4x6` while the panel was 128x160 — 4x6 fitted more, but it is
the smallest pixel font in the tree and its glyphs are hard to read on the
glass, so 6x8 was chosen for legibility at the cost of 11 columns and 6 rows.
The wider panel removes the trade rather than forcing it: 6x8 now fits 40
columns against the 32 the old 128-pixel panel could not hold.

`CONFIG_FONTS` has to be on for any of this — it gates the whole font menu and
nothing selects it, and without it `FONT_6x8` is dropped silently and the build
falls back to 8x16, which is the default and 16 columns wide.
`FONT_8x16` is turned off explicitly; it is `default y` and would otherwise be
linked in alongside the font actually used.

`CONFIG_CONSOLE_TRANSLATIONS` is **off**. It costs about 15 KB of Unicode
translation tables. The stubs left behind are correct for this console:
`conv_uni_to_pc` passes ASCII through unchanged, and the only dereference of
`vc_translate` sits inside an `IS_ENABLED()` guard that compiles out.

Input is on for a different reason entirely — the USB host port, below — and
not for the panel. 240x240 RGB565 is 115,200 bytes against the ~4 MB that are
free.

## The USB port is a host, not a console

The S3 has one USB PHY on the die, and two peripherals can drive it: the OTG
controller, and the USB-Serial/JTAG block the ROM flashes the chip over. They
cannot both hold it, and it comes up belonging to Serial/JTAG.

The controller needs no driver of its own — `usb@60080000` carries
`"esp,esp32s3-usb", "snps,dwc2"`, and `drivers/usb/dwc2` already knows the
Synopsys core and takes the `snps,dwc2` fallback match. The missing piece was
the PHY: `dwc2`'s probe calls `devm_phy_get(dev, "usb2-phy")`, the device tree
has a `usb_phy@60008120` node, and with nothing registered to provide it that
call returns `-EPROBE_DEFER` — so the controller waited forever for a driver
that did not exist. `drivers/phy/phy-esp32s3-usb.c` is that driver, and it is
small because the work is: the node's two `reg` windows are `RTC_CNTL
USB_CONF` (0x60008120) and `USB_WRAP OTG_CONF` (0x60039000), and four bits
across them hand the pads over — `pad_enable`, then `phy_sel = 0` for the
internal PHY, then `sw_hw_usb_phy_sel` and `sw_usb_phy_sel`, in ESP-IDF's own
order.

Two properties of that driver are deliberate and worth knowing before changing
it:

- **It writes registers, never eFuses.** Espressif's own path can pin the same
  choice in silicon, and `EFUSE_DIS_USB_OTG_DOWNLOAD_MODE` — already burned on
  modules made before date code 2219 — makes the ROM's USB download mode
  unavailable for good. A register write is undone by a chip reset, which is
  the only reason the board stays flashable over USB. Do not turn this into a
  strap.
- **Every write is read-modify-write.** Both registers have bits set in their
  reset values, the two `clk_force_on` bits in `OTG_CONF` among them, and
  clearing those would stop the wrap's own clock.

The host runs in PIO, not DMA. Linux on this board is given the 8 MB of octal
PSRAM at `0x3d800000`, and the USB core's DMA master is understood to reach
internal SRAM only; a buffer it cannot reach does not produce an error, it
produces whatever the bus returned. At full speed that costs nothing, and
`dwc2_set_esp32s3_params()` in `drivers/usb/dwc2/params.c` records that this is
the line to revisit if the master's window turns out to include PSRAM.

What the port is not: there is no gadget stack, so the board cannot be a USB
device; there is no serial class driver, so a USB-serial adapter enumerates and
binds to nothing; and there is no VBUS regulator anywhere in the device tree, so
the port powers nothing — a device plugged in needs its own supply, which is
what a powered hub provides.

Storage is the exception to that list, and it is the one class driver the port
carries. A USB mass-storage device — a flash drive, or a card reader with a card
in it — binds to `usb-storage`, becomes a SCSI disk, and appears as `/dev/sda`
with its partitions beside it. The block layer, SCSI and the FAT filesystem are
built in for it, which is what the `linux` partition's headroom went on. Mounting
is manual: the board mounts what it is told to, and nothing watches the port.
Only FAT is understood — a drive formatted exFAT or NTFS will enumerate and then
fail to mount, which is a missing filesystem, not a missing driver.

The cost is the console, and it is not avoidable: `ttyGS3` exists only while
the `acm` node is enabled, so that node is off, `console=ttyGS3` is out of the
kernel command line, and `usb-console` and `S47usbconsole` are retired with it.
The console is UART0 on D0/D1, which is where the earlycon and the getty were
pointed all along. The board is still flashed over its USB port, because the
ROM takes the PHY back while the chip is in download mode.

## Temperature, from the SoC's own sensor

`drivers/hwmon/esp32s3-tsens.c` reads the SENS block's TSENS sub-block and offers
it as `temp1_input` in millidegrees. The digital half of the sequence —
`tsens_power_up`, `tsens_clk_div`, `tsens_dump_out`, `tsens_ready`, `tsens_out`,
and the `tsens_clk_en` clock gate — is all in the S3's own register headers.

The bias is not: the range/offset DAC lives in an *analog* register behind the
SoC's internal I2C master, not on any bus this kernel has a driver for. That
sequence is taken from the ROM routine the ESP32, ESP32-S2 and ESP32-S3 share as
`rom_i2c_writeReg()`; only the S2's implementation is published, and the S3's
`ANA_CONFIG`/`ANA_CONFIG2` registers sit at the same addresses that implementation
uses. It is the one part of the driver that cannot be checked against an S3
header, so `probe()` writes the DAC and reads it straight back — a mismatch fails
the probe with an error in dmesg rather than registering a sensor that would
return plausible-looking nonsense.

Two limits are documented in the driver rather than discovered on the bench: the
part is not a precision sensor (Espressif's own calibration table concedes 1-3 °C
of error depending on range; this driver pins the row with the smallest error,
which covers -10..80 °C), and SENS is shared silicon — core 0's ESP-IDF touches
the same analog block during Wi-Fi and ADC calibration and nothing on the Linux
side can arbitrate that. The sensor is therefore powered up, sampled and powered
down again for each read.

`espctl system info` prints the reading, finding the hwmon device by the name its
driver registers rather than assuming `hwmon0`, and omitting the line entirely if
the device is not there.

## Fork and memory banking

Linux still runs in NOMMU mode. The fork patches keep a private page set per
process and move it on scheduling transitions; `/proc/meminfo` and
`/proc/PID/status` expose the associated accounting.

Since 0.8, `swap-banks.patch` is the default (`FORK_SWAP_BANKS=1`, the build
default): a context switch exchanges the resident contents with the incoming
process's set instead of saving and restoring a separate backup, so the
running process owns no backup of its own and N processes sharing a region
need N-1 page sets instead of N. It was suspected of corrupting memory and
built out for a week; that turned out to be incident 15 (the flash cache, not
this), and with that fixed, twenty factory boots ran clean and it shipped --
see `experiments/mmu-poc/fork/README.md` and incident 16 in DEVELOPMENT.md.
`FORK_SWAP_BANKS=0` builds the older save-and-restore copy model instead. The
backend requires UP Linux, rejects multithreaded fork and limits private
memory per fork to 512 KiB by default (`fork_bank_max_bytes`). `libfork.so.0`
exposes the compatible userspace entry points; it does not replace the whole
C library.

There is no copy-on-write, and there cannot be one on this chip: the TRM
(section 15.6) specifies that an unpermitted write is dropped and raises an
asynchronous interrupt, not a restartable fault. Without a fault to copy the
page and retry the store from, COW has nothing to hang on. There is no
hardware isolation either.

`mmu-run` is separate: it loads constrained freestanding Xtensa payloads and
switches owned remap pages. Ordinary Bash/Make/MicroPython processes use the
Linux FDPIC loader and the fork backend, not `mmu-run`.

### Walking a large directory tree costs slab, permanently

`SLUB_TINY` (see the memory section of the kernel config) removes the
`kmalloc-rcl-*` caches: `include/linux/slab.h` sets
`KMALLOC_RECLAIM = KMALLOC_NORMAL` when it is on, so there is no separate
reclaimable kmalloc cache to shrink. `SReclaimable` therefore reads 0, and
nothing that allocates through kmalloc is ever given back to the shrinker.

That matters most for the dentry and inode caches, because a directory walk
creates one of each per node visited. Dentry and inode objects are freed only
by the superblock shrinker, which runs from `shrink_slab()` — reached from
`shrink_node_memcgs()` when kswapd or direct reclaim runs, or from
`drop_slab()` behind `/proc/sys/vm/drop_caches`. On a board whose free memory
sits in a narrow band, that reclaim path is exercised rarely, so the objects
accumulate.

The cost is real and it is not small. Measured on the board, with a USB drive
attached and WiFi down:

```
before:           Slab 2128 kB   MemAvailable 3112 kB
find /sys -name 'fork_bank*' >/dev/null
after:            Slab 3748 kB   MemAvailable 1428 kB
find /home -name 'nosuchfile' >/dev/null
after:            Slab 3748 kB   MemAvailable 1428 kB
```

1620 kB for the walk of `/sys`, and a second `cat /proc/meminfo` immediately
after reads the same 3748 — it is not drift and it does not come back. The
walk of `/home` cost nothing at all: the cost scales with the number of nodes
visited, and a small tree's dentries fit in slab pages that already exist.
`/sys` is the tree on this board that has thousands of nodes, which is why it
is the one that shows the cost.

This is a property of `SLUB_TINY`, not a bug in this image, and it is the
trade the allocator was chosen for: the same option took slab from 3692 kB to
1700 kB at boot, which is a far larger win than the leak costs. It is written
down here so that a program which walks a large tree is understood to be
spending memory it will not get back, and so that "`find /` made the board run
out of memory" is recognised rather than chased as a fork or USB problem.

## Storage layout (arduino-nano-esp32 profile, 16MB flash / 8MB PSRAM)

| Partition | Offset | Size | Contents |
|---|---|---|---|
| `nvs` | `0xa000` | 20K | ESP-IDF NVS (unused by Linux) |
| `phy_init` | `0xf000` | 4K | WiFi PHY calibration data |
| `factory` | `0x10000` | 768K | `network_adapter.bin` (Core 0 firmware) |
| `etc` | `0xd0000` | 448K | jffs2, writable, wear-leveled — `/etc` |
| `linux` | `0x140000` | 3.8125M | `xipImage`, XIP kernel |
| `rootfs` | `0x510000` | 6.1875M | `rootfs.cramfs`, read-only root |
| `home` | `0xb40000` | 4.75M | jffs2, writable — `/home` |

Those seven partitions are all the flash there is; a USB drive is not one of
them. It is mounted from its own device node (`/dev/sda1`) at a directory the
user creates, and it lives on the drive, not on the board. Nothing in this table
changes when one is plugged in.

The authoritative source of this layout is
`new-files/esp-hosted/network_adapter/partition_table.esp32s3.16m8r`, and this
table is a copy of it — if the two disagree, the CSV is right. It matters more
than it looks: the kernel derives the rootfs XIP address from the partition
offset (`0x42000000 + 0x510000`), so flashing `rootfs.cramfs` at the wrong
offset panics the kernel with "Cannot open root device". A booting board prints
both, which is the quickest way to check this table against reality:

```
0x000000510000-0x000000b40000 : "rootfs"
cramfs: checking physical address 0x42510000 for linear cramfs image
```

The layout has been recut several times — `rootfs` grown for curl and its CA
bundle, then again for nano and ncurses; `factory` grown for the BLE stack;
`linux` and `home` shrunk to pay for it. Moving `rootfs` means the kernel's XIP
address moves with it, and moving `linux` means `CONFIG_KERNEL_LOAD_ADDRESS`
(`0x42000000 +` the `linux` offset) has to move too, or the board boot-loops in
the bootloader before Linux prints anything.

The most recent recut moved 64K from `rootfs` to `linux`, for the framebuffer
console: fbcon and the 4x6 font cost about 69 KB and the `linux` partition had
68,216 B free, so it did not fit by roughly a kilobyte. `rootfs.cramfs` is
6,209,536 B in a 7,077,888 B partition, so the 848 KiB it was not using paid for
it without anything else moving. Note which offsets did **not** change: `linux`
still starts at `0x140000`, so `CONFIG_KERNEL_LOAD_ADDRESS` is still
`0x42140000` and only the size grew. Had the offset moved, the load address
would have had to move with it.

`/` is read-only cramfs by design — no wear on the root filesystem no
matter how the system is used. Anything that needs to persist (WiFi
credentials, init scripts, host keys) lives under `/etc`, and user data under
`/home` — both separate writable jffs2 partitions mounted **over** the cramfs.
Note the consequence: a file baked into the image under `/etc` or `/home` is
shadowed at runtime by the jffs2 mount, so it only shows up on a freshly
flashed board.

## Security model

- SSH (`dropbear`) requires a real login
  (`/etc/shadow`, SHA-256). Default password `arduino`. The image never
  prompts for a new one, so run `passwd` yourself before the
  board is reachable from anywhere you do not trust.
- No session timeout, no brute-force throttling yet.
- Nothing listens on the network until someone turns it on with
  `remote-login ssh`, and `remote-login off` closes it again. SSH is
  encrypted
  on the wire. `remote-login port` and `remote-login auth` set the
  port and whether SSH takes passwords, keys or both; the settings sit in
  `/etc/remote-login.conf` and inetd.conf is written from them.

## Known gaps

Recovery mode and OTA are not implemented. GitHub Actions exists for the
earlier base pipeline; it is not hardware-in-the-loop verification of every
new image. There is no SoftAP — the
board is STA only (see above). curl's HTTPS support works but is experimental.
