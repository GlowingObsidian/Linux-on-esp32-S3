# Writable SD rootfs: implementation status

Updated 2026-11-16. Approximate overall progress: 85%. Confidence: medium; allocation-failure diagnosis confirmed.
This is engineering progress, not a probability of successful first boot.

## Required result

Boot firmware and Linux kernel remain in onboard flash. The entire writable
rootfs is on the 1 GB microSD card. Keep SMP, forkbank and Bash functional.
Disable the SPI display, framebuffer and display console/getty. Keep the tested
UART configuration. SD uses D10/GPIO21 CS, D11/GPIO38 MOSI, D12/GPIO47 MISO,
D13/GPIO48 SCK, with low-speed initialization and up to 20 MHz data clock.

## Verified work

- Reviewed the existing experimental GDMA driver and NuttX SPI/GDMA sequence;
  defects and ownership limitations are recorded in `spi-dma-findings.md`.
- Generated patch 0081 from the exact current SPI driver, applied independently
  and byte-compared against its target source.
- Fixed SPI clock selection for low-speed initialization, RX-only dummy bytes,
  GPIO CS support, native CS preservation and error propagation. Advertise only
  the byte transfers and single-lane modes actually implemented.
- Actual C callbacks pass host ASan/UBSan checks for clock range, 400 kHz,
  20 MHz, RX/TX/full duplex, odd lengths, 512-byte transfers, CS and errors.
- Cross-compiled `drivers/spi/spi-esp32.o` successfully with the cached Xtensa
  toolchain in the integrated SMP/fork kernel. This initial
  patch implements PIO prerequisites; later patches add the DMA path.

## POC build result

- Added SPI GDMA patch 0082 with separate internal SRAM TX/RX buffers and
  descriptors, 4092-byte chunks, SPI interrupt completion, TX total-EOF and RX
  EOF/length checks, error recovery, and a guard against the old GDMA misc driver.
- Host ASan/UBSan tests exercise actual PIO and DMA callback code, including
  unaligned caller buffers, multiple chunks, lost IRQ, short receive, missing
  total-EOF and descriptor errors. These do not emulate silicon timing.
- Added S3 external-cache publication patch 0083 and `fork/sd-exec.patch`.
  Read-only RAM file text stays shared across fork; writable executable file
  mappings remain rejected. Cache-register transactions and fork/MMU operations
  share a raw lock. Host checks verify D-bus writeback before I-bus invalidation.
- `build/build-sd-kernel.sh` built an SD-profile XIP kernel successfully using the
  cached toolchain. The prior configuration and image were saved. New output is
  separate from the normal pipeline's shipping artifacts/stamps.
- `build/make-sd-rootfs.py` migrated the verified working CramFS into a writable
  ext2 image, preserving Bash/fork tools. Only UART getty remains; /tmp is on SD.
  ext2 passes e2fsck; image is 896 MiB, with partition 1 starting at 1 MiB.
- The actual firmware ELF has no LOAD segment overlapping DMA SRAM or its IRAM
  alias. The pool ends below the firmware linker's ROM-reserved boundary.
- `build/verify-sd-artifacts.py` verifies the packaged kernel's embedded DT,
  configuration, GPIO CS, 20 MHz ceiling, CPU0 SPI IRQ, SRAM, flash partitions,
  MBR/ext2 signature and image SHA.

Artifacts: `build-output/sd-rootfs-poc/`.
Kernel SHA: `29fa4418b89d0e92db9a349d534f9cc48237c0559eca9c576147b04effc6a32a`.
SD image SHA: `6d921d604bc064bf2d5942f9f194844256dd74746931ce97cb5408710d7ccb59`.
There is an existing initcall section-mismatch build warning; it is recorded in
`kernel-build.log`. No SD hardware boot or execution regression has passed yet.

The identified card is `/dev/disk5`, 1,015,808,000 bytes, removable USB, original
FAT32 volume SD UUID `3AB0A82A-7C7B-3270-A02D-26658F51D1A4`. The user explicitly
approved erasing/writing it. Its 12 visible files were copied and SHA-verified
under `build-output/sd-card-backup-2026-11-16/`; Spotlight metadata was inaccessible.
macOS denied raw-disk access even via administrator-authenticated osascript.
The command failed on the first backup-sector read, before any card write.
A guarded Terminal writer is prepared at
`build-output/sd-rootfs-poc/write-card.sh` and checks a full readback hash.

## Remaining acceptance gates

