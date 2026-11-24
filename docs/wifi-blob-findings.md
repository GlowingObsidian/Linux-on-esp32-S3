# Wi-Fi blob inside Linux — findings

## The undefined-symbol problem, solved

The final `vmlinux` link failed with 270 undefined references. The count was
misleading: it is per-object-file, and the same symbol repeats across many
objects. The real number is **21 distinct names**.

Method: `nm` the six archives (`libnet80211.a libpp.a libcore.a libwapi.a
libespnow.a libphy.a`), collect every `U`, subtract every defined, subtract
every name in `esp32s3.rom*.ld`. Result: 21.

Of the 21:

- **13 mesh-only** — defined only in `libmesh.a`, which is deliberately not
  linked (972644 bytes, drags ~200 more `esp_mesh_*`/`esp_event_*` refs).
  Referenced by `ieee80211_ioctl.o` and `ieee80211_mesh_quick.o`, both of
  which the station path pulls in, so the symbols must exist. Stubbed in
  `esp_blob_support.c` with return values read off the real implementations.
- **5 already supplied** — `WIFI_EVENT`, `ets_delay_us`, `hexstr2bin`,
  `puts`, `sprintf` (the last binds to the kernel's own `lib/vsprintf.c`).
- **3 already supplied** — `phy_enter_critical`, `phy_exit_critical`,
  `phy_printf`, in `esp_blob_phy.c`.

Everything else the blob reaches for is a **ROM address**. 81 of them, now
bound by name in `arch/xtensa/kernel/vmlinux.lds.S`:

(78 came out of the undefined-set computation, which ran against a log where
`phy_get_romfuncs`, `rom_phy_param_addr` and `rom_rx_gain_force` were already
bound. Those three are the original bindings and must stay: 78 + 3 = 81.)

| region | count | what |
|---|---|---|
| `0x4000xxxx` | 24 | ROM text: `strncmp`, `strncpy`, `strcpy`, `printf`, `roundup2`, `phy_get_romfuncs`, `rom_phy_param_addr`, `rom_rx_gain_force`, `rom_read_hw_noisefloor`, `ets_backup_dma_copy`, `esp_pp_rom_version_get`, `esp_net80211_rom_version_get`, `__divdi3`, `__umoddi3`, `__udivdi3`, `__moddi3`, `__floatsidf`, `__floatundisf`, `__subdf3`, `__divsf3`, `__divdf3`, `__fixdfsi`, `__extendsfdf2`, `__popcountsi2` |
| `0x3fcef8xx` | 54 | ROM data: the Wi-Fi driver's globals, internal SRAM 1 Block 8 |
| `0x3ff1ee5x` | 3 | `our_instances_ptr`, `our_controls_ptr`, `g_wdev_last_desc_reset_ptr` |

## Why the libc names must bind to ROM, not the kernel

The kernel has **no out-of-line** `strncmp`, `strncpy`, `strcpy` or `printf`:

- `arch/xtensa/include/asm/string.h:18` `#define __HAVE_ARCH_STRCPY`
- `arch/xtensa/include/asm/string.h:59` `#define __HAVE_ARCH_STRNCMP`

Both are `static inline` with hand-written Xtensa assembly, so `lib/string.c`
compiles them out and `lib/lib.a` carries no such symbol. `printf` is not in
the kernel at all. Verified: `nm vmlinux.o` shows `U strncmp`, `U strncpy`,
`U strcpy`, `U printf`.

Same for the libgcc helpers (`__divdi3` and friends): the kernel's `libs-y`
has no `libgcc.a`, and `arch/xtensa/lib/lib.a` only carries the `si` variants
(`__divsi3`, `__udivsi3`, `__modsi3`, `__umodsi3`), not the `di` ones.

## The mesh stubs

Signatures read off `objdump -d` of `libmesh.a(mesh_quick.o)` and
`(mesh_parent.o)`. All return `int`; `0x102` is `ESP_ERR_INVALID_ARG`.

| symbol | NULL/range behaviour | real return |
|---|---|---|
| `mesh_set_ie_crypto_config(ie, conf)` | `!ie` → 0x102 | 0 |
| `mesh_set_rssi_threshold(conf)` | `!conf` → 0x102 | 0 |
| `mesh_set_parent_monitor_config(conf)` | `!conf` → 0x102 | 0 |
| `mesh_set_parent_candidate(cand, conf)` | `!cand \|\| !conf` → 0x102 | 0 |
| `mesh_get_rssi_threshold(conf)` | `!conf` → 0x102 | 0 |
| `mesh_get_parent_monitor_config(conf)` | `!conf` → 0x102 | 0 |
| `mesh_get_parent_candidate(conf)` | `!conf` → 0x102 | 0 |
| `mesh_clear_parent_candidate()` | — | 0 |
| `ieee80211_vnd_mesh_roots_set(idx, arg, roots)` | `idx >= 5 \|\| !roots` → 0x102 | 0 |
| `ieee80211_vnd_mesh_roots_get(idx, roots, count)` | `idx >= 4` → -1 | -1 |
| `ieee80211_vnd_mesh_quick_set(conf)` | `!conf` → 0x102 | 0 |
| `ieee80211_vnd_mesh_quick_get(conf)` | `!conf` → 0x102 | 0 |
| `ieee80211_init_mesh_assoc_ie(ie, arg, len)` | `!ie \|\| !arg` → 1 | 1 |

The stubs return the same values the real code returns for the same inputs,
so a station-mode caller reaching one sees the pre-existing failure mode
rather than a new one. A *valid* call also refuses, where the real code
would have worked — but nothing in the station path makes a valid call.

## Patch mechanics (unchanged from before, restated)

Buildroot applies patches with:

```
patch -F0 -g0 -p1 --no-backup-if-mismatch -d "${builddir}" -t -N
```

`-F0` is no fuzz, `-g0` is no whitespace-fuzzy context. A hunk whose added
lines sit at the very end of the hunk body with **no trailing context line**
is rejected at `-F0`. Every hunk in patch 0073 is therefore generated with
`diff -u` from a real tree, which always emits trailing context.

New-file blocks must end with a newline; `diff -u` emits
`\ No newline at end of file` when they do not, and stripping that marker
produces `patch: **** malformed patch`.

## The patch, as it now stands

`new-files/board/espressif/esp32s3/patches/linux/0073-net-wireless-esp32-ng-run-the-wifi-blob-inside-linux.patch`
— 83054 bytes, preamble restored, 8 blocks:

| file | change |
|---|---|
| `arch/xtensa/Makefile` | 6 `libs-$(CONFIG_ESP32_WIFI_BLOB)` lines |
| `arch/xtensa/kernel/vmlinux.lds.S` | 81 ROM address bindings |
| `arch/xtensa/boot/dts/arduino-nano-esp32.dts` | `wifi_blob` node under `&{/}` |
| `drivers/.../esp32-ng/Kconfig` | `ESP32_WIFI_BLOB` |
| `drivers/.../esp32-ng/Makefile` | 3 objects |
| `drivers/.../esp32-ng/esp_blob.c` | new, 1670 lines |
| `drivers/.../esp32-ng/esp_blob_support.c` | new, 259 lines (127 + 132 mesh stubs) |
| `drivers/.../esp32-ng/esp_blob_phy.c` | new, 381 lines |

Verified with Buildroot's exact invocation: exit 0, zero rejects, zero `.orig`.
