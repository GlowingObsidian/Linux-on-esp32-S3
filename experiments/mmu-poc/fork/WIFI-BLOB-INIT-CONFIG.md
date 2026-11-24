# `esp_wifi_init_internal` — what the blob actually wants

Measured from the archives, not from the IDF header. The IDF header
(`components/esp_wifi/include/esp_wifi.h:95-118`) describes the *public*
`wifi_init_config_t`, which is the same struct — but the blob is compiled
against its own copy, and the offsets below are what its code reads, so the
offsets are the authority.

## Why the current probe cannot work

`esp_blob.c` calls `esp_wifi_init_internal(NULL)`. Disassembly of
`libnet80211.a`'s `esp_wifi_init_internal`:

```
00000068 <esp_wifi_init_internal>:
  68:	entry	a1, 32
  6b:	mov.n	a4, a2
  6d:	bnez	a2, 7e            <- arg != NULL -> 7e
  70:	l32r	a10, 4
  73:	movi	a2, 0x102           <- ESP_ERR_INVALID_ARG
  76:	l32r	a8, 1c
  76:	R_XTENSA_ASM_EXPAND	puts
  79:	callx8	a8
  7c:	retw.n
  7e:	l32i.n	a10, a2, 0        <- arg->osi_funcs  (offset 0)
  80:	l32r	a8, 20
  80:	R_XTENSA_ASM_EXPAND	wifi_osi_funcs_register
  83:	callx8	a8
```

`NULL` returns `0x102` immediately. That is the `esp_wifi_init_internal: 258`
the board would print.

## `wifi_osi_funcs_register` — what it checks

```
00000000 <wifi_osi_funcs_register>:
   0:	entry	a1, 32
   3:	beqz.n	a2, 2a
   5:	l32i.n	a8, a2, 0        <- osi->version
   7:	1c0c                	movi.n	a12, 1
   9:	3c0c                	movi.n	a12, 3
   b:	0c4d                	mov.n	a4, a12
   d:	83c280               	moveqz	a12, a2, a8   <- version == 0 ? 1 : 3
  10:	fffb81               	l32r	a8, ...
  10:	R_XTENSA_ASM_EXPAND	puts
  13:	0008e0               	callx8	a8
  16:	02a132               	movi	a3, 0x102
  19:	0e0c                	movi.n	a14, 0
  1b:	fffb81               	l32r	a8, ...
  1b:	R_XTENSA_ASM_EXPAND	puts
  1e:	0008e0               	callx8	a8
  21:	02a122               	movi	a2, 0x102
  24:	f01d                	retw.n
  26:	00
  ...
```

It reads `osi->version` at offset 0 and rejects with `0x102` when it is
wrong. `g_osi_funcs._version` is set to `ESP_WIFI_OS_ADAPTER_VERSION`
`0x00000008` in `esp_blob.c`, which matches IDF
(`components/esp_wifi/include/esp_private/wifi_os_adapter.h`).

## `wifi_menuconfig_init(config)` — the real gate

Called from inside `esp_wifi_init_internal` after the OS interface is
registered. Disassembly of `libnet80211.a`'s `ieee80211_ioctl.o`:

```
00000044 <wifi_menuconfig_init>:
  44:	entry	a1, 48
  47:	l32r	a3, ...            -> g_wifi_menuconfig
  4a:	movi	a12, 88
  4c:	movi	a11, 0
  4e:	mov.n	a10, a3
  50:	R_XTENSA_ASM_EXPAND	memset
  53:	callx8	a8               memset(g_wifi_menuconfig, 0, 88)

  58:	beqz.n	a2, 6c            <- config == NULL -> log + 0x102
  6c:	l32i	a14, a2, 200       <- config->magic
  6f:	l32r	a3, ...            (1f2f3f4f)
  72:	beq	a14, a3, 89         <- must equal 0x1F2F3F4F

  89:	l32i	a9, a2, 4          <- config->wpa_crypto_funcs.size
  8b:	movi	a3, 116
  8e:	l32i	a10, a2, 8         <- config->wpa_crypto_funcs.extra
  90:	bne	a9, a3, 96         <- size must be 116
  93:	beqi	a10, 1, b3         <- extra must be 1
```

