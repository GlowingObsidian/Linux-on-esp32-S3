# ESP32-S3 Linux SMP bring-up — 2026-11-15

## Explicit PSRAM atomic candidate packaged, 2026-11-22

Hardware follow-up: the candidate was flashed with both written-data hashes
verified. User-supplied console results pass the 30-second test and the
600-second test. The latter took 604.912 seconds, exited 0, and completed
3,268 rounds / 6,536,000 mutex and atomic updates, with cache-fault checks,
concurrent storage, fork/exec, migration, TLS, FP, signals, timers and all
synchronization checks passing. The shared caches report enabled at 16 KiB
instruction / 32 KiB data. Repeated cold boots remain pending; the measured
795.861520 CoreMark score is a separate unresolved performance issue.
The paragraphs below describe the original packaging-time status.

The candidate in `build-output/smp-psram-atomics-2026-11-22` is now packaged
and software-verified. It is **not flashed or hardware-accepted**: the user
reported that the Nano was down. The kernel is 3,907,544 bytes and the rootfs
is 6,426,624 bytes, leaving 90,152 and 61,440 bytes in their existing partitions.
Patch 0136 routes userspace atomics and futex RMWs through the shared software
gate; patch 0137 reports the actual shared EXTMEM cache configuration.
Matching libc, loader, full GCC libatomic, libgcc_s and curl were rebuilt.
The native-CAS audit passed before removing offline Xtensa metadata, with
allocated sections and program headers verified identical after removal.
Dynamic-table checks retain every original export name in libc, the loader,
libgcc_s and curl; 216 other original regular files remain byte-identical.
The UBSan source-derived regression passed 40,000 shared updates and scalar,
lane, flag, invalid-operand and failed-CAS/read checks. This does not prove
the hardware gate or SMP stability.

The image includes `/usr/libexec/smp-validation/atomic-dual` and `smp-stress`.
The artifact directory's `README.txt`, manifests and verified flash helper
record the next steps. Only linux/rootfs writes are planned; UART is not
opened and firmware/partition table/persistent seeds are preserved. Board
acceptance and performance measurement remain outstanding. The clean build's
userspace stages still need the new compiler configuration/library wiring
after candidate acceptance; these artifacts use the preserved incremental
toolchain and library build copies.

## Integration correction, 2026-11-16

The timer-fix log supplied by the user reports `SMP-TIMER: PASS pinned CPU1
timer wakeup` and reaches repeated login attempts without the prior RCU stall
through approximately 104 seconds. This validates the CPU-local timer change;
it does not validate Bash or forkbank.

The diagnostic image omitted the fork patch stack while retaining a rootfs
whose libfork issues `clone(SIGCHLD, 0, ...)`. The cached base kernel's
`mm/nommu.c:dup_mmap()` returns success without cloning private mappings.
`kernel/fork.c:copy_mm()` and Xtensa `copy_thread()` then create a child with
the parent's userspace addresses and stack, without the bank ownership and
switching required to isolate it. This is a concrete corruption mechanism.
The original fork README explicitly prohibits using libfork on the base
NOMMU kernel. Flashing that combination was an integration error.

The log's EXCCAUSE 2 is `EXCCAUSE_INSTRUCTION_FETCH_ERROR`, as defined by the
pinned `arch/xtensa/include/asm/regs.h`; syscall is cause 1. The changing PCs
are observations of invalid instruction fetches, not proof of a specific
cache defect. Both the original forkbank config and diagnostic config disable
FAST_SYSCALL_XTENSA, ruling out a change in that setting as the explanation.

The normal fork build and packaging checks now explicitly require
CONFIG_XTENSA_NOMMU_FORK=y, including the cached-build return path. The new
check rejects the diagnostic config and accepts the retained original forkbank
config. Sixteen kernel-config tests pass. The original full fork patch stack
also applies cleanly over board patches through 0078 in a pinned-source fixture
at build-output/fork-smp-source/base.

