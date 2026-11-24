# libcoexist.a — what it needs, and what the ROM already has

Measured from `libcoexist.a` (esp32s3) and `esp32s3.rom.ld`. This is the
analysis behind the coex symbols in the `vmlinux.lds.S` hunk of
`0073-net-wireless-esp32-ng-run-the-wifi-blob-inside-linux.patch`.

## The archive

`components/esp_coex/lib/esp32s3/libcoexist.a`, 96224 bytes, ten members:

| member | what it holds |
|---|---|
| `coexist_api.o` | the public `coex_*` / `esp_coex_*` entry points |
| `coexist_core.o` | `coex_core_*` and the two data objects `coex_env`, `coex_pti_tab` |
| `coexist_param.o` | `coex_rom_osi_funcs_init`, `coex_rom_data_init`, `coex_rom_osi_funcs_deinit` |
| `coexist_arbit.o`, `coexist_dbg.o`, `coexist_hw.o`, `coexist_scheme.o`, `coexist_timer.o`, `external_coex.o`, `hal_external_coexist.o` | the rest |

It is a **thin wrapper over the mask ROM**. `coexist_param.o`'s
`coex_rom_data_init` is the whole story:

```
24: entry a1, 32
27: l32r a8, .bss.s_coex_rom_data_init
2a: l8ui a9, a8, 0
2d: bnez.n a9, 54            ; already run -> return
2f: l32r a10, coex_env       ; \
32: l32r a9,  coex_env_ptr   ;  |
35: s32i.n a10, a9, 0        ;  |
37: l32r a10, coex_schm_env  ;  |
3a: l32r a9,  coex_schm_env_ptr
3d: s32i.n a10, a9, 0
3f: l32r a10, coex_pti_tab
42: l32r a9,  coex_pti_tab_ptr
45: s32i.n a10, a9, 0
47: l32r a9,  g_coex_param_ptr
4a: l32r a10, g_coex_param
4d: s32i.n a10, a9, 0
4f: movi.n a9, 1
51: s8i a9, a8, 0            ; mark done
54: retw.n
```

It writes **our** `coex_env`, `coex_schm_env`, `coex_pti_tab` and
`g_coex_param` addresses into the ROM's four pointer slots
(`0x3fcef834`, `0x3fcef82c`, `0x3fcef830`, `0x3fcef820`). The ROM then
operates on our objects. Those four `_ptr` names are ROM data and are
bound in the `vmlinux.lds.S` hunk; the four objects are ours and come
from the archive.

## `coex_pre_init` — the call chain

```
coex_pre_init (coexist_api.o)
  coexist_printf("[%u] CO: version %s", ...)
  coex_core_pre_init (coexist_core.o)
    if (!coex_env[0]) {
        coex_env[0] = g_coa_funcs_p->[8](1)     ; create the coex semaphore
        if (!coex_env[0]) { log; return 0x101 }
    }
    if (coex_schm_init()) { log; g_coa_funcs_p->[12](coex_env[0]); return 0x101 }
    coex_core_lock()
    coex_rom_osi_funcs_init()                   ; coexist_param.o
      coexist_printf("[%u] CO: version %s", ...)
      coex_rom_data_init()                      ; the pointer handover above
      if (!coexist_funcs) {
          coexist_funcs = g_coa_funcs_p->[36](48)   ; 48-byte ROM osi table
          if (!coexist_funcs) { log; return 0x101 }
      }
      coexist_funcs[0]  = coex_core_pti_get
      coexist_funcs[4]  = coex_core_timer_idx_get
      coexist_funcs[8]  = coex_core_request
      coexist_funcs[12] = coex_core_release
      coexist_funcs[16] = coex_core_status_get
      coexist_funcs[20] = coex_core_event_duration_get
      coexist_funcs[24] = coex_core_ble_conn_dyn_prio_get
      coexist_funcs[28..60] = the remaining ROM entry points
    coex_core_unlock()
```

`coex_core_lock`/`coex_core_unlock` are the only two functions in the
archive with **no relocation records at all** — they take the ROM's own
lock through `coex_env`, which is why they need nothing bound.

`coex_core_init` is `movi.n a2, 0; retw.n` — a stub that always succeeds.

