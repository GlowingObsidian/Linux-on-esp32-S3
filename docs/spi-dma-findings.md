# SPI DMA on the Nano ESP32 — what a DMA path would need

Read-only investigation, 2026-10-20. **Revised 2026-10-31** against the real
TRM and the ESP-IDF source, after the first version's central conclusion turned
out to be wrong. See "Corrections" at the end for what changed and why.

**Verdict, revised: a DMA path is buildable, and the goal is CPU offload, not
throughput.** The 2026-10-20 measurement below still stands — the panel is not
a throughput bottleneck — but it answered the wrong question. The user's
requirement is to take the per-byte `memcpy_toio` loop off the CPU, and that is
a real cost the measurement never looked at. The section "The measurement" is
kept because it is correct and because it explains why a *throughput* argument
for DMA would be wrong; it is no longer the deciding section.

## First SD boot: initialization corrections

The first SD kernel (`29fa4418...`) brings both CPUs up, identifies the SU01G
969 MiB card, but DMA-sized status/data reads return -ETIMEDOUT. Sector 0 cannot
be read, so no partition 1 is discovered and userspace is never reached.
The separate `rootflags=noatime` option is rejected by ext2 during initial root
mount; remove it from the command line. The generic noatime option in fstab is
handled by mount(8), unlike filesystem-specific initial rootflags.

Patch 0084 corrects omissions found by comparing entire initialization routines:

- NuttX `esp32s3_dma_init()` enables SYSTEM_DMA_CLK_EN and also DMA_CLK_EN at
  GDMA_MISC_CONF +0x3c8 bit 4. Our initial backend enabled only the system gate.
  Set the internal bit and check GDMA_DATE before accepting the controller.
- NuttX `esp32s3_spi_dma_init()` and pinned IDF `spi_ll_master_init()` set SPI
  DMA_CONF bits 19 and 20 (RX/TX segment-clear controls). Preserve these in
  initialization, DMA setup and cleanup, rather than writing zero or only the
  TX/RX enable bits.
- On error, report the exact failing phase (SPI IRQ/done, TX total EOF, RX EOF,
  RX descriptor) and register/descriptor control values before reset.

TRM v1.8 pp. 1117-1120 describes the distinct SPI/GDMA completion and data-flow
conditions. Chapter 3 describes internal clock/reset and descriptor operation.
Local extracts are saved in build-output/sd-root-work/trm-spi-dma.txt and
trm-gdma.txt. These are concrete implementation gaps, not proof of the exact
hardware timeout cause: corrected hardware boot remains necessary. The original
host model did not cover these initialization requirements. It now exercises
clock setup/readback and asserts FIFO-control flags before simulated DMA starts.

## 2026-11-16: SD and SMP audit

The old panel measurements and firmware ownership descriptions below are historical.
Both CPUs now execute Linux; Wi-Fi is parked. The active goal is a full writable
SD root filesystem through SPI2, with display support disabled.

Review of patch 0072 found that its `/dev/esp32-gdma` memcpy test is not a usable
SPI-DMA backend:

- Its **start bits are correct**: IN/RX bit 22, OUT/TX bit 21. The old table
  below incorrectly said OUT bit 22 and is now corrected against the pinned
  ESP-IDF `gdma_reg.h` (`ESP_IDF_REV` in `build/sources.lock`).
- It allows 4096 bytes, but masks size and length to 12 bits, encoding that
  request as zero. SPI descriptors will be limited to 4092 aligned bytes.
- Its source starts at `DATA_ADDR`, destination at `DATA_ADDR + 2048`, but it
  allows requests larger than 2048; clearing the destination then destroys
  part of the staged source before DMA starts.
- It places the source descriptor on IN/RX and destination on OUT/TX. NuttX's
  SPI implementation uses OUT for memory-to-peripheral TX and IN for
  peripheral-to-memory RX. The old test's direction comments must not be copied.
- It accepts completion of either channel, including descriptor-done rather
  than requiring successful completion of the whole transfer.
- It hardcodes channel 0 and a fixed SRAM window. Its private mutex cannot
  prevent another driver from reusing those resources.
