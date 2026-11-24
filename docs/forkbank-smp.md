# Forkbank with ESP32-S3 SMP

## Status

The integrated candidate builds with SMP, two CPUs and XTENSA_NOMMU_FORK
enabled. The user has now confirmed boot/login and passing fork-test-dynamic,
fork-smp-test and the Bash 5.2.37 suite. Sustained operation and peripheral
regressions remain to be checked. The earlier SMP diagnostic kernel
omitted the fork backend; it must not be used with this project's libfork.

## Why the original kernel/rootfs combination failed

libfork implements fork through clone(SIGCHLD, 0, ...), without CLONE_VM.
The base NOMMU dup_mmap returns success without duplicating private mappings.
Xtensa copy_thread preserves the userspace stack address. Without forkbank,
the resulting parent and child can overwrite each other's stack and data.
The user log's instruction-fetch faults at changing data-memory addresses
are consistent with that corruption. Other independent defects are not ruled
out until the corrected combination is tested.

The original backend explicitly depends on !SMP. Linux's documented NOMMU
interface normally requires CLONE_VM; this project's bank switching is the
additional mechanism that provides private fork memory.

## Scheduling and ownership

The S3 TRM section 4.3.3.2 describes dual-core-shared instruction and data
caches. This backend exchanges the shared cache-MMU table entries at
0x600c5000; those exchanges are not independent process page tables per CPU.
Two siblings cannot simultaneously use the same addresses for different
private contents. A lock around the exchange alone cannot prevent the other
CPU from accessing a sibling's resident bytes after that lock is released.

The SMP fork patch therefore confines each bank family to one CPU. Independent
address spaces can execute on either CPU. This is not a system-wide userspace
CPU pin: after exec replaces a banked address space, the task regains its user
affinity through the scheduler's compatibility-affinity API.

The first bank fork restricts the parent before creating shared bank regions.
The newborn's affinity is set after its mm is copied and before it becomes
runnable. A task field supplies task_cpu_possible_mask, preventing userspace
affinity changes from moving a live bank sibling to another CPU. Reading a
task field also avoids dereferencing an unreferenced remote task->mm.

A raw spinlock protects bank ownership, sibling lists, deferred orphan pages,
remote memory access and the shared MMU transaction registers across CPUs.
Bank switching retains interrupt service during word exchanges; the MMU
transaction retains its short local IRQ exclusion. No interrupt handler takes
the bank lock. Raw locking prevents kernel preemption during the exchange.

The original N-1 shadow-page model, 64 KiB MMU exchanges, bank budgets and
private-memory isolation remain. CPU hotplug is excluded by Kconfig. The
existing restrictions on multithreaded fork and pinned private memory remain.
A failed fork after CPU restriction can leave the parent restricted until exec;
this is safe but is a future affinity-policy optimization.

## Validation before flashing

The normal fork builder applies the full fork stack before its final config
and build, rather than compiling a fork-less intermediate image for release.
Build and packaging explicitly require the backend. The cached-build return
path performs that check too. smp-banks.patch and the scheduler patch are
included in the kernel input fingerprint.

The final packaging checks firmware vector placement, kernel load address,
partition-table/CSV/embedded-DT agreement, image sizes and combined-image
contents. Both kernel and rootfs are rebuilt; firmware and partition boundaries
are unchanged. The candidate disables the parked Wi-Fi blob and KALLSYMS,
removes temporary tracing and no_hash_pointers, and retains host-side ELF and
System.map for address resolution.

Board acceptance commands, after a successful Bash login:

```
cat /proc/cpuinfo
cat /proc/interrupts
/usr/bin/fork-test-dynamic
/usr/bin/fork-smp-test
bash /usr/share/program-tests/bash-test.sh
```

fork-smp-test checks private globals/stack, enforced family placement, release
after exec and independently exec'd workers bound to CPU0 and CPU1. It requires
an initially unrestricted CPU mask and has a 45-second alarm. All three fork/Bash board suites above passed in user-provided output.
Host fork tests alone do not validate the Xtensa bank backend.

References: the local TRM under docs/reference, the original fork README and
patch series under experiments/mmu-poc/fork, pinned Linux scheduler affinity
and exec sources, and https://docs.kernel.org/admin-guide/mm/nommu-mmap.html.