So the struct is validated, not trusted. `magic` sits at **offset 200**,
`wpa_crypto_funcs` at **offset 4** with a `size` field of **116** and an
`extra` field of `1`.

## The struct, as the blob reads it

| offset | field | read at |
|---|---|---|
| 0 | `wifi_osi_funcs_t *osi_funcs` | `esp_wifi_init_internal+0x7e` |
| 4 | `wpa_crypto_funcs_t wpa_crypto_funcs` (116 bytes) | `wifi_menuconfig_init+0x89` |
| 120 | `int static_rx_buf_num` | `+0xb3` (`2..32`) |
| 124 | `int dynamic_rx_buf_num` | `+0xc3` (`<= 0x400`) |
| 128 | `int tx_buf_type` | `+0xd1` (`0` or `1`) |
| 132 | `int static_tx_buf_num` | `+0xd6` (`1..64` when type 0) |
| 136 | `int dynamic_tx_buf_num` | `+0xee` (`1..128` when type 1) |
| 140 | `int cache_tx_buf_num` | `+0x159` |
| 144 | `int csi_enable` | `+0x161` |
| 148 | `int ampdu_rx_enable` | `+0x120` |
| 152 | `int ampdu_tx_enable` | `+0x168` |
| 156 | `int amsdu_tx_enable` | `+0x16f` |
| 160 | `int nvs_enable` | `+0x15c` |
| 164 | `int nano_enable` | `+0x17e` |
| 168 | `int rx_ba_win` | `+0x125`, `+0x144` (`2..32`) |
| 172 | `int wifi_task_core_id` | `+0x138` (`< 2`) |
| 176 | `int beacon_max_len` | `+0x181` |
| 180 | `int mgmt_sbuf_num` | `+0x10c` (`6..32`) |
| 184 | `uint64_t feature_caps` | `+0x18e`, `+0x19b` |
| 192 | `bool sta_disconnected_pm` | `+0x191` (`l8ui`) |
| 196 | `int espnow_max_encrypt_num` | `+0x1a1` |
| 200 | `int magic` | `+0x6c` |

Total 204 bytes. `wpa_crypto_funcs` is 116 bytes: `size` at +0, `extra` at
+4, then the function-pointer table.

## Where `wpa_crypto_funcs` comes from — and why the probe must NOT fill it

`wpa_crypto_funcs` is a **B** symbol, 116 bytes, at `0x3d842770` in the
linked kernel. It is defined only by `libnet80211.a`'s `ieee80211_crypto.o`,
and in the image it is **all zeros**.

`wpa_crypto_funcs_init` (offset `0x14` in `libnet80211.a`'s
`ieee80211_ioctl.o`, linked at `0x42383f18`) copies **from** that BSS symbol
**into its argument**:

```
42383f18 <wpa_crypto_funcs_init>:
42383f18:	entry	a1, 32
42383f1b:	beqz.n	a2, 42383f2e        <- dst == NULL -> 0x102
42383f1d:	l32r	a10, ... (3d842770 <wpa_crypto_funcs>)   <- SOURCE (BSS)
42383f20:	mov	a11, a2               <- DEST = argument
42383f22:	movi	a12, 116
42383f25:	call8	423aab30 <__memcpy>
42383f28:	movi.n	a2, 0
42383f2a:	retw.n
42383f2e:	movi	a12, 1 ; ... wifi_log ; movi a2, 0x102 ; retw.n
```

**So it is a destination-filler, not a source-provider.** The blob calls it
itself, on the config we hand in, late in `esp_wifi_init_internal`:

```
esp_wifi_init(&cfg)                 -> esp_wifi_init_internal (0x423766d4)
  wifi_osi_funcs_register(cfg->osi_funcs)   version==8, magic==0xDEADBEAF
  wifi_api_lock
  wdev_funcs_init(cfg->feature_caps)        l32i a10, a4, 184 / l32i a11, a4, 188
  net80211_funcs_init
  wifi_init_in_caller_task (0x42376118)
    wifi_menuconfig_init(cfg)       magic==0x1f2f3f4f, crypto.size==116, crypto.extra==1
    misc_nvs_init
    ic_create_wifi_task             (libpp.a, 0x42397248)
    ieee80211_ioctl_init            (0x423864d0)
  queue wifi_init_process (0x42384090) to the wifi task
    wifi_nvs_init   (0x42387d24)
    wifi_lmac_init
    wifi_hmac_init  (0x42383f68) -> ieee80211_ifattach (0x42379b50)
    wifi_crypto_init (0x42383f48) -> wpa_crypto_funcs_init(&cfg->wpa_crypto_funcs)
```

`wifi_crypto_init` is `addi a10, a2, 4` then `call8 42383f18`, i.e. it
passes `&cfg->wpa_crypto_funcs` — the field at offset 4 that
`wifi_menuconfig_init` already size-checked.

**Calling it early from the probe is what produced `esp_wifi_init_internal:
258` on the board.** It copied 116 zero bytes into the config, so
`crypto.size` read 0, and `wifi_menuconfig_init` at `0x42383df8` failed
`bne a9, a3` (size must be 116) and returned `0x102` = 258. The board log
showed the smoking gun directly: `init config: osi=1b29ac7a crypto=0/0
magic=1f2f3f4f`.

The probe therefore leaves the 116 pointers zeroed and sets only the two
words the check reads.

### Why not `g_wifi_default_wpa_crypto_funcs`

IDF's own `esp_wifi.h:174` declares
`extern const wpa_crypto_funcs_t g_wifi_default_wpa_crypto_funcs;` and
`WIFI_INIT_CONFIG_DEFAULT()` (`:216`) uses it. That symbol is **absent from
all seven blob archives and from vmlinux**. This build links the `-remote`
OS adapter variant — `g_wifi_osi_funcs_md5` at `3d82675c` is `0x4242dc00`,
whose strings are `"fb76ce5"`, `"fddc5e5-remote"`, `"fddc5e5"`,
`"ieee80211_crypto.c"`. In that variant the table is BSS and the blob fills
it, so zeroed + size/extra is the correct input, not a deviation.

## Where `feature_caps` comes from

`config_get_wifi_feature_capabilities` is a **T** symbol at `0x4239ffcc`,
defined by `libpp.a`'s `wdev.o`:

```
00000004 <config_get_wifi_feature_capabilities>:
   4:	entry	a1, 32
   7:	l32r	a8, ...            -> g_wifi_menuconfig
   a:	l32i	a2, a8, 64        <- low 32 bits
   d:	l32i	a3, a8, 68        <- high 32 bits
  10:	retw.n
```

It returns a `uint64_t` in `a2:a3` — the two words at `g_wifi_menuconfig`
offsets 64 and 68. That is **inside the struct this same function fills**
(`feature_caps` at offset 184 maps to `g_wifi_menuconfig` + 64, per
`wifi_menuconfig_init+0x198`/`+0x19b`).

So the order matters and the header's `WIFI_INIT_CONFIG_DEFAULT()` already
has it right: `feature_caps` is a field of the config the caller passes in,
and the blob reads it back out of its own copy. The probe supplies it
directly rather than calling the getter, which would read the zeroed
`g_wifi_menuconfig` before `wifi_menuconfig_init` has run.

## The fix

