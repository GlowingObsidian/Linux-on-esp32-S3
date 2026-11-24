# Restoring writable flash `/etc` and `/home`

Research date: 2026-11-17. Initial research status is recorded below; later
sections contain the hardware-passed SRAM gate and ready cache candidate.
No native writable driver or writable-filesystem board image exists yet.
The working wlan0/SMP/forkbank system remains unchanged.

## Conclusion

Restore JFFS2-backed directories while keeping the working kernel and cramfs
rootfs in XIP flash. The filesystem and space are available. The substantial
missing part is a Linux-native, SMP-safe flash erase/program backend, with
explicit internal-memory execution and external-stack handling. Neither a
new partition nor re-enabling MTD_WRITEABLE supplies that backend.

Both CPUs can remain Linux CPUs. A write temporarily coordinates them; it
does not require dedicating a core to IDF again. Wi-Fi timing during those
critical windows requires measurement and regression testing.

## What the original implementation actually did

The locked firmware revision is
`jcmvbkbc/esp-hosted@e2f19edab6c5bec988346906412cb8e7443661db`.
Its `main/linux_flash.c` served Linux requests on IDF's core0 through IPC.
It synchronized READY/DONE flags, set `g_spi_flash_skip_ipc`, then called
`esp_flash_erase_region`, `linux_flash_read`, or `esp_flash_write`. The Linux
side entered an interrupt-masked `__xipram` wait until the server finished.
Repository patch0053 also serialized the shared command between `/etc` and
`/home`; two independent JFFS2 mounts must not race the same controller.

Patch0074 removed that IPC backend when both cores were handed to Linux.
The current `drivers/mtd/chips/map_esp32.c` deliberately reports MTD_ROM,
MTD_CAP_ROM, and returns EROFS for erase/write. This is an intentional missing
backend, not an absent JFFS2 feature: the current built kernel already has
CONFIG_JFFS2_FS=y and write-buffer support.

The previous firmware also needed explicit invalidation of Linux's direct
flash mapping after modification; IDF's own mapping bookkeeping did not know
about that mapping. Preserve that coherency requirement in the native port.

## Layout that preserves the current images

| Region | Offset | Size | Intended use |
|---|---:|---:|---|
| factory | 0x010000 | 0x0c0000 | Existing handoff firmware, read-only |
| etc | 0x0d0000 | 0x070000 | 448 KiB writable JFFS2 |
| linux | 0x140000 | 0x3d0000 | Existing XIP kernel slot, read-only |
| rootfs | 0x510000 | 0x630000 | Existing XIP cramfs slot, read-only |
| home | 0xb40000 | 0x4c0000 | 4.75 MiB writable JFFS2 |

NVS/phy entries below factory remain unchanged. The proposal fits exactly
within 16 MiB, with no overlapping regions; the CSV is checked and retained
in `build-output/flash-rw-research/proposed-partitions.csv`, not installed.

The checked-in original table differs: its Linux slot was smaller, rootfs
started at0x450000, and home started at0xb00000. Restoring that old home
offset would overwrite part of today's rootfs. Restore the directory behavior,
not that obsolete byte-for-byte layout. The original etc offset still fits.

Both JFFS2 images, the ESP partition table, DT fixed-partitions, packaging
size checks, flash tooling and init mounts must describe the same layout.
The original image maker used 64-KiB logical eraseblocks. Keeping that geometry
initially avoids a filesystem format change; the backend can implement one
logical erase as sixteen physical 4-KiB sector operations, restoring CPU access
between sectors. Physical geometry still needs chip verification.

## NuttX and pinned IDF findings

NuttX was audited at `de5bfd40404953f6c75cf4401ef4ed0a892c2677`.
`esp32s3_spiflash.c` serializes operations, arranges a peer-core blocking task,
restricts scheduling/non-IRAM interrupts, suspends caches, then reverses the
sequence. It installs guard functions around flash operations. This provides
a concrete concurrency pattern, not drop-in Linux scheduler code.

`esp32s3_spiflash_mtd.c` explicitly detects PSRAM-backed stacks and defers such
operations through a worker. Its non-ROM write implementation also stages
external data through an internal buffer with cache-state transitions.
Linux's PSRAM stacks/data make both concerns relevant, beyond code placement.