1. SPI DMA with exclusive pair/SRAM ownership, correct RX/TX descriptors,
   completion/error handling and bounded internal buffers; disable conflicting
   experimental GDMA access.
2. SD device tree, MMC_SPI and writable filesystem configuration; remove display
   resources and configure UART-only console.
3. Correct execution of RAM-loaded SD binaries, external cache maintenance and
   forkbank handling of read-only executable mappings. Measure RAM cost under
   Bash and fork tests; do not silently discard these features.
4. Profile-aware build/package checks and a populated SD image with verified
   ownership, modes and boot configuration. Preserve cached build stages and
   the known-working flash-rootfs image.
5. Hardware: card detection, DMA data correctness, root mounted read/write,
   persistence across reboot, fork/Bash regression and concurrent CPU/I/O stress.

The user ran the guarded Terminal writer successfully. All 896 MiB read back
with SHA matching the image, and disk5 was ejected. This is SD media verification;
SD-over-SPI operation and root boot on the Nano remain unverified. User handles boot logs; do not open
the UART capture. Flash only after the user signals download mode.

## Flash completed

The board was running the Arduino partition layout (app0/app1/ffat). After the
user signaled download mode, identified ESP32-S3 MAC 34:85:18:7b:81:b8 on
usbmodem101, backed up 0x0..0x140000, and restored bootloader@0, SD partition
table@0x8000, matching existing handoff firmware@0x10000 and SD kernel@0x140000.
DIO/80 MHz/16 MB settings match the firmware build's flash_args. Firmware ELF
space_for_vectors matches kernel CONFIG_VECTORS_ADDR=0x40379000. esptool reported
four successful flash hash checks and left the board in the bootloader.
No UART capture or automatic boot/reset was performed. Await user boot log.

## First SD boot and correction

The first boot log is preserved under `build-output/sd-rootfs-poc/first-boot/`.
Both CPUs start and MMC identifies SU01G, 969 MiB. DMA-sized status/data reads
return -110; sector 0 fails, so partition 1 is not discovered. Separately ext2
rejects rootflags=noatime. Userspace and executable-cache changes were not reached.

Patch 0084 aligns GDMA internal clock enabling and SPI master FIFO controls with
NuttX and the pinned IDF source, adds a hardware identity readback and error-phase
diagnostics. Remove rootflags=noatime from the SD DT. Tests now cover the omitted
initialization conditions. These changes require another hardware boot; do not
claim the timeout root cause is proven until that succeeds or reports status.

Corrected kernel SHA `f913023a0ef02df7722951ebb387d0f398044e4e949d5169cc844b37a2289c8e` was flashed kernel-only at 0x140000 after
the user signaled download mode. esptool verified its hash and left the board
in the bootloader. Await corrected hardware boot log.

## Second SD boot: PIO regression

The 0084 kernel rejects OCR voltage masks before any DMA-sized data transfer.
The DT voltage range is unchanged; no electrical power fault is established.
0085 restores the first kernel's PIO completion policy, resets CPU FIFOs before
PIO, and confines master DMA FIFO flags to DMA transactions. It logs card/host
OCR on rejection. If DMA fails, return its error and disable DMA for subsequent
MMC requests, allowing MMC to retry commands via PIO; never replay a partially
consumed data phase as though it succeeded. PIO recovery is not DMA acceptance.

Host tests exercise mixed DMA/PIO/DMA transitions and recovery and reject the
previous driver. 0085 kernel built; awaiting hardware test.

0085 kernel `5767b5d7d1c599fadb64f8593d9a48a177e9dec62ca147144bfeb59ed5704503` flashed kernel-only at
0x140000; esptool hash verified, board left in bootloader. Await user boot log.

## Third SD boot: OCR response is zero

0085 still rejects the card before DMA: OCR=00000000, host=00780000. The
PIO-transition hypothesis did not restore detection and is not a proven cause.
No supply-voltage fault is established. The response parser copies the SPI R3
bytes into cmd.resp[1], which reaches mmc_select_voltage unchanged. Kernel I/O
copy implementation was checked: aligned FIFO copies use 32-bit accesses and
handle unaligned RAM, so the byte-MMIO hypothesis is not supported.

Requested a full removal of BOTH USB-C and UART VBUS power, including SD power,
then reboot of the same image before another flash. This checks retained card
state without conflating it with power-supply quality. Await user result.
If unchanged, use an isolated PIO baseline rather than additional DMA guesses.
SD root mount, RAM executable loading and forkbank are still untested on hardware.