The upstream fork Kconfig requires !SMP. Its context switching, bank ownership,
orphan list, remote-memory access and shared cache-MMU programming rely on UP
exclusion. Removing that dependency alone is unsafe. Fork siblings share
addresses backed by different banks and cannot execute concurrently under the
S3's shared mappings. A proposed integration keeps each bank family on one
CPU while independent executable address spaces remain eligible for both;
the implementation and its hardware acceptance requirements are now documented in
[forkbank-smp.md](forkbank-smp.md). The integrated candidate builds, but its
board acceptance tests have not yet run.

KALLSYMS has been disabled in the board seed at the user's request. Existing
partition boundaries remain in place pending measurement of the integrated
image. The integrated kernel and rootfs have now been built and packaged in
`build-output/smp-fork-2026-11-16`. Kernel size is 3,344,440 bytes; rootfs size
is 6,217,728 bytes. Firmware, bootloader and partition-table hashes match the
last flashed candidate. Kernel SHA-256 is
242ab7abad2dbf15c5dd64493b5a0f22e0f160caa00fec8f77bfb0a072b0fd74.
Sixteen config tests, the five-group host fork suite, the sanitizer-backed
bank accounting model (including 100 teardown/refork cycles and allocation
failures), source-overlay comparisons, final embedded DT checks, and rootfs
content checks pass. The new SMP board test cross-compiles. No new flash has
been performed; hardware validation remains pending.

Scope: Linux on both cores. Wi-Fi adapter development is parked. This is
bring-up evidence, not release validation.

## Confirmed on hardware

The second kernel flashed during this investigation printed:

```
smp_init_cpus: Core Count = 2
smp_init_cpus: Core Id = 0
smp: Bringing up secondary CPUs ...
smp: Brought up 1 node, 2 CPUs
```

It subsequently stopped before the login prompt. Visible initcall tracing
showed that USB core initialization returned, and localized the hang to
clocksource_done_booting(). Further checkpoints confirmed that the mutex and
watchdog steps return, then stop_machine(change_clocksource, ...) does not.

A synchronous CPU0-to-CPU1 callback returned successfully. Its remote sample
reported CPU1 INTENABLE=0x4 and zero timer IRQs, while CPU0 reported
INTENABLE=0x44 and 96 timer IRQs. Idle tick suppression prevents concluding
from these counts alone that CPU1 timer setup is broken. The stop-machine
barrier trace is the next test.
Both-core startup is established; successful device initialization, user tasks
on both CPUs, IPI stress, and stability are not established.

## Defects found in patch 0075

1. Enabling HAVE_SMP inherited SECONDARY_RESET_VECTOR=y. The linker placed
   that section at the ROM reset address 0x40000400, then placed the following
   exception handlers there. Short jumps between those handlers and vectors
   at 0x40379000 failed to link. ESP32 secondaries enter through ROM using
   ets_set_appcpu_boot_addr(), so a Linux handler at the ROM reset address is
   inappropriate. Kconfig now excludes that option for this SMP platform.
   The corrected ELF places .exception.text at 0x403794f0 and links.
2. The startup assembly tested the whole PRID value, and the C helper masked
   its low 14 bits. On ESP32-S3 the core number is bit 13. CPU0 therefore
   entered the secondary wait path before printing anything. Both paths now
   extract bit 13, matching the pinned IDF's xt_utils_get_core_id(). The
   second boot log above confirms the resulting primary/secondary startup.
3. ipi_init() runs in init_IRQ(), before the esp32-intc platform driver is
   registered. Its domain cannot be used there. IPIs now use the existing
   root Xtensa PIC domain, with direct DT-based routing in both matrix banks.
4. request_percpu_irq() requires a per-CPU-devid descriptor; the old mapping
   did not create one. The corrected path uses handle_percpu_irq and
   request_irq with IRQF_PERCPU and IRQF_NOBALANCING.
5. Input 1 was called free but is UART0's input in the DT. IPIs now reserve
   level input 2. The later matrix initialization restores the IPI rows
   instead of disconnecting them. CPU1 enables its IPI input and lets the
   timer setup enable its own interrupt.
6. Payload read/OR/write and read/clear could lose an interrupt request.
   set_bit() and xchg() now provide atomic producer/consumer updates.

