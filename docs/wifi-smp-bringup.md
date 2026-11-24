# Linux-hosted ESP32-S3 Wi-Fi: SMP restart, 2026-11-16

## Latest result and correction, 2026-11-17

The station candidate is hardware-functional: the supplied complete connection
trace shows actual AP scans, factory MAC, radio IRQs, WPA2 association, DHCP,
DNS and 11 transmitted/11 received Internet ping replies. It is **not stable
or warning-free**. Both complete user logs are preserved with the exact image
and symbols in `build-output/wifi-smp-audit/station-hardware-1/`.

All six distinct stack shapes in the 1,873-line trace were classified and
symbolicated. The first fault is `esp_cmd_work` holding `cmd_lock` and
`cmd_pending_queue_lock` with `spin_lock_bh` while the new native transport
calls `esp_wifi_register_mgmt_frame_internal`. Even a nonblocking scan API
uses `ieee80211_ioctl -> osi_semphr_take -> down_killable -> schedule` to talk
to the radio task. The initial atomic count0x400 matches two BH disable levels.
Auth and association IE APIs exhibit the same bug. Later softirq-unlock and
workqueue warnings carry underflowed atomic count0xfffffc00, including on RX
and timer jobs sharing the worker. These do not establish separate faults in
all those workers. The native transport integration missed this Linux caller
context contract.

There is a separate temperature ownership fault: hwmon's private mutex does
not protect PHY access, and hwmon always powered down/gated TSENS after its
read. The trace shows a valid hwmon reading followed by repeated PHY not-ready
warnings. This needs state preservation and common arbitration.

New unflashed `context-tsens-candidate` fixes these in patches0112/0113.
The native transport only queues from atomic callers; a CPU0 system worker
executes blocking commands/data. Queue is bounded to16 existing skbs (each
at most4096 bytes); no permanently allocated new task/workqueue. Shutdown
rejects new submissions and drains accepted packets before radio teardown.
TX statistics use the cached byte count, never an skb after ownership transfer.
Hwmon and PHY temperature read/power callbacks share the recursive CPU-owned
PHY lock; hwmon saves/restores power/force/divider/clock/DAC/analog state on
success and errors. Shared critical sections use bounded busy waits and skip
power-up settling when already powered. Rare hwmon reads can therefore add
bounded interrupt latency on that CPU; concurrent Wi-Fi/load testing is needed.

Regression harnesses fail on the old implementations and pass on the fixes.
All13 current host suites pass, including concurrent hwmon/PHY access and
atomic caller -> deferred radio execution, queue bounds/ownership/shutdown.
The full configured Xtensa build verifies22 patches and recompiles all four
changed files. Embedded DT/SMP/forkbank/PIO/partition/SRAM gates pass. Existing
missing-prototype, vendor COMMON-symbol and section-mismatch warnings remain.

Kernel SHA256 `244743c6c7ff0df257dfc4b8c4eefc9ff4cfb666e20cbb56641178eb74665b85`,
3,957,048 bytes, 40,648 bytes spare. Matching symbols/config are beside it.
Ready for a freshly authorized kernel-only flash at0x140000. No rootfs, SD,
firmware or partition changes. Hardware regression is pending; preserve the
working station candidate for rollback. Reboot rather than reusing a worker
whose atomic state was corrupted by the old image.

## Previous candidate state (historical)

## Current state, 2026-11-17

The latest hardware-proven kernel is `shared-irq-candidate`, SHA256
`680b6222ef1dac20b34ab6f03682a30705e5fdfefa6553d3b558f23d6e830445`.
It boots both Linux CPUs and starts the radio/PHY; the workqueue warning is
gone. The blob reads factory MAC `34:85:18:7b:81:b8`, but the host interface
received zeros because a response contained two nested command headers.
Its Wi-Fi IRQ counter was zero without a scan/traffic test, so interrupt
delivery has not been established. The original log and matching symbols
are preserved in `build-output/wifi-smp-audit/radio-init-3/`.

The new **unflashed** `station-candidate` implements patches0106–0111:
single-header MAC replies; the actual three-argument RX callback and exactly
once vendor-buffer release; native passive scans with real beacon/probe IEs;
open/WPA2-CCMP station commands and EAPOL/management-frame bridging; honest
capability advertisement (no SAE/WEP); association rejection handling; and
draining deferred scan cleanup before radio deinitialization. Linux owns the
SME/WPA handshake, using the private radio APIs already called by the original
esp-hosted firmware. NuttX supplies OSI/SMP integration references; it does
not replace Linux's cfg80211/userspace protocol implementation.

Kernel SHA256:
`339a9e1fc3bbfbea85e3ba4ada91983c46ce7291fbe7fb800094536224ccafc9`.
Size 3,957,016 bytes, with 40,680 bytes spare before the unchanged XIP rootfs.
Twenty patches were reverse/reapplied in scratch; the incremental Xtensa
kernel build passed. Existing vendor COMMON-symbol/modpost section warnings
remain. Matching vmlinux/System.map/config/DTB are beside the image. SRAM
overlap, partition bounds, embedded DT and SMP/forkbank config gates passed.

Focused ASan/UBSan tests cover real producer/consumer MAC and scan wire
formats; actual management IEs/RSSI/BSSID; auth/assoc frame reconstruction;
association rejection (regression fails before0111); EAPOL borrowed-buffer
copying; open/WPA2 config; CCMP/IGTK bounds and unsupported-key rejection;
and RSN parser truncation. Existing queue/timer/PHY/IRQ/RX tests pass. Actual
32-bit Xtensa compilation checks the ABI against pinned IDF headers, and
the peripheral IRQ regression preserves UART/USB and grouped MAC/BB routing.
These are software checks with captured vendor calls, not radio simulation
or proof of association. See `build/test-wifi-native.py`.

Next gate: freshly authorized kernel-only flash at0x140000, followed by user
reset/log capture. Keep firmware, partition table, rootfs and SD unchanged.
Do not open UART. Test normal boot/storage, manually enable the radio, then
check the factory MAC, scan results, Wi-Fi interrupt increments and errors:

```sh
echo 1 > /sys/module/esp32_wifi/parameters/enable
ip link show espsta0
ip link set espsta0 up
iw dev espsta0 scan
cat /proc/interrupts
grep -E '^(MemFree|MemAvailable|Slab|MmapCopy):' /proc/meminfo
dmesg | tail -80
```

No scan, association, DHCP, traffic or Wi-Fi-under-SMP-load success is claimed
yet. Check real IRQ delivery and SRAM capacity under those operations. Radio
teardown/re-enable is not a supported hardware-proven lifecycle; reboot for
a fresh test. SPI SD DMA remains disabled and RSA stays parked.

The sections below record the earlier investigation and candidate stages.

The working baseline is `build-output/hybrid-journaled-sd`: both CPUs run Linux,
Bash/libraries execute from flash, and writable directories live on journaled SD.
Wi-Fi runs inside Linux; there is no remaining FreeRTOS radio task on CPU0.
RSA changes remain parked. SPI SD DMA remains unresolved and disabled.