## Power-cycle clarification and PIO isolation

User confirms thorough full power cycles on every test. Retained-card-state
check is therefore ruled out; do not request it again. The SDIO log is normal
MMC core probe order (SDIO -> SD -> MMC), all through mmc_spi, not native SDIO.

0086 and SD_TRANSFER_MODE=pio add an isolated PIO profile: only the SPI2 MMIO
resource, SPI clock and SPI reset are assigned to the controller. GDMA resources
are absent, so its clocks, registers and SRAM are not initialized by the driver.
The driver logs PIO baseline explicitly and omits FIFO resets introduced for
DMA transitions. SMP, forkbank and the same writable SD image remain selected.
This is a controlled isolation build, not acceptance of the required DMA feature.

PIO baseline kernel `3bd68b89d336db33f03a1614e7610a409ec8547d3cb613f1e9124e586b2f60c2` flashed kernel-only at
0x140000. Flash hash verified; no reset/UART capture. Await user boot log.

## Hardware retest update

User reports raw Arduino test and previous Arduino SD test both failed; after
adjusting the circuit the previous Arduino test worked again. This supports
an intermittent connection as a contributor, but does not validate Linux DMA.
Card is now disk4, 1,015,808,000 bytes, FAT32 SD UUID
3C9F896B-C261-3744-8EF4-C796D7535026. Prepared guarded disk4 writer using the
unchanged verified ext2 image. No card write performed by agent; await user result.
Arduino upload means the Linux boot stack must be restored before Linux retest.

User raw Arduino retest passed CMD0/8/ACMD41/CMD58 (OCR=80ff8000), SDSC
byte addressing, exact MBR CRC32=6f625234, ext2 CRC32=f18cba41, and 64 CRC16/data
checked reads per speed at 400 kHz, 1/4/10/20 MHz. After explicit download-mode
signal restored bootloader, SD partition table, handoff firmware and PIO baseline
kernel; all four esptool hashes verified. No reset or UART capture. Await boot.

## SD root boot succeeded; Bash contiguous allocation failed

After the circuit was adjusted and passed the raw Arduino test, the same PIO
kernel detected SU01G, read partition 1, mounted ext2 at 179:1, pivoted root,
and ran normal init services/login. The successful boot log and matching
artifacts are preserved in `build-output/sd-rootfs-poc/sd-mounted-bash-enomem/`.

Bash exec then failed to allocate 733184 bytes: NOMMU alloc_pages_exact needs
an order-8 (1 MiB) block initially, while the largest free block was 512 KiB.
Total free was 1852 KiB, clean file cache about 1308 KiB, slab 511 pages (not
511 objects), kernel stacks 384 KiB. Exact vmlinux trace resolves through
sys_execve -> bprm_execve -> load_elf_fdpic_binary -> elf_fdpic_map_file ->
vm_mmap_pgoff -> do_mmap -> page allocator. This failure is not in the forkbank
clone/switch path. RAM executable publication has run sufficiently for init and
services; full Bash/fork regression still requires testing. DMA remains disabled
and was not validated after connection repair.

Bash already uses -Oz, LTO and external help files. An isolated shared-Readline
relink reduced its read-only segment from 731904 to 593128 bytes but still
requires order 8; adding Readline's 231660-byte text increases total text RAM.
This experiment is not selected for the image. Original Bash binaries untouched.

0087 adds opt-in NOMMU_EXEC_DROP_FILE_CACHE: after successfully copying private
immutable executable text, drain local pending LRU additions and best-effort
invalidate clean source file-cache pages in that range. Busy/dirty/mapped pages
are skipped by the kernel API. Costly immutable-text allocations get bounded
__GFP_RETRY_MAYFAIL retries. Direct/XIP, anonymous, shared, writable mappings and
read/allocation failure behavior stay unchanged. Enabled only in the SD profile.
Host tests run actual do_mmap_private code and cover all these branches.
No SD image rewrite or Bash feature removal is required; hardware proof pending.

0087 memory kernel `9f2e521948e7cf225c987d2e88f7fe0675c5fc0a346d92f3c1388641dd59008c` flashed kernel-only;
esptool hash verified and left board in bootloader. Await Bash login test.

## Memory kernel boot and Bash login verified