## The ROM symbols, and which of them are real

`esp32s3.rom.ld` declares the `rom_coexist` group. Every name the archive
references is in it:

| symbol | value | kind |
|---|---|---|
| `esp_coex_rom_version_get` | `0x40005b68` | function |
| `coex_core_ble_conn_dyn_prio_get` | `0x40005b8c` | function |
| `coex_env_ptr` | `0x3fcef834` | data |
| `coex_pti_tab_ptr` | `0x3fcef830` | data |
| `coex_schm_env_ptr` | `0x3fcef82c` | data |
| `coexist_funcs` | `0x3fcef828` | data |
| `g_coa_funcs_p` | `0x3fcef824` | data |
| `g_coex_param_ptr` | `0x3fcef820` | data |

`coex_core_timer_idx_get` is **commented out** in `esp32s3.rom.ld` itself
and is deliberately not bound. It is referenced only from
`coexist_param.o`'s `coex_rom_osi_funcs_init`, which is reached only from
`coex_core_pre_init`, which is reached only from `coex_pre_init`. So the
port must define it itself; see below.

## The two symbols that are not in the ROM

`coex_core_timer_idx_get` and `coexist_printf` are referenced but not
declared in `esp32s3.rom.ld`. Neither is in `libcoexist.a` for esp32s3
either — `coex_core_timer_idx_get` is defined by `coexist_core.o` (so it
resolves from the archive), and `coexist_printf` is defined by
`libesp_phy.a` (`network_adapter/build/esp-idf/esp_phy/libesp_phy.a`).

`libesp_phy.a` is **already linked** by the same `arch/xtensa/Makefile`
hunk that links the seven blob archives, so `coexist_printf` resolves
without adding anything.

## The data objects the archive defines

| symbol | member | size | contents |
|---|---|---|---|
| `coex_env` | `coexist_core.o` `.dram1.1` | 16 | zeros |
| `coex_pti_tab` | `coexist_core.o` `.dram1.2` | 39 | `0e0a0c0c 0e010101 010a0403 02010101 080a0808 0a080a08 080a0a0a 0a080808 08030303 0f0e08` |
| `g_coex_param` | `coexist_core.o` `.data.g_coex_param` | 12 | `a8610000 204e0000 88130000` = 25000, 20000, 5000 |

All three are ours; the ROM reaches them through the `_ptr` slots.

## What this means for the link

The eight ROM names go in `vmlinux.lds.S` as `symbol = 0xADDR;`
assignments, exactly as the port already does for `g_ic_ptr`,
`wDevCtrl_ptr`, `BasicOFDMSched_ptr` and the rest of the Wi-Fi ROM data.
The archive supplies the four objects. `coexist_printf` comes from
`libesp_phy.a`. Nothing else is needed for `coex_pre_init` and
`esp_coex_adapter_register` to link and run.

## What is NOT needed, and why that matters

`libesp_event.a` is linked in the same hunk for
`esp_event_loop_create_default()`. It drags in FreeRTOS
(`xQueueGenericCreate`, `xTaskCreatePinnedToCore`, `vTaskDelete`, …),
which this port does not have. That is a separate problem from coex and
is not solved by anything above.

---

# `libesp_event.a` — the part that is a problem, and the part that is not

`libesp_event.a` (131460 bytes, built only at
`network_adapter/build/esp-idf/esp_event/libesp_event.a` —
`components/esp_event/libesp_event.a` does not exist) has three members,
and they are not equally hostile:

| member | undefined symbols |
|---|---|
| `default_event_loop.c.obj` | `esp_event_handler_instance_register_with`, `esp_event_handler_instance_unregister_with`, `esp_event_handler_register_with`, `esp_event_handler_unregister_with`, `esp_event_isr_post_to`, `esp_event_loop_create`, `esp_event_loop_delete`, `esp_event_post_to`, `memcpy` |
| `esp_event.c.obj` | `__assert_func`, `calloc`, `esp_log_timestamp`, `esp_log_write`, `free`, `memcpy`, `memset`, `vQueueDelete`, `vTaskDelete`, `vTaskSuspend`, `xQueueCreateMutex`, `xQueueGenericCreate`, `xQueueGenericSend`, `xQueueGenericSendFromISR`, `xQueueGiveMutexRecursive`, `xQueueReceive`, `xQueueTakeMutexRecursive`, `xTaskCreatePinnedToCore`, `xTaskGetCurrentTaskHandle`, `xTaskGetTickCount` |
| `esp_event_private.c.obj` | `xQueueGenericSend` |