Pinned IDF is `24394052fcc462fe27935b4cf15ea1f3e98549c1`.
`cache_utils.c` checks that the active stack is internal before cache-off,
coordinates the peer CPU, and disables unsuitable interrupts. Its API and
generic chip driver provide buffer staging and timeout handling. Their
FreeRTOS scheduler/IPC callbacks cannot simply be called from Linux.

Do not rely on auto-suspend yet. The pinned generic driver's capability test
only advertises suspend for a particular manufacturer; other chip drivers
may differ. The Nano's exact chip ID and suspend support have not been checked.
Newer IDF documentation describes options unavailable or different in this
pinned tree. Suspend support also does not automatically prove PSRAM access
and all current Linux mappings remain safe.

A NuttX issue reporting SMP LittleFS failure contains an unverified proposed
cache fix in a comment. It is not evidence to copy that fix: a contributor
demonstrated both cores and working storage at a named revision, and the
reporter later could not reproduce the issue. The source audit is the basis.

## Proposed Linux implementation boundary

1. Keep normal MTD/filesystem work in sleepable context. A Linux mutex owns
   the flash controller from preparation through result/coherency handling.
2. Prepare bounded internal-memory buffers and command state while external
   memory is accessible. Handle page boundaries, alignment, short writes,
   range arithmetic, write protection, status and errors explicitly.
3. Coordinate both online CPUs. `stop_machine` with callbacks on both CPUs
   is a candidate rendezvous, not the cache-off solution by itself. Linux
   stopper code, scheduler state and normal stacks still live externally.
4. Enter a small, verified IRAM/DRAM guard on both CPUs before changing cache
   state. Use dedicated internal stacks or a proven stack-free parking stub;
   the programming side needs safe stack/register-window handling for its
   ROM/HAL call chain. All peer acknowledgements and guard state stay internal.
5. Program one bounded unit or erase one sector through an audited ROM/HAL
   path, without sleeping, logging, allocating, or touching external data.
   Timeout/error handling must remain executable while flash is unavailable.
6. Restore mappings/cache access and invalidate modified direct flash aliases
   as required, then release the peer and return to ordinary Linux execution.
   Generic stop-machine barriers may run only when external memory is safe.
7. Register NOR MTD semantics and enforce a writable-range allowlist for etc,
   home and an explicitly authorized scratch area. Preserve read-only protection
   of bootloader, table, firmware, NVS/phy, kernel and XIP rootfs.

Prefer the smallest hardware-facing ROM/HAL subset plus a Linux guard, rather
than importing the entire IDF flash subsystem and emulating FreeRTOS again.
Which low-level primitive has an adequately bounded busy-wait remains an
implementation gate; a timeout around an already-blocking ROM call is not enough.

### Placement trap found in this kernel

CONFIG_XIP_KERNEL and CONFIG_ARCH_MTD_XIP are enabled, but CONFIG_MTD_XIP is
not. In the actual `include/linux/mtd/xip.h`, that leaves `__xipram` empty.
Also inspect the current linker's early `.xiptext` collection before relying
on its later relocation stanza. A function annotation is not placement proof.
Use a dedicated explicitly relocated guard section and inspect final ELF
addresses, literals, direct calls, relocations and disassembly.

Internal SRAM is not the normal Linux page allocator. Existing vectors,
atomic guard, ROM state, Wi-Fi pool `[3fcce800,3fcdc700)` and SPI reservation
`[3fccc700,3fcce740)` must remain owned. Static ELF LOAD ranges were recorded,
but gaps alone do not prove free runtime SRAM: cache banks, aliases and
post-handoff dynamic ownership must be audited before selecting a new range.
No additional SRAM region has been claimed. Stack/code budget is unmeasured.

## Filesystem and migration details

Mount separate JFFS2 volumes over the flash defaults for `/etc` and `/home`.
Keep the read-only cramfs executable/library mappings: this preserves the
demonstrated memory benefit for Bash/forkbank. JFFS2 nevertheless adds runtime
metadata/GC costs; reduced RAM consumption is not promised without measurement.
Keep logs and temporary traffic in RAM to avoid continuous flash writes.

Inventory and back up the current SD state before initializing flash images.
Check both capacity limits, preserve owner/mode/symlinks and WPA/account files,
and verify readback. Never auto-format on a failed mount. Retain a known-good
SD boot profile and read-only defaults for recovery.