User log confirms ext2 root mount, normal init services and Bash login after
0087. No allocation failure in this run. free reports 7796 KiB total, 6084 used,
1208 free, 504 buffer/cache and 852 available. df reports 887.9 MiB SD root with
7.4 MiB used. Preserved boot and tested artifacts in sd-boot-bash-pass/.
Fork/Bash regression, SD persistence and sustained I/O still require tests. DMA
is disabled by the PIO profile and unverified after hardware connection repair.
No new build running; current work is memory accounting and user-run tests.

## PIO regression passed; DMA retest prepared

User ran fork-test-dynamic (5/5), fork-smp-test (isolation, family placement,
exec release and workers on both CPUs), and the full Bash suite successfully
from SD root with 0087. The PIO read benchmark transferred 1 MiB in 2.433 s
(0.411 MiB/s), with 2.330 s kernel CPU time. These results do not validate DMA.

A separate incremental DMA build is in build-output/sd-rootfs-dma-retest/.
Kernel SHA256: 3a28dee258cc647ae5b26c5c64d194d37e6e70b3af892515193bd793b4d6ad8f.
The only kernel configuration change from the working PIO image is selection
of the DMA device tree. SMP, forkbank and 0087 remain enabled. The verified PIO
kernel and SD image remain unchanged. No SD rewrite or firmware update needed.

Actual SPI callback regression tests passed with sanitizers: DMA SRAM/descriptor
bounds, clock initialization, DMA/PIO transitions, missing IRQ/EOF, short RX,
descriptor errors and subsequent-request PIO recovery. Actual NOMMU copy-path
tests passed. Artifact verification passed for embedded DT, SRAM, IRQ routing,
flash offset/size, retained configuration and SD image hash. Hardware DMA remains
unverified; awaiting user download-mode signal for a kernel-only flash at 0x140000.
Successful boot alone is insufficient if a DMA error switched requests to PIO;
inspect the complete boot log and repeat read integrity/throughput tests.

DMA retest kernel flashed after explicit download-mode signal. Confirmed Nano
MAC 34:85:18:7b:81:b8; wrote only Linux at 0x140000, 3334392 bytes. Esptool
verified the written hash and left the board in bootloader. Firmware, partition
table and SD untouched. Await user boot log; DMA hardware remains unverified.

## DMA retest booted through PIO recovery; completion correction

User boot log shows DMA initialization, then TX total EOF timeout on a 64-byte
400 kHz read: OUT_RAW=1, IN_RAW=3, TXdesc=RXdesc=40040040. The descriptors
returned ownership, TX OUT_DONE and RX success EOF are present. Driver fallback
to PIO succeeded and SD root/Bash booted. This is not successful DMA validation.
Saved complete log in build-output/sd-rootfs-dma-retest/boot.log.

0088 changes the single-descriptor-per-chunk TX completion condition to OUT_DONE,
after SPI_TRANS_DONE. TRM v1.8 section 3.4 defines OUT_DONE as descriptor payload
completion; NuttX SPI waits on SPI transaction completion rather than GDMA total
EOF. Also require TX descriptor CPU ownership, EOF flag and exact length. RX EOF,
RX length, hardware errors and timeout recovery remain required. A test models
OUT_DONE without total EOF and reproduces failure in the old driver; corrected
callbacks pass with sanitizers, including held TX owner and short TX length.
Built and artifact-verified kernel is in build-output/sd-rootfs-dma-0088/,
SHA256 84ba69d95f01f4fd67cc559d275ca37a57e8f6f4d5639f87ebc5388a53d36eaa.
Not yet flashed; SD and Bash remain unchanged.

## RAM accounting and isolated terminal-library candidate

User drop_caches=2 measurement recovered only 24 KiB slab (2144 -> 2120 KiB),
with MmapCopy unchanged at 3412 KiB and kernel stacks 400 KiB. SLUB_TINY disables
SLAB_RECLAIM_ACCOUNT, so zero SReclaimable is not proof of no reclaimable slabs;
however this measurement shows unused filesystem metadata is not a large saving.
Bash maps confirm 716 KiB text, libc 488 KiB and ncurses 204 KiB. BusyBox code
and libraries are shared across init/syslogd/klogd/inetd; do not sum PrivateRAM.

An isolated relink using the existing ncurses obj_s archive members required by
Bash removes its libncurses.so dependency without changing Bash source/config.
ELF LOAD memory: original Bash+ncurses 1018560 bytes; candidate 883348 bytes,
a reduction of 135212 bytes (132 KiB), before page rounding/runtime allocation.
Candidate build-output/sd-memory/bash-static-terminal is not installed and has
not been hardware tested. Existing shell and SD image are untouched. This is
an incremental candidate, not a claim that the >6 MiB footprint is solved.