The ROM startup now clears its entry address, releases the core, and publishes
the fresh entry address, following the ROM-wait pattern used by the references.
RTC stall masks and SYSTEM register addresses were checked against pinned IDF
headers and the local TRM. IPI correctness still needs runtime stress proof.

## Build and flash evidence

The existing cached toolchain and Buildroot kernel tree were reused. Targeted
incremental `make ARCH=xtensa CROSS_COMPILE=... xipImage` builds were used for
boot experiments; these are not full release-wrapper runs. No toolchain/stage
markers or volumes were deleted.

Patch 0075 was regenerated from source diffs. Relevant files were separately
extracted from the pinned Linux 7.2.4 archive, patches through 0075 applied in
order, and the result compared byte-for-byte with the intended source.
`build/test-kernel-config.py` passed all 14 tests.

The diagnostic config disables ESP32_WIFI_BLOB and the wifi_blob DT node.
Disabling only the node leaves the blob linked: that SMP image was 4,104,264
bytes, exceeding the existing 3,997,696-byte kernel partition. With the blob
excluded, the PRID-corrected image was 3,749,928 bytes and SHA-256
731b99d7f358149510a6a59126c1c2d85eb4a22679c0c92ab7112ffb427a0d0a.
Compiled disassembly confirmed PRID bit-13 extraction in both startup and
smp_init_cpus before the second flash.

Firmware, bootloader, partition table, and rootfs were reused from the existing
packaged artifacts, checked against their manifest. First flash wrote those
and the kernel; second flash wrote only linux. Esptool verified each write.
Offsets are bootloader 0x0, table 0x8000, firmware 0x10000, linux 0x140000,
rootfs 0x510000. Kernel vectors agree with firmware at 0x40379000.

Diagnostic artifacts and logs are under `build-output/smp-bringup-2026-11-15/`;
build logs are `build-output/smp-{link,ipi}-check.log` and
`build-output/smp-candidate-build.log`. Artifact files may change between boot
attempts; retain manifests and flash logs to identify the actual image.

## Compatibility and remaining work

The software fork-bank backend explicitly depends on !SMP. Do not remove that
restriction or claim fork validation on the diagnostic kernel. Its rootfs is
reused, but the diagnostic kernel has no fork-bank support. The normal release
pipeline still expects the fork kernel. An explicit SMP build profile and its
acceptance checks remain work after boot stabilization.

No UART console capture is authorized in this investigation. The user boots
the board and supplies logs. Each flash requires their download-mode signal.

