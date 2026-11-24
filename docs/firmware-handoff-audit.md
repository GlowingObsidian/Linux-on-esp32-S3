# Firmware handoff audit — 2026-11-19

The Nano failures have two separate confirmed mechanisms: a firmware lookup
invalidates a Linux flash mapping, and the native NOR guard rejects a harmless
cache statistics overflow. Both corrections are built. Hardware acceptance
requires Nano, a sustained write and persistence after installing the update.

The source fixes live in `patches/02-firmware-network-adapter.patch` and kernel
patch 0129. Evidence, exact before/after sources, disassemblies and the update
manifest are in `build-output/flash-rw-research/handoff-map-fix/`.

## What actually runs

| Stage | Work performed | State Linux receives |
| --- | --- | --- |
| ROM and second-stage bootloader | Load the factory handoff app and initial flash mappings. | The existing bootloader and partition table are retained. |
| Pinned IDF startup | Configure flash/MSPI, initialize octal PSRAM, establish cache modes, initialize the RTOS on CPU0. | Flash DIO at 80 MHz; PSRAM 8 MiB at 80 MHz; shared I-cache 16 KiB and D-cache 32 KiB. |
| IDF single-core setup | Hold CPU1 using clock/run-stall/reset controls, including the documented S3 clock-gating workaround. | CPU1 is stopped until Linux starts it. This does not reserve a core for IDF after the jump. |
| `app_main()` | Call `linux_boot()` directly. | It starts no Wi-Fi, transport, NVS, JFFS2 reader or flash-server task. Merely linked vendor libraries do not initialize the radio. |
| Corrected `map_flash_and_go()` | Resolve the Linux partition first; deinitialize esp_timer; stop all three SYSTIMER alarms and their interrupts; disable both timer-group watchdogs; mask ordinary CPU interrupts. | No old RTOS alarm can fire into Linux's interrupt setup. SYSTIMER counters and clocks remain running. |
| Corrected `init_linux_mmap()` | Validate the firmware's contiguous flash prefix; write back dirty data under the old mappings; disable/invalidate both caches from IRAM; install Linux mappings; restore the saved cache autoload modes. | Firmware entries 0..2 remain usable until the jump. Entries 3..255 identity-map flash. Entries 256..383 are invalid; entries 384..511 map eight MiB of PSRAM at `0x3d800000`, with instruction alias `0x43800000`. No temporary IDF cache tags survive. |
| Final jump | Publish stores with `memw`, pass the boot tags in a2 and jump to `0x42140000`. | Device-tree bootargs remain authoritative; the handoff does not read `/etc`. |
| Linux `head.S` and SMP startup | Install Linux vectors/stack/PS/interrupt state, copy XIP data, clear BSS, set CPU-local timers and start CPU1. | Both cores run Linux. No FreeRTOS scheduler or IDF server remains active. |

The vector address remains `0x40379000`. The corrected firmware's cache-off
function and literals are in internal instruction RAM, above the configured
I-cache reservation. Its cache-off branch paths call ROM functions only;
diagnostics use lock-free ROM `esp_rom_printf` after both caches are enabled.
Ordinary newlib `printf` is forbidden while the handoff's IRQ mask is active.
The existing guard's SRAM/IRAM
reservations do not overlap firmware load segments.

## Why Nano failed

The old order was `init_linux_mmap()` followed by `esp_partition_find()`.
IDF's partition list is lazy. Its first lookup maps the table at physical
`0x8000`, reads it, then unmaps that temporary page.

IDF reserves three firmware pages, then 128 PSRAM pages. Its allocator's first
dynamic slot is therefore entry `0x83`. Directly installing Linux's mappings
does not update that allocator. The later lookup overwrites entry `0x83` and
its munmap leaves `0x4000`, the hardware's invalid-entry value.

That is the page containing the reported Nano PC `0x42832e54`. The board
reported `errors=0x20`, `mmu_fault_content=0x14000` and the same fault address.
Copying an executable from that broken flash page to RAM can also copy bad
instruction bytes, so the RAM-copy test does not exclude this mechanism.
The host regression reproduces the hole using the actual handoff functions.

Moving the lookup earlier fixes the hole. A complete handoff also discards
cached data from temporary mappings before assigning those virtual addresses
to Linux. The final code writes back dirty PSRAM before invalidation and
remaps while both caches are disabled. It preserves the original cache modes.

The old `rsil a2,3; wsr a2,ps` was independently wrong: RSIL returns the old PS,
so writing a2 back undoes the interrupt mask. The correction retains the new
INTLEVEL, zeros INTENABLE and declares the register/memory clobbers.

## Why the larger download failed

