# Linux 7.2.4 for the Arduino Nano ESP32

Native dual-core Xtensa LX7 Linux for the **Arduino Nano ESP32 (ABX00083)**
with the **u-blox NORA-W106-10B** module. The board has 16 MiB of flash and
8 MiB of octal PSRAM. The kernel runs at 240 MHz and executes in place from
flash.

This fork builds on [Paulneja’s Linux-on-ESP32-S3](https://github.com/paulneja/Linux-on-esp32-S3).
The firmware settings, pin assignments, and flash layout target the Nano.

## Quick start

Download a full factory image and its checksum from [Releases](https://github.com/GlowingObsidian/Linux-on-esp32-S3/releases).

| Image | Hardware | Flash and PSRAM |
| --- | --- | --- |
| ESP32-S3 DIO 80 | ESP32-S3 N16R8 with 16 MiB SPI NOR and 8 MiB octal PSRAM | 80 MHz DIO flash and 80 MHz octal PSRAM |
| Nano ESP32 QIO 120 | Arduino Nano ESP32 (ABX00083) | 120 MHz QIO flash and 120 MHz octal PSRAM, experimental |

The DIO image uses UART0 on GPIO43/44 and omits the Nano-specific RGB LED
and I²C pin assignments. Automatic NOR write support covers JEDEC
`c84018` and `ef4018`. Other chips fall back to read-only factory defaults.
ESP-IDF uses an 80 MHz DOUT ROM header for the QIO profile and selects
120 MHz QIO after ROM startup. The 120 MHz octal PSRAM timing is temperature
sensitive. [Espressif MSPI timing guide](https://docs.espressif.com/projects/esp-idf/en/v5.1/esp32s3/api-guides/flash_psram_config.html)

Use a clean install for the factory layout. Use matching artifacts from
the same build. To build the image locally, clone the source with Git and
use Docker.

```sh
git clone https://github.com/GlowingObsidian/Linux-on-esp32-S3.git
cd Linux-on-esp32-S3
bash build/reproduce-macos.sh   # macOS
# bash build/reproduce.sh      # Linux
```

See the [build and flashing guide](build/README.md). A full-chip flash
replaces persistent state. Use UART0 on D0/D1 at **115200 8N1** for the
Linux console. The onboard USB port can enter ROM download mode and
becomes a USB host after Linux boots.

The writable-state image mounts `/etc` and `/home` automatically after
checking flash and cache coordination at boot. It falls back to read-only
factory defaults if those checks or the state mounts fail.

Log in as `root` with the factory password `arduino`. Run `wifi` to join a
network and `remote-login ssh` to enable SSH. The fresh image ships an empty
`/home/root`.

```sh
coremark          # two cores
coremark-single
iperf3 --version
fastfetch
```

## Board measurements

Measured on the earlier 80 MHz Nano ESP32 image with Wi-Fi and SSH off
and no USB peripherals attached.

| Measurement | Result |
| --- | --- |
| Boot to UART login | About **5 s** from the ROM banner after a software reboot |
| RAM before CoreMark | **4,176 KiB available** of **7,772 KiB** |
| CoreMark, two cores | **990.28 iterations/s** |
| CoreMark, one core | **546.61 iterations/s** |

Both CoreMark runs passed CRC validation. Boot timing is a single UART
capture with about 1 s logging granularity. Measurements use the
board-tested writable-state profile.

## Porting progress

### System

| Feature | Status | Notes |
| --- | --- | --- |
| Linux boot and flash XIP | Working | Linux 7.2.4 on Xtensa NOMMU. |
| Dual-core SMP | Working | Cross-core atomics, CPU migration, and stress tests passed. |
| Forkbank | Working | Fork and exec, process isolation, and CPU placement tested. |
| Onboard flash storage | Working | Read-only cramfs and a board-tested writable JFFS2 profile. |
| Bash, BusyBox, and MicroPython | Working | Included in the factory userspace. |

### Networking

| Feature | Status | Notes |
| --- | --- | --- |
| Wi-Fi station | Working | Saved network autojoin and manual connection. |
| SSH | Working | Dropbear, enabled with `remote-login ssh`. |
| Wi-Fi throughput and long-run stability | In progress | Receive queue and transmit buffer work. |
| Bluetooth | Disabled | Omitted from the build. |

### Peripherals

| Feature | Status | Notes |
| --- | --- | --- |
| GPIO | Working | Nano pin names through `espctl gpio`. |
| UART0 | Working | Boot log and login console on D0/D1. |
| On-chip temperature sensor | Working | Reports ESP32-S3 die temperature through `hwmon` as `temp1_input` in millidegrees Celsius. |
| Onboard RGB LED | Working | Red, green, and blue channels under `/sys/class/leds/`. |
| USB host | Working | HID and FAT storage. |
| SPI2 | Working, disabled by default | Tested on the Nano header. |
| ST7789 240×240 | Working, disabled by default | Tested as a framebuffer and boot console. |
| ST7735R 128×160 | Working, disabled by default | Tested as a framebuffer and console. |
| Hardware RSA | Working | Kernel Crypto API driver with boot self-tests. |
| GDMA | In progress | General-purpose DMA integration. |

### Enabling SPI2 and the displays

Build a display-enabled image. In the Nano device tree, set `&spi2` to
`status = "okay"` and keep `CONFIG_SPI_ESP32=y`. Wire SPI2 to the Nano
header using **D13 SCK**, **D11 MOSI**, **D12 MISO**, and **D10 CS** as
needed by the device. Use the [Nano panel device-tree patch](new-files/board/espressif/esp32s3/patches/linux/0063-esp32s3-dts-nano-pin-map-st7789-panel-temp-node.patch)
as the starting point.

- **ST7789 240×240** — Keep the supplied `sitronix,st7789v` panel node
  and enable `CONFIG_FB_TFT_ST7789V`. The tested module used SPI mode 3,
  D3 for D/C, and D4 for reset.
- **ST7735R 128×160** — Use the [tested panel node from the earlier build](https://github.com/paulneja/Linux-on-esp32-S3/blob/7353354/new-files/board/espressif/esp32s3/patches/linux/0063-esp32s3-dts-nano-pin-map-st7735-panel-temp-node.patch)
  with `compatible = "sitronix,st7735r"` and enable
  `CONFIG_FB_TFT_ST7735R`. That module used D3 for D/C, D4 for reset,
  and D2 for backlight.

For boot text, keep `CONFIG_VT_CONSOLE=y` and
`CONFIG_FRAMEBUFFER_CONSOLE=y`, add `console=tty1` alongside UART0 in the
kernel command line, and retain the tty1 getty for a login prompt.
For general SPI use, declare the attached device in place of the panel node.
The board-tested writable Wi-Fi profile assigns SPI2's former SRAM region
to Wi-Fi. Use the display configuration with its separate SRAM assignments.

## Documentation

- [Arduino Nano ESP32 hardware manual](https://docs.arduino.cc/resources/datasheets/ABX00083-datasheet.pdf)
- [Build and flashing guide](build/README.md)
- [SMP and forkbank](docs/forkbank-smp.md)
- [Wi-Fi driver and board measurements](docs/wifi-smp-bringup.md)
- [Flash and filesystem layout](docs/flash-filesystem-memory.md)
- [Changelog](CHANGELOG.md) and [license](LICENSE)