## 2026-11-23 allocator investigation

Concurrent-load64KiB allocation warnings decode to ordinary NOMMU mmap,
not bank_alloc_pages. ForkShadow returns0 after the run. Configured libc
uses64KiB minimum heap growth and already retries smaller mappings; a
warning is not alone proof that malloc failed. A candidate8KiB growth/
4KiB release policy preserves public ABI and PSRAM-safe atomics. Paired
on-board checks reduce anonymous mappings72KiB to40KiB live/24KiB freed;
large allocation/calloc/realloc, threaded allocator, fork/SMP and atomic
checks pass. Rootfs-only package prepared; installed-system acceptance
pending. Full evidence: build-output/memory-allocation-2026-11-23/findings.md.
This does not remove the NOMMU contiguous-allocation requirement or prove
all fragmentation/forkbank problems solved.


## Flash boundary blocker after allocator update

Installed allocator hash/fork/SMP/atomic checks passed. A subsequent large
Fastfetch temporary upload to/home failed EBUSY; writable NOR guard then
latched off and JFFS2 emitted EROFS while mounts still said rw. Home had
4.2MiB free. cache_live reports real MMU fault bit0x20 plus statistics
overflow bit0x80, fault virtual0x43000000 and content0x14000.

Found exact over-read in arch/xtensa/kernel/align.S: fast_load_store and
fast_unaligned unconditionally read aligned EXCVADDR+4 even for byte or
halfword wholly within the first word. An emulated byte in the last mapped
flash word therefore probes0x43000000. Patch0143 fetches a second word only
when width+offset exceeds4. Source mechanism matches retained fault; no
claim of completed hardware recovery. Guard and NOR templates unchanged.
Actual assembled regression passes190 alignment/width/boundary cases;
kernel/config/DTB/SRAM preservation checks pass. RAM hardware harness was
killed during exec on old faulted kernel, not counted as pass.
User declined private/etc/home backup; none copied. No fsck/format/remount
or fault-clear/write-enable bypass performed. Kernel-only recovery package:
build-output/flash-boundary-fix-2026-11-23, SHA256
41c6876e931fbbda8edbe5059f9e510417cc7312ebfcf65cf5cb8b83c9767417.
Fresh download readiness, boot, boundary reads and bounded JFFS2 write
verification required. Other platform tasks remain pending.


## Confirmed resident-memory reduction

The 32KiB-cache firmware has passed startup, CPU benchmarks and bounded Wi-Fi
runs (dual956.23 CoreMark, TX3.95/RX5.82Mbps). Separate kernel patch0145 removes
BIOSET_NEED_BVECS from the private iomap split pool, whose only caller is
bio_split -> bio_alloc_clone with zero requested vectors and shared bi_io_vec.
Both32-bio reserves and the normal writeback bvec reserve remain unchanged.
This preserves filesystem support rather than reducing a forward-progress
reserve. Actual function-body ASan/UBSan tests cover success and failure paths.

Kernel b73c19dc70b0d96e290ba4294e645a77b1d250596da9b6df2cd2592a93cec5ca
was flashed alone with write hash verified. Hardware allocator metadata shows
biovec-max70->38 pages: exactly128KiB reclaimed. Metadata offsets were derived
from the exact configured kernel; only ownership fields were read, no allocated
object contents. Cache names were resolved from the local ELF. Wi-Fi autojoin,
atomic20,000/20,000, fork5/5, SMP fork isolation/placement/exec release and10s
SMP133rounds/266,000updates passed. Concurrent storage passed, ForkShadow0,
flash guard1, no MMU faults or failed Wi-Fi submissions/completions.
Evidence: build-output/memory-footprint-v2.

MemTotal7772KiB excludes420KiB: boot444KiB reserved minus24KiB init returned.
The128KiB saving improves free memory, not MemTotal. One clean-cache reclaim
check returned408KiB but left~2MiB slab resident; it is not a proposed recurring
fix. Large NOMMU process statm values also have a separate unsigned span issue
when stack precedes data; no accounting-only change is claimed to free memory.