The later download's saved status was `errors=0x80`, not the Nano MMU fault.
Pinned IDF headers and the TRM identify EXTMEM status bits 7..11 as statistics
counter overflow. Patch 0129 excludes exactly those five global bits from
the fault predicate while preserving raw diagnostic snapshots. Actual MMU,
cache-access and unknown error bits still block flash operations.

## SMP, forkbank, storage and speed

Forkbank exchanges only PSRAM mappings at data addresses
`0x3c000000..0x3dffffff`, after checking their PSRAM type. It does not rewrite
the flash entry `0x83`. Its shared cache lock is also used by native NOR
operations. The handoff ordering defect therefore does not require an SMP or
forkbank defect to explain it.

The user's single-thread and two-pthread CoreMark runs both validated their
CRCs. They establish functioning pthread execution in this build; they are
not flash-load or complete scheduler tests. Firmware clock/cache settings
are unchanged by this update. The reported download rate alone cannot
separate Wi-Fi/TLS time from guarded flash-write time.

Normal BusyBox init, read-only XIP cramfs, writable JFFS2 `/etc` and `/home`,
and the removal of SD support are preserved. The update writes neither
persistent partition. Handoff lookup and cache maintenance do not erase or
program NOR. Logs/runtime data remain in RAM and mounts use noatime; ordinary
JFFS2 metadata updates and garbage collection still consume flash cycles.

USB detection remains a separate hardware/driver acceptance item. The handoff
does not start a USB host; Linux's DWC2 driver owns that job. This audit does
not claim the earlier attach-detection failure is fixed.

## Verification and references

- The complete firmware patch applies to exact esp-hosted revision
  `e2f19edab6c5bec988346906412cb8e7443661db`; applied files match built inputs.
- `build/test-firmware-handoff.py` reproduces the old invalid entry, exposes
  stale tags with a lookup-only fix, and checks complete mapping/cache order,
  dirty-data preservation, saved modes and failures before remapping.
- `build/verify-firmware-handoff.py` follows the linked branch/loop paths and
  checks vector coupling, IRAM code/literals, cache-off ROM calls, image
  hashes, configuration, SRAM ownership and persistent erase boundaries.
- Kernel patch 0129 already passed 69 linked cache scenarios, 109 linked NOR
  scenarios, the actual sysfs-handler checks and the existing regression gates.
- Pinned IDF `24394052fcc462fe27935b4cf15ea1f3e98549c1`: `esp_partition/partition.c`,
  `spi_flash/flash_mmap.c`, `esp_mm/esp_mmu_map.c`, `esp_psram/esp_psram.c`,
  `esp_system/port/cpu_start.c`, FreeRTOS `port_systick.c` and S3 cache HAL/ROM.
- Local S3 TRM, sections 4.3.1 and 4.3.3: internal RAM aliases and shared
  external-memory cache/mapping rules; EXTMEM register descriptions distinguish
  actual faults from counter overflow.
- Cached NuttX S3 flash guard: peer-CPU quiescence, internal execution/stack
  requirements and cache maintenance. Linux implements its own runtime guard.

Recovery firmware: 164672 bytes, SHA256
`3781fabe56e97e7fe77e4dbee56192b635e7233413fe931f13d32ecc40701b90`.
Kernel: 3894264 bytes, SHA256
`e0dc4622aaa0835e67a619d62c09da1631371c069870d5ce334f0fdbcf5f2e54`.

## Pre-Linux abort in the first handoff update

The first installed handoff correction (`b8ef0296...`, ELF prefix
`f811b4088b406662`) boot-looped. The exact backtrace decodes to
`init_linux_mmap+0xad -> printf -> _vfprintf_r -> lock_acquire_generic` at
`0x4037728f`. Pinned IDF newlib locks treat nonzero PS.INTLEVEL as ISR context
and explicitly abort recursive mutex acquisition. The previous verification
checked cache availability but missed the separate stdio/IRQ constraint.

The recovery changes four diagnostic calls to `esp_rom_printf`, whose linked
target is ROM address `0x400005d0`. Ordinary newlib output no longer runs after
RSIL. The regression test reproduces the exact failed firmware's abort and
checks that ROM output reaches the Linux jump. The linked verifier now rejects
ordinary `printf` throughout `init_linux_mmap` and after RSIL in the caller.
Complete patch/source identity, cache paths, vectors, configuration and erase
boundaries pass. This firmware-only recovery was installed after a fresh user
download-mode signal; exact board/table/failed-firmware preflight and esptool
written-data hash verification passed. The user subsequently reported all
prescribed SD-free boot, Nano, small flash-write and reboot-persistence tests
passed. Sustained download/write reliability, speed and USB attachment were
not covered by those checks. The original mapping correction remains in place.
