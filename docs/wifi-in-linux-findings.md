# Wi-Fi inside Linux — findings

Running notes for assumption (3): the ESP32-S3 Wi-Fi stack runs on the Linux side,
the way `GrieferPig/esp32-s31-linux` does it, with no SD card involved.

Every figure below was measured in this tree or read out of the pinned source.
Where something is inferred rather than measured it says so.

## Toolchain

- Target: Arduino Nano ESP32 (ESP32-S3, N16R8 — 16 MB flash, 8 MB octal PSRAM).
- Blob libraries: `esp-idf/components/esp_wifi/lib/esp32s3/`
  - `libnet80211.a` 1227656 B
  - `libpp.a` 648530 B
  - `libmesh.a` 972644 B
  - `libsmartconfig.a` 186040 B
  - `libwapi.a` 52534 B
  - `libespnow.a` 54850 B
  - `libcore.a` 4820 B
- IDF revision pinned by `build/sources.lock`: `ESP_IDF_REV=24394052fcc462fe27935b4cf15ea1f3e98549c1`.

## Symbol accounting (measured)

**A first pass at this was wrong and the numbers below replace it.** The first reader
walked the archives with a hand-written `ar` parser that took the 16-byte member-name
field literally. These archives use the GNU long-name table, so most members come back
as `/47`, `/210`, `/661` — offsets into the table, not names. Anything not ending in
`.o` was skipped, which silently dropped most of the objects:

```
members of libnet80211.a, as the broken parser saw them:
['', '', '/0', '/20', 'ieee80211_api.o', 'ieee80211.o', '/47', '/67', '/92', ...]
```

`ieee80211_scan.o` is a `/N` entry, which is why `scan_add_bssid` — defined in it —
looked undefined. The corrected figures come from `nm` over the extracted objects,
which resolves the long-name table properly. Object counts per archive:

| library | objects |
|---|---|
| `libnet80211.a` | 52 |
| `libpp.a` | 25 |
| `libmesh.a` | 15 |
| `libsmartconfig.a` | 8 |
| `libwapi.a` | 3 |
| `libespnow.a` | 2 |
| `libcore.a` | 1 |

Union across all seven: **1497 undefined**, **2702 defined**. Resolving undefined
against defined leaves **109 names the blob needs from outside itself**.

That 109 is the whole external surface of the Wi-Fi stack, and it is what a Linux-side
port has to satisfy.

### Where the 109 resolve