Latest timed load/store handler-body costs CPU0/CPU1: idle0.013/0.238%, dual
CoreMark0.0075/0.0654%, Wi-FiRX0.099/0.310%. Entry/exit cost excluded. These do
not identify emulation as the dominant speed limit. RX drops remain under
investigation: core backlog counters zero, intermittent drops, no exact cause
assigned yet. DMA lower priority than memory/forkbank and CPU/Wi-Fi work.

## USB archive extraction and physical fragmentation

The November 24 extraction log records a real allocation failure. Symbolizing
the stack against the retained kernel leads from mmap to alloc_pages_exact.
The 36 KiB copied mapping asked the buddy allocator for an order-4 (64 KiB)
block before trimming unused pages. The failure snapshot had 948 KiB free,
one 32 KiB block, and no blocks of 64 KiB or larger. About 1.2 MiB was dirty or
under writeback on the USB extraction workload.

In this kernel both COMPACTION and MIGRATION depend on MMU. Forkbank's limited
64 KiB PSRAM remapping does not provide virtual mappings and page migration
for arbitrary live allocations. Forcing the compaction Kconfig option or
writing compact_memory cannot provide that support.

A first reversible comparison is to lower vm.dirty_background_bytes to 262144
and vm.dirty_bytes to 524288 before repeating the extraction into a fresh
directory. Record the original byte and ratio settings, the filesystem type,
the result of unzip, /proc/buddyinfo, and Dirty/Writeback from /proc/meminfo.
Restore each original byte value when nonzero or its original ratio otherwise.
The thresholds initiate earlier writeback and throttle writers sooner. Actual
writeback may exceed the thresholds. This can reduce pressure at the cost of
slower USB writes, and does not guarantee the required contiguous block.
No persistent settings have been changed. SSH to the last known Nano address
timed out before the retained kernel and this mitigation could be tested.

The retained kernel later passed live Wi-Fi autojoin, fork 5/5, SMP fork,
20,000 atomics and 142 stress rounds (284,000 updates), with verified 64 KiB
flash storage and no cache/MMU faults. ForkShadow returned to zero. The
biovec-max metadata count is now 6 pages, down from 38 after the first fix
and 70 originally. Total confirmed reserve saving is 256 KiB. USB writeback
comparison requires the USB disk, which was absent during the live checks.

The USB comparison ran on VFAT with a synthetic archive (384 entries,
4.5 MiB extracted). Both the baseline and earlier-writeback phases failed
with unzip ENOMEM. Live kernel logs recorded 36 KiB NOMMU mmap failures
in both phases. The writeback byte thresholds were restored to zero and
the dirty ratios restored to 20/10 after the test. Earlier writeback alone
does not solve this reproducer.

Live /proc/pagetypeinfo reported order-10 pageblocks (4 MiB). The boot
log confirmed mobility grouping was disabled, and all free blocks were
classified Unmovable. At this RAM size, the page allocator cannot have
enough 4 MiB pageblocks to group allocations by mobility. The order-5
candidate uses 128 KiB pageblocks while retaining the same maximum buddy
allocation order. The goal is to let the allocator segregate allocation
types and limit future fragmentation. Live grouping effectiveness needs
measurement. Compaction remains unavailable.

The first acceptance gate is boot with mobility grouping on and evidence
of more than one allocation class on the actual workload. Compare the same
VFAT archive and original dirty ratios on both kernels. Check allocation
warnings and the order-4-or-higher buddy counts as well as extraction exit
status. Retest forkbank, Wi-Fi and flash writes before promoting the setting.

The order-5 build enabled mobility grouping on hardware, with 40 unmovable
and 24 movable 128 KiB pageblocks at the initial snapshot. Both USB tests
still failed the same 36 KiB request, once with original writeback and
once with the earlier thresholds. Once again, there were zero 64 KiB blocks
left. The temporary USB mount was unmounted and the original thresholds
verified. Fork 5/5, SMP placement, 20,000 atomics and 214,000 stress updates
passed. CoreMark was 921.5 versus 924.2 on the retained kernel. Wi-Fi measured
3.84/4.19 and 3.52/3.93 Mbps versus 4.52/7.33 on the retained kernel. These
short tests cannot attribute the Wi-Fi difference to mobility grouping.
Do not promote this candidate. The verified rollback image is staged.