0088 flashed kernel-only after explicit download-mode signal. Nano MAC verified;
esptool verified flash hash, board left in bootloader, no UART capture. SD and
working Bash unchanged. Await user boot log and DMA read/write validation.

## 0088 hardware result and next memory candidate

0088 passed the earlier 64-byte stage but timed out on a 512-byte read at 20 MHz:
SPI_RAW=2, OUT_RAW=0, IN_RAW=3, TXdesc=c0200200, RXdesc=40200200. TX ownership
is still held. Driver fell back to PIO, which mounted SD root. DMA remains
unverified. SPI RAW bit 1 is the DMA TX FIFO-empty error, an important omission
in the earlier completion-only interpretation. The driver does not clear this
latched error per transaction; establish whether fresh before assigning cause.
Do not relax TX completion further or claim ownership of SRAM proves correctness.

An isolated resident-services BusyBox built from the patched busybox-netcat
source measures 279416 LOAD bytes versus 831428 for full BusyBox: 552012 bytes
(539 KiB) less before page rounding and runtime allocations. Includes init,
getty, login, sh/hush, logging, inetd and test/true/false. Candidate is not installed
or hardware tested. Full BusyBox must remain on SD for all other applets; only
resident-service symlinks would select the core. This and the 132 KiB Bash
candidate total about 671 KiB of potential idle loaded-segment savings, not a
verified change to actual board RAM. Existing SD image remains unchanged.

User reiterates Wi-Fi inside Linux is the next task, with research in
re/wifi-blob/FINDINGS.md. Read its queue-wrapper ABI diagnosis and full NuttX OSI
contract gap list. Preserve INET, cfg80211, network tools and the future Wi-Fi
integration path during memory work. No networking features removed. Older
wifi-port-budget.md estimates predate SD-backed RAM executable copies and must
not be reused as measured current headroom. Wi-Fi remains parked until current
SD/DMA/memory work is stable; no untested Wi-Fi changes introduced.

## 0089 DMA FIFO correction and memory userspace SD image

0089 follows cached official IDF spi_hal_prepare_data: clear SPI RX/TX FIFO error
latches after reset before each DMA transfer. Enable error IRQ wakeups and reject
fresh FIFO errors, including a master stalled without transaction-done. Enable
SRAM descriptor/data bursts as NuttX requests for SPI. Retain TX ownership/length,
RX EOF/length, GDMA errors, bounded waits and PIO recovery. Add successful chunk,
byte and error counters at /sys/class/spi_master/spi0/dma_status; include TX link
and FIFO registers in failures. This fixes verified software omissions; whether
it resolves hardware DMA remains unproven. Actual callback tests cover stale
versus fresh FIFO errors, stopped master, counters, bursts and prior regressions.
Kernel built successfully, SHA256:
6fb246ed537fd06e4dc57e520b94ec40482d02a80efe7b3a2732a52d8fdbe852.

User prefers rewriting SD and states no files require preserving. Prepared
build-output/sd-rootfs-memory-dma-0089/ as a separate combined artifact set.
make-sd-rootfs.py --memory-userspace selects resident BusyBox core service links
and the smaller Bash, while retaining full /bin/busybox and saving original Bash
at /usr/libexec/bash-original. No networking features removed. Generic file links
remain on full BusyBox; only init/getty/login/sh/logging/inetd select core.
Root-aware symlink resolver handles absolute links against image, not build host;
four regression tests cover link resolution, loops, custom login preservation,
hashes and retention. e2fsck passes. debugfs dumps from the completed ext2 image
match candidate, retained BusyBox and original Bash hashes; service symlinks are
verified. Potential loaded-segment reduction is 687224 bytes (671 KiB), not a
hardware-measured RAM claim. New userspace still requires boot/fork/Bash tests.
SD image SHA256:
60fdef1e040ff82bddcc1ddd3be138d582f271bff2cf28b28149383969342575.
Known-working PIO kernel and original SD image hashes remain unchanged.

Current SD card identified as disk4, removable USB 1015808000 bytes, Linux
partition at offset 1048576, size 938475520. Reader now uses usb-drd1, not the
old usb-drd0 path. New Terminal writer checks this metadata, original MBR hash
and ext2 UUID before writing, then full 896 MiB readback hash and ejects.
Metadata guard and script syntax checks pass. No card write or kernel flash
performed at this stage; await user Terminal writer result and next DL signal.