## References checked

- `re/wifi-blob/nuttx-ref/esp32s3_wifi_adapter.c`: separate raw and wrapped queue
  APIs, `esp_task_create_pinned_to_core` CPU affinity, explicit IRQ CPU routing.
- Pinned IDF `24394052fcc462fe27935b4cf15ea1f3e98549c1`,
  `components/esp_wifi/esp32s3/esp_adapter.c` and
  `components/esp_coex/esp32s3/esp_coex_adapter.c`: the wrapper has two pointers
  (`handle` first, `storage` second); internal allocators require
  `MALLOC_CAP_8BIT | MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL`.
- ESP32-S3 TRM, local `docs/reference/esp32-s3_technical_reference_manual_en.pdf`:
  memory aliases/interrupt matrix. The TRM does not define the private blob ABI.
- Actual copied `libpp.a:pp.o` disassembly: `pp_create_task` calls OSI slot376,
  saves the returned wrapper, then loads its first word as the queue handle.
  Object machine IDs were changed only on analysis copies, never vendor archives.
- Matching firmware ELF hash
  `5a843c227300b13f4de685188a7fb5e0c1520d84494d83f0ab3d54943117b9d8`.
  LOAD ranges and I-bus aliases checked against the new SRAM resource.
- PHY archive disassembly was searched for the listed hardware FP operations;
  none found. This is a PHY-only check, not proof about every ROM routine.

## Implemented candidate patches

- 0092: distinct Wi-Fi queue wrapper/delete callback; allocation overflow check;
  pinned kernel threads bind before wakeup; handle published before wakeup.
- 0093: retain IRQ CPU in three-cell fwspec; reject mapping zero; start IRQ with
  NO_AUTOEN; preserve enable-before-map state and serialize mask transitions.
- 0094: bounded SRAM allocator for internal-capability callbacks, shared free
  dispatch, capability-preserving realloc, correct internal free-space report.
  Radio startup work is scheduled on CPU0, matching configured Wi-Fi task core.
- 0095: explicit pinned IDF components path for incremental kernel linking;
  select Linux generic allocator.
- 0096: IRQ-thread affinity matches the interrupt-matrix route CPU.
- 0097: reject unimplemented/malformed control packets instead of passing their
  bytes to the Ethernet transmit API. Unsupported requests receive failure.

The SRAM range is `[0x3fcce800,0x3fcdc700)`, 57,088 bytes. It does not overlap
matching firmware/kernel static LOAD segments or their I-bus aliases. The SPI
DMA reservation `[0x3fccc700,0x3fcce740)` remains separate. The range stops below
the bootloader boundary. Capacity under a running radio is not yet measured.

## Important remaining implementation

The old Linux backend translates only CMD_INIT_INTERFACE and CMD_GET_MAC.
It does **not** yet implement scan/authentication/association/key commands or the
management-frame event bridge. The former fallback incorrectly sent command
payloads to `esp_wifi_internal_tx`; patch0097 blocks this. A netdev appearing is
not proof of usable networking. The original firmware's `main/cmd.c` contains
relevant direct radio APIs and management callbacks; an audit copy is in
`build-output/wifi-smp-audit/base/firmware-cmd.c`. NuttX supplies OSI and direct
radio integration references; Linux cfg80211 still needs its own complete bridge.

## Verification and hardware gate

Run the actual extracted callbacks against allocation/scheduling/IRQ mocks:

```
python3 build/test-wifi-osi.py
python3 build/test-wifi-irq.py
python3 build/test-wifi-internal-memory.py
python3 build/test-wifi-control.py
python3 build/verify-wifi-artifacts.py build-output/wifi-smp-audit/build
```

All passed with ASan/UBSan for the C callback tests. The full Xtensa kernel links.
The candidate is 3,952,792 bytes and fits the existing 0x3d0000 partition with
44,904 bytes spare. No partition table, firmware, rootfs or SD replacement is
needed for this radio-initialization test. Exact vmlinux/System.map accompany it.
The kernel build still emits existing adapter missing-prototype warnings, vendor
COMMON-symbol warnings, DATA_MAIN redefinition and a modpost section mismatch;
these are not evidence of a clean hardware boot.

Radio remains gated off at boot. Nothing has been flashed or hardware-verified.
After an explicitly authorized kernel-only flash, first verify normal login and
storage, then trigger radio initialization manually:

```
echo 1 > /sys/module/esp32_wifi/parameters/enable
```

The immediate acceptance gate is completion of PHY/radio initialization with
valid queues, IRQ delivery and adequate internal SRAM. Scan, connection, DHCP,
traffic and SMP load testing remain later gates; no Wi-Fi success claimed yet.
The user captures UART output and controls reset. Never open UART automatically.

## First hardware result and consolidated PHY candidate

The first flashed candidate (`df33334d...e2fa3729`) completed coexistence init,
created/ran the CPU0 Wi-Fi task, exchanged queue/semaphore messages and returned
success from `esp_wifi_init_internal()`. It then faulted in `set_chanfreq` at
`4240c66e`, loading word+0x5c through NULL `g_phyFuns`. Exact linked ELF shows
`register_chipv7_phy` calls `phy_get_romfunc_addr` before table use. The shim only
called `register_chipv7_phy_init_param`, which does not perform that work.
Preserved log and exact image/symbols: `build-output/wifi-smp-audit/radio-init-1/`.
No log evidence establishes an IPI/cache/DMA failure at this fault.

Additional audit references downloaded at NuttX commit
`c803668d6c763c823b3286df1cc40c528abadf3e`:
`arch/xtensa/src/common/espressif/{esp_wireless,esp_wifi_utils}.{c,h}`, plus
`arch/xtensa/src/esp32s3/{Make.defs,hal_esp32s3.cmake}`. Make.defs selects Espressif
third-party HAL `cf95a27f2aa263d77c7a8d7f56007abfc899fc47`. That HAL's
`components/esp_phy/src/phy_init.c` implements the PHY lifecycle and calls full
`register_chipv7_phy`; it is not a separate NuttX-only PHY implementation.
Copies and SHA-relevant source files are under `re/wifi-blob/nuttx-ref/`.
The current HAL uses some APIs not exported by our older pinned blob, so copying
its entire implementation without checking versions would be incorrect.

Consolidated patches 0098–0103:

| Contract | Old implementation | Candidate correction / proof |
|---|---|---|
| PHY registration | parameter-copy helper at probe | full registration at `_phy_enable`, after OSI/clock setup; linked call checked |
| Calibration enum | bool true=1 (CAL_NONE) | integer CAL_FULL=2, checked against pinned header |
| Init-data object | 112-byte buffer | full 128-byte object, byte-for-byte pinned initializer including implicit zero tail; bytes also checked in image |
| Factory MAC | reversed bytes | eFuse fields 40,32,24,16,8,0; known board address reproduced in test |
| Derived MACs | second-byte bit OR | four-universal-address offsets in last byte, matching pinned board config |
| MAC reset | bit6 | S3 WIFIMAC_RST bit2; preserve other reset bits |
| APB callback | writes unrelated PERIP bits | fixed-frequency no-op PM lock; actual common radio gate at SYSCON +0x14 |
| Analog power | wrong offsets/masks; fictitious PWDET register | SAR +0x3c, TSENS +0x50/+0x54, clock gate +0x104; SAR/PWDET shared client state |
| PHY critical section | only local IRQ disable | nested CPU-owned raw spinlock plus IRQ/preemption state; concurrent two-CPU host test |
| Radio domain | firmware state assumed | clear RTC Wi-Fi powerdown, pulse modem reset mask, remove isolation before coex/radio init |
| Power saving | inherited default | explicitly WIFI_PS_NONE; full Linux modem-sleep support is outside this candidate |
| RTC calibration | constant zero | hardware Q19->Q12 value, with S3 divided-XTAL branch |
| Timer resolution | microseconds rounded to milliseconds/jiffies | hrtimer deadlines, wide arithmetic |
| Timer lifetime | disarm could rearm; sync-cancel self-deadlock/free races | active state and refcounted registry/work ownership; self-disarm/delete and queued cancellation tested |
| Timer callback context | generic workers | serialized work on CPU0; hardware expiry never calls sleeping blob code |

The PHY power and critical-section callback tests use actual extracted C with
ASan/UBSan; the critical-section test also runs two host pthreads with nested
entries. Timer tests use deterministic expiry/work mocks, including callback
self-deletion. These establish software invariants, not on-silicon timing.

New tests: `build/test-wifi-{phy,mac-clock,timers}.py`. Earlier queue/IRQ/SRAM/control
tests still pass. Seven focused callback tests pass. The full Xtensa candidate
builds at `build-output/wifi-smp-audit/phy-candidate/linux.bin` with matching
vmlinux, System.map, config and manifest. Embedded DT, SRAM/static-image overlap,
PIO isolation, forkbank and existing flash partition limits pass. It is a
kernel-only replacement; no rootfs/SD/firmware/partition rewrite is required.

### Explicitly unfinished / not claimed

- No NuttX Wi-Fi hardware run was performed in this session. This is a source and
  contract comparison with pinned implementations.
- The first Linux radio run did not complete PHY startup, so RF calibration,
  actual radio IRQ delivery and peak internal-heap demand are not yet verified.
- cfg80211 scan/auth/association/key and management-frame bridging remains
  incomplete; unsupported commands fail explicitly.
- Association, DHCP, data transfer, reconnect, sustained concurrent load,
  shutdown/unbind and power-saving behavior are not hardware-verified.
- NVS is disabled/RAM-backed; calibration persistence is not implemented.
- Flash writes remain disabled; SRAM/IRAM cache-off handling is not provided.
- Full-source comparison does not imply every callback is proven under all
  concurrent use patterns. It identifies contracts and allows focused gates.

## Second hardware result: radio initialization passed

Kernel `d300ababcdf12e86943c72f0cb4c60405653072f22815c62f5a31d46da36703f`
booted, mounted the writable SD directories, initialized the full PHY with
`ret=0 function_table=ready`, generated the correct STA/AP MACs, started the
station, registered RX and created wiphy/espsta0. No Oops appears in the supplied
log. This proves the previous NULL-PHY-table startup fault is passed, not that
association or data traffic works. Exact log/image/symbols are preserved in
`build-output/wifi-smp-audit/radio-init-2/`.

Full boot-log assessment:

- Both CPUs online; UART, USB controller and SD initialize successfully.
- `percpu max_distance ... vmalloc space 0` is the embedded allocation fallback
  on NOMMU; per-CPU allocation succeeds, and boot continues on both CPUs.
- The workqueue stack trace is a WARN at alloc_workqueue: Linux7.2 requires
  explicit WQ_PERCPU or WQ_UNBOUND. The kernel defaults to WQ_PERCPU and
  continues. This was introduced by the new timer queue; patch0104 sets the flag.
- `EXT4 recovery complete` is successful journal replay following an unclean
  reset. The SD mounts read/write in ordered mode, and hybrid bind setup succeeds.
- CramFS root read-only and XIP at0x42510000 are intentional. Writable directories
  are supplied from the SD, confirmed by the hybrid-init line.
- RSA not-ready is the known parked issue and remains unchanged.
- Repeated queue waits and mutex prints are debug tracing; hashed %p values are
  not corrupt addresses. Patch0104 lowers hot-path tracing to pr_debug.
- `(NULL device *)` came from init_adapter memset clearing the parent device;
  patch0104 restores the platform parent after initialization.

The log also exposed a real IRQ integration gap: the blob registers source2
(Wi-Fi baseband) and source0 (MAC) on CPU0 input0, then one ISR. The shim's scalar
source slot overwrote source2. NuttX's direct interrupt-matrix routes retain both.
Patch0105 preserves the full source set and extends the Linux irqchip fwspec with
additional sources. One Linux IRQ owns the group; mask/unmask operates on every
row, preserving threaded ONESHOT behavior. Existing 2/3-cell peripheral routes
remain unchanged. Runtime changes to an already-mapped group are rejected; full
radio teardown/reconfiguration is not yet implemented or verified.

`shared-irq-candidate/` contains the next kernel-only image and matching symbols.
Eight focused callback tests pass. The peripheral IRQ regression test also passes
for CPU ownership, deferred enable/IPI interaction, UART/USB routes and grouped
MAC/BB masking. Embedded DT/partition/SRAM gates pass. This new image itself is
not yet flashed or hardware-tested. Scan/auth/key bridging remains incomplete.

## Station candidate flashed, 2026-11-17

After a fresh user download-mode signal, verified USB board MAC
`34:85:18:7b:81:b8`, read the live 4096-byte table at0x8000 and matched it
byte-for-byte to the last working table. Verified station-candidate kernel
SHA256 `339a9e1fc3bbfbea85e3ba4ada91983c46ce7291fbe7fb800094536224ccafc9`
and wrote exactly 3,957,016 bytes at0x140000. Esptool reported written-data
hash verification. Board left in bootloader (`--after no-reset`); no UART
capture. Firmware/table/rootfs/SD were not written. Logs and manifest are in
`build-output/wifi-smp-audit/station-candidate/`. Await user reset and
boot/radio/scan log; this image is flashed, not yet hardware-verified.

## Context/temperature correction flashed, 2026-11-17

Fresh download-mode signal received. USB MAC34:85:18:7b:81:b8 and live4096-byte
partition table verified against the preceding working board readback. Wrote
context-tsens-candidate kernel SHA256
`244743c6c7ff0df257dfc4b8c4eefc9ff4cfb666e20cbb56641178eb74665b85`
(3,957,048 bytes) only at0x140000; esptool verified written-data hash.
Firmware/table/rootfs/SD untouched. Board remains in bootloader without reset
or UART capture. Await user normal boot, scan/connection/traffic, hwmon query
and subsequent dmesg to test both corrections. Hardware verification remains
false for this image; the prior station kernel already demonstrated working
WPA2/DHCP/DNS/ICMP with the identified warnings.