Today's SD profile also persists `/root`, `/var`, `/opt` and `/usr/local`.
Returning only `/etc` and `/home` changes persistence for those directories.
Decide explicitly whether selected state should bind into home; do not silently
drop or promise to fit all SD content into 4.75 MiB.

Original pre-Linux `/etc/cmdline` and `/etc/board.dtb` overrides are a separate
boot feature. Current firmware uses the compiled boot contract instead.
Restoring writable Linux mounts alone does not restore those overrides.
If required later, reintroduce the read-only early JFFS2 reader and verify
firmware/vector coupling. Its old lack of decompression means compressed
JFFS2 nodes must not be assumed readable by that boot reader.

## Hardware validation gates before replacing SD

- Prove both-CPU entry/exit and SRAM placement with no flash mutation first.
- After explicit authorization and backup, test one isolated scratch eraseblock;
  check erased state, boundary/unaligned patterns, complete readback and aliases.
- Exercise requests from either CPU, concurrent readers, both JFFS2 writers,
  fork/Bash, timer/IPI traffic and active Wi-Fi. Measure pause latency and loss.
- Test failed/timeout operations and return to the ordinary kernel safely.
- Validate mount, create/rename/fsync/unlink, reboot persistence, full filesystem,
  GC and controlled interrupted-write recovery. Track memory before/after.
- Only then package etc/home/table/kernel/init changes together and verify all
  image limits, hashes and readback. Preserve the current working image set.

Research confidence is high on the missing mechanisms and space feasibility.
Safe code/stack placement, exact chip capabilities, implementation size and
Wi-Fi behavior during erase remain unproven. No flashable writable-flash build
is claimed.

## Primary references

