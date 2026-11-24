# Wi-Fi-in-Linux port — plan

Three questions to answer, in order. Written 2026-11-10.

1. **Can Linux drive the Wi-Fi blob?** Link the blob into Linux, supply the
   osi_funcs table with Linux bodies, bring the MAC up, associate.
2. **Core 0 does flash only.** No radio, no IPC, no RTOS on core 0.
3. **Is the interface still `espsta0`?** Answered below — yes, by construction.

## Answer to (3), before anything is built

`espsta0` is a Linux-driver choice, not a blob or firmware name.

- `kernel-driver-esp32-ng/main.c:485`:
  `esp_cfg80211_add_iface(adapter->wiphy, "espsta%d", 1, NL80211_IFTYPE_STATION, NULL)`
- `kernel-driver-esp32-ng/esp_cfg80211.c:130`: `if (!strstr(netdev->name, "espsta"))`
- `strings` over every object in `libnet80211.a`, `libpp.a`, `libcore.a`, `libwapi.a`,
  `libmesh.a`: no interface-name string. The blob does not name the netdev.

So the port keeps `espsta0` for free. `wlan0` is what other drivers ask cfg80211 for
(`wlan%d`); the kernel does not impose it. Renaming would be a one-line change in
`main.c` if ever wanted, and would break the `strstr` guard at `esp_cfg80211.c:130`,
which drives the core-0/core-1 IP handshake — so it is not free.

## The shape

Today: core 0 runs `network_adapter` (ESP-IDF + FreeRTOS + the blob) and Linux talks to
it over IPC. `espsta0` exists in Linux, but the radio underneath it is core 0's.

Target: the blob is linked into the Linux kernel image and runs as Linux code. Core 0
boots Linux and does nothing else. No IPC, no firmware radio.

## Step 1 — partition change

The blob does not fit in `linux` (60 KiB free, blob is 552 KiB full / 287 KiB STA-only).
The firmware frees up to 768 KiB when Wi-Fi leaves it.

`new-files/esp-hosted/network_adapter/partition_table.esp32s3.16m8r`:

```
factory,          app,  factory, 0x00010000, 0x000c0000    768 KiB
linux,            0x40, 0x0,     0x00140000, 0x003d0000   3904 KiB
```

`factory` was left at 768 KiB: `network_adapter.bin` is 538 KiB and only shrinks once
the radio leaves the firmware, which is a later step. `linux` is now 3904 KiB, grown
from 3136 KiB to hold the blob and KALLSYMS; `rootfs` sits at `0x00510000` and `home`
at `0x00b40000`.

**This wipes `/etc` and `/home` on the board.** Back up first with
`flash.sh --backup` — and those backups hold account hashes and Wi-Fi credentials, so
they stay private and out of the repository.

## Step 2 — the osi_funcs table

`g_wifi_osi_funcs` has 116 entries (`esp_adapter.c:562-679`). Every one needs a Linux
body. They fall into groups:

| group | count | Linux body |
|---|---|---|
| memory | ~12 | `kmalloc` / `kzalloc` / `kfree`, or `vmalloc` for large |
| mutex / semaphore | ~10 | `struct mutex`, `struct semaphore` |
| queue | ~8 | `kfifo` + `wait_queue_head_t`, or a small wrapper |
| event group | ~6 | `wait_event` over a bitmask |
| task | ~5 | `kthread_create` / `kthread_run` |
| timer | ~6 | `timer_list` / `hrtimer` |
| interrupt | ~6 | `request_irq`, `enable_irq` / `disable_irq` |
| logging | ~4 | `printk` |
| NVS | ~11 | **no-op or a RAM key-value store** — flash is read-only |
| coex | ~20 | stubs returning 0 (single radio, no BT) |
| PHY glue | ~6 | `esp_phy_enable` / `_disable`, `phy_enter_critical` = spinlock |
| misc | ~20 | `esp_random`, `esp_timer_get_time`, `esp_read_mac`, `os_get_time` |

The NVS group matters: the blob uses NVS to persist calibration and config. With flash
read-only, NVS must be a RAM store that starts empty. Calibration is then redone every
boot — which is what the PHY already does on a cold start.

## Step 3 — link the blob into the kernel

The blob is ordinary ELF archives. Add to the kernel link:

- `libnet80211.a`, `libpp.a`, `libcore.a`, `libwapi.a` (STA-only set, 287 KiB)
- `libphy.a` (28 symbols in, 15 out)
- the ROM linker script `esp32s3.rom*.ld` for the 70 absolute addresses

The ROM script is the easy part — it is a list of `PROVIDE( sym = 0xADDR )` lines, the
same thing IDF links against.

**The unknown that could stop this:** the ROM's use of the `0x3fcef8xx` slots
(`s_wifi_queue`, `pp_task_hdl`, `s_pp_task_create_sem`, …). The relocations prove the
blob only reads those addresses. What the ROM writes into them is undocumented — the
TRM check established that no document answers it. If the ROM populates them during its
own Wi-Fi init, and that init does not run when Linux owns the MAC, the blob reads
garbage. **This is the single most likely reason step 1 of the test fails**, and it is
only answerable by running it.

## Step 4 — radio power, clock, interrupt

- `wifi_clock_enable_wrapper` → `wifi_module_enable()` — a `SYSTEM` register write.
- `wifi_reset_mac_wrapper` → `periph_module_reset(PERIPH_WIFI_MODULE)`.
- `set_intr_wrapper` → `esp_rom_route_intr_matrix(cpu_no, intr_source, intr_num)`.
- `set_isr_wrapper` → `xt_set_interrupt_handler(n, f, arg)`.

Core 0 currently owns all of these. In the target they are Linux's. The interrupt matrix
is at `0x600C2000` (TRM Table 4.3-3) and is already described in the DTS
(`arch/xtensa/boot/dts/esp32s3.dtsi:300-310`), though the board DTS overrides `reg` to
`0x600C2800` — the APP matrix, not core 0's.

## Step 5 — core 0

Once the blob is in Linux, `network_adapter` has nothing to do. Core 0's job becomes
"boot Linux". The firmware partition can shrink to near nothing, and the IPC
(`patches/linux/0016-misc-esp32-ipc-introduce-generic-IPC-for-ESP32.patch`) and the
esp32-ng driver's core-0 handshake become dead code.

**Do not remove them in the same step as the port.** Keep the firmware bootable until
Linux demonstrably drives the radio, so there is a way back.

## Order of work

1. Partition change + rebuild. Confirm the board still boots and `espsta0` still appears
   via the existing firmware path. This proves the repartition is safe before anything
   else changes.
2. Write the osi_funcs table. Compile it. No board yet.
3. Link the blob. Compile. No board yet.
4. Board test: does the kernel image still boot with the blob linked in but not called?
5. Board test: call the blob's init. Watch for the `0x3fcef8xx` problem.
6. Only then: remove the firmware's radio.

## What is not in this plan

- SMP. The DCache gate (`XCHAL_DCACHE_SIZE 0` vs the real 32/64 KiB write-back cache)
  is a separate problem, and one core is enough to answer "can Linux drive the MAC".
- Flash read-only as a whole. The NVS-in-RAM piece is needed for step 2, but the rest of
  the read-only work is independent.
- `wpa_supplicant` wiring. The test is "does the MAC come up and associate", and
  credentials can be hardcoded for it.