Card writer initially stopped before writing: raw-device 16-byte UUID read
returned EINVAL on macOS. Fixed to read an aligned 512-byte superblock sector
and slice UUID bytes 104..119. Sector-enforcing regression accepts original
image and rejects wrong UUID/short reads. No card data was written by failed run.

User completed replacement SD writer; saved full readback hash matches the new
image. After explicit download-mode signal verified Nano MAC and wrote only
0089 Linux kernel at 0x140000 (3334424 bytes). Esptool verified hash and left
board in bootloader; no reset/UART capture. Await boot, DMA counters, memory
comparison and userspace regression tests. Neither hardware objective marked done.

## Hardware failure of small-userspace split; selected XIP/SD recovery

0089 still reports a fresh SPI TX-FIFO underrun at 20 MHz/512 bytes, with TX
ownership held and RX descriptor complete. It recovers to PIO. DMA unresolved.
The reduced resident BusyBox changed allocation layout: cold full-BusyBox tools
need 819200 bytes and the new Bash needs 811008 bytes, both order 8, but buddy
has no 1 MiB block despite about 4 MiB total free. Boot services and Bash login
fail. This was a packaging regression, not proof that all RAM is consumed.
Saved complete failure log beside the image. Do not reuse that image as stable.

User selected flash XIP system plus writable SD directories; native flash JFFS2
would be preferred if feasible but requires separate cache-safe SMP write/erase
support. Existing patch 0074 intentionally returns EROFS and advertises ROM MTD;
JFFS2 alone cannot supply that backend. Wi-Fi research remains parked/preserved.
Re-read upstream issue #8: its successful model is CramFS in flash, ext2 SD /home,
with /etc remaining in flash; it does not prove SMP-safe flash writes or DMA.

Prepared hybrid-xip-sd-pio artifacts: restored original working Bash/BusyBox,
CramFS built with -X, root=mtd:rootfs (CRAMFS_MTD enabled), early /sbin/hybrid-init
mounts existing SD at /data and seeds .linux-state once. Bind etc/home/root/var/
tmp/opt/usr-local before normal init; logs go to real SD-backed /var/log rather
than old /run/log symlink. Missing SD falls back to flash defaults and temporary
RAM tmp/log mounts. Host test proves initial seeding, preserved SD modifications,
mode and fallback. Hardware writable mounts and recovered RAM headroom pending.

Matching partition table adds rootfs at 0x510000 size 0x630000 while retaining
all existing firmware/kernel offsets. Built image and embedded DT/partition
binary verified; original Bash/BusyBox hashes match known-working image. No SD
rewrite required. Flash plan: partition table 0x8000, kernel 0x140000, rootfs
0x510000; keep bootloader/handoff firmware. Await download-mode signal. DMA is
intentionally PIO baseline for this recovery, not represented as fixed.

Hybrid XIP/SD image flashed after explicit download-mode signal. Nano MAC
verified; partition table at 0x8000, kernel at 0x140000 and XIP rootfs at
0x510000 each hash-verified by esptool. Bootloader, handoff firmware and SD
unchanged; board left in bootloader, no UART capture. Await hardware boot,
SD writable mounts, memory figures and fork/Bash regression. DMA remains PIO.

Journaled data recovery: new ext4 SD image with 8 MiB journal and checksummed
metadata built and e2fsck-verified. Full card readback recorded and verified.
Kernel includes EXT4/JBD2; flash root checks journaled card profile before
using persistent state, retries interrupted seeding via unique temp paths,
and keeps frequent logs in RAM. Software gates pass; hardware reset recovery
pending. User explicitly stopped RSA work to prioritize usable storage.
0090/0091 experiments parked in build-output/rsa-fix/parked and removed from
build stack; only applied 0090 reversed with exact git-apply check. Storage
only kernel restored byte-identically to 60591a78633ccf96ff91fed69483ab8db22c8ffa8505f11a25de305eaf6492cd.
No new flash yet; await DL signal for kernel and rootfs only. DMA remains PIO.

After user confirmed DL readiness, verified Nano MAC and wrote only the
storage-only journal-support kernel and XIP rootfs at 0x140000/0x510000.
Both esptool hashes verified, left in bootloader. SD full readback already
verified. Partition table, bootloader and handoff firmware unchanged. RSA
experiments excluded and parked; DMA remains disabled. Await boot, mount/free
figures, shell/fork regressions and clean-reboot persistence before reset test.