References: local TRM v1.8, chapter 9 (rows 79/80 and matrix banks) and chapter
17 (core control and software interrupt registers); pinned IDF
`components/xtensa/include/xt_utils.h`, `soc/esp32s3/include/soc/system_reg.h`,
and `soc/esp32s3/include/soc/rtc_cntl_reg.h`;
[NuttX CPU startup](https://github.com/apache/nuttx/blob/master/arch/xtensa/src/esp32s3/esp32s3_cpustart.c),
[Zephyr S3 core release](https://github.com/zephyrproject-rtos/zephyr/blob/main/soc/espressif/esp32s3/esp32s3-mp.c).

The pinned IDF's components/esp_hw_support/cpu.c explicitly emulates CAS for
external RAM using an internal DRAM lock. Linux's native S32C1I paths operate
on PSRAM in this memory layout. This is a concrete compatibility concern,
but it has not yet been established as the cause of the stop-machine wait.
NuttX IPI/timer/atomic reference files were downloaded at revision
c803668d6c763c823b3286df1cc40c528abadf3e into
`build-output/nuttx-smp-reference/`. NuttX's periodic S3 tick uses SYSTIMER;
this Linux port uses per-core CCOUNT/CCOMPARE, so timer code is not a direct
transplant.

## Atomic adaptation candidate (hardware verification pending)

Patch 0076 routes atomic RMW, atomic_set, bit operations and native-sized
release stores through an IRQ-masked software operation protected by one
internal-SRAM guard. Its own guard acquisition uses native S32C1I on SRAM,
never Linux atomic/locking APIs. Full-word spinlocks and writer-preferring
read/write locks avoid mixing independently modified queue bytes with an
emulated word-wide compare/exchange. This is a correctness-first S3 bring-up
implementation; fairness/performance and stress behavior remain to be tested.

TRM v1.8 chapter 4, table 4.3-1 and section 4.3.2 establish the SRAM1 aliases:
IRAM 0x40378000 and DRAM 0x3fc88000 represent the same memory in the same
order. The guard occupies the last four bytes of the firmware's existing
4-KiB vector reservation: IRAM alias 0x40379ffc, DRAM 0x3fc89ffc. A linker
assertion prevents overlap with exception handlers. A boot relocation entry
initializes the guard to zero. This does not claim bytes from the GDMA window.
The ELF and raw xipImage were checked for the guard placement, initialization
and relocation entry before flashing. Patch application was verified
byte-for-byte against its source target.

The candidate kernel is 3,766,384 bytes; SHA-256
fd90bbb08f16a5c3429234798da39a97d4600cd8d4c61d5fe43747323b013963.
It includes four synchronized two-CPU rounds, each requiring exactly 4,000
atomic increments. This test and actual full-boot/stability evidence are still
pending. Do not describe the PSRAM race diagnosis as hardware-confirmed yet.

## Atomic candidate board result and clock handoff

The user-provided atomic-fix boot log passed all four concurrent PSRAM atomic
rounds (4,000/4,000 each), completed the CPU1 callback, and returned from the
clocksource stop-machine transition. It still cut off around the clock driver
initcalls. This establishes that the adaptation passes that bounded test; it
does not establish the original atomic path failed or that all SMP hangs are
resolved.

The next initcall after gated_fixed_clk_driver_init is gpio_clk_driver_init.
Its devm_gpiod_get(..., GPIOD_OUT_LOW) forces gate outputs low. The UART0
clock node maps that GPIO to SYSTEM_PERIP_CLK_EN0_REG bit 2. This can disable
the live early console before the UART driver claims its clock. The SPI host
and ST7789 initcalls occur later, so lack of D13/SCK activity does not establish
that those drivers have failed.

Patch 0077 preserves inherited GPIO gate values on S3 machines while setting
output direction. Other machines and mux clocks retain their original
behavior. The handoff hazard is source-confirmed; explaining the observed
cutoff through it remains an inference pending the next boot. Candidate
SHA-256 e9919b07e58f5d8238bd6fd3009f269655ee77e3c8b238342c55e8269843bb09,
3,766,384 bytes.

## Clock preservation board result and remaining timer fault

The clock-preservation image completed all 251 logged initcalls. UART0 clock
registration, UART console handoff, SPI, ST7789 framebuffer console, USB host,
GPIO, I2C, and GDMA initialization returned successfully. Cramfs mounted and
/sbin/init started startup scripts. That removes the previous console-clock
handoff blocker. Peripheral initialization is not transfer/stress validation.

At 25 seconds RCU reported CPU1 timer-softirq=0 and a missing timer wakeup for
the rcu_sched task scheduled on CPU1. The new source diagnosis is that the
CCOUNT clockevents use per-CPU irq_enabled flags but generic enable_irq and
disable_irq_nosync share one descriptor nesting depth. Independent CPU idle
transitions can therefore mark a timer enabled while its CPU-local INTENABLE
bit remains masked. Patch 0078 controls only LINUX_TIMER_INT on the current
S3 CPU, with local IRQs saved/restored. Other interrupt inputs are preserved;
other Xtensa platforms keep the prior path.

The timer candidate includes a TIMER_PINNED callback explicitly placed on CPU1
with a bounded wait from CPU0. It must pass, and the later RCU stall must cease,
before the timer diagnosis is called hardware-validated. Candidate SHA-256
33362f21ab7c39841de7df345a4631f6212e5a55e6dc83c23a9675f27a84390b;
3,766,376 bytes. Redundant loglevel=8 was removed so diagnostic boot arguments
fit the 256-byte command-line buffer (253 bytes) instead of passing a truncated
"l" to init.

Other observed follow-ups: RSA initialization returns -ETIMEDOUT without
registering acceleration; cache-info sysfs returns -ENOENT; fault-injection
init returns -ENODEV. I2C's earlier -EPROBE_DEFER resolves at late probe. The
reused diagnostic rootfs still carries retired /home startup scripts and must
be replaced through the normal rootfs cleanup path before release validation.


## Board forkbank/SMP acceptance, 2026-11-16

The user confirmed the flashed kernel/rootfs boot and accept login. On that
image, fork-test-dynamic passed all five groups: independent globals/BSS/stack/
heap with 64 pipe handshakes, nested fork, fork+exec, signals, and repeated
fork/teardown. fork-smp-test passed private-memory isolation, enforced bank
family placement, release after exec, and independently exec'd workers on
both CPUs. Bash 5.2.37 passed its complete suite including pipelines, process
substitution, mapfile, wait, signals, and ten repeated command substitutions.
These are user-reported hardware results for kernel SHA-256
242ab7abad2dbf15c5dd64493b5a0f22e0f160caa00fec8f77bfb0a072b0fd74
and rootfs SHA-256
22848b7c67e6e5036ed4ff61b66fa52a2d5a08740cb004c478f0172f62543fa4.
Sustained operation, repeated boot and peripheral regressions remain pending.

An initial report of no login input prompted inspection of the actual rootfs.
It contains an empty-ID display getty despite trim-target.sh having a fix.
That is a real packaging defect which can attach an extra process to the
serial console; it was not proven to cause the reported input problem. The
user subsequently confirmed input works without another flash. A final-pack
normalizer and four regression tests were added; the prepared console-fix
rootfs differs only in /etc/inittab and has not been flashed. Do not confuse
its results with the hardware-validated original candidate.


## Repeated SMP/fork board test, 2026-11-16

Five consecutive iterations of fork-smp-test followed by fork-test-dynamic
passed completely in user-provided output. Timer counts advanced CPU0
5391 -> 6328 (+937), CPU1 3920 -> 5048 (+1128). IPI counts advanced CPU0
780 -> 1140 (+360), CPU1 491 -> 662 (+171). Function-call counters advanced
on both cores; CPU1 rescheduling count advanced 57 -> 62. ForkShadow was
zero both before and after. Available memory was 3192 -> 3188 KiB; a single
page difference is not proof of a leak or proof that all leaks are absent.
Recovered pages increased by 10664 KiB and MMU-exchanged traffic by 50624 KiB;
these are cumulative counters, not memory currently allocated. Maximum bank
switch time was 13042 us. SPI and USB IRQ counters stayed zero, which does
not validate peripheral operation or by itself establish a driver failure.
The final dmesg command/output was garbled, so absence of kernel errors is
not yet established by this report.


## Post-stress log excerpt, 2026-11-16

The user's dmesg tail ends at boot timestamp 7.678261 and shows no RCU stall,
lockup or unhandled exception in that excerpt. It confirms readonly cramfs
mount at 0x42510000 and init startup. USB repeatedly attempts full-speed
enumeration, logs Not connected, and attempts a port power cycle. In the built
DWC2 source, hcd.c:1871 emits Not connected and returns -ENODEV when the cached
port_connect_status is clear during URB enqueue. This establishes a USB
enumeration problem, not its physical or SMP-related cause. The board is
configured for host mode and has no VBUS regulator in the DT; the user's USB
connection/topology is awaiting clarification before further diagnosis.


## Peripheral follow-up candidate and benchmark, 2026-11-16

On the original hardware-tested image, the user completed three Bash prime
benchmark rounds (two equal jobs sequentially, then two independently exec'd
workers concurrently). All 12 jobs returned 669 primes through 5000. Means:
182.526 seconds sequential vs 139.629 seconds parallel, 1.30722x speedup and
23.5019% less elapsed time. Parallel user+kernel CPU time divided by elapsed
was 1.91–1.92 cores. Total benchmark duration was 966.465 seconds. No task
affinity or CPU offlining was used. The user also reported 39-minute uptime
on that same boot. Shared cache/memory contention is a plausible performance
limit, not a measured diagnosis.

Patch 0080 fixes a source-level SMP gap: the peripheral matrix selects a CPU
while the inherited root PIC mask/unmask callback accesses current-CPU
INTENABLE. The new S3 SMP child irqchip gates the source in its target matrix
and requests CPU-local input enable through a nonblocking IPI when remote.
It preserves fixed DT affinity and leaves timer/IPI inputs untouched. The
three host tests execute the actual changed callback/helper bodies with
mocked registers, checking remote delivery/coalescing, mask-before-deferred-
enable completion, fixed affinity and IRQ-state preservation. These validate
state transitions, not electrical IRQ delivery. The kernel builds and links.
NuttX esp_irq.c uses CPU-specific handles in up_enable_irq; the TRM chapter 9
describes the two interrupt matrix banks. The Linux integration is an S3
irqchip adaptation, not a direct transplant of NuttX code. USB/serial symptom
causation is still unproven.

A verified, unflashed candidate at build-output/smp-peripherals-2026-11-16
contains that kernel, final-pack console correction, real util-linux 2.39.3
lscpu, and a bounded pthread/atomic test. The test is needed because uClibc
NPTL uses native S32C1I while kernel RMW operations use the SRAM guard; the
process/Bash tests do not establish shared userspace-thread atomic behavior.
All existing selected applications remain. Rootfs has 131072 bytes free.
Firmware, bootloader and partition table match the running baseline.

The handoff audit confirmed in firmware disassembly that map_flash_and_go
executes rsil a2,3 followed by wsr.ps a2, restoring old PS and undoing its
intended mask. This is a pre-entry race, not an observed runtime failure. A
draft correcting it was generated/verified against pinned upstream and saved
under build-output/firmware-handoff/02-handoff-fix-unbuilt.patch. Automatic
approval review rejected its firmware build because vendor CMake can execute
git reset --hard in the mounted esp-hosted tree. No firmware build/write was
performed. The firmware patch's effective source content was restored to the
working version, preserving existing user changes; the unbuilt draft is not
in the new candidate.

The cached firmware app_main calls only linux_boot: no radio/transport loop
continues beside Linux. Internal SRAM nevertheless is not Linux's general
page allocator: the DT memory node is PSRAM. Vector/atomic reservations,
cache-owned SRAM, ROM state and GDMA descriptor/buffer ownership require an
explicit map. Historical GDMA notes about a concurrent IDF core and held SPI
DMA channel do not describe this boot-shim firmware. GDMA remains incomplete
and unchanged. network_adapter is a retained build/artifact name, not an
active network adapter service.


## First peripheral-fix board result, 2026-11-16

The user confirms login and actual util-linux lscpu output: Xtensa, two CPUs
0 and 1 online. Two one-core sockets are the currently exported Linux topology,
not two physical chips. pthread-smp-test terminated by its 45-second alarm
before printing either PASS marker; this is a failed/timed-out check, not
proof of working cross-core pthread synchronization. The same test compiled
and ran successfully on the build host, ruling out an obvious barrier logic
deadlock but not a target-specific test issue. The actual Xtensa futex.h still
uses native S32C1I on user addresses; patch 0076 did not cover these helpers.
Both native userspace and futex atomic paths require further audit; the exact
timeout phase is not yet established.

The new ENA counter reports 4 on CPU0: the remote peripheral enable IPI path
has executed. UART has 58 interrupts on CPU0; USB now has 1, compared with
zero in the prior report. These show delivery, not successful USB enumeration.
Timers continue on both CPUs (3361/1844). SPI remains zero and is unvalidated.
No claim of complete SMP/peripheral/thread correctness follows from this log.


## Peripheral-fix USB success and mutex wait localization, 2026-11-16

The new image enumerates USB VID 1908/PID 0226 as full-speed mass storage,
attaches SCSI sda, reads 1984000 512-byte sectors of capacity, identifies
sda1, and mounts FAT. The user reports access appears to work. The log
includes an initial disconnect and a later successful discovery. FAT reports
a dirty-volume flag; this is not proof of new SMP corruption. Sustained
transfer/data-integrity tests remain pending. Memory after USB use: total
7772 KiB, used 4032, free 2384, buffers/cache 1356, available 2576. This one
snapshot does not establish a leak; reclaimable cache and permanent/slab
allocations need separation.

For the timed-out pthread test, proc/task shows parent TID156 sleeping in
read(3,...), worker TID157 restricted to CPU0 and worker TID158 to CPU1, both
sleeping in syscall191 futex at the same address 0x3dc3651c, operation0x80
(FUTEX_WAIT_PRIVATE), expected value2. Thread creation and requested placement
have succeeded. Both mutex workers are waiting rather than completing the
mutex phase; native userspace atomic phase has not yet been reached.


## Load/store exception cost measured on both cores, 2026-11-23

The current cpuinfo fast_handler_count/total fields are per-CPU u32s, fetched
through this_cpu_ptr without CPU labels or aggregation and printed with%d.
An unpinned cat can therefore sample different CPUs. Both fast_load_store and
fast_unaligned enter the shared counter path in arch/xtensa/kernel/align.S.
Timing begins after initial register saves and ends before final restores/rfe;
reported cycles measure the handler body, not complete trap latency. Failed
fast decodes fall through to the C path without the normal timed exit. Do not
treat these fields as a complete atomics profile or a corruption count.

A small userspace sampler built with the existing PSRAM-safe toolchain pins
itself to CPU0 then CPU1 before reading. Per-core before/after values use
unsigned32 modular differences and240MHz. No kernel/settings change was made.
Whole-process intervals include executable startup and CoreMark calibration.

Measured handler-body CPU percentages (CPU0/CPU1):
-10-second idle:0.1828%/0.0191%.
-Single CoreMark:0.0367%/0.0344%; score503.570775, validated.
-Dual CoreMark:0.0089%/0.0899%; score798.296966, validated; scaling1.5853x.
-20-second Wi-Fi TX:0.2929%/0.3835%.
-20-second Wi-Fi RX:0.0785%/0.3751%.

Thus the large cumulative counts are real but measured time is small in these
workloads. Even allowing unmeasured entry/exit overhead, these observations
strongly argue against fast load/store emulation being the main explanation
for the CoreMark or Wi-Fi gap. Actual offending PCs/opcode classes were not
profiled, and the full cost is not claimed. Raw per-core counters, process
output, source and method limitations are saved in
build-output/wifi-backpressure-2026-11-23/cpu-exceptions/.


## Controlled compiler experiments, 2026-11-23

Espressif's current official CoreMark component selects-O3, jump tables and
tree switch conversion; current accepted benchmark uses-O2. Matching those
flags alone measured508.177 single and773.450 dual, CRC-valid, versus current
503.571/798.297. No repeatable improvement established; no working benchmark
was replaced. First-O3 LTO plus no-semantic-interposition variant failed CRC
(list/state, matrix passed). Adding no-strict-aliasing did not resolve it.
Those invalid variants are rejected, not reported as speed gains. A pure LTO
variant is prepared to isolate the flag interaction, not yet board-tested.
No kernel/compiler default was changed from these results. Raw sources,
flags, disassembly/audit and logs: build-output/performance-2026-11-23/.
Source: https://github.com/espressif/idf-extra-components/blob/master/coremark/CMakeLists.txt

A checksum-verified PSRAM streaming probe pinned independent workers to CPU0
and CPU1. For 8 KiB per worker, one core measured 171.70 and 181.57 MiB/s
around a two-core 359.62 MiB/s run. For 128 KiB per worker, the same sequence
was 44.62, 50.04, 45.47 MiB/s. That gives about 2.04x for cache-resident data
and 1.11x for the streaming case. It isolates a shared-memory bandwidth limit
for this workload. CoreMark's time spent waiting on that path is unknown.
Evidence: build-output/nommu-pageblock-128k/psram-smp-baseline.log.

A read-only sampler used the pinned IDF extmem_reg.h offsets. Chip-wide
instruction-bus misses increased by 2,300,959 around the single CoreMark
invocation and 2,099,851 around dual. D-bus PSRAM misses increased by
182,160 and 528,452, respectively. The single/dual validated scores were
526.1 and 909.8. The access totals had already saturated, so no miss rates
are calculated. Samples include calibration, Linux activity and shell
overhead. This is evidence to pursue shared data-cache contention, without
ascribing a portion of CoreMark time to it. The TRM section 4.3.3.2 describes
shared I/D caches and arbitration of cache misses to external memory.
Evidence: build-output/wifi-rx-batching/coremark-cache.log.

### S31 reference and S3 D-cache sizing

The S31 July 21 release tag 294b135 points to Linux 759f403. Its feature
commit e5f2ceb replaced the uncached PSRAM mapping with a cached mapping.
The RISC-V kernel adds S31 cache maintenance for instruction publication
and noncoherent DMA, and writes back the early page tables before enabling
Sv32. The release records 949.9 single-core CoreMark. The Nano already
uses flash XIP and cached PSRAM on Xtensa, so the S31 register and MMU
sequence does not transfer.

Current S3 hardware uses a 32 KiB I-cache and 32 KiB D-cache. TRM section
4.3.3.2 and pinned IDF support a 64 KiB D-cache in SRAM2. IDF excludes the
extra bank at 0x3fcf0000..0x3fcf8000 from the heap in this mode. The isolated
firmware candidate changes only the D-cache capacity. The SRAM2 range
passed ELF-load and firmware-symbol overlap checks, Linux/firmware IRAM
ownership checks passed for both kernels, and the bootloader matched
byte-for-byte. The compiled startup passes 65536 bytes, eight ways and
32-byte lines to the ROM configuration function. No flash was written.

Checksum-verified baseline sweep on the 32 KiB D-cache used identical
code pinned to one or two cores. Aggregate dual throughput was 349 MiB/s
at 8 KiB per worker, 300 at 16, 55 at 24, and 52 at 32. The 24 KiB point is
the first target for a same-kernel comparison. Kernel, firmware and radio
settings should otherwise match. Run the identical CoreMark binaries and
check Wi-Fi, fork/SMP and flash before evaluating adoption. The sweep and
firmware evidence are in build-output/dcache64-candidate.

Espressif's S3 datasheet v2.2 lists 1329.92 dual-core at 240 MHz. The
revision history records a score update in v2.0, without attributing
it to compiler flags. Espressif's public IDF CoreMark component uses
a no-flash linker mapping and -O3 with jump tables and switch conversion.
The public configuration is a benchmark reference. Its placement and
toolchain differ from the Linux FDPIC benchmark and it does not establish
the exact conditions of the datasheet run.

### Hardware result with the 64 KiB D-cache

Measured on the same pageblock kernel with the same benchmark binaries.
The live setting was 65536 bytes, with 32-byte lines. Two-worker reads
at 24 KiB per worker rose from 55.22 to 253.32 MiB/s, and at 32 KiB from
52.20 to 320.11 MiB/s. At 128 KiB each, streaming rose from 48.18 to
52.38 MiB/s. Single CoreMark 526.1 -> 537.6 and dual 921.5 -> 971.4
validated. The user measured 996.3 and 967.0 in two additional dual runs,
the latter after bringing Wi-Fi up.

TCP was 4.88 / 7.34 Mbps upload/download, versus 3.84 / 4.19 and
3.52 / 3.93 on the same kernel with 32 KiB D-cache. The prior retained
kernel measured 4.52 / 7.33. These are short runs and the Wi-Fi numbers
do not isolate a causal improvement. Driver recorded zero failed TX
submissions and completions. The user connected via the saved network
with the wifi command. This does not verify autojoin.

Cross-core atomics, SMP fork, 298,000 updates, and the verified 64 KiB
flash write passed. Flash guard stayed enabled with no MMU fault.
Retain 64 KiB and reflect it in the Nano firmware defaults. The staged
Wi-Fi batch kernel now expects this exact firmware, restores the retained
allocator config, and keeps batching off by default for on/off comparison.