- The claimed `SOC_RESERVE_MEMORY_REGION` reservation is absent from the
  current repository firmware patch and cached firmware main sources. The
  address must be validated against the built firmware and ROM reservations;
  the driver's comment is not proof of ownership. The Linux RAM node describes
  PSRAM only, so the boot argument is not a substitute for that firmware audit.

Reusable pieces are the register-block address, clock/reset bit definitions,
12-byte descriptor layout, and the internal-SRAM staging approach. The transfer
routine needs replacement for SPI, with explicit ownership and bounded buffers.
The experimental misc driver must not run alongside an owner of the same pair.

NuttX reference reviewed at revision
`c803668d6c763c823b3286df1cc40c528abadf3e`:
[esp32s3_spi.c](https://github.com/apache/nuttx/blob/c803668d6c763c823b3286df1cc40c528abadf3e/arch/xtensa/src/esp32s3/esp32s3_spi.c),
[esp32s3_dma.c](https://github.com/apache/nuttx/blob/c803668d6c763c823b3286df1cc40c528abadf3e/arch/xtensa/src/esp32s3/esp32s3_dma.c).
The sequence sets peripheral selection, disables memory-to-memory mode, prepares
separate TX/RX descriptors, resets and starts the channels, updates SPI registers,
starts SPI, and waits for its transfer interrupt. Linux must express this using
its SPI-controller callbacks and its own locking/interrupt infrastructure.

The local TRM is now available at
`docs/reference/esp32-s3_technical_reference_manual_en.pdf`.

Sources for the original investigation, in order of authority:

- **ESP32-S3 TRM v1.8, chapter 3 (GDMA)** — the actual PDF, downloaded to
  `/tmp/trm-fetch/trm.pdf` and chapter 3 extracted to `/tmp/trm-fetch/ch3.txt`.
  Line numbers below are into that extraction. **The PDF is not in this repo**;
  the earlier version of this document cited `docs/ESP32-S3-TRM.pdf`, which has
  never existed here. Re-download from
  `https://documentation.espressif.com/esp32-s3_technical_reference_manual_en.pdf`.
- **ESP-IDF source**, at
  `/refs/esp32-linux-build/build/esp-hosted/esp_hosted_ng/esp/esp_driver/esp-idf`
  inside the build container. This is the same tree the firmware is built from,
  so its register headers describe the silicon this board actually has.
- This repo's `patches/linux/0012-spi-add-esp32-host-driver.patch`.

## The headline

**The SPI peripheral on the ESP32-S3 has no DMA-link register.** Its register
map ends at `0xF0`:

```
0x00 SPI_CMD      0x2C SPI_DOUT_MODE   0x40 SPI_DMA_INT_ST
0x04 SPI_ADDR     0x30 SPI_DMA_CONF    0x44 SPI_DMA_INT_SET
0x08 SPI_CTRL     0x34 SPI_DMA_INT_ENA 0x98 SPI_W0 .. 0xD4 SPI_W15
0x0C SPI_CLOCK    0x38 SPI_DMA_INT_CLR 0xE0 SPI_SLAVE
0x10 SPI_USER     0x3C SPI_DMA_INT_RAW 0xE4 SPI_SLAVE1
0x14 SPI_USER1                         0xE8 SPI_CLK_GATE
0x18 SPI_USER2                         0xF0 SPI_DATE
0x1C SPI_MS_DLEN
0x20 SPI_MISC
0x24 SPI_DIN_MODE
0x28 SPI_DIN_NUM
```

There is no `SPI_DMA_OUT_LINK_REG` and no `SPI_DMA_IN_LINK_REG`. On the older
ESP32 the SPI peripheral owned its own DMA link registers; on the S3 that job
moved to GDMA, and the descriptor pointer lives in the **GDMA** register block:

```
GDMA_IN_LINK_CH0_REG   = DR_REG_GDMA_BASE + 0x20   (0x6003F020)
GDMA_OUT_LINK_CH0_REG  = DR_REG_GDMA_BASE + 0x80   (0x6003F080)
DR_REG_GDMA_BASE       = 0x6003F000                (soc/esp32s3/include/soc/reg_base.h:56)
```

Channel stride is `0xC0`, five channels (`gdma_struct.h:346` `channel[5]`).

So the driver's `SPI_DMA_CONF_REG` writes (`SPI_DMA_TX_ENA` / `SPI_DMA_RX_ENA`)
are only half the switch. They tell the SPI peripheral to take its data from
the DMA FIFO rather than the CPU FIFO. Nothing points GDMA at a descriptor
list, and nothing starts a channel.

## What the driver does today

`esp32_spi_transfer_one` is a pure-PIO loop, 64 bytes at a time:

```c
while (remaining_words) {
	unsigned int n_words = min(remaining_words, ESP32_SPI_FIFO_SIZE);
	...
	esp32_spi_prepare_transfer(spi, device, t, n_words);
	memcpy_toio(spi->regs + SPI_W0_REG, tx_ptr, n32);
	esp32_spi_start_transfer(spi);
	esp32_spi_wait(spi);
	memcpy_fromio(rx_ptr, spi->regs + SPI_W0_REG, n32);
	...
}
```

`ESP32_SPI_FIFO_SIZE` is 64. A 240x240 RGB565 frame is 115200 bytes, so a
single frame is ~1800 trips around that loop, each one a `memcpy_toio` into a
64-byte MMIO window plus a spin. **That loop is the CPU cost this work is
about.**

`esp32_spi_wait` spins on `SPI_USR` and then polls `SPI_DMA_INT_RAW` for
`SPI_TRANS_DONE_INT` — it does not use the completion it sets up in probe
(`esp32_spi_irq` completes `spi->done`, and the `wait_for_completion` path is
`#if 0`'d out). The IRQ is registered and effectively unused.

`probe` sets `pdev->dev.dma_mask = NULL` (patch `0012`, line 446), which is
what stops the SPI core from ever trying to DMA-map a transfer buffer. There is
no `can_dma` callback, so `spi_map_buf` is never reached either.

## The descriptor

From `components/hal/include/hal/dma_types.h:19-33`:

```c
typedef struct dma_descriptor_s {
    struct {
        uint32_t size : 12;         /*!< Buffer size */
        uint32_t length : 12;       /*!< Number of valid bytes in the buffer */
        uint32_t reversed24_27 : 4; /*!< Reserved */
        uint32_t err_eof : 1;       /*!< Whether the received buffer contains error */
        uint32_t reserved29 : 1;    /*!< Reserved */
        uint32_t suc_eof : 1;       /*!< Whether the descriptor is the last one in the link */
        uint32_t owner : 1;         /*!< Who is allowed to access the buffer ... */
    } dw0;                          /*!< Descriptor Word 0 */
    void *buffer;                   /*!< Pointer to the buffer */
    struct dma_descriptor_s *next;  /*!< Pointer to the next descriptor */
} dma_descriptor_t;

ESP_STATIC_ASSERT(sizeof(dma_descriptor_t) == 12, "dma_descriptor_t should occupy 12 bytes in memory");
```

Field positions agree with TRM §3.4.1 (ch3.txt:68-103): `owner` DW0[31],
`suc_eof` DW0[30], `err_eof` DW0[28], `length` DW0[23:12], `size` DW0[11:0].

`DMA_DESCRIPTOR_BUFFER_MAX_SIZE` is `4095`. But IDF's own SPI path uses
`LLDESC_MAX_NUM_PER_DESC = 4096 - 4 = 4092`
(`components/soc/include/soc/lldesc.h:15`), and `lldesc_setup_link` is what
`spi_hal_iram.c:168` calls. **Use 4092 for SPI TX.**

## Descriptors must be in internal RAM — the constraint that shapes everything

TRM §3.4.1 (ch3.txt:46): *"These linked lists, stored in internal RAM,
consist of..."*
TRM §3.4.1 (ch3.txt:66): *"Linked lists should be in internal RAM for the GDMA
engine to be able to use them."*
TRM §3.4.6 (ch3.txt:176): *"Once configured and enabled by software, the GDMA
controller starts to read the linked list from internal RAM."*
TRM §3.4.6 (ch3.txt:190-191): *"The third word (DW2) in a descriptor can only
point to a location in internal RAM, given that the third word points to the
next descriptor to use and that all descriptors must be in internal memory."*

The GDMA-visible internal window is `0x3FC88000~0x3FCFFFFF`:

- TRM Table 4.3-1 (ch3.txt:1749): `0x3FC8_8000  0x3FCE_FFFF  416 KB  Internal SRAM 1` (data bus)
- TRM ch3.txt:1902: *"GDMA uses address range 0x3FC8_8000~0x3FCE_FFFF to access Internal SRAM 1"*
- IDF agrees: `SOC_DMA_LOW 0x3FC88000`, `SOC_DMA_HIGH 0x3FD00000`
  (`soc/esp32s3/include/soc/soc.h:206-207`), and `esp_ptr_dma_capable()` is
  exactly that range test (`esp_memory_utils.h:178-181`).
- IDF's own LCD driver says it in a comment and allocates accordingly
  (`components/esp_lcd/src/esp_lcd_panel_io_i80.c:135-136`):
  ```c
  // DMA descriptors must be placed in internal SRAM
  bus = heap_caps_calloc(1, sizeof(esp_lcd_i80_bus_t) + num_dma_nodes * sizeof(dma_descriptor_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
  ```
  Same for SPI (`components/driver/spi/gpspi/spi_common.c`, `dmadesc_tx =
  heap_caps_malloc(sizeof(lldesc_t) * dma_desc_ct, MALLOC_CAP_DMA)`).

**Linux's entire memory is `0x3d800000 + 0x00800000`** (the DT `memory@0`
node), which is entirely in the *external* range. A `kmalloc` descriptor is
therefore not reachable as a descriptor. **A carve-out is required.** The
earlier version of this document concluded the opposite; see "Corrections".

### The 20-bit link address field is not a shift

`GDMA_OUTLINK_ADDR_CHn` is bits [19:0] of `GDMA_OUT_LINK_CHn`
(`gdma_reg.h:468-474`: `GDMA_INLINK_ADDR_CH0 = 0x000FFFFF`, `_S = 0`;
TRM Register 3.7, ch3.txt:905). IDF writes the **raw pointer**:

```c
static inline void gdma_ll_tx_set_desc_addr(gdma_dev_t *dev, uint32_t channel, uint32_t addr)
{
    dev->channel[channel].out.link.addr = addr;   /* no shift, no mask */
}
```
(`components/hal/esp32s3/include/hal/gdma_ll.h:461-464`; RX is the same at
`:205-208`.)

`0x3fc88000 & 0xFFFFF = 0x0C8800`, so the top 10 bits are not in the register
at all. The engine reconstructs them from the internal-RAM aperture the channel
is already constrained to. **Consequence: no 1 MB alignment requirement and no
shift.** The only placement rule is the one above — the descriptor base must be
inside `0x3FC88000~0x3FCFFFFF`. One constraint, not two.

## What GDMA needs configured

Per channel, from `gdma_reg.h` / `gdma_ll.h` (all offsets relative to
`0x6003F000`, `n` = channel 0..4, stride `0xC0`):

| Register | Offset | Field | Value for SPI2 TX |
|---|---|---|---|
| `GDMA_OUT_CONF0_CHn` | `0x60 + n*0xC0` | `out_rst` bit 0 | pulse 1 then 0, resets TX FSM and FIFO pointer |
| `GDMA_OUT_CONF0_CHn` | | `out_data_burst_en` bit 5 | 1 |
| `GDMA_OUT_CONF0_CHn` | | `out_dscr_burst_en` bit 4 | 1 |
| `GDMA_OUT_CONF1_CHn` | `0x64 + n*0xC0` | `out_ext_mem_bk_size` bits 14:13 | see below |
| `GDMA_OUT_LINK_CHn` | `0x80 + n*0xC0` | `outlink_addr` bits 19:0 | descriptor base, raw pointer |
| `GDMA_OUT_LINK_CHn` | | `outlink_start` bit 21 | 1 to start |
| `GDMA_OUT_PERI_SEL_CHn` | `0xA8 + n*0xC0` | `sel` bits 5:0 | `0` = SPI2, `1` = SPI3 |

Peripheral ids (`soc/esp32s3/include/soc/gdma_channel.h:11-12`):
`SOC_GDMA_TRIG_PERIPH_SPI2 = 0`, `SOC_GDMA_TRIG_PERIPH_SPI3 = 1`.

`EXT_MEM_BK_SIZE` encoding — TRM Table 3.4-4 (ch3.txt:252-257) settles a
contradiction between two IDF headers: `0 → 16 bytes`, `1 → 32 bytes`,
`2 → 64 bytes`. `gdma_ll.h:53-55` (`GDMA_LL_EXT_MEM_BK_SIZE_16B/32B/64B`) is
right; `gdma_reg.h`'s "2/3: reserved" comment is wrong.

**The peripheral-select register is a shared resource**, one per channel, and
the default is `6'h3f` (63) = no peripheral. Two claimants on one channel
silently steal it from each other. See "The firmware already holds a channel".

## The exact TX sequence

`components/hal/spi_hal_iram.c:163-176` is the reference, verbatim:

```c
if (trans->send_buffer) {
    if (!hal->dma_enabled) {
        //Need to copy data to registers manually
        spi_ll_write_buffer(hw, trans->send_buffer, trans->tx_bitlen);
    } else {
        lldesc_setup_link(hal->dmadesc_tx, trans->send_buffer, (trans->tx_bitlen + 7) / 8, false);

        spi_dma_ll_tx_reset(hal->dma_out, hal->tx_dma_chan);
        spi_ll_dma_tx_fifo_reset(hal->hw);
        spi_ll_outfifo_empty_clr(hal->hw);
        spi_ll_dma_tx_enable(hal->hw, 1);
        spi_dma_ll_tx_start(hal->dma_out, hal->tx_dma_chan, hal->dmadesc_tx);
    }
}
```

Expanded (`spi_hal_iram.c:20-28`):

1. `gdma_ll_tx_reset_channel()` — `out.conf0.out_rst = 1; out_rst = 0;`
2. `spi_ll_dma_tx_fifo_reset()` — `hw->dma_conf.dma_afifo_rst = 1; = 0;`
3. `spi_ll_outfifo_empty_clr()` — `hw->dma_int_clr.outfifo_empty_err = 1;`
4. `spi_ll_dma_tx_enable(1)` — `hw->dma_conf.dma_tx_ena = 1;`
5. `gdma_ll_tx_set_desc_addr()` then `gdma_ll_tx_start()` — `out.link.addr = p; out.link.start = 1;`

Order matters: reset before start, FIFO reset between them.

`lldesc_setup_link` (`components/soc/lldesc.c:3-30`) fills each descriptor
`size = length = chunk`, `buf = data`, `eof = 0`, `owner = 1`,
`qe.stqe_next = &desc[n+1]`, then sets `eof = 1` and `next = NULL` on the tail.
TX needs no size rounding; RX rounds up to a 32-bit boundary.

## The three constraints that decide the design

1. **External-RAM transfers must be block-aligned and burst-sized — but only
   for RX.** TRM §3.4.9 (ch3.txt:237-239), Table 3.4-3: transmit descriptors
   need no alignment; receive descriptors' size and buffer address pointer must
   be block-aligned. IDF states it outright
   (`components/esp_hw_support/dma/gdma.c:317-319`):
   ```c
   if (dma_chan->direction == GDMA_CHANNEL_DIRECTION_TX) {
       // TX channel can always enable burst mode, no matter data alignment
       gdma_ll_tx_enable_data_burst(group->hal.dev, pair->pair_id, true);
       gdma_ll_tx_enable_descriptor_burst(group->hal.dev, pair->pair_id, true);
   ```
   **The panel is a TX path**, so this is the easy side. Nothing on this board
   uses the RX path.

2. **`owner` is a handshake, not a flag.** `dw0.owner = 1` means the DMA engine
   owns the buffer; GDMA clears it when done, but only on a transmit descriptor
   if `GDMA_OUT_AUTO_WRBRACK_CHn` is set (TRM §3.4.1, ch3.txt:68-103). The
   driver already has the transfer-done interrupt wired and unused, which is
   the smaller path: leave `out_check_owner` at its default (0, disabled) and
   wait on `SPI_TRANS_DONE_INT`.

3. **The SPI peripheral still clocks every byte.** DMA removes the CPU from
   the *copy*, not from the transfer. `esp32_spi_wait` still waits for
   `SPI_USR` to clear and `SPI_TRANS_DONE_INT` to fire. What changes is that
   the CPU is not doing ~1800 `memcpy_toio` calls per frame while it waits.

## The cache problem, and why it is the real work

**The kernel believes there is no data cache. The silicon has one.**

`arch/xtensa/variants/esp32s3/include/variant/core.h` (from patch
`0004-xtensa-import-ESP32S3-core-variant.patch`, upstream commit
`af9c484846761f8eb06e6df1d0f4ff4b09f3f602` by Max Filippov):

```
:242  #define XCHAL_DCACHE_LINESIZE   16
:247  #define XCHAL_DCACHE_SIZE       0
:249  #define XCHAL_DCACHE_IS_WRITEBACK 0
:290  #define XCHAL_DCACHE_SETWIDTH   0
:294  #define XCHAL_DCACHE_WAYS       1
```

With `XCHAL_DCACHE_SIZE == 0`, every macro in
`arch/xtensa/include/asm/cacheasm.h` (`:146`, `:155`, `:164`, `:184`, `:192`)
is inside `#if XCHAL_DCACHE_SIZE` and **expands to nothing**. In
`arch/xtensa/include/asm/cacheflush.h:51-67`, with
`XCHAL_DCACHE_IS_WRITEBACK == 0`, `__flush_dcache_page` and
`__flush_dcache_range` are empty static inlines. `arch/xtensa/kernel/pci-dma.c`
is not even built — `arch/xtensa/kernel/Makefile:12` is
`obj-$(CONFIG_MMU) += pci-dma.o`, and `CONFIG_MMU` is n.

Meanwhile the firmware's own sdkconfig says the hardware has a 32 KiB
write-back L1 D-cache over PSRAM:

```
CONFIG_ESP32S3_DATA_CACHE_32KB=y
CONFIG_ESP32S3_DATA_CACHE_SIZE=0x8000
CONFIG_ESP32S3_DATA_CACHE_LINE_32B=y
CONFIG_ESP32S3_DATA_CACHE_LINE_SIZE=32
```

And `CONFIG_MEMMAP_CACHEATTR=0xffff222f` (`nano_esp32_linux.config:33`, built
`.config:231`) marks the PSRAM window cache-bypassed — nibble 7 covers
`0x3C000000-0x3DFFFFFF` and its value is `2` = cache bypass
(`arch/xtensa/Kconfig:634-663`). Verified by disassembling
`arch/xtensa/kernel/head.o`: `initialize_cacheattr`
(`arch/xtensa/include/asm/initialize_mmu.h:217-236`, the `XCHAL_HAVE_MPU == 0`
branch) loads `0xffff222f` into `a4` and writes it via `wdtlb`/`witlb` into
TLB way 0 at boot.

**So the kernel has told the hardware to bypass its cache for PSRAM, and has
no way to flush it.** That is the correct configuration for a CPU-only system
and the wrong one for DMA. If the framebuffer stays in PSRAM — and at 115200
bytes for 240x240 RGB565 it will — the driver must write back dirty lines
before GDMA reads them.

`ARCH_DMA_MINALIGN` is 16 here (`arch/xtensa/include/asm/cache.h:16-18,32`,
`L1_CACHE_BYTES = XCHAL_DCACHE_LINESIZE = 16`), but the hardware line is
**32** — IDF asserts the PSRAM alignment must be a multiple of the real cache
line (`gdma.c:314`). **A driver must align to 32, not 16.**

### The writeback IDF uses

`esp_cache_msync` (`components/esp_mm/esp_cache.c:25-53`) →
`cache_hal_writeback_addr` (`components/hal/cache_hal.c:179`) → ROM
`Cache_WriteBack_Addr = 0x400016c8` (`esp32s3.rom.ld:384`), which is
`esp_rom_cache_writeback_esp32s3.S` writing:

- `EXTMEM_DCACHE_SYNC_ADDR_REG` = address
- `EXTMEM_DCACHE_SYNC_SIZE_REG` = length
- freeze via `EXTMEM_DCACHE_FREEZE_REG` (clear MODE, set ENA, poll DONE)
- set `EXTMEM_DCACHE_WRITEBACK_ENA` (`BIT(1)` of `EXTMEM_DCACHE_SYNC_CTRL_REG`,
  **default 1'b0**) and poll `EXTMEM_DCACHE_SYNC_DONE`
- unfreeze

`EXTMEM_DCACHE_INVALIDATE_ENA` is `BIT(0)`, **default 1'b1**.

There is a documented erratum: `ESP_ROM_HAS_CACHE_WRITEBACK_BUG (1)`
(`esp_rom_caps.h:28`) — *"ROM api Cache_WriteBack_Addr access cacheline being
writen back may cause cache hit with wrong value."* IDF's workaround
(`esp_rom_cache_esp32s2_esp32s3.c:96-160`) handles unaligned head/tail one line
at a time under `XTOS_SET_INTLEVEL(XCHAL_NMILEVEL)`, then suspends autoload
around the call. **A Linux driver must mask interrupts the same way.**

## What the change would touch

- `esp32_spi_transfer_one` — a DMA branch, taken when the transfer is long
  enough to be worth it (a threshold, since a 2-byte register write should not
  pay for a descriptor).
- **A DT change**: shrink `memory@0` to `0x3d800000 + 0x00780000` and add a
  `reserved-memory` node claiming `0x3fc88000 0x78000`, then `ioremap` it in
  the driver. This is the carve-out. ~480 KB of 8 MB.
- **`pdev->dev.dma_mask`** in patch `0012` line 446: `NULL` → `DMA_BIT_MASK(32)`.
  Today `dma_map_phys()` (`kernel/dma/mapping.c:161-162`) hits
  `WARN_ON_ONCE(!dev->dma_mask)` and returns `DMA_MAPPING_ERROR`.
- **The EXTMEM writeback**, written into the driver. Not optional, and not
  something the kernel will do for it.
- GDMA channel claim and release, with the peripheral-select register treated
  as the shared resource it is.
- The `EXT_MEM_BK_SIZE` choice (32 B, matching the real cache line).

**What is *not* needed, despite looking like it:** `dma_alloc_coherent` cannot
work here at all. `dma_direct_alloc()` (`kernel/dma/direct.c:203-243`) with
`!dev_is_dma_coherent(dev)` needs `CONFIG_ARCH_HAS_DMA_ALLOC`,
`CONFIG_DMA_GLOBAL_POOL`, `CONFIG_ARCH_HAS_DMA_SET_UNCACHED` or
`CONFIG_DMA_DIRECT_REMAP`; none is set, so it hits
`pr_warn_once("coherent DMA allocations not supported on this platform.\n")`
and returns NULL. The carve-out replaces it, and is better anyway: the
descriptors need to be in a *specific* physical window, which no generic
allocator would give.

## The firmware already holds a GDMA channel

The firmware's SPI2 **slave** path uses DMA:
`components/.../network_adapter/main/spi_slave_api.c:550` passes `DMA_CHAN` to
`spi_slave_initialize()`. `CONFIG_SOC_GDMA_PAIRS_PER_GROUP=5`. Bluetooth is
off (`sdkconfig:541` `# CONFIG_BT_ENABLED is not set`), so `slave_bt.c`'s UHCI
pair is dead code and does **not** hold a second channel.

**Which pair the firmware holds is unread.** Linux taking the same pair would
have both engines driving one channel. This is a board read:
`GDMA_IN_PERI_SEL_CHn` at `0x6003F048 + n*0xC0` and `GDMA_OUT_PERI_SEL_CHn` at
`0x6003F0A8 + n*0xC0`, five each, looking for `sel == 0` (SPI2).

## The measurement, and what it does and does not say

Measured on the board, 2026-10-20. Every row writes to `/dev/fb0`; the only
variable that changes is how many times the device is opened.

| Path | opens | bytes | time |
|---|---|---|---|
| `dd bs=64 count=1` in a shell loop, 1000x | 1000 | 64 KB | 390 s |
| `dd bs=4096 count=1` in a shell loop, 1000x | 1000 | 4 MB | 396 s |
| `dd bs=40960 count=1` in a shell loop, 100x | 100 | 4 MB | 44 s |
| `dd bs=64 count=1000` — one `dd` | 1 | 64 KB | 1 s |
| `dd bs=40960 count=1` — one `dd` | 1 | 40 KB | 1 s |
| micropython, 1000 writes of 64 B, one fd | 1 | 64 KB | **159 ms** |
| micropython, 200 writes of 4 KB, one fd | 1 | 800 KB | **42 ms** |

The first three rows are flat in bytes and linear in opens: 1000 opens cost
390 s whether they move 64 KB or 4 MB. The last two hold one fd and move
800 KB in 42 ms — about 20 MB/s.

**The cost is per `open()`/`close()` of `/dev/fb0`, not per byte and not per
write.** The mechanism is `fb_deferred_io`: fbtft's `fb_release` flushes and
waits, so every close pays a panel update. A program that holds the fd open
never pays it.

**What this measures and what it does not.** It measures wall-clock time to
completion on a path where the CPU has nothing else to do. It does **not**
measure CPU occupancy. A held-fd write still runs the PIO loop — ~1800
`memcpy_toio` calls per 240x240 frame — and that CPU time is invisible to a
stopwatch that is only waiting for the same CPU. The user's requirement is to
get that loop off the CPU, and this table cannot speak to it either way.

The trap worth recording regardless: **a shell `dd` loop is the obvious way to
measure this panel and it is wrong by 2450x**, because it opens and closes the
device once per iteration. Any future measurement of `/dev/fb0` has to hold the
fd.

The frame figures in the original table (40 KB) are the old 128x160 panel. The
panel is now 240x240, so a frame is 115200 bytes.

## Corrections to the 2026-10-20 version

1. **"No carve-out is needed" (old `:133`) was wrong.** The old reasoning was
   that a single descriptor with `suc_eof = 1` and `next = NULL` has no next
   descriptor to point at, so nothing has to be in internal RAM. That is sound
   about DW2 and misses the separate, explicit rule that **the descriptors
   themselves** must be in internal memory — TRM §3.4.1 line 66 and §3.4.6
   lines 190-191, plus IDF's own `MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA` at
   `esp_lcd_panel_io_i80.c:136`. A carve-out is required.
2. **`GDMA_INLINK_ADDR_CHn` is not "right-shifted" (old `:147`).** Bits [19:0],
   raw pointer, no shift — `gdma_ll.h:461-464`, `gdma_reg.h:468-474`.
3. **"all 6 GDMA channels" (old `:151`) — there are 5** (`gdma_struct.h:346`,
   `channel[5]`; TRM Register 3.5 is `n: 0-4`).
4. **`EXT_MEM_BK_SIZE` is resolved**, not ambiguous: TRM Table 3.4-4 says
   `0 → 16 B`, `1 → 32 B`, `2 → 64 B`. `gdma_ll.h` is right, `gdma_reg.h`'s
   "2/3: reserved" is wrong.
5. **The frame size was stale** — 128x160 / 40 KB, now 240x240 / 115200 B.
6. **The cited `docs/ESP32-S3-TRM.pdf` does not exist in this repo.** The TRM
   has to be downloaded; the extraction used here is at `/tmp/trm-fetch/ch3.txt`.

## The correction this implies elsewhere

`ARCHITECTURE.md:233-238` states that "the USB core's DMA master is understood
to reach internal SRAM only". That claim is about USB, and nothing here changes
it — USB is not in TRM Table 15.5-4 at all, and its DMA reach remains
unverified. But the same paragraph's framing implies the *chip* cannot DMA to
PSRAM, and that is wrong for GDMA: `CONFIG_SOC_PSRAM_DMA_CAPABLE=y` and
`CONFIG_SOC_GDMA_SUPPORT_PSRAM=y` are in this repo's own firmware sdkconfig,
and TRM §3.4.9 states it outright. The paragraph should say which engine it
means.

## What is still unknown

- **Whether Linux can reach `0x3fc88000` at all.** Everything above is
  downstream of this. The port is NOMMU with `XCHAL_HAVE_IDENTITY_MAP 1`,
  `PAGE_OFFSET == PHYS_OFFSET == 0x3d800000`
  (`arch/xtensa/include/asm/page.h:29-30`), and nothing in this tree has ever
  touched that window. If `ioremap` faults or aliases, the design is dead and
  there is no second option — the descriptors *must* be internal.
- **Which GDMA pair the firmware holds.** See above.
- **The EXTMEM register block addresses as Linux sees them.** The names come
  from IDF; the base has not been read from the TRM here.