## Corrected image hardware result, 2026-11-17

User log `context-tsens-candidate/hardware-regression-user.log` (240 lines)
confirms repeated scanning, valid MAC, WPA2 connection, DHCP and DNS/Internet
ICMP. No BUG/WARNING/atomic-context stack trace or PHY TSENS-not-ready message
appears. Hwmon reads29.9 C and the subsequent15-second wait shows no former
PHY temperature warnings. Wi-FiIRQ8 has1016 CPU0 deliveries; both CPUs have
timer/IPI activity. Free RAM is2040 KiB of7768 KiB total. These observed
regression gates pass, without claiming sustained-load/long-duration stability.

Internet ping received8/10 replies (20% loss), so reliability is unresolved.
Next isolate with a longer ping to the local default gateway and interface
error/drop counters before attributing loss to RF, LAN/WAN or this driver.
An initial interactive network choice1 was rejected once, then the same
selection worked; UI/input cause not established. No new build started.

## wlan0 and user-controlled connections, 2026-11-17

User authorized renaming to wlan0 and quieter logs, then explicitly clarified:
boot enables the driver/interface only; wifi retains the original scan/connect/
disconnect/off workflow, with no automatic association or DHCP.

`wlan0-user-control` is built, not flashed. Kernel SHA256
`e5d6a04b3fd40750d720e27de1fdb80a21b88dedaa67a82381b0a16dd116d04e`;
rootfs SHA256 `8e7feaa631fa24e7c2a4ab6e10ef5740745b22d45f6133e49c83cf109e12c619`.
Kernel3,957,048 bytes (40,648 spare), XIP rootfs6,356,992 bytes. Firmware,
partition table and SD filesystem stay unchanged. Flash matching kernel at
0x140000 and rootfs at0x510000 only after a fresh download-mode signal.

Patch0114 renames netdevs to wlan%d, routes IPv4 notifications by the notifier's
owned netdev identity rather than a name substring, handles pre-probe enable
intent without an erroneous ENODEV, and moves routine OSI/MAC/queue messages
to debug. Patch0115 quiets the old manual-enable boot instructions. Meaningful
radio/link status and warnings remain. Full24-patch kernel builds.

Immutable /usr/sbin/wifi-boot runs from hybrid-init before BusyBox init/ifup:
atomically migrates old SD /etc/network/interfaces espsta0 tokens, removes WLAN
auto/allow-hotplug entries, saves the previous file, preserves credential files
and modes, and waits at most20 seconds for wlan0 after enabling the radio.
Existing SD-backed init scripts need not be overwritten. Current flash defaults
already use manual wlan0; no temporary files/writes needed on read-only fallback.
Recovery: WIFI_RADIO_ENABLE=0 in /etc/default/wifi or explicit kernel command
line esp32_wifi.enable=0. Saved WPA config is preserved but never autojoined.
Only user wifi connect/interactive actions invoke ifup and WPA/DHCP.

All16 Wi-Fi host suites pass (including nine wifi utility unit tests within
one suite), plus hybrid-init seeding/fallback tests. Repacked image extracted
and checked against exact current scripts; Bash/BusyBox hashes unchanged.
Embedded DT/SMP/forkbank/SRAM/flash-layout and matching-rootfs gates pass.
Hardware boot/name/manual-connection regression remains pending.

## Writable internal flash question (research, no changes)

Hardware can support persistent flash storage, but current Linux MTD map is
read-only after removal of the original IDF CPU0 IPC flash server (patch0074).
The current table ends at0xb40000 in16 MiB flash, leaving0x4c0000 (4.75 MiB)
unallocated, before any new filesystem metadata/reservations. A future design
can keep kernel/rootfs XIP and use a separate flash filesystem for mutable
state, potentially eliminating SD. It requires a native SMP-safe erase/write
backend with internal SRAM/IRAM execution, CPU/interrupt/cache coordination
and coherency for direct flash mappings. Merely adding JFFS2 and a partition
is insufficient. IDF's suspend/resume hardware path is another chip-dependent
option requiring evaluation, not assumed supported by this board's flash.
No flash write-enable, partition change or experimental erase was attempted.
Reference: https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/spi_flash/spi_flash_concurrency.html

## wlan0 matching kernel/rootfs flashed, 2026-11-17

Fresh user download-mode signal received. Verified boardMAC34:85:18:7b:81:b8
and unchanged live4096-byte table against prior board readback. Verified both
local SHA256 hashes and wrote only kernel0x140000 (3,957,048 bytes) and XIP
rootfs0x510000 (6,356,992 bytes). Esptool reported written-data hash verification
for each image. Firmware/table/SD were not flashed. Board left in bootloader
without reset or UART capture. Await normal user reset and test: wlan0 should
exist automatically, remain disconnected/no DHCP until wifi is used, preserve
SD credentials, and avoid prior scheduling/temperature warning regressions.
Logs/manifest: build-output/wifi-smp-audit/wlan0-user-control/.

## wlan0 user boot result, 2026-11-17

`wlan0-user-control/hardware-user.log` confirms driver/radio initialization
at boot, correct factory MAC, wlan0 initially DOWN/unassociated/no IP, then
user wifi scans and WPA2 connection with DHCP/DNS. Internet ping10/10 replies
(0% loss in this short sample). No prior atomic/workqueue or PHY temperature
warning in supplied log; temperature30.4 C. Startup MemAvailable2736 KiB,
connected MemFree1816 KiB (different metrics; no leak conclusion).

Two cleanup observations remain: first interactive selection1 rejected once
then succeeds on retry, cause not established; ROM-slot messages remain because normal startup also calls the
emergency-level ROM dump helper (pr_emerg), which was intentionally outside
the earlier routine pr_info-to-debug conversion. RSA not-ready is known parked;
ext4 journal recovery after reset completes. Sustained/reconnect/SMP-load
network testing is pending; no further flash or rebuild started from this log.


## Static TX candidate and S31 comparison, 2026-11-23

The flashed candidate is recorded in
`build-output/wifi-memory-2026-11-23/verification.json`. The user confirmed
saved-network autojoin at boot (192.168.1.44), static TX stats with 17,792 bytes
free internal SRAM, and atomic-dual 20,000/20,000. This supersedes the earlier
manual-only boot policy: remembered radio-on plus saved credentials now joins;
remembered radio-off must remain off. Off/on across reboot still needs hardware
validation. Neither initial connection nor successful atomics accepts Wi-Fi
stability under load.

Paired 30-second Nano-client/Mac-server TCP tests measured 2.434 Mbit/s Nano TX
and 3.773 Mbit/s Nano RX. The two tests added 916 ESP_ERR_NO_MEM rejections and
44 final failed transmissions. These are failed acceptance results, despite
improvement over the preceding dynamic-TX image. SRAM free stayed 17,792 bytes;
that alone cannot distinguish radio-pool exhaustion from allocator failure.