**`esp_event.c.obj` is the FreeRTOS one.** It is the member that needs
`xQueueGenericCreate`, `xTaskCreatePinnedToCore`, `vTaskDelete` and the
rest. `default_event_loop.c.obj` needs none of those — it needs only
`esp_event_loop_create`, which lives in `esp_event.c.obj`.

## What `esp_event_loop_create_default` actually does

```
00000000 <esp_event_loop_create_default>:
   0: entry a1, 64
   3: l32r a2, .literal.esp_event_loop_create_default   ; -> &s_default_loop
   6: movi a10, 0x103                                   ; ESP_ERR_INVALID_STATE
   9: l32i.n a8, a2, 0
   b: bnez.n a8, 24                                     ; already created -> return 0x103
   d: l32r a11, .literal+4                              ; -> .rodata = "sys_evt"
  10: movi.n a12, 20                                    ; sizeof(esp_event_loop_args_t)
  12: mov.n a10, a1                                     ; dst = stack
  14: callx8 memcpy                                     ; memcpy(args, &{.queue_size=32,
                                                        ;   .task_name="sys_evt",
                                                        ;   .task_priority=20,
                                                        ;   .task_stack_size=0x0b00,
                                                        ;   .task_core_id=0}, 20)
  1a: mov.n a11, a2                                     ; &s_default_loop
  1c: mov.n a10, a1                                     ; &args
  1e: callx8 esp_event_loop_create
  21: ...
  24: mov.n a2, a10
  26: retw.n
```

`.rodata` is `20 00 00 00 | 00 00 00 00 | 14 00 00 00 | 00 0b 00 00 | 00 00 00 00`
= `{ .queue_size = 32, .task_name = NULL (patched to "sys_evt"), .task_priority = 20, .task_stack_size = 0x0b00, .task_core_id = 0 }`.

So creating the default event loop means creating a FreeRTOS task named
`sys_evt` with 2816 bytes of stack, priority 20, on core 0. **That is
exactly the task s31's `__wrap_esp_event_loop_create` bumps to priority
23** (`firmware/radio/radio_stack.c:400-437`), for the reason its comment
gives: *"The default radio event consumer completes synchronous Wi-Fi
control. Its IDF priority 20 can starve behind controller/service work on
CPU0."*

## Does the port need it?

The blob does **not** post through the IDF event loop. It posts through
`osi->_event_post` — the OS interface at offset 180 of
`struct wifi_osi_funcs` — which `esp_blob.c` now points at
`esp_wifi_blob_event_post`. `wifi_event_post` in `libnet80211.a`'s
`ieee80211_api.o` reads `*g_osi_funcs_p`, loads the word at `+180` and
`callx8`s it with `WIFI_EVENT` as the base. Nothing in that path touches
`esp_event_loop_*`.

`esp_wifi_init_internal` does not call `esp_event_loop_create_default`
either — it was verified from the linked `vmlinux` that its call chain is
`wifi_osi_funcs_register` → `wifi_api_lock` → `wdev_funcs_init` →
`net80211_funcs_init` → `wifi_init_in_caller_task` → queue
`wifi_init_process`. No `esp_event` anywhere.

**Conclusion: `esp_event_loop_create_default()` is not required by this
port, and linking `libesp_event.a` for it pulls in `esp_event.c.obj` and
therefore FreeRTOS.** The two options are:

1. **Drop the call and the archive.** Nothing in the blob's init path
   needs the default loop. This removes the whole FreeRTOS dependency.
2. **Keep the call and supply FreeRTOS shims** for the 13 `x*`/`v*`
   symbols plus `__assert_func`, `calloc`, `free`, `esp_log_timestamp`,
   `esp_log_write`. That is a second, larger piece of work — a queue and
   task emulation — for a loop nothing posts to.

Option 1 is the one that matches what the blob actually does.

---