| source | count |
|---|---|
| some other archive IDF ships (S3 `libphy.a`, `libnet80211`'s ROM shims, …) | 28 |
| `components/esp_rom/esp32s3/ld/esp32s3.rom*.ld` | 70 |
| **neither** | **11** |

The 70 ROM names are the cheap ones: absolute addresses in the S3 mask ROM, already
present on the silicon. A Linux port reaches them the same way IDF does, by linking
against the ROM linker script's addresses.

The 28 come from archives IDF already builds or ships, mostly `libphy.a`:
`register_chipv7_phy_init_param`, `phy_change_channel`, `phy_chan_filt_set`,
`phy_enable_low_rate`, `phy_disable_low_rate`, `phy_chan_dump_cfg`,
`phy_get_most_tpw`, `phy_set_most_tpw`, `set_rx_sense`, `noise_check_loop`,
`tx_pwctrl_background`, `ant_dft_cfg`, `ant_wifirx_cfg`, `ant_wifitx_cfg`,
`pTxRx`, `esp_crc8`, `g_chm`, `g_scan`, `g_osi_funcs_p`, `g_intr_lock_mux`,
`g_wifi_global_lock`, `net80211_funcs`, `mesh_rxcb`, `sta_rxcb`,
`wifi_sta_rx_probe_req`, `s_wifi_queue`, `s_netstack_free`, `xphyQueue`.

### The 11 that are neither

```
SC_EVENT
WIFI_EVENT
esp_event_handler_register
esp_event_handler_unregister
esp_mesh_send_event_internal
ets_delay_us
hexstr2bin
putchar
puts
snprintf
sprintf
```

Every one of them is trivially satisfiable:

- `SC_EVENT`, `WIFI_EVENT` — `ESP_EVENT_DECLARE_BASE` declarations
  (`esp_event_base.h:15`, `esp_wifi_types.h:900`). They are `extern esp_event_base_t
  const` **data**, not functions.
- `esp_event_handler_register` / `_unregister` — IDF's event loop, which a Linux port
  replaces with its own.
- `esp_mesh_send_event_internal` — 20 lines in `components/esp_wifi/src/mesh_event.c`.
- `ets_delay_us` — `esp32s3.rom.ld:27` has it as `PROVIDE( ets_delay_us = 0x40000600 )`.
  It is in the ROM; it is only in this list because the grep for ROM names missed the
  `PROVIDE(` prefix.
- `hexstr2bin` — a wpa_supplicant helper.
- `putchar`, `puts`, `snprintf`, `sprintf` — libc, which Linux has.

**The finding that matters for the port:** of the 109, **zero are FreeRTOS and zero
are IPC or interrupt calls.** A regex for the FreeRTOS naming convention
(`^(x|v|pv|ux|pc|e)[A-Z]`) over the whole external set returns exactly one hit,
`xphyQueue` — a queue *handle* the blob owns, not a call into FreeRTOS. There is no
`xTaskCreate`, no `xQueueSend`, no `vTaskDelay`, no `xSemaphoreTake` anywhere in it.

That is the load-bearing result, and it survives the parser fix. It means the FreeRTOS
surface of the Wi-Fi stack is not in the blob at all — it is in IDF's own glue, which
this project can rewrite. The blob calls through function pointers
(`g_wifi_osi_funcs`, ~120 of them), through the ROM, through `libphy.a`, and through
libc, and through nothing else.

## The FreeRTOS surface, located

All of it, in four files:

| file | what it needs |
|---|---|
| `components/esp_wifi/esp32s3/esp_adapter.c` | `xQueueCreate` ×2, `xQueueSend` ×2, `xTaskCreate` ×1, `xTaskCreatePinnedToCore` ×1 |
| `components/esp_coex/esp32s3/esp_coex_adapter.c` | `xSemaphoreTake` ×4 |
| `components/esp_phy/src/phy_common.c` | `esp_timer_create` ×1 |
| `components/esp_wifi/src/smartconfig_ack.c` | `vTaskDelay` ×4, `vTaskDelete` ×1, `xTaskCreate` ×1 |

`components/esp_wifi/src/` holds only seven files, and four of them are the
initialisation path rather than the radio:

- `mesh_event.c` 878 B
- `smartconfig.c` 2580 B
- `smartconfig_ack.c` 8733 B
- `wifi_default.c` 15391 B
- `wifi_default_ap.c` 980 B
- `wifi_init.c` 11678 B
- `wifi_netif.c` 5001 B

## The osi_funcs contract

`esp-idf/components/esp_wifi/include/esp_private/wifi_os_adapter.h` (163 lines)
declares `wifi_osi_funcs_t`: a struct of roughly 120 function pointers whose first
field is `int32_t _version` and whose last is `int32_t _magic`, followed by
`extern wifi_osi_funcs_t g_wifi_osi_funcs;`.

`components/esp_wifi/esp32s3/esp_adapter.c` (679 lines) defines `g_wifi_osi_funcs`
and every wrapper in it. `set_intr_wrapper` calls
`esp_rom_route_intr_matrix(cpu_no, intr_source, intr_num)`; `set_isr_wrapper` calls
`xt_set_interrupt_handler(n, (xt_handler)f, arg)`; `is_from_isr_wrapper` is
`!xPortCanYield()`; `wifi_clock_enable_wrapper` calls `wifi_module_enable()`;
`wifi_reset_mac_wrapper` calls `periph_module_reset(PERIPH_WIFI_MODULE)`.

This struct is the seam. A Linux port replaces the bodies, keeps the table, and the
blob never knows.

## Open — not yet resolved

- **The PHY.** The `phy_*` family and `register_chipv7_phy_init_param` resolve to
  `components/esp_phy/lib/esp32s3/libphy.a` (223754 B). That archive defines 257
  symbols and leaves 123 undefined, and **none of its undefined names are blob
  symbols** — it does not call back into the Wi-Fi stack. What it does call is the
  next thing to read: it is the part that touches the analog front end and the
  calibration data, and it is the largest remaining unknown.
- **`g_osi_funcs_p`.** The blob's own pointer to the osi_funcs table, which
  `libnet80211.a` defines and `libpp.a` consumes. Worth confirming the table is
  written exactly once, at init, so a Linux port knows where the seam is.
- **Interrupt routing.** `set_intr_wrapper` in `esp_adapter.c` calls
  `esp_rom_route_intr_matrix(cpu_no, intr_source, intr_num)`, and the firmware side
  (`network_adapter/main/linux_ipc.c:236-246`) allocates the IPC ISR on PRO cpu IRQ6
  — which is `XCHAL_TIMER0_INTERRUPT`, the same number Linux uses for
  `LINUX_TIMER_INT`. That collision has to be resolved for any port that keeps the
  firmware's IPC alive.

## The PHY — `libphy.a`

`components/esp_phy/lib/esp32s3/libphy.a` is 223754 B, 22 objects. It defines 265
symbols and leaves 128 undefined; resolving those against its own definitions leaves
**15 externals**:

```
__divdf3  __divdi3  __fixdfsi  __floatsidf  __subdf3   <- compiler-rt, Linux has them
ets_delay_us  memcpy  memset  sprintf                  <- ROM / libc
phy_get_romfuncs  rom_phy_param_addr  rom_rx_gain_force <- ROM
phy_enter_critical  phy_exit_critical  phy_printf      <- IDF glue
```

**None of the 15 is a Wi-Fi blob symbol.** The PHY does not call back into the Wi-Fi
stack — the dependency runs one way, Wi-Fi → PHY.

Three of them are ROM addresses, straight out of `esp32s3.rom.ld`:

| symbol | address |
|---|---|
| `phy_get_romfuncs` | `0x40005c58` |
| `rom_phy_param_addr` | `0x40006324` |
| `rom_rx_gain_force` | `0x4000642c` |

The other three are IDF glue, and they are small:

- **`phy_enter_critical` / `phy_exit_critical`** — `components/esp_phy/src/phy_init.c:160`
  and `:172`. Each is a `portENTER_CRITICAL`/`portEXIT_CRITICAL` pair around
  `s_phy_int_mux`, choosing the ISR variant when `xPortInIsrContext()`. The level
  argument is returned as a constant zero and ignored on exit — the comment says so:
  *"Interrupt level will be stored in current tcb, so always return zero."* On Linux
  this is a spinlock and a local-IRQ-disable, nothing more.
- **`phy_printf`** — `components/esp_phy/src/lib_printf.c:45`. A varargs print.

So the PHY's whole outside dependency, other than libc and the ROM, is **one spinlock
pair and one printf**. That is the largest remaining unknown in the Wi-Fi port, and it
turns out to be small.

## What the blob's outside world is, in full

1. `g_wifi_osi_funcs` — ~120 function pointers, defined in
   `components/esp_wifi/esp32s3/esp_adapter.c`.
2. The S3 mask ROM — 70 addresses from `esp32s3.rom*.ld`.
3. `libphy.a` — 28 symbols, and the PHY in turn needs only the 15 above.
4. libc and compiler-rt — `memcpy`, `strlen`, `sprintf`, `__divdi3`, `__udivsi3`, …

Nothing else. No FreeRTOS, no IPC, no interrupt controller, no task, no queue.

## Relocation audit — call vs data, for all 109

Name-based analysis cannot tell a call from an address load. The relocations can.
`readelf -rW` over all 106 objects, every `R_XTENSA_*` entry, gives:

| relocation | count | meaning |
|---|---|---|
| `R_XTENSA_32` | 46054 | 32-bit data address |
| `R_XTENSA_SLOT0_OP` | 42877 | PC-relative — the call/jump form |
| `R_XTENSA_ASM_EXPAND` | 7881 | paired with a call through the assembler-expand stub |

A symbol that is **called** carries `R_XTENSA_ASM_EXPAND`; a symbol that is only
**read as an address** carries `R_XTENSA_32` alone. Splitting the 109 on that rule:

### 59 are data only — addresses, never called

```
BasicOFDMSched_ptr  SC_EVENT  WIFI_EVENT  ap_no_lr_ptr  g_chm  g_config_func
g_eb_list_desc_ptr  g_esp_mesh_quick_funcs_ptr  g_hmac_cnt_ptr  g_ic_ptr
g_intr_lock_mux  g_lmac_cnt_ptr  g_mac_sleep_en_ptr  g_mesh_init_ps_type_ptr
g_mesh_is_root_ptr  g_mesh_is_started_ptr  g_mesh_topology_ptr
g_net80211_tx_func  g_osi_funcs_p  g_per_conn_trc  g_pm_cfg_ptr  g_pm_cnt_ptr
g_pm_ptr  g_scan  g_timer_func  g_tx_cacheq_ptr  g_tx_done_cb_func
g_txop_queue_status_ptr  g_wdev_last_desc_reset_ptr  g_wifi_global_lock
g_wifi_menuconfig_ptr  if_ctrl_ptr  lmacConfMib_ptr  mesh_rxcb  net80211_funcs
our_controls_ptr  our_instances_ptr  our_tx_eb  our_wait_eb  pTxRx
pp_sig_cnt_ptr  pp_task_hdl  pp_wdev_funcs  rc11BSchedTbl_ptr  rc11NSchedTbl_ptr
rcLoRaSchedTbl_ptr  s_encap_amsdu_func  s_fragment_ptr  s_michael_mic_failure_cb
s_netstack_free  s_pp_task_create_sem  s_pp_task_del_sem  s_wifi_queue  sta_rxcb
trc_ctl_ptr  wDevCtrl_ptr  wDevMacSleep_ptr  wifi_sta_rx_probe_req  xphyQueue
```

**This closes the FreeRTOS question.** `xphyQueue`, `s_wifi_queue`, `pp_task_hdl`,
`s_pp_task_create_sem`, `s_pp_task_del_sem`, `s_netstack_free` and `mesh_rxcb` are
all in this list. The blob **loads their addresses**; it never calls them. They are
slots in the ROM's reserved block at `0x3fcef8xx`–`0x3fcef9xx`
(`esp32s3.rom.ld:2060-2083`), which the ROM's own Wi-Fi code populates — the blob
reads the handle the ROM put there, it does not itself invoke a scheduler.

The names are misleading and that is worth saying plainly: a symbol called
`s_pp_task_create_sem` sitting in ROM, read as data by the blob, is not a call into
FreeRTOS. It is the blob looking up a handle.

### 50 are called

```
__divdi3 __divsf3 __extendsfdf2 __floatundisf __moddi3 __popcountsi2 __udivdi3
__umoddi3                              <- compiler-rt
memcmp memcpy memmove memset printf putchar puts roundup2 snprintf sprintf
strcmp strcpy strlen strncmp strncpy strnlen hexstr2bin crc32_le esp_crc8
                                       <- libc / IDF helpers
ets_backup_dma_copy ets_delay_us ets_printf rom_read_hw_noisefloor
                                       <- ROM
ant_dft_cfg ant_wifirx_cfg ant_wifitx_cfg noise_check_loop phy_chan_dump_cfg
phy_chan_filt_set phy_change_channel phy_disable_low_rate phy_enable_low_rate
phy_get_most_tpw phy_set_most_tpw register_chipv7_phy_init_param set_rx_sense
tx_pwctrl_background                   <- libphy.a
esp_event_handler_register esp_event_handler_unregister
esp_mesh_send_event_internal           <- IDF esp_event / mesh
esp_net80211_rom_version_get esp_pp_rom_version_get   <- IDF version strings
```

**Not one of the 50 is FreeRTOS.** Every one is libc, compiler-rt, a ROM function,
`libphy.a`, or a two-line IDF shim. There is no task, queue, semaphore, mutex, delay
or yield call in the blob's entire external surface.

## What is still not proven

- **Indirect calls.** The blob can hold a function pointer it was handed and call
  through it. `g_osi_funcs_p` is exactly that, and it is in the data-only list —
  so the blob dereferences it and calls whatever IDF put there. Those targets are
  enumerable (they are the ~120 wrappers in `esp_adapter.c`) but I have not walked
  them one by one.
- **What the ROM does with `0x3fcef8xx`.** The relocations prove the blob only reads
  those slots. They do not say what the ROM writes into them, or when. That is a
  question about the mask ROM, and the mask ROM is not in this tree.

## TRM check (ESP32-S3 TRM v1.8, `docs/reference/esp32-s3_technical_reference_manual_en.pdf`, 1531 pp)

Read to settle two things the IDF headers could not: the `0x3fcef8xx` region, and
whether the DCache is real. It settles the second and confirms the first is out of
scope for any document.

### 1. `0x3fcef8xx` — the TRM documents the region, not the contents

- Table 4.3-1 (p. 403): data bus `0x3FC8_8000`–`0x3FCE_FFFF` = **Internal SRAM 1**, 416 KB.
- Table 15.3-3 (p. 685): SRAM 1 = Block2–Block8; `0x3FCE_0000`–`0x3FCE_FFFF` = **Block8**.
- Table 4.3-3 (p. 409): `0x600C_4000`–`0x600C_BFFF` = **Reserved**. The EXTMEM/cache
  register block IDF places at `0x600C4000` has no entry in the TRM peripheral map.

So the ROM's use of the tail of SRAM1 Block8 is undocumented. The TRM confirms the
address range belongs to SRAM; it says nothing about which words the ROM writes there.
**This gap cannot be closed by reading any document.** It needs the board.

### 2. DCache — the TRM confirms the silicon and confirms the kernel is wrong

§4.3.3.2 (p. 405), verbatim:

> The size of DCache can be configured to 32 KB or 64 KB, while its block size can be
> configured to 16 B, 32 B or 64 B.

§4.3.3.3 (p. 406), the operation list:

> 1. **Write-Back**: … clear the dirty bits in dirty blocks and update the new data to
>    the external memory. … **Only DCache has this function.**
> 2. **Clean** … **Only DCache has this function.**
> 3. **Invalidate** … **Both ICache and DCache have this function.**

The cache is real, it is write-back, and it is 32 or 64 KB. `XCHAL_DCACHE_SIZE 0` in
`arch/xtensa/variants/esp32s3/include/variant/core.h:242-294` is a lie the kernel tells
itself, and every `system_flush_invalidate_dcache_range()` in `arch/xtensa/mm/misc.S`
compiles to `dsync` and nothing else. This is now sourced to the TRM, not inferred.
It is the gate for assumption (4).

Also §4.3.4 (p. 407): GDMA reaches SRAM 1 at `0x3FC8_8000`–`0x3FCE_FFFF` and SRAM 2 at
`0x3FCF_0000`–`0x3FCF_FFFF`, and "GDMA cannot access the internal memory occupied by
cache." Matches the earlier finding that a GDMA descriptor list must live in internal RAM.

### 3. New — the cache sync interrupt exists (relevant to assumption 4)

Table 9.3-1 (p. 542) lists, as peripheral interrupt sources:

| no. | source | map register |
|---|---|---|
| 56 | `CACHE_IA_INT` | `INTERRUPT_COREx_CACHE_IA_INT_MAP_REG` |
| 61 | `DCACHE_PRELOAD_INT` | `INTERRUPT_COREx_DCACHE_PRELOAD_INT_MAP_REG` |
| 62 | `ICACHE_PRELOAD_INT` | `INTERRUPT_COREx_ICACHE_PRELOAD_INT_MAP_REG` |
| 63 | `DCACHE_SYNC_INT` | `INTERRUPT_COREx_DCACHE_SYNC_INT_MAP_REG` |
| 64 | `ICACHE_SYNC_INT` | `INTERRUPT_COREx_ICACHE_SYNC_INT_MAP_REG` |
| 94 | `CACHE_CORE0_ACS_INT` | `INTERRUPT_COREx_CACHE_CORE0_ACS_INT_MAP_REG` |
| 95 | `CACHE_CORE1_ACS_INT` | `INTERRUPT_COREx_CACHE_CORE1_ACS_INT_MAP_REG` |

Offsets, Table 9.4.1 / 9.4.2 (pp. 548, 552): `INTERRUPT_CORE0_DCACHE_SYNC_INT_MAP_REG`
= `0x00FC`, `INTERRUPT_CORE1_DCACHE_SYNC_INT_MAP_REG` = `0x08FC`. Base `0x600C2000`
(Table 4.3-3) gives `0x600C20FC` and `0x600C28FC`.

A hardware completion signal for a DCache sync. That is what makes a Linux-side cache
flush **waitable** instead of polled — the missing piece for assumption (4).

### 4. Confirmed — the IRQ6 collision is real, and worse than assumed

Table 9.3-2 (p. 544): CPU interrupt **6** is **Internal, Timer.0**, priority 1. §9.3.3
(p. 545) says the interrupt matrix routes a peripheral source only to `Num_P` in
0–5, 8–10, 12–14, 17–28, 30–31. And §9.3.3.3:

> Setting the corresponding configuration register `INTERRUPT_COREx_SOURCE_Y_MAP_REG` of
> the source to any `Num_I` **disables** this interrupt Source_Y.

So `intr_matrix_set(0, ETS_FROM_CPU_INTR0_SOURCE + N, 6)` in
`network_adapter/main/linux_ipc.c:236-246` is not routing a peripheral to CPU IRQ6 — it is
writing 6 into a map register, which per the TRM **disables** that source. The firmware's
IPC ISR therefore lands on PRO cpu **timer 0**, the same line as
`XCHAL_TIMER0_INTERRUPT` / `LINUX_TIMER_INT`. Real collision, TRM-confirmed, and the
firmware's own `esp_intr_alloc` call is what actually installs the handler.

## The osi_funcs table — enumerated (closes the indirect-call gap)

`components/esp_wifi/esp32s3/esp_adapter.c:562-679` defines `g_wifi_osi_funcs`. Counted
from the initialiser: **116 entries**, first `._version`, last `._magic`.

This is the table the blob calls *through*, so it is the complete list of indirect call
targets. Splitting it by what each target actually is:

| kind | count | examples |
|---|---|---|
| wrapper defined in this file | 72 | `set_intr_wrapper`, `queue_send_wrapper`, `task_create_wrapper` |
| `esp_coex_common_*_wrapper` (in `esp_coex_adapter.c`) | 15 | `_semphr_take`, `_spin_lock_create`, `_task_yield_from_isr` |
| direct IDF / libc function | 29 | `esp_cpu_intr_enable`, `esp_phy_enable`, `esp_read_mac`, `nvs_*`, `esp_log_write`, `malloc`, `free` |

### The FreeRTOS surface of the table, in full

Grepping the initialiser for the FreeRTOS naming convention returns exactly **nine**
symbols, and every one is a **direct function pointer**, not a wrapper:

```
._queue_delete           = (void(*)(void *))vQueueDelete,
._queue_msg_waiting      = (uint32_t(*)(void *))uxQueueMessagesWaiting,
._event_group_create     = (void *(*)(void))xEventGroupCreate,
._event_group_delete     = (void(*)(void *))vEventGroupDelete,
._event_group_set_bits   = (uint32_t(*)(void *,uint32_t))xEventGroupSetBits,
._event_group_clear_bits = (uint32_t(*)(void *,uint32_t))xEventGroupClearBits,
._task_delete            = (void(*)(void *))vTaskDelete,
._task_delay             = vTaskDelay,
._task_get_current_task  = (void *(*)(void))xTaskGetCurrentTaskHandle,
```

The other FreeRTOS calls sit one level down, inside wrappers:

- `esp_adapter.c` wrappers: `xQueueCreateStatic` / `xQueueCreate` (`:126`, `:136`),
  `vQueueDelete` (`:143`), `xSemaphoreCreateCounting` (`:212`), `xSemaphoreCreateRecursiveMutex`
  (`:226`), `xSemaphoreCreateMutex` (`:231`), `vSemaphoreDelete` (`:236`),
  `xSemaphoreTakeRecursive` / `xSemaphoreGiveRecursive` (`:241`, `:246`),
  `xQueueSend` (`:256`), `xQueueSendFromISR` (`:265`), `xQueueGenericSend` (`:270`, `:275`),
  `xQueueReceive` (`:280`), `xEventGroupWaitBits` (`:289`),
  `xTaskCreatePinnedToCore` (`:298`), `xTaskCreate` (`:303`), `xPortCanYield` (`:186`).
- `esp_coex_adapter.c:59,70,84,89,95,97,103,148,164,180,190,203,208,214,216,222`:
  `xPortInIsrContext`, `xSemaphoreCreateCounting`, `vSemaphoreDelete`, `xSemaphoreTake`,
  `xSemaphoreGive`, `xPortCanYield`, `xSemaphoreCreateCountingStatic`, `xSemaphoreTakeFromISR`,
  `xSemaphoreGiveFromISR`.

### What this closes

The earlier relocation audit proved no **direct** reference from the blob is FreeRTOS.
This proves the same for **indirect** references: the blob's only way to reach FreeRTOS is
through these 116 pointers, and the FreeRTOS ones among them are a named, finite, 20-odd
symbol list, all of which are ordinary mutex/queue/event-group/task primitives.

**Nothing here needs a scheduler to be reimplemented.** Every one maps onto a Linux
primitive: `xSemaphoreTake` → `mutex_lock` / `down`, `xQueueSend` → a waitqueue or a
`kfifo` with a completion, `xTaskCreate` → `kthread_create`, `xEventGroupWaitBits` →
`wait_event`, `vTaskDelay` → `msleep`, `xTaskGetCurrentTaskHandle` → `current`.

The gap named in the previous section is closed. What remains genuinely unknown is only
the ROM's use of `0x3fcef8xx`, and the TRM check above establishes that no document
answers it.

## The blob's true external surface — measured, STA-only, no mesh

Re-derived this session over `libnet80211.a libpp.a libcore.a libwapi.a libespnow.a`
(the STA-only set; `libmesh.a` and `libsmartconfig.a` excluded). Method: collect every
undefined symbol in the extracted objects, subtract every symbol defined inside the same
set, then subtract everything the ROM linker scripts define
(`esp32s3.rom.ld`, `esp32s3.rom.newlib*.ld`, `esp32s3.rom.api.ld`, `esp32s3.rom.libgcc.ld`)
and everything `libphy.a` defines.

**18 symbols are satisfied by neither the ROM nor `libphy.a`:**

```
WIFI_EVENT
ets_delay_us
hexstr2bin
ieee80211_init_mesh_assoc_ie
ieee80211_vnd_mesh_quick_get
ieee80211_vnd_mesh_quick_set
ieee80211_vnd_mesh_roots_get
ieee80211_vnd_mesh_roots_set
mesh_clear_parent_candidate
mesh_get_parent_candidate
mesh_get_parent_monitor_config
mesh_get_rssi_threshold
mesh_set_ie_crypto_config
mesh_set_parent_candidate
mesh_set_parent_monitor_config
mesh_set_rssi_threshold
puts
sprintf
```

**Twelve of the eighteen are mesh-only.** `libmesh.a` is the archive that defines them;
drop it and they disappear from the set entirely. That leaves:

```
WIFI_EVENT
ets_delay_us
hexstr2bin
puts
sprintf
```

Five symbols, all trivial. `ets_delay_us` is a busy-wait; `hexstr2bin` is a hex parser;
`puts`/`sprintf` are logging; `WIFI_EVENT` is an IDF event base, an `esp_event` handle.

**Every `phy_*`, `ant_*`, `set_rx_sense`, `tx_pwctrl_background`, `noise_check_loop` and
`register_chipv7_phy_init_param` reference resolves inside `libphy.a`** — all 14 verified
one by one. The Wi-Fi blob and the PHY are a closed pair against the ROM.

### `g_osi_funcs_p`, not `g_wifi_osi_funcs`

The blob does **not** reference `g_wifi_osi_funcs`. Every object that needs the OS
interface references **`g_osi_funcs_p`** — a *pointer*, which the ROM linker script
defines (`esp32s3.rom.ld`). `g_wifi_osi_funcs` is the IDF-side definition; the blob's
route to it is the pointer.

Consequence for the port: there is no `esp_wifi_init` to call. The blob exports
**`esp_wifi_init_internal`** (`ieee80211_api.o`, offset `0x68`) and reads `g_osi_funcs_p`
itself. The Linux side defines `g_osi_funcs_p`, points it at the table, and calls
`esp_wifi_init_internal`.

### The entry points the blob actually exports

From `ieee80211_api.o` and `ieee80211_output.o`:

```
esp_wifi_init_internal      ieee80211_api.o    0x68
esp_wifi_start              ieee80211_api.o    0x14
esp_wifi_stop               ieee80211_api.o    0x6c
esp_wifi_set_mode           ieee80211_api.o    0x14
esp_wifi_set_config         ieee80211_api.o    0x1c
esp_wifi_get_config         ieee80211_api.o
esp_wifi_connect            ieee80211_api.o    0x1c
esp_wifi_disconnect         ieee80211_api.o    0x1c
esp_wifi_scan_start         ieee80211_api.o    0x48
esp_wifi_set_channel        ieee80211_api.o    0x1c
esp_wifi_set_mac            ieee80211_api.o    0x1c
esp_wifi_internal_reg_rxcb  ieee80211_api.o    0x18
esp_wifi_internal_tx        ieee80211_output.o 0x04
```

224 `esp_wifi_*` symbols are defined across the STA-only set. The 13 above are the ones
a station-only port needs.

## `0x3fcef8xx` — CLOSED. It is the ROM's globals block, and the linker script names every word.

An earlier session recorded this as "the TRM does not document the ROM's use of
`0x3fcef8xx` ... that gap cannot be closed by reading any document." That was wrong, and
the correction matters because the port depends on it.

The TRM does not document it. **`esp32s3.rom.ld` does** —
`components/esp_rom/esp32s3/ld/esp32s3.rom.ld`. It is not a document about the ROM, it is
the ROM's own symbol table, and it names every global in the block with its address.

**62 globals, `0x3fcef81c` through `0x3fcef9fc`.** The full block:

```
phy_param_rom              0x3fcef81c     g_coex_param_ptr           0x3fcef820
g_coa_funcs_p              0x3fcef824     coexist_funcs              0x3fcef828
coex_schm_env_ptr          0x3fcef82c     coex_pti_tab_ptr           0x3fcef830
coex_env_ptr               0x3fcef834     sta_rxcb                   0x3fcef838
mesh_rxcb                  0x3fcef83c     s_netstack_free            0x3fcef840
g_tx_cacheq_ptr            0x3fcef844     g_hmac_cnt_ptr             0x3fcef848
g_ic_ptr                   0x3fcef84c     g_chm                      0x3fcef850
g_scan                     0x3fcef854     net80211_funcs             0x3fcef858
s_encap_amsdu_func         0x3fcef85c     g_per_conn_trc             0x3fcef860
g_tx_done_cb_func          0x3fcef8ac     wifi_sta_rx_probe_req      0x3fcef8b0
s_michael_mic_failure_cb   0x3fcef8b4     g_timer_func               0x3fcef8b8
g_net80211_tx_func         0x3fcef8bc     g_config_func              0x3fcef8c0
g_mesh_is_started_ptr      0x3fcef8c4     g_mesh_init_ps_type_ptr    0x3fcef8c8
g_mesh_topology_ptr        0x3fcef8cc     g_mesh_is_root_ptr         0x3fcef8d0
g_mac_sleep_en_ptr         0x3fcef8d4     g_txop_queue_status_ptr    0x3fcef8d8
g_esp_mesh_quick_funcs_ptr 0x3fcef8dc     g_pm_cfg_ptr               0x3fcef8e0
g_pm_ptr                   0x3fcef8e4     g_pm_cnt_ptr               0x3fcef8e8
trc_ctl_ptr                0x3fcef8ec     BasicOFDMSched_ptr         0x3fcef8f0
rcLoRaSchedTbl_ptr         0x3fcef8f4     rc11NSchedTbl_ptr          0x3fcef8f8
rc11BSchedTbl_ptr          0x3fcef8fc     ap_no_lr_ptr               0x3fcef900
xphyQueue                  0x3fcef904     g_wifi_menuconfig_ptr      0x3fcef908
s_pp_task_del_sem          0x3fcef90c     s_pp_task_create_sem       0x3fcef910
pp_task_hdl                0x3fcef914     s_wifi_queue               0x3fcef918
g_wifi_global_lock         0x3fcef91c     g_intr_lock_mux            0x3fcef920
if_ctrl_ptr                0x3fcef924     s_fragment_ptr             0x3fcef928
g_eb_list_desc_ptr         0x3fcef92c     pp_sig_cnt_ptr             0x3fcef930
g_lmac_cnt_ptr             0x3fcef934     wDevMacSleep_ptr           0x3fcef938
wDevCtrl_ptr               0x3fcef93c     g_osi_funcs_p              0x3fcef940
pp_wdev_funcs              0x3fcef944     our_tx_eb                  0x3fcef948
our_wait_eb                0x3fcef94c     lmacConfMib_ptr            0x3fcef950
pTxRx                      0x3fcef954     sdk_cfg_priv_opts          0x3fcef958
```

**`g_osi_funcs_p = 0x3fcef940`.** The OS-interface pointer the blob reads lives at
`0x3fcef940`, in internal SRAM 1 Block 8. That is the whole reason the region mattered.

### What this means for the port

The ROM owns these slots. The blob does not define them and must not — it reads them.
So the Linux side does not declare `g_osi_funcs_p` as a variable; it **writes the address
of its own `wifi_osi_funcs_t` into `0x3fcef940`** before the blob runs, and the ROM's
other 61 slots stay as the ROM left them.

That is one `writel()` at a known address, and it is why the ROM linker script has to be
part of the port: it is the map of what the blob will read.

### The `0x3fcef8xx` region in Linux's memory

Board DT `memory@0/reg` is `0x3d800000` + `0x00800000`, so Linux's RAM ends at
`0x3e000000`. `0x3fcef940` is outside it — not Linux memory, not cacheable by the kernel,
and reachable only through `ioremap` (identity on this NOMMU build). Writing it does not
corrupt Linux. Reading it back is how the port confirms the pointer took.

---

## The link, and where the archives have to come from

### The endianness trap, measured

`$TC-gcc -c t.c` with no flags produces `elf32-xtensa-be`. The same command with
`XTENSA_GNU_CONFIG=/refs/esp32-linux-build/build/xtensa-dynconfig/esp32s3.so` produces
`elf32-xtensa-le`. **That environment variable is what selects little-endian**, not a
command-line flag — there is no `-mlittle-endian` on this cross compiler
(`unrecognized command-line option '-mlittle-endian'`).

The kernel's own `.cmd` files carry **no endian flag at all**; only
`-mabi=windowed -pipe -mlongcalls -mtext-section-literals -mforce-no-pic
-mno-serialize-volatile -mno-fdpic`. So a hand-run link that omits `XTENSA_GNU_CONFIG`
fails with `compiled for a little endian system and target is big endian` on every object,
and grepping the kernel build for an endian flag finds nothing. The kernel build already
has the variable set (`container-build.sh` exports it); only a hand-run harness needs it.

### The link closes

With `XTENSA_GNU_CONFIG` set, `$TC-ld -m elf32xtensa --no-relax`, the six archives
extracted, and generated stubs for what the set leaves undefined:

```
vmlinux.test  729156 bytes
text 360055, data 4658, bss 12332, dec 377045
2522 symbols resolved
```

The 152 symbols still missing at that point are libgcc soft-float (12), the 13 mesh-only
names, libc string/printf, and the kernel symbols the real vmlinux supplies. None is a
reason the link cannot close in the kernel.

### `ets_delay_us` is `0x40000600`

`esp32s3.rom.ld:27` is `PROVIDE( ets_delay_us = 0x40000600 );`, and
`esp32s3.rom.api.ld:40` has `PROVIDE ( esp_rom_delay_us = ets_delay_us );`. An earlier note
in this file recorded `0x40000930`; that was wrong.

The port does **not** bind it to the ROM address. `esp_blob_support.c` defines it against
the kernel's own `udelay` — the same calibrated busy-wait, and the call stays inside Linux
where a traceback can see it. The other three ROM names (`phy_get_romfuncs = 0x40005c58`,
`rom_phy_param_addr = 0x40006324`, `rom_rx_gain_force = 0x4000642c`) are bound as absolute
assignments in `arch/xtensa/kernel/vmlinux.lds.S`, which costs nothing and produces no code.
A C definition of any of those three would be a duplicate symbol and fail the link.

### Where the archives are injected

`libs-y` → `KBUILD_VMLINUX_LIBS` → `--start-group ${libs} --end-group` in
`scripts/link-vmlinux.sh`. The upstream idiom for a Kconfig-gated archive is
`libs-$(CONFIG_X) += <path>.a`, as `arch/arm64/Makefile:162` and
`arch/mips/Makefile:261-262` do. The hook point is `arch/xtensa/Makefile:69`
(`libs-y += arch/xtensa/lib/`).

### The stage-ordering blocker, and its fix

The kernel links in the `base-rootfs` stage. The six archives live in the ESP-IDF
submodule of esp-hosted, which the `firmware` stage clones — **one stage later**. So
`libs-y` pointed at files that did not exist yet.

`build/container-build.sh` now has a `blob-libs` stage between `espctl` and `base-rootfs`.
It is the clone and the submodule checkout and nothing else — no IDF tools, no Python
environment, no build — and it asserts each of the six archives is present and non-empty,
so a missing one is named at the point of cause rather than an hour later as a link error.
`firmware` finds the clone already there, because `clone_locked` is idempotent.

`build/reproduce-macos.sh` carries `blob-libs` in `DOWNSTREAM` and in `stage_inputs`, with
`build/sources.lock` and `new-files/toplevel/apply-local-changes.sh` as its inputs; `package`
includes it in its union because it copies the xipImage that now depends on the archives.

### The patch series applies clean

Replayed against a pristine `linux-7.2.4` tarball in lexical order, exactly as Buildroot
does: **zero fuzz, zero offset, zero rejects**, and the `wifi-blob` device-tree node lands
in `arch/xtensa/boot/dts/arduino-nano-esp32.dts`.