The reference https://github.com/GrieferPig/esp32-s31-linux was inspected from
its downloaded main archive, with its kernel submodule pinned to
2bf05bc5f379066cf5f64d750afbb0b4ba0bf364. Useful contracts:

- `firmware/radio/radio_stack.c:s31_wifi_tx_done` is empty; registering that
  callback alone does not provide credits or prove recycling.
- `firmware/radio/s31_softmac.inc:s31_sm_tx_done` reports completion and returns
  flight credit after its final old-slot access. Its comment explicitly says
  stage-0 completion does not establish EB/buffer recycle. Completion identity
  and actual buffer reuse must therefore not be conflated.
- Kernel `drivers/net/wireless/espressif/esp32s31_softmac.c` counts outstanding
  frames, stops mac80211 queues at its admission window, and wakes them from
  retirement. NAPI handles statuses before new TXQ service. This is a useful
  model for bounded submission and queue backpressure, not drop-after-enqueue.
- Kernel `drivers/platform/esp32s31-radio-rtos.c` preserves IDF task priorities
  with SCHED_RR priority+1 and a 10ms radio interval, using registered SRAM
  stacks. Our shim maps every nonzero task priority to nice -10.

A bounded runtime experiment changed only the verified `wifi` task (PID58)
from SCHED_OTHER to FIFO23, below its existing IRQ thread FIFO50. A detached
150-second rollback was armed before changing policy; explicit restoration to
SCHED_OTHER was also completed and verified. Paired throughput was 2.540/4.189
Mbit/s TX/RX, with 1,009 buffer rejections and 47 final TX failures. A single
pair cannot establish a small throughput effect, and scheduling alone clearly
did not resolve the rejection/drop mechanism. No permanent scheduler change
was made. Raw logs and counter deltas are in
`build-output/wifi-memory-2026-11-23/wifi-e2e/fifo23/` and
`wifi-e2e/scheduler-comparison.json`.

Next implementation target is the S3 transport admission/completion contract:
retain rejected data, stop the netdev before its software queue fills, reserve
control-command progress, and resume on verified capacity without losing skb
ownership or waking stopped interfaces. The S31 private PP layouts, HE/MCS9
settings, RISC-V atomics/MMU handling, and chip-specific GDMA cannot be copied
into the S3 build. No S31-derived fix has been built or flashed yet.


## Queue-progress/backpressure kernel ready, 2026-11-23

Patch0139 fixes an actual blocked-producer wakeup omission in osi_queue_recv.
The new concurrent test fails the flashed source: a producer still waits after
receive frees its full queue. Fixed source passes front/back producer and
consumer wake tests with ASan/UBSan. Existing built main.c already snapshots
payload length before transport ownership transfer; the newly added ownership
regression confirms this, so no duplicate main.c patch was made.