# `coexist_printf` — the one symbol that is neither in the archive nor the ROM

`libcoexist.a` calls `coexist_printf` from `coexist_api.o`,
`coexist_core.o`, `coexist_param.o` and `coexist_scheme.o`. It does not
define it, and `esp32s3.rom.ld` has no such name. First link of the
corrected patch failed on exactly this:

```
libcoexist.a(coexist_api.o):(.text.esp_coex_adapter_register+0x38):
    undefined reference to `coexist_printf'
```

## Where it is

`lib_printf.c.obj` in **`libesp_phy.a`** —
`network_adapter/build/esp-idf/esp_phy/libesp_phy.a`, 102868 bytes.

**This is not `libphy.a`.** Two different files, both named for the PHY:

| file | path | size | holds |
|---|---|---|---|
| `libphy.a` | `components/esp_phy/lib/esp32s3/` | 223754 | the radio blob's PHY: `register_chipv7_phy`, `g_phyFuns`, `phy_param`, `rf_init` |
| `libesp_phy.a` | `network_adapter/build/esp-idf/esp_phy/` | 102868 | the IDF component: `phy_init.c.obj`, `phy_override.c.obj`, `phy_common.c.obj`, `btbb_init.c.obj`, `lib_printf.c.obj` |

`libphy.a` is already linked (it is one of the seven blob archives).
`libesp_phy.a` was not.

## Why linking it is safe

`lib_printf.c.obj` defines the whole family of blob log shims:

```
T coexist_printf   T core_printf    T mesh_printf     T net80211_printf
T phy_printf       T pp_printf      T rtc_printf      T sc_printf
T wapi_printf      T wpa2_printf    T wpa_printf      T wps_printf
t lib_printf$constprop$0
```

`coexist_printf` is a variadic trampoline onto the local clone:

```
00000000 <coexist_printf>:
   0: entry a1, 80
   3: addi a12, a1, 16        ; va_list
   6: addi a11, a1, 48        ; register save area
   9: movi.n a13, 4
   b: s32i.n a3, a1, 20       ; spill a3..a7
  ...
  15: mov.n a10, a2           ; fmt
  17: s32i.n a12, a1, 4
  19: s32i.n a11, a1, 0
  1b: s32i a13, a1, 8
  1e: callx8 lib_printf$constprop$0
  24: mov.n a2, a10
  26: retw.n

00000000 <lib_printf$constprop$0>:
   0: entry a1, 128
   3: mov.n a12, a2           ; fmt
   5: mov.n a13, a3
   7: mov.n a14, a4
   9: mov.n a15, a5
   b: movi.n a11, 79          ; size = 79 -> 80-byte buffer on the stack
   d: mov.n a10, a1
   f: callx8 vsnprintf
  15: mov.n a2, a10
  17: retw.n
```

`lib_printf$constprop$0` is **local** (`t`), so it is reachable only from
inside `lib_printf.c.obj` and can never be the reason a member is pulled.

`lib_printf.c.obj`'s **only** undefined symbol is `vsnprintf`. The kernel
defines it at `lib/vsprintf.c:2860` and exports it at `:2990`
(`EXPORT_SYMBOL(vsnprintf)`), and `CONFIG_PRINTK=y`. So the member
resolves with nothing new.

The linker pulls one archive member at a time. Only `coexist_printf` is
referenced, so only `lib_printf.c.obj` is pulled. The other eleven
`*_printf` symbols are global and ride along inside that same member —
they cost their bytes and nothing else. The PHY members
(`phy_init.c.obj`, `phy_override.c.obj`, `phy_common.c.obj`,
`btbb_init.c.obj`) are separate members and are not pulled: nothing
references `esp_phy_enable`, `include_esp_phy_override`,
`phy_track_pll_init` or `esp_btbb_enable`.

## Note on the 80-byte buffer

`lib_printf$constprop$0` formats into a **79-character** stack buffer and
discards the result — it calls `vsnprintf` and never writes it anywhere.
The blob's `coexist_printf` is therefore a formatting-and-discard shim,
not a log sink. Nothing is printed by it in this port; the messages the
coex layer would have logged are dropped. That is worth knowing when
reading a board log that seems short on CO: lines.
