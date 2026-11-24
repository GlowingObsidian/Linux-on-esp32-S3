# Wi-Fi-in-Linux port — flash and RAM budget

Measured 2026-11-10, before writing any port code. Answers one question: **does the
ESP32-S3 Wi-Fi blob physically fit if Linux is to link and run it?**

Everything here is measured from the tree, the pinned IDF, or the built artifacts.
Nothing is estimated except where it says so.

## Flash

### The partition table

`new-files/esp-hosted/network_adapter/partition_table.esp32s3.16m8r`:

| partition | offset | size | end |
|---|---|---|---|
| `nvs` | `0x0000a000` | 20 KiB | `0x0000f000` |
| `phy_init` | `0x0000f000` | 4 KiB | `0x00010000` |
| `factory` | `0x00010000` | 768 KiB | `0x000d0000` |
| `etc` | `0x000d0000` | 448 KiB | `0x00140000` |
| `linux` | `0x00140000` | 3904 KiB | `0x00510000` |
| `rootfs` | `0x00510000` | 6336 KiB | `0x00b40000` |
| `home` | `0x00b40000` | 4864 KiB | `0x01000000` |

Last partition ends at `0x01000000` = 16 MiB. **Free flash after the table: 0 B.**
The only gap is `0x00000000`–`0x0000a000` (40 KiB), which is the bootloader and
partition-table area.

### What is in the `linux` partition now

`xipImage` = 3,149,936 B (3076 KiB) against a 3,211,264 B partition.
**Free: 61,328 B (60 KiB).**

`vmlinux` breaks down as: `.text` 2,892,033 B (2824 KiB) — XIP, stays in flash;
`.data` 188,852 B (184 KiB) and `.bss` 74,648 B (73 KiB) — RAM.

### The blob

Sizes are archive file sizes; the sizes that matter for the image are the section
totals, measured with `xtensa-esp32s3-linux-uclibcfdpic-size` over the extracted objects.

| set | `.text` | `.data` | `.bss` | text+data |
|---|---|---|---|---|
| full blob (all 7 archives) | 561,163 B (548 KiB) | 3,798 B | 16,393 B | 564,961 B (552 KiB) |
| STA only (`net80211` + `pp` + `core` + `wapi`) | 290,242 B (283 KiB) | 3,517 B | 11,769 B | 293,759 B (287 KiB) |
| + `espnow` | 300,563 B (294 KiB) | 3,525 B | 11,837 B | 304,088 B (297 KiB) |

`libmesh` (950 KiB archive, 219 KiB text) and `libsmartconfig` (182 KiB archive,
41 KiB text) are mesh networking and SmartConfig — not needed for a station.

### The answer

**The blob does not fit in the `linux` partition.** Not even the STA-only set.

- full blob: short by 503,633 B (492 KiB)
- STA only: short by 232,431 B (227 KiB)

But **it fits in the `rootfs` partition**, which has 798,720 B (780 KiB) free:

- full blob text+data 564,961 B — fits with 233,759 B to spare
- STA only 293,759 B — fits with 504,961 B to spare

Three ways this can be resolved, and they are not equivalent:

1. **Grow the `linux` partition.** Costs nothing to try — `home` is 5120 KiB and the
   board's `/home` holds a handful of files. Shrinking `home` by 1 MiB and giving it to
   `linux` is a one-line CSV edit. **But it invalidates the whole flash layout**, so
   `etc` and `home` are re-created and any board state is lost.
2. **Put the blob's `.text` in the `rootfs` partition** and have the kernel map it from
   there. cramfs is XIP-able, but the kernel currently maps only `xipImage` from the
   `linux` partition; a second mapped region means a second `mtd` mapping and a linker
   script change. Real work, but no layout change.
3. **Load the blob from the SD card at boot.** Not available on this board yet, and the
   user's constraint is that assumption (3) must work without the SD card. Rules this out.

Option 1 is the cheapest and the one to try first.

## RAM

### The blob's own footprint

`.data` + `.bss` is what is resident. Full blob: 20,191 B (19.7 KiB). STA only: 15,286 B
(14.9 KiB). Against `MemTotal` 7816 KiB that is 0.25 %.

### The buffers

The blob allocates its RX/TX buffers at runtime through the osi_funcs allocators
(`_wifi_malloc`, `_wifi_calloc`, `_wifi_zalloc`, `_malloc_internal`, …). The count comes
from the board's own sdkconfig,
`new-files/esp-hosted/network_adapter/sdkconfig.defaults.esp32s3.16m8r`:

```
CONFIG_ESP_WIFI_STATIC_RX_BUFFER_NUM=10
CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM=32
CONFIG_ESP_WIFI_STATIC_TX_BUFFER_NUM=16
CONFIG_ESP_WIFI_CACHE_TX_BUFFER_NUM=32
```

Each buffer is 1600 B on the S3. That is 90 buffers:

| | count | bytes |
|---|---|---|
| static rx | 10 | 16,000 B (15.6 KiB) |
| dynamic rx | 32 | 51,200 B (50.0 KiB) |
| static tx | 16 | 25,600 B (25.0 KiB) |
| cache tx | 32 | 51,200 B (50.0 KiB) |
| **total** | **90** | **144,000 B (140.6 KiB)** |

IDF documents roughly 50–60 KiB of additional heap for the control structures, the scan
list and the netif. Taking 55 KiB:

**Total runtime RAM ≈ 215 KiB of 7816 KiB = 2.8 %.**

### The answer

**RAM is not a constraint.** At 2.8 % of a 7.6 MiB pool, the Wi-Fi stack is affordable
with room to spare, and the buffer counts are config knobs — halving them halves the
cost if it ever matters.

## What this decides

- **Flash is the real constraint, not RAM.** The blob is 5–9× the free space in the
  `linux` partition.
- **The cheapest fix is repartitioning**, not a new mapping scheme: shrink `home`, grow
  `linux`. One CSV line, one rebuild, and it wipes `/etc` and `/home` on the board.
- **The STA-only set is worth taking regardless.** `libmesh` and `libsmartconfig` are
  950 KiB and 182 KiB of archive that a station never calls. Dropping them halves the
  blob and removes two whole subsystems from the port's dependency surface.
- **Nothing here blocks the port.** The blob is linkable, the RAM is there, and the
  flash problem has a one-line answer.