The transport now admits at most eight pending data frames (including the
worker's current skb), stops the netdev at eight, and resumes at four only if
the interface is running, port open, and teardown has not started. The total
sixteen-entry queue reserves capacity for commands. Rejected data waits up to
250ms, with TX completion notification plus a short timed fallback. Exact
pinned ppProcTxDone disassembly shows the public callback precedes buffer
recycle, so completion is explicitly not treated as reusable DMA credit.
The callback retains no vendor pointer; stats add tx_done, tx_done_failed and
tx_queue_full. Successful callbacks and admission are distinct observations.

Build artifacts: build-output/wifi-backpressure-2026-11-23/. Kernel SHA256:
65b6944ddf660642b80c43eea0d9074bf78e0337576cbcdcf5671b8fc6ae863a.
Kernel bytes3,907,544; Linux partition spare90,152. Config/DTB and all three
cache-off/NOR templates are byte-identical to the flashed candidate. Five
focused sanitizer suites and Wi-Fi boot/state suites passed. Compiler retained
existing vendor COMMON, section mismatch and Xtensa NOR-object ABI warnings;
no new compile error occurred. Kernel-only helper checks current firmware,
partition table, flash ID and board identity. Flash and hardware acceptance
remain pending a fresh download-mode signal. No performance gain is claimed.


## Queue/backpressure kernel flashed, 2026-11-23

Fresh user download-ready signal received. Confirmed Nano MAC34:85:18:7b:81:b8,
16MB flash and unchanged partition table. Streaming firmware readback repeatedly
stopped at140KiB; no writes were attempted during those failures. Direct
esptool verify-flash against the SHA256-checked preceding firmware succeeded
(digest matched). Wrote only3,907,544 kernel bytes at0x140000 using esptool5.5.0,
no-reset before/after; written-data hash verified. Kernel SHA256 remains
65b6944ddf660642b80c43eea0d9074bf78e0337576cbcdcf5671b8fc6ae863a.
Firmware/rootfs/settings/data preserved. Board left in download mode for manual
reset; post-boot connection, counters, throughput and stability remain pending.
Logs: build-output/wifi-backpressure-2026-11-23/flash.log and flash-plan.json.


## Post-flash network validation, 2026-11-23

User clarified the initial unreachable interval was because the Nano was off;
exclude it from driver-failure evidence. A subsequent boot again autojoined
saved Wi-Fi at192.168.1.44. Startup12310 is ESP_ERR_WIFI_TX_DISALLOW (0x3016)
in the pinned IDF header. Initial atomic-dual passed20,000/20,000 over SSH.

Short paired30-second tests measured2.818 Mbit/s TX and3.424 Mbit/s RX;
TX retransmissions36 (preceding candidate93). Counter deltas:914 no-buffer
rejections,7 final failed submissions,0 completion failures,0 software queue
overflows. Internal SRAM free remained17,792 bytes. This improves the final
drop count over the preceding44 but does not fix error257.

Sustained verification passed3x128KiB each direction with SHA256/exact-byte
checks and5/5 fresh authenticated SSH sessions. A180-second SMP stress ran
alongside60-second TCP TX and RX tests. Both traffic commands completed,
measuring2.274/2.726 Mbit/s. SMP passed857 rounds and1,714,000 mutex/atomic
updates, fork/exec/storage, migration, cache visibility, TLS, floating point,
signals, timers, barriers, conditions and semaphores. No hardware cache fault
was reported. The link remained associated at-49dBm and internal SRAM free
remained17,792 bytes. Ping193 sent/190 received (1.6% loss), average134.396ms,
maximum1138.087ms, under the combined load.

Hardware acceptance is still FAILED for error-free/full-speed Wi-Fi. The
current image is not declared fully stable or maximized. Exact sustained
counter deltas and raw output are in wifi-e2e/sustained/summary.json under
build-output/wifi-backpressure-2026-11-23. Radio-off persistence across boot
remains untested on hardware. Fastfetch/GDMA/CoreMark optimization remain
separate pending items, with no claimed inclusion in this kernel.


## Missing cached-TX capability found and fixed, 2026-11-23

A read-only diagnostic sampled the exact flashed vendor descriptor/counter
symbols after checking both ROM pointer slots against the linked addresses.
Class9 (cached TX) was0/0, despite the old stats literal saying cache_tx32.
Pinned esp_wifi.h independently defines CONFIG_FEATURE_CACHE_TX_BUF_BIT as
(1<<1); config_is_cache_tx_buf_enabled disassembly reads bit1 from
 g_wifi_menuconfig+64. Our init set cfg.feature_caps=0. Setting the cache count
alone therefore left the cached-TX path disabled. This was missed in patch0138.
The internal-free-heap callback and PSRAM allocation callbacks were rechecked
against the pinned IDF and already match their intended memory classes.

Patch0140 sets only the cached-TX feature bit, reports actual vendor getters
for static/cache counts and enabled state, and aborts startup if the vendor
reports cache disabled or the wrong size. The new init regression uses actual
source assignments plus the pinned IDF bit definition: old source fails,
fixed source passes. Four adjacent sanitizer suites pass. Full kernel built;
config/DTB and three cache-off/NOR templates remain byte-identical.

Kernel-only flash completed after a fresh download-ready signal. Board MAC,
flash ID, partition table and existing-firmware digest passed preflight.
Wrote3,907,544 bytes at0x140000, SHA256:
5d8fc0a08828170b9abc81e6e71d3831afc65bf235c5ae17822c86346a666037.
Written-data hash verified. Firmware/rootfs/settings/data preserved. Board
left in download mode for manual reset. Runtime cache_tx_enabled1/cache_tx32
and measured performance/stability gains remain pending post-boot validation.
Artifacts: build-output/wifi-cache-enable-2026-11-23/.


## SRAM hot-code experiment, 2026-11-23

Patch0141 is opt-in. It reserves data alias 0x3fca0000..0x3fcb0000, executes
33,780 bytes of marked Wi-Fi hot code at 0x40390000 and uses the existing
boot relocation table. Layout verification checks exact image bytes, boot-copy
triple, known static SRAM owners, and unchanged cache-off/NOR templates.
Kernel text helpers recognize the exact noncontiguous SRAM text range.
Driver claims the region and compares loaded words with flash before radio init.
Kernel SHA256 ed5cd8d276d64d3076e2257a2010ec9618b3c4bc03646d5d84c0da84880fa107.
Kernel-only flash and write hash passed; user confirmed iram_bytes=33780,
cache_tx_enabled=1 and initial 19/19 successful submissions/completions.

PERFORMANCE ACCEPTANCE FAILED: isolated 30-second TX/RX 2.387/3.599 Mbps
versus previous cached-TX build 3.228/3.704 Mbps. These single pairs do not
establish a statistically repeatable regression, and do not demonstrate gain.
The short test had 572 no-memory returns, three final submission failures,
zero completion failures and zero software queue overflows.
Sustained 180-second SMP plus 60 seconds each TX/RX passed 855 rounds and
1,710,000 updates, fork/exec/storage, migration, cache visibility and other
SMP checks. Three 128 KiB bidirectional integrity checks and five fresh SSH
sessions passed. Loaded TX/RX 2.037/2.795 Mbps; ping 195 sent/192 received,
1.5% loss, average 148.928 ms, maximum 1711.139 ms. Internal free SRAM
remained 17,792 bytes. This is boot/integrity evidence, not full stability
or throughput acceptance. Artifacts: build-output/wifi-iram-2026-11-23/.

The S31 reference includes both IDF-style internal TX and a mac80211 softmac
path with budgeted NAPI, batched RX/status processing, bounded outstanding
frames, completion-driven queue wakeups, SRAM radio stacks and SCHED_RR
priority+1. Our shim still collapses nonzero vendor priorities to nice -10;
a previous temporary FIFO experiment alone did not fix throughput.
NuttX's S3 adapter maps vendor priority order, applies CPU affinity and
rejects external memory for internal allocations. Its common netdev copies
RX before freeing the blob buffer and notifying the network layer; TX calls
esp_wifi_internal_tx through esp_wifi_api. These are concrete integration
differences, not verified root causes or independently reproduced NuttX/S31
throughput results. Next diagnosis must time packet-stage latency and buffer
lifetime rather than assume IRAM placement alone solves the limitation.


### Live wait/pool diagnostics after SRAM test

No new kernel flashed. Read-only /proc stack sampling caught TX retry
waits; no process-state sampling, so this does not establish sleep-time
percentages. Verified vendor descriptor sampling showed TX static free-list
empty count +815 and cached-TX +361 during upload, with allocation-failure
fields unchanged. Cached class9 allocation/recycle counters both +3704:
0/0 descriptor allocation accounting does not mean the cache is disabled.
Static descriptor count is not outstanding-frame occupancy.
The fallback retry timer is jiffies-based at HZ100, despite a 2ms request;
completion notifications can wake it earlier. This is a latency candidate,
not a proven complete cause. The current iw interface exposes no bitrate.
Exact observations and limitations: build-output/wifi-iram-2026-11-23/diagnosis.md.


## Opt-in packet timing build, 2026-11-23

Patch0142 adds runtime-disabled-by-default counters for TX queue residence,
each vendor TX call, retry wait and RX allocation/copy. Read-only timing
parameter exposes coherent locked 64-bit count/total/max and >=1/10/100ms
counts; timing_enabled enables recording. Queue timestamp uses last8 skb
cb bytes with compile-time overlap check, preserving esp_skb_cb. There is
no per-packet allocation/logging. Retry policy and buffer configuration
are unchanged. Actual helpers and ownership/context, queue progress, OSI
and internal allocator sanitizer gates passed. Kernel compiled, exact SRAM
relocation/static-owner/image fit and three cache-off template checks pass.
Config and DTB are byte-identical to the currently booted SRAM image.
Kernel3,908,584 bytes, SHA256
62e7589fd7c06f466d3d7f5aa6befa66cd928c69ed16f9e235f0da5a762d2c2e.
Packaged in build-output/wifi-timing-2026-11-23. Not flashed; fresh manual
download readiness required. This is a measurement build, not a claimed
performance improvement. Paired recording-off/on tests and counter deltas
are required to assess measurement overhead.


### Timing results and DHCP ordering, 2026-11-23

Diagnostic kernel flashed with written-data hash verified; booted successfully.
Recording-off TCP TX/RX3.307/3.354 Mbps, on3.370/3.669 Mbps; these single
pairs show no obvious recording penalty, not a throughput-fix acceptance.
TX phase:9046 queued packets, mean queue13.003ms,9324 API calls mean278us,
278 retry waits mean18.259ms and total5.076s. Download phase outbound queue
mean20.225ms despite zero retry waits; API mean188us, RX allocation/copy146us.
Queue delay therefore remains independently of radio-buffer rejection.
Recording restored disabled. Full before/after deltas: artifact live-tests/summary.json.

Boot still logged12310. Positive error comes from data TX, not command
processing, despite generic combined log wording. Controlled reconnect with
header-only AF_PACKET logging captured DHCP355.127/359.117/362.198 before
EAPOL362.257..362.403. Corresponding errors12294 at355.179/359.163/362.250
prove early data submission while not connected. This did not reproduce
boot12310 exactly and must not be presented as complete proof of that packet.
Interfaces launched wpa_supplicant in post-up after DHCP. Added tested
/etc/wifi-wait-auth pre-up hook: bring link up, start supplicant with fresh
log, wait bounded45s for connection-complete event, then permit DHCP; wrong
key/timeout/disconnection do not permit DHCP. Installed into writable/etc
with interfaces backup before-auth-order-20261123, credentials and power
state unchanged. Source overlay updated. Four readiness cases and existing
Wi-Fi boot/script tests passed; repeated live packet capture in progress.


Startup-order validation completed: pre-up authentication gating removed
the reconnect's early DHCP requests, but a remaining rejection occurred
during teardown. Moved supplicant termination from pre-down to post-down
so DHCP can release its lease while the old protected link is authorized.
Final header capture identified RELEASE at667.554, EAPOL673.769..673.907,
DISCOVER676.568 and REQUEST676.667. No new TX/command errors occurred
in664.593..719.936. Historical boot12310 and previous test errors remain
in dmesg; full boot12310 acceptance requires a fresh manual reset.
Post-reconnect 128 KiB bidirectional SHA256/exact-byte integrity and
atomic-dual20,000/20,000 passed. Helper/interfaces installed in persistent
/etc and reflected in source overlay; rootfs binary itself was not rebuilt
or flashed. Source/readiness tests passed. Original interface backup
/etc/network/interfaces.before-auth-order-20261123 retained.


Fresh manual boot validation passed after startup/teardown ordering fix.
At42.37s uptime saved Wi-Fi autojoined192.168.1.44, installed helper and
interfaces hashes match, no radio TX/command error including12310 appears
in fresh dmesg. Initial no-memory/submission/completion failure counters
are zero. Cached-TX enabled and33,780-byte SRAM verification retained.
This verifies one fresh boot, not long-run/full-throughput acceptance.
Evidence: build-output/wifi-timing-2026-11-23/boot-validation/.


## Focused speed candidate,2026-11-23

Flash boundary recovery verified: final1/2/3/4-byte MTD reads pass,64KiB
write/sync/SHA256 readback passes; guard remains enabled with no MMU fault.
A boot CRC notice was identified by inode metadata as the interrupted
Fastfetch upload (inode24), not an unrelated file. Its978944-byte prefix
matched exactly; appended missing52372 bytes, full hash matched, actual
Fastfetch2.71.0 version/modules/default execution passed. It is installed
in persistent/home/root/.local/bin with login PATH/default config.
No private data backup copied; user declined.

Before/after Fastfetch process: free3064KiB both times, used4076->4100KiB.
The high Fastfetch reading includes its transient executable/runtime cost.

Patch0144 adds bounded runtime TX CPU0/1 selection and independent2ms
hrtimer retry, default CPU0/legacy. Context/ownership/shutdown tests and
CPU1/high-resolution branch regression pass. Firmware config changes only
Icache16->32KiB; bootloader identical,QIO80/OPI80 unchanged. Firmware and
Linux IRAM loads all at/above40378000, clear of enlarged cache window;
Wi-Fi reservation/boot-copy/cache-off preservation gates pass.

Matched baseline with host awake: CoreMark497.422447 single/803.770414
dual; TCP TX3.271/RX3.721Mbps. No gain claimed before new hardware run.
Rootfs candidate removes Neofetch, adds/usr/bin/fastfetch symlink to the
installed persistent binary and defaults. All other file hashes retained.
Package: build-output/platform-tune-2026-11-23. GDMA was inspected only;
no GDMA changes made, parked per user's current Wi-Fi/CPU-speed focus.
Fresh manual download readiness and hardware acceptance pending.


Cache32 hardware candidate FAILED at radio startup. User boot log PC40381381
resolves to firmware xTaskGetCurrentTaskHandle, RA40381826 to __getreent;
radio worker oopses before autojoin. Static SRAM overlap gates did not prove
radio runtime ABI compatibility. Revert cache32 source default to validated
cache16. Exact previous firmware154751... rollback at0x10000 prepared in
build-output/cache32-rollback-2026-11-23; preserve current kernel/rootfs/user
partitions. Do not reuse cache32 firmware or claim it accepted.


## Vector/literal collision resolved in candidate, 2026-11-23

The cache32 startup crash is now explained exactly, rather than attributed
only to a generic runtime ABI dependency. xTaskGetCurrentTaskHandle loads
its pxCurrentTCB pointer through a literal at 0x40379744. The firmware ELF
contains 0x3fc981cc there, but Linux exception code replaces that word with
0xa0030003. This matches the reported fault address byte for byte. Firmware
IRAM .text and its literals started at 0x40379000, inside Linux's vector page.

build/patch-firmware-vector-reservation.py places firmware .iram0.text at
MAX(ALIGN(0x1000),0x4037a000), preserving the whole Linux vector/atomic-guard
page. The corrected text begins at 0x4037a000 and ends at 0x40387200, below
Wi-Fi IRAM 0x40390000. The formerly conflicting literal is at 0x4037a744.
The new ownership gate checks allocated firmware sections (including BSS,
excluding linker dummy alias reservations) against Linux IRAM and the guard,
normalizing SRAM instruction/data aliases. verify-wifi-iram.py invokes it.
It rejects the failed image and accepts the corrected candidate. This is a
stronger check than the earlier Wi-Fi-only reservation gate.

Candidate build-output/firmware-vector-fix-2026-11-23 contains reproducible
build script, literal/fault proof, regression verification and firmware-only
flash helper. Firmware SHA256 4394b650aeeb40aa2eca743c2de2b78affa79ddf87c113785b79b2f83e90cb37,
164656 bytes. Bootloader unchanged; QIO80/OPI80 and ROM image header unchanged.
Hardware startup and performance acceptance still required. Cache32 source
default remains reverted until hardware acceptance. No speed gain claimed.

After firmware rollback, saved Wi-Fi autojoined .44 with no startup failure
and no MMU faults. Focused live TCP comparisons, host kept awake:

| TX worker/retry | Upload Mbps | Download Mbps |
| --- | ---: | ---: |
| CPU0 / original wait | 3.052 | 3.494 |
| CPU1 / original wait | 2.841 | 3.491 |
| CPU0 / 2ms hrtimer | 3.267 | 3.703 |
| CPU1 / 2ms hrtimer | 2.470 | 3.423 |

These single pairs do not establish a repeatable useful gain. Defaults
restored, no persistent tuning applied. One submission failure occurred in
the baseline phase, zero completion failures; later phases added neither.
SRAM free remained 17792 bytes. The dominant speed gap remains unresolved.


### Corrected firmware hardware acceptance, 2026-11-23

Firmware4394b650... flashed at0x10000; written hash verified. Manual reset,
saved Wi-Fi autojoined .44 at63.95s sampled uptime. Icache control0x0f and
/proc/cpuinfo confirm32KiB; no startup oops or TX error. Matched benchmark
binaries and host-awake20s TCP runs: single536.036256 (+7.8%), dual956.230712
(+19.0%), upload3.949517Mbps (+20.7%), download5.816823Mbps (+56.3%). Both
CoreMark CRC validations passed. These are individual matched comparisons,
not statistical or maximum-performance claims.

64KiB owned temporary network/flash roundtrip, fsync, SHA256 and exact bytes
passed; file removed. atomic-dual20,000/20,000 and10s SMP126 rounds/252,000
updates passed, including fork/exec isolation and concurrent storage. Guard
remains1; MMU error latches zero. Wi-Fi8758 accepted/completed, zero failed
submissions/completions;303 NO_MEM responses retried successfully. RX drops
are being investigated separately; no blanket stability claim.

Build pipeline now configures IDF before applying the linker reservation,
then builds. Firmware fingerprint includes the reservation helper. Source
SDK defaults now select32KiB with conflicting trailing16KiB override removed;
the source/generated memory configuration gate enforces32KiB alongside
QIO80/OPI80. DMA remains lower priority than memory/forkbank and Wi-Fi/CPU.


## OSI SRAM placement accepted; next bounded candidate

Kernel95ba68... moves hot queue/semaphore/mutex/ISR/RX/TX callbacks into
35,500-byte IRAM and allocates queue/synchronization controls from the unused
tail of the existing64KiB reservation. Hardware2752bytes controls in SRAM,
radioDMA pool unchanged65536/free17792; autojoin and no startup errors.
Matched CoreMark955.44 (unchanged); TCP upload4.542/download6.342Mbps.
64KiB exact network/flash roundtrip, atomics and short SMP/storage/fork checks
passed. One pair only; maximum-throughput/long-term stability not established.

Controlled IPv6 proof: two batches of5 link-local frames each added5 RX drops;
a protocol-specific listener received5 and added0 drops. This image lacks
IPv6. It proves a contributor to rx_dropped, not the origin of every historical
drop. Softnet overflow counters remained zero. No counters were hidden/reset.

Exact IDF24394052... FreeRTOS heap requires INTERNAL|8BIT for queues/control
objects. Its S3 iperf settings use staticRX16,dynamicRX64,RXBA32 and Icache32.
Live scalar metadata mirrors actual vendor getters: configured protocol BGN,
40MHz bandwidth, power saving off, AP reports11n. The raw diagnostic's last
two field labels were corrected to secondary-channel/RSSI (no credentials or
allocated object contents read); see radio-state-decoded.json.

O3-only CoreMark948.48 and one reference unroll profile920.50 validated but
were slower; neither installed or promoted. Larger-buffer candidate and two
memory/accounting fixes are packaged in build-output/platform-final-pass.
No claimed CPU gain and no GDMA changes. Hardware validation pending.


### Larger-buffer profile rejected; retain verified changes

0149 booted with correct24/16/64/32 getters and no TX/MMU errors, but RX measured
5.346 then4.246Mbps, below the prior6.342Mbps; TX4.836Mbps. Do not promote it.
Source patch0149 removed and pipeline restored to0140..0148. The replacement
retained-memory-fixes kernel restores byte-identical accepted Wi-Fi source95ba
while retaining iomap reserve and statm fixes0147/0148. No new CPU gain claimed.
Hardware acceptance pending; user reports9% usage remaining, so no further
experiments. Prior startup, control SRAM, coremark and integrity evidence remains
in wifi-sram-controls; final package/verification in retained-memory-fixes.

### Receive handoff candidate, 2026-11-24

Pinned ESP-IDF 5.1.1 passes received Wi-Fi buffers to lwIP by reference.
The Linux blob shim instead copies into a network buffer and queues it to
a worker so the vendor buffer can be returned promptly. In the exact
Linux version, netif_rx() called from the worker disables and re-enables
bottom halves for each packet. The RX batching candidate queues up to eight
consecutive STA/AP data frames and submits them under one bottom-half
disable/enable pair. Pending data is submitted before EAPOL, commands, and
events are processed. Radio-buffer lifetime and Linux FIFO submission are
preserved. The parameter rx_batch defaults off so the same image can compare
both paths. RX queue residence and allocation/rejection counts are reported
separately from Linux protocol discards.

Host ASan/UBSan tests exercise worker order, control/short frames, batching
bounds, RX allocation failure and exact vendor-buffer release, plus timing
isolation. The candidate builds on the retained kernel config and DTB and
passes SRAM ownership, exact IRAM boot copy, flash template and image fit
checks. See build-output/wifi-rx-batching. ab.py runs off/on/on/off with
matched workloads and restores parameters.

### Matched receive tests and default, 2026-11-24

With diagnostic timing enabled, the off/on/on/off TCP comparison averaged
5.31 Mbps download with batching off and 7.16 Mbps on. Mean RX queue wait
across the two download runs dropped from 13.46 to 9.32 ms. With timing
disabled, download averaged 6.45 Mbps off and 7.13 Mbps on. The two on
runs were 5.08 and 9.17 Mbps, spanning the off runs at 5.87 and 7.02 Mbps.
Upload averaged 4.91 Mbps off and 4.81 Mbps on with timing disabled.
The host benchmark endpoint used Wi-Fi. Both comparisons logged zero
driver RX allocation/rejection failures, failed TX submissions/completions,
and Linux softnet backlog drops.

The board passed the 64 KiB concurrent flash check, fork isolation, and
304,000 two-core updates with batching active. Patch 0150 integrates the
exact board-tested Wi-Fi source and retains batching off by default.
Timing and batching can both be toggled without reflashing. Results and
counter deltas are in build-output/wifi-rx-batching/ab-summary.json and
build-output/wifi-rx-production-ab/ab-summary.json.

### CoreMark placement check, 2026-11-24

The same 64 KiB D-cache firmware returned 532.30 and 536.87 when the
single-context binary ran on each CPU in isolation. Two simultaneous
independent pinned processes returned 485.16 and 494.44, for an aggregate
diagnostic score of 979.60. The existing dual-pthread binary returned
974.05. All CRC checks passed. The diagnostic sum is within 0.6% of the
pthread score and 8.4% below the isolated sum. This shows a concurrent
execution cost and rules out pthread overhead as the main gap in this
setup. It does not isolate the shared cache, external memory, or memory
layout. See build-output/coremark-affinity-2026-11-24.

The same session reported 3,040 KiB free and zero live ForkShadow pages.
The buddy allocator had no free 512 KiB or 1 MiB blocks but had one free
2 MiB block, which can be split. This snapshot does not reproduce the
later-workload failure to allocate a contiguous block.

### Transmit retry comparison on the 64 KiB D-cache build

Four 15-second TCP uploads used the same Wi-Fi link and receive batching
setting. The normal retry averaged 5.03 Mbps and 167 temporary TX pool
rejections per run. The high-resolution fallback averaged 4.56 Mbps and
1,180 rejections per run. Mean retry wait fell from about 16 ms to about
3 ms, while the number of attempts grew sevenfold. No submissions were
permanently lost and no completion failures were reported. In this setup
the faster retry mostly submits again before a radio buffer is available.
Keep the normal timer. Logs are in build-output/wifi-tx-retry-ab.