In `esp_blob.c`'s probe, replace `esp_wifi_init_internal(NULL)` with a
populated config. Note `wpa_crypto_funcs` is **not** filled here — see
"Where `wpa_crypto_funcs` comes from" above: the blob fills it itself, and
calling the helper early is what produced the board's `258`:

```c
struct wifi_blob_init_config cfg;

memset(&cfg, 0, sizeof(cfg));
cfg.osi_funcs = &g_osi_funcs;
cfg.wpa_crypto_funcs.size = WPA_CRYPTO_FUNCS_SIZE;	/* 116 */
cfg.wpa_crypto_funcs.extra = 1;
cfg.static_rx_buf_num = 10;
cfg.dynamic_rx_buf_num = 32;
cfg.tx_buf_type = 1;
cfg.dynamic_tx_buf_num = 32;
cfg.ampdu_rx_enable = 1;
cfg.ampdu_tx_enable = 1;
cfg.rx_ba_win = 6;
cfg.nvs_enable = 0;
cfg.wifi_task_core_id = 0;
cfg.mgmt_sbuf_num = 6;
cfg.feature_caps = 0;
cfg.magic = 0x1F2F3F4F;

ret = esp_wifi_init_internal(&cfg);
```

The buffer numbers are IDF's defaults for a station-only build
(`CONFIG_ESP_WIFI_STATIC_RX_BUFFER_NUM` 10, `DYNAMIC_RX_BUFFER_NUM` 32,
`DYNAMIC_TX_BUFFER_NUM` 32, `RX_BA_WIN` 6, `MGMT_SBUF_NUM` 6). They are
inside the ranges `wifi_menuconfig_init` validates against, which is the
only thing that matters for the first run — tune them once the blob is up.

`feature_caps = 0` means no optional features. That is the conservative
choice and it is what a station-only build with no SAE/OWE extras reports.

## The coexistence calls, and the event loop that is not one

`esp_wifi_init_internal` is not the first thing IDF does. Its own
`wifi_init.c` calls `esp_coex_adapter_register()` and `coex_pre_init()`
from a SECONDARY system-init hook *before* `esp_wifi_init()`, and
`esp_event_loop_create_default()` before that. This port has no system-init
hook, so `esp_blob.c`'s probe makes the calls itself.

**The two coex calls are required and are made.** s31's
`firmware/radio/radio_stack.c` says why in its own comment:

> ESP-IDF normally performs these two calls together from its SECONDARY
> system-init hook. Registering the adapter alone leaves the coexistence
> lock/environment uninitialised; esp_wifi_init() then faults as soon as
> it registers its scheduler callbacks.

They come from `libcoexist.a`, which is a thin wrapper over the ESP32-S3
mask ROM — the eight ROM names it needs are bound in the `vmlinux.lds.S`
hunk. `COEX-LINK-ANALYSIS.md` has the call chain and the disassembly.

**`esp_event_loop_create_default()` is deliberately NOT called.** It looks
like part of the sequence and is not:

- **Not needed.** The blob posts every Wi-Fi event through
  `osi->_event_post` — offset 180 of `struct wifi_osi_funcs`, which the
  probe points at `esp_wifi_blob_event_post`. Nothing in
  `esp_wifi_init_internal`'s call chain touches `esp_event_loop_*`, and
  nothing the blob does afterwards does either.
- **Cannot be linked.** It lives in `libesp_event.a`'s
  `default_event_loop.c.obj`, which calls `esp_event_loop_create()`, which
  lives in `esp_event.c.obj`, which needs FreeRTOS —
  `xQueueGenericCreate`, `xTaskCreatePinnedToCore`, `vTaskDelete`,
  `xQueueReceive` and nine more. This port has no FreeRTOS. s31 can call it
  because s31 has a 1919-line FreeRTOS emulation underneath
  (`s31_rtos_queue.c`, `s31_rtos_core.c`, `s31_rtos_event.c`).

`libesp_event.a` was linked for one build and failed the link on exactly
those symbols. It is now not linked at all.