- [NuttX flash guard, pinned](https://github.com/apache/nuttx/blob/de5bfd40404953f6c75cf4401ef4ed0a892c2677/arch/xtensa/src/esp32s3/esp32s3_spiflash.c)
- [NuttX MTD/PSRAM-stack path, pinned](https://github.com/apache/nuttx/blob/de5bfd40404953f6c75cf4401ef4ed0a892c2677/arch/xtensa/src/esp32s3/esp32s3_spiflash_mtd.c)
- [Pinned IDF cache guard](https://github.com/espressif/esp-idf/blob/24394052fcc462fe27935b4cf15ea1f3e98549c1/components/spi_flash/cache_utils.c)
- [IDF ESP32-S3 flash concurrency requirements](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/spi_flash/spi_flash_concurrency.html)
- [Original firmware flash server](https://github.com/jcmvbkbc/esp-hosted/blob/e2f19edab6c5bec988346906412cb8e7443661db/esp_hosted_ng/esp/esp_driver/network_adapter/main/linux_flash.c)
- [NuttX issue18469, including resolution](https://github.com/apache/nuttx/issues/18469)

Exact downloaded source snapshots, revisions, hashes, configured Linux files,
proposed CSV and internal LOAD-range report are in
`build-output/flash-rw-research/`. The local S3 TRM remains the authority for
region-specific aliases and cache/peripheral registers during implementation;
no new register sequence was inferred solely from an issue comment.

## Clarified target and NVS/PHY audit, 2026-11-17

User target: no SD requirement; firmware/kernel/cramfs stay read-only; only
/etc and /home are persistent writable flash filesystems. Runtime /run, /tmp
and logs can remain RAM filesystems. Do not carry forward the broader SD
profile's persistent /root, /var, /opt or /usr/local bindings by default.

Current Linux Wi-Fi sets cfg.nvs_enable=0. Its OSI NVS callbacks are RAM-backed
and do not program the NVS partition. PHY uses the compiled phy_init_data
array, an in-RAM calibration buffer and PHY_RF_CAL_FULL at first radio
initialization each boot; it does not load the phy_init flash partition.
The factory MAC comes from eFuse, independently of these partitions.

The matching firmware configuration explicitly disables both
CONFIG_ESP_PHY_INIT_DATA_IN_PARTITION and
CONFIG_ESP_PHY_CALIBRATION_AND_DATA_STORAGE. Its old app_main NVS init/erase
path was removed by patch02; the actual cached app_main has no remaining
nvs_flash_init/nvs_flash_erase calls. ESP_WIFI_NVS_ENABLED remains selected
in the firmware config, but is not evidence of partition use: the firmware
radio initialization path is disabled and Linux supplies its own config.

Thus neither legacy partition is needed by the current boot/radio paths.
Their entries can be omitted from a future validated partition table, leaving
those low flash addresses reserved rather than moving factory/kernel. They
total24 KiB (20 KiB NVS +4 KiB PHY), discontiguous from the proposed etc/home
regions; removing them does not automatically enlarge either filesystem.
No partition was removed, erased or rewritten during this audit. Restore
only/etc and/home persistence first; retaining radio-calibration persistence
would be a separate design decision, not a reason to reintroduce flash NVS
writes outside those two allowed regions.

## TRM review and first implementation, 2026-11-17

The local TRMv1.8 was now directly extracted with pypdf and relevant pages
rendered with PDFium. Visual review of printed page403 confirms table4.3-1
and SRAM1's bus aliases. Pages404–406 establish SRAM0/2 cache roles, shared
I/D cache and the difference between writeback and dirty-data invalidation.
Chapter30 explicitly focuses on GP-SPI2/3; it is not a complete SPI0/1 flash
programming manual. The pinned ROM/HAL is needed for those controller details.

First opt-in guard implementation: experiments/flash-guard/. It uses a162-byte
assembly template copied through the DRAM alias into SRAM1, two2-KiB internal
stacks, and a control block entirely inside an8-KiB reservation at3fcc8000.
No cache-disable or flash command is present. Both stop-machine callbacks
enter the relocated code, report PC/SP, rendezvous with bounded CCOUNT loops,
restore their original SP and return. Sysfs test/result attributes are manual.

Static verifier build/verify-flash-guard.py checks exact embedded template
bytes, final linked branches/control flow (53 reachable instructions), no
reachable padding trap, no literal loads/external calls/cache-register writes,
allowed internal memory base registers, DT bounds and ELF/known-owner overlap.
Actual C callback host harness checks arguments and negative error propagation.
This is not hardware execution or proof that caches may safely be disabled.

The candidate retains the existing EROFS flash driver, partition table, SD
rootfs profile, SMP/forkbank/Wi-Fi. It also quiets normal link/ROM messages per
user request, preserving actual fault diagnostics. A fresh kernel-only hardware
flash signal is needed before the first SRAM/IRAM runtime test. Cache-off,
ROM command integration and JFFS2 persistence are subsequent work.

ROM note: the pinned consolidated S3 linker file exposes both
esp_rom_spiflash_* and higher-level spi_flash_* entries, plus mutable guard
callbacks/data. ROM location alone does not prove absence of leftover IDF
callback dependencies. Exact primitive level and timeout behavior must be
audited before calling one inside the eventual native guard.

## General SRAM as normal Linux RAM: measured cost

The actual configured Xtensa kernel uses FLATMEM. An out-of-tree object built
against its real headers measures sizeof(struct page)=32 bytes (symbol size
0x20), with4-KiB pages. Example only: a candidate free span aligned from the
firmware's static end,3fc95000..3fcc8000, contains204 KiB. Extending the current
flat mem_map high end from3e000000 to3fcc8000 would require230.25 KiB more
page metadata in the straightforward span-based model. This can cancel the
benefit even before runtime ownership/reservations are resolved.

No memory bank was added. Prefer explicitly owned internal pools for necessary
guard/DMA/radio allocations first. General page allocation would need a more
careful memory-model/hole-reclamation design plus benchmarks. The example
span is not a declaration that all its bytes are free at runtime. Exact data:
research/sram-flatmem-cost.json and page-probe/pageprobe.o.

## First hardware gate passed, 2026-11-17

User executed the flashed SRAM guard once: runs=1 result=0 code_bytes=162,
cpu0_internal=1 cpu1_internal=1, flash_operations=0 cache_changes=0.
This confirms both CPUs execute the relocated code, record internal PC/SP,
keep stack canaries and return through stop_machine on this hardware.
It does not prove cache-off execution, erase/program, or JFFS2 persistence.

Next-cache audit: pinned IDF cache_hal.c suspends I then D, and resumes I
then D. esp_rom_cache_esp32s2_esp32s3.c adds cache-idle polling after the
ROM suspend calls when ESP_ROM_HAS_CACHE_SUSPEND_WAITI_BUG is selected.
Do not bypass the applicable erratum workaround merely by calling ROM.
Archived cache HAL, S3 cache LL and ROM patches beside other research sources.

## Cache suspend/resume candidate ready, 2026-11-17

Patch0118 adds manual sysfs mode2; mode1 repeats the original SRAM test.
The same8-KiB reservation remains3fcc8000..3fcca000. Dry code is at+0,
cache code at+0x100 (2,455 bytes), control at+0xe00 (216 bytes), and the
original two2-KiB stacks remain at+0x1000/+0x1800. No flash command, MTD
write-enable, partition-table change or filesystem mount is included.

The flattened template reproduces the rev0 ROM tag-preserving suspend path:
I then D bus shut/cache enable clear, separate autoload-disable/buffer-clear
writes, bounded autoload/preload completion, separate freeze-mode/enable
writes, freeze round-trip and pinned IDF idle workaround. Resume restores
saved writable fields, I then D. No tag, invalidate, writeback, MMU, SPI0/1
command or clock register is written. Pinned header says suspend retains tags.

Both callbacks fully mask INTENABLE before Linux's window spill on the safe
original stack. The final compiler output makes no intervening windowed call
before relocated entry. SRAM checks pending NMI and all99 interrupt routes on
both cores; only CPU0 owns final release. A peer timeout requests abort and
remains internal. Restoration skips untouched D state; waits autoload/preload
DONE before clearing BUFFER_CLEAR/reopening; confirms freeze and both idle
states plus original writable fields before release. Irrecoverable restoration
stays in SRAM until user reset, because ordinary return would be unsafe.

Final review caught and corrected an address-construction error before any
board flash; absolute-MMIO instruction tests now reject such errors. Also
corrected partial-restoration and separate-write ordering to match ROM.

Verified final ELF/image bytes,818 reachable instructions with restricted
SRAM/MMIO bases, local branches and no cache-path external calls/literals.
31 actual-linked-instruction fault/race scenarios pass: both cores, timeout
wraparound, missing peer/master, NMI/prefetch preconditions, partial I/D
suspension, reversible faults and fatal restoration. ASan/UBSan actual-C
callback boundary tests and all16 preserved Wi-Fi/boot suites pass. Build
warnings exactly match the prior SRAM candidate. These are software checks;
physical register windows, cache timing and the exact new layout remain a
hardware gate. The4-KiB PSRAM value probe does not establish dirty-line
residency at guard entry, and its passing alone is not dirty-tag proof.

Artifacts: build-output/flash-rw-research/cache-candidate/.
Verification: build/verify-cache-guard.py ARTIFACTS (includes callback/fault
checks), plus host-tests.log. No UART, board read, reset or flash was performed.
Next: fresh download-mode-ready signal, kernel-only flash, user reset and
manual mode1 then mode2 plus result/cache_state and boot tail.

## Minimum-memory filesystem findings

Exact target-compiled measurements and tradeoffs are now in
`docs/flash-filesystem-memory.md`. JFFS2 is the available raw-NOR starting
point; absolute minimum RAM still needs workload measurements.64-KiB erase
geometry requests4,316 bytes of block arrays across/etc and/home, compared
with69,056 at4 KiB, before node refs/slab/GC. The/etc64-KiB geometry reserves
192 KiB for write/GC progress, leaving roughly256 KiB before format overhead;
verify seeded content fits before choosing geometry. Current uncompressed
write mode already avoids the267,980-byte global zlib deflate workspace.
Inflate is global, not per mount, and requests42,284 bytes. No filesystem or
compression change was made.

Actual FLATMEM high-end rounding refines the earlier SRAM metadata estimate:
a straightforward bank addition requests256 KiB extra, versus the example
204-KiB SRAM gain. PFN validity/hole reclaim is not implemented; use dedicated
internal pools for this task. DFU/double-reset recovery and external DTB remain
parked follow-ups, after writable partitions.

## Cache-test kernel flashed, runtime gate pending, 2026-11-17

Fresh ready signal, expected S3/MAC and unchanged live table verified. Kernel
only at0x140000 written; esptool reported Hash of data verified. CandidateSHA256
5c819b2744a2868b2f47f84342f59e2d8bb8d7e592614faac4a51ccc164c7a39.
No firmware/table/rootfs/SD writes, UART or reset. User now boots and executes
manual SRAM mode1 then cache mode2; native flash writes remain unimplemented.

## Actual stage-1 rejection and source cross-check, 2026-11-17

The flashed cache candidate returned from mode 2 with stage=1, attempt=0,
no cache-off cycles or flash operations, preserved PSRAM values, and saved
error words 0,2,0. The user reports no hang or reboot. The earlier mode 1
again passed on both CPUs. The zero after/off words were unfilled snapshots,
not current register values. The original cpu_internal flags also included
whole-guard success, so their zeros did not establish failed SRAM placement.

The exact rejection is the CPU0 access-status bit at 0x600c40f0, bit 1:
CORE0_IBUS_WR_ICACHE_ST in the pinned ESP-IDF S3 extmem_reg.h. The assembly
captured it before its first cache write and returned precondition status 5.
The C handler then overwrote EBUSY with EIO while rechecking saved flags.
Origin of the already-set latch is still unproven; it cannot be attributed
to this test disabling caches, since the test never reached that operation.

Rechecked the actual pinned S3 cache_err_int.c, archived as
build-output/flash-rw-research/idf-s3-cache_err_int.c (SHA256
 ebe5a0ef83cd408c3aae6558da59c3a56531ec3c62ed925548d1362a391cd9f2).
Its startup clears access-error latches through cache_ll_l1_clear_access_error_intr
before enabling them. The pinned LL implements CPU0 clearing at EXTMEM +0xec;
extmem_reg.h documents bit 1 as a write-one clear. The new retry uses that
specific status operation, not a cache tag/data/enable or flash command.

The TRM v1.8 sections 4.3.1-4.3.3 confirm instruction-bus external mappings
allow reads/fetches, shared I/D caches, SRAM1 aliases and loss of dirty PSRAM
on invalidation. Its peripheral map labels much of EXTMEM reserved: it does
not by itself specify the status-clear register. IDF's S3 definitions supply
that detail. No extra TRM register behavior has been invented.

Rechecked NuttX de5bfd40404953f6c75cf4401ef4ed0a892c2677 spiflash guard and
MTD worker. The peer blocks with non-IRAM interrupts disabled; cache suspend
and resume use the HAL; PSRAM-backed callers need staged/internal execution.
This supports the existing two-CPU/internal-stack guard. It is not permission
to import NuttX/FreeRTOS scheduler callbacks into Linux. Existing ROM-matched
preload/autoload DONE checks, separate write ordering, freeze/idle checks,
partial D-restore handling and CPU0-only release remain in the new template.

Patch 0119 preserves strict mode 2, adds read-only cache_live with setup
before/after flag samples, preserves negative precondition/timeout errno,
separates internal-location checks from guard status, and tracks snapshot
validity explicitly. New manual mode 3 alone may record/acknowledge exactly
CPU0's 0x02 latch when the other two status words are zero. It reads all
three status words back before any cache mutation. Another or recurrent
flag rejects the test. Original flags and acknowledgement remain visible.
There are no flash commands, automatic tests or new writable MTD paths.

The configured image had CONFIG_ESP32_REGS and CONFIG_DEVMEM disabled. The
requested /dev/esp32-regs reads therefore created a temporary ordinary file
in devtmpfs and echoed command text. No hardware register was changed. The
new guard attribute supplies the readback without that driver; reset removes
the temporary devtmpfs file. Never assume that pathname is a character device.

New candidate: build-output/flash-rw-research/cache-latch-candidate/linux.bin,
SHA256 0147c83c02b9743594809db3f4bf6ae05f47c3564eb3e7305302e50dd67ea946,
3,961,456 bytes, 36,240-byte slot margin. SRAM reservation and both 2-KiB
stacks stay unchanged; control grows to 240 bytes within the same reservation.
Exact linked 2,560-byte cache template and 853 reachable instructions pass
39 fault/race scenarios, including the actual observed flag, strict refusal,
explicit acknowledgement, recurrent/mixed/other-CPU error rejection and
unchanged timeout/recovery behavior. Actual callback and sysfs-handler
ASan/UBSan tests pass. Hardware cache-off and native flash remain unproven.

## Cache-latch candidate flashed, hardware test pending, 2026-11-17

Fresh user ready received. Expected S3 v0.1/MAC and unchanged4096-byte live
partition table verified. Only the new kernel at0x140000 was written;
esptool5.5 confirmed its written-data hash. Erase covered0x140000..0x507fff,
within the kernel slot and below rootfs0x510000. Firmware/table/rootfs/SD were
not written. No UART capture or reset. Board left in bootloader for user reset.
See cache-latch-candidate/flash.log, board-check.log and integration-manifest.
Hardware cache suspension remains unproven; user was asked for one quiet-boot
mode1/mode3 test and saved/live diagnostics before any NOR writes are enabled.


## Combined native NOR and SD-free candidate SOFTWARE READY, 2026-11-17

Latest artifacts: build-output/flash-rw-research/nor-candidate/.
Kernel: cb6b11232e1b2505e25aa00ca836493221bafbb7898c76bbd44519cb00730466,
3,974,000 bytes at 0x140000; 23,696-byte margin.
Rootfs: c055c30607cd7cff2aaf4c36b9292abd8c6ff1f4591ce3c7f0554657c072501c,
6,361,088 bytes at 0x510000; 126,976-byte margin.
JFFS2 seeds: etc 0xd0000/0x70000 and home 0xb40000/0x4c0000.
Both stages 3 and 4 are included in this image; stage 3 hardware writes and
stage 4 persistence/no-SD reboot remain pending. No new image has been flashed.

Patch stack 0092..0123 (32 patches). 0120 adds native SPI1 call0 NOR code,
strict etc/home allowlist, explicit scratch gate and locked MTD writes.
0121 includes the pinned IDF USB HOST input settings and live USB diagnostics.
0122 rejects suspended flash and disabled SPI1 clock, preserving status/QE.
0123 fixes a real static blocker: ld coalesced three NOR literals into other
kernel functions. NO_TRANSFORM property flags now keep the copied literal pool
private. Normal instruction relaxation resumes after that pool. Final code is
self-contained; only the verified internal callx0 bridge reaches the call0 ABI
object. The remaining linker ABI warning is intentional at that boundary.

Final checks pass: 75 actual-linked NOR instruction scenarios, 46 linked cache/
peer scenarios, 10 actual init-script scenarios and 18 guard/Wi-Fi host suites.
The Wi-Fi ABI test runs in Docker with the actual pinned 32-bit compiler.
Verified exact image bytes, literals, local branches, SRAM/firmware ownership,
IRQ masking/window spill/restoration, page/partition boundaries, protection/
suspend refusal, timeout parks, two flash-only cache invalidations, full SPI1
register restore, uncompressed JFFS2 inode/dirent/data CRCs and GC reserve.
The etc seed occupies 60,380 bytes of nodes; home 1,652 bytes. These are seed
format sizes, not runtime RAM measurements. Dedicated SRAM grows by 4 KiB to
0x3fcc7000..0x3fcca000 (12 KiB); no general Linux SRAM bank was added.

flash-init boots without SD and mounts only etc/home persistently after the
manual gate. First boot keeps writes locked. flash-state-enable sets a marker
only after both writable mounts succeed. Optional --import-sd exists but was
not requested/run. Root remains XIP cramfs RO; temporary files/logs use RAM.
No auto-association/DHCP. No DFU, U-Boot, SD DMA or RSA work added.

NEXT: obtain fresh explicit download-mode-ready. Do not open UART or reset.
Use reviewed nor-candidate/flash-plan.json: full 16-MiB flash backup, expected
MAC/JEDEC/security check, unchanged live table, firmware and prior kernel/root
hash checks; write only etc/linux/rootfs/home at listed offsets. Require esptool
written-data hashes for all four images. Preserve backup last 64-KiB scratch
slice and old etc/home ranges. Leave reset/log capture to the user.

After boot: read cache_live, mode1 SRAM, mode3 explicit old-flag cache gate,
mode4 read-only SPI1 identification; inspect result/nor_state/cache_state.
Then explicit backed-up scratch_test 0xff0000 (64 KiB within home, unmounted),
inspect results. Only if passed enable rw (echo1), run flash-state-enable,
create persistence probes and reboot without SD. Read-only identification must
pass before any Linux erase/program. Hardware write/cache silicon behavior,
peripheral/network load and persistence remain unproven. Hardware gates 2/4.


## Actual Nano flash matched; installation authorized without backup

User supplied fresh download-mode-ready and explicitly said "no need to backup
just flash". This overrides the earlier backup guideline for this installation;
do not ask again or take a full flash backup. Native USB only; no UART/reset.

Actual esptool flash-id: ESP32-S3 rev0.1, MAC34:85:18:7b:81:b8, manufacturer
c8/device4018, 16 MiB quad 3.3-V flash. User confirms GD25B128EWIGR (U3).
Secure boot and flash encryption disabled; live 4096-byte partition table
matches SHA2568fdbb5214cb860478d612ec2937fe76a03d2217892f04e1a7e6272eb1929d4ec.

The prior Winbond-only JEDEC guard was an incorrect assumption. Patch0124
accepts C84018 via the same existing raw NOR command path; no separate chip
implementation. Pinned IDF spi_flash_chip_gd.c explicitly shares Winbond read,
page-program, sector/block erase operations; it selects64-KiB block erase,
256-byte pages and generic status operations. Archived that exact source.
The pinned Linux gigadevice.c also identifies C8:40:18 as16-MiB GD25Q128 family.
User's exact part attribution is authoritative; JEDEC alone is not a full
package/suffix identification. NOR safety capability check now accepts the
verified C84018 and EF4018 command layouts, not arbitrary NOR/other geometry.

Final rebuilt kernel: SHA256222031a3f5916dc4ab229fb9609b45961d9b54f9a240f65e5c0f576164f2e5c1,
3,974,000 bytes;23,696-byte kernel slot margin. Rootfs and etc/home seeds unchanged.
33 patches0092..0124. 86 actual-linked NOR instruction scenarios now pass,
including GigaDevice identification/read/program/erase/protection cases.
46 linked cache/peer and final image/input/IRQ/SRAM/JFFS2 CRC gates also pass.
10 boot scenarios/18 preserved guard/Wi-Fi suites remain current for their
unchanged code. No new ROM primitive, cache sequence or general SRAM bank.

Four-image flash started using flash-plan.json, esptool5.5, before/after
no-reset. Record final flash.log hashes before marking flashed. No full backup,
firmware write, partition-table write, UART access or user reset performed.
Linux-native NOR write and SD-free persistence hardware gates still pending.


## Four-image flash complete; all esptool hashes passed

Fresh user ready; user explicitly waived backup. Only etc0xd0000/458752 bytes,
linux0x140000/3974000 bytes, rootfs0x510000/6361088 bytes and home0xb40000/
4980736 bytes were written. flash.log contains exactly four matching Wrote
records and four Hash of data verified confirmations, followed by Staying in
bootloader. Esptool process exit0. Erase bounds match flash-plan.json and do
not cover firmware, bootloader, partition table or rootfs/kernel boundaries.
No full backup, UART capture or reset. Firmware/partition table/SD were not
written. Flash installation complete; Linux native writes and persistence
still need hardware tests. KernelSHA256222031a3f5916dc4ab229fb9609b45961d9b54f9a240f65e5c0f576164f2e5c1.

NEXT: user removes SD and resets, waits for quiet healthy boot, then runs a
conditional manual gate: cache_live; mode1 SRAM; mode3 recorded CPU0 flag/cache
check; mode4 read-only identification; scratch_test0xff0000 (64KiB within the
freshly seeded home, only when both MTDs unmounted); echo1 to rw_enable;
flash-state-enable; small etc/home persistence probes and sync. Each step
must succeed before the next. Read result/nor_state/cache_state/mounts/meminfo
and dmesg even after a failure. User's backup waiver remains recorded; no
additional backup was taken. Reboot and read probes with SD absent only after
reviewing the first native erase/program/mount result. Stage3/4 pending.


## SPI1 register-clock preflight correction, 2026-11-18

The new diagnostic image confirmed normal BusyBox PID1 and passed SRAM/cache
gates. Native NOR mode4 rejects SPI1 CLOCK_GATE at0x600020e8, value0, before
any SPI1 command. The earlier predicate requiring CLK_EN(bit0)=1 was wrong.
This field is register-clock gating, not proof of a disabled MSPI functional
clock. Pinned NuttX de5bfd40404953f6c75cf4401ef4ed0a892c2677, archived
esp32s3_spiflash.c:624, explicitly writes0 to SPI_MEM_CLOCK_GATE_REG(SPI_PORT)
before issuing SPI1 USR. Pinned IDF spi_mem_reg.h defines the field at+0xe8;
its spimem_flash_ll.h user_start writes cmd.usr without such a preflight.
Remove this incorrect predicate and leave the inherited register unchanged.
Do not substitute an unsupported shared/functional-clock toggle.
