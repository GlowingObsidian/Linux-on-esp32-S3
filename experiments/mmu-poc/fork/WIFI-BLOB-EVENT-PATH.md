# The blob's event path, as the firmware wrote it

`esp_wifi_blob_event_post()` is the OS interface's `_event_post`. The blob
raises Wi-Fi events through it and the driver's upper half is what turns
them into cfg80211 state -- `process_cmd_event()` is what makes an
association visible to `wpa_supplicant`.

In the IDF build there is a firmware in between: core 0's
`network_adapter/main/cmd.c` runs `wifi_event_handler()`, which translates
each IDF event into the driver's wire format and calls
`send_command_event()`. In this port there is no firmware in between, so
`esp_blob.c` does that translation itself.

**The encoding is the firmware's, not a new one**, because the host half
already parses it. Measured from
`esp-hosted/esp_hosted_ng/esp/esp_driver/network_adapter/main/cmd.c`.

## The event base

```
esp_event_base.h:19   typedef const char*  esp_event_base_t;
esp_event_base.h:15   #define ESP_EVENT_DECLARE_BASE(id) extern esp_event_base_t const id
esp_event_base.h:16   #define ESP_EVENT_DEFINE_BASE(id)  esp_event_base_t const id = #id
wifi_init.c:34        ESP_EVENT_DEFINE_BASE(WIFI_EVENT);
```

So `WIFI_EVENT` is the string `"WIFI_EVENT"`, and comparing with `strcmp`
is correct. It is defined once, in `esp_blob_support.c`, because the blob's
`ieee80211_api.o` references it by name; `esp_blob.c` declares it extern.

## The IDF ids

`esp_wifi_types.h:844-873`, first member 0:

| id | name |
|---|---|
| 0 | `WIFI_EVENT_WIFI_READY` |
| 1 | `WIFI_EVENT_SCAN_DONE` |
| 2 | `WIFI_EVENT_STA_START` |
| 3 | `WIFI_EVENT_STA_STOP` |
| 4 | `WIFI_EVENT_STA_CONNECTED` |
| 5 | `WIFI_EVENT_STA_DISCONNECTED` |

## The translation, from `cmd.c`'s `wifi_event_handler()`

```
case WIFI_EVENT_STA_START:        sta_init_flag = 1;            /* no wire event */
case WIFI_EVENT_STA_CONNECTED:    station_connected = 1;
                                  esp_wifi_set_tx_done_cb(...);
                                  esp_wifi_internal_reg_rxcb(ESP_IF_WIFI_STA,
                                                             wlan_sta_rx_callback);
                                                                /* no wire event */
case WIFI_EVENT_STA_DISCONNECTED: handle_sta_disconnected_event(event_data);
                                                                /* EVENT_STA_DISCONNECT */
case WIFI_EVENT_SCAN_DONE:        handle_scan_event();          /* EVENT_SCAN_RESULT, len 0 */
default:                          log
```

Two things follow, and both are load-bearing:

1. **`WIFI_EVENT_STA_CONNECTED` produces no wire event.** The firmware only
   records state and re-registers the rx callback. What carries the
   association to the host is `EVENT_ASSOC_RX`, built by `sta_rx_assoc()`
   from the *association response frame* -- a management frame, not an
   event. So the connect path in this port runs through
   `blob_rx_cb`/`esp_wifi_internal_reg_rxcb`, not through `_event_post`.
2. **`EVENT_SCAN_RESULT` is used for two different things.** The bare
   header with `len = 0` is "scan done"; a full `struct scan_event` is one
   AP's result, built by `sta_rx_probe()` from a probe response. The host
   half distinguishes them by `len`.

## The wire structs

`struct event_header` (adapter.h:245) is 4 bytes, `__packed`:

```c
struct event_header {
	uint8_t    event_code;
	uint8_t    status;
	uint16_t   len;
} __packed;
```

Event codes (adapter.h:128-134): `EVENT_SCAN_RESULT = 1`,
`EVENT_STA_CONNECT`, `EVENT_STA_DISCONNECT`, `EVENT_AUTH_RX`,
`EVENT_ASSOC_RX`.

`handle_sta_disconnected_event()` fills:

```c
event->header.event_code = EVENT_STA_DISCONNECT;
event->header.len  = htole16(payload_len - sizeof(struct event_header));
event->header.status = 0;
memcpy(event->ssid,  disconnected->ssid,  disconnected->ssid_len);
memcpy(event->bssid, disconnected->bssid, MAC_ADDR_LEN);
event->reason = disconnected->reason;
```

The IDF source struct it reads (`esp_wifi_types.h:920`):

```c
typedef struct {
	uint8_t ssid[32];
	uint8_t ssid_len;
	uint8_t bssid[6];
	uint8_t reason;
	int8_t  rssi;
} wifi_event_sta_disconnected_t;
```

Note the firmware copies `ssid_len` bytes, not 32 -- the rest of the
32-byte field stays zero from `prepare_event()`'s `memset`.

## The framing

`prepare_event(if_type, &buf, event_len)` sets `if_type = ESP_STA_IF`,
`if_num = 0`, `pkt_type = PACKET_TYPE_EVENT`, allocates `event_len` and
zeroes it. Then `esp_shmem_write()` wraps it:

```c
*header = (struct esp_payload_header){
	.if_type   = buf_handle->if_type,
	.if_num    = buf_handle->if_num,
	.flags     = buf_handle->flag,
	.packet_type = buf_handle->pkt_type,
	.len       = htole16(buf_handle->payload_len),
	.offset    = htole16(sizeof(struct esp_payload_header)),
};
```

So the wire form is `esp_payload_header` then the event body -- exactly
what `blob_rx_cb()` already builds for data, with
`packet_type = PACKET_TYPE_EVENT` instead of `PACKET_TYPE_DATA`.

## The host side, confirmed

`main.c:706` `skb_pull(skb, offset)` runs *before* the dispatch, so by the
time `process_cmd_event()` is called `skb->data` is the event body and
`(struct event_header *)skb->data` is correct. `priv` is resolved from the
payload header earlier in the same function, so the event needs
`if_type = ESP_STA_IF` and `if_num = 0` to find the station interface.

## What this port does

`esp_wifi_blob_event_post()` in `esp_blob.c`:

| IDF event | action |
|---|---|
| `WIFI_EVENT_STA_START` (2) | log |
| `WIFI_EVENT_STA_STOP` (3) | log |
| `WIFI_EVENT_STA_CONNECTED` (4) | log ssid/bssid/channel, no wire event |
| `WIFI_EVENT_STA_DISCONNECTED` (5) | build `blob_disconnect_event`, post |
| `WIFI_EVENT_SCAN_DONE` (1) | build bare `event_header`, post |
| anything else | log |

`blob_post_event()` builds the skb, queues it on `rx_q[PRIO_Q_HIGH]` and
calls `esp_process_new_packet_intr()` -- the same path a received frame
takes.
