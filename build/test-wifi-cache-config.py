#!/usr/bin/env python3
"""Exercise actual init assignments against the pinned IDF capability contract.

Supplying a cache size without its enable capability must fail this gate.
"""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

source = Path(sys.argv[1]).read_text()
header = Path(sys.argv[2]).read_text()
match = re.search(r'^#define CONFIG_FEATURE_CACHE_TX_BUF_BIT\s+\(1\s*<<\s*(\d+)\)', header, re.M)
assert match, 'Pinned IDF cache capability declaration missing'
bit = int(match[1])
start = source.index('struct wifi_blob_crypto_funcs {')
end = source.index('} __packed;', source.index('struct wifi_blob_init_config {')) + len('} __packed;')
structures = source[start:end]
start = source.index('\tmemset(&cfg, 0, sizeof(cfg));')
end = source.index('\n', source.index('\tcfg.magic = WIFI_INIT_CONFIG_MAGIC;', start))
assignments = source[start:end]
cap = re.search(r'^#define BLOB_WIFI_FEATURE_CACHE_TX_BUF\s+.*$', source, re.M)
code = '''#include <stdint.h>
#include <string.h>
#include <assert.h>
#include <stdio.h>
typedef uint8_t u8; typedef uint32_t u32; typedef uint64_t u64;
#define __packed __attribute__((packed))
#define BIT_ULL(n) (1ULL << (n))
#define WPA_CRYPTO_FUNCS_SIZE 116
#define WIFI_INIT_CONFIG_MAGIC 0x1F2F3F4F
static int g_osi_funcs;
'''
code += (cap[0] + '\n') if cap else ''
iperf = len(sys.argv)>3 and sys.argv[3]=='iperf'
code += structures
if iperf: code += '\n#define CONFIG_ESP32_WIFI_IRAM 1\n'
code += '\nint main(void) { struct wifi_blob_init_config cfg;\n' + assignments
code += f'''
    /* These are independent conditions in the pinned IDF: a size alone
     * cannot activate ieee80211_alloc_tx_buf's cached-buffer path. */
    int enabled = !!(cfg.feature_caps & (1ULL << {bit}));
    assert(cfg.tx_buf_type == 0 && cfg.static_tx_buf_num == {24 if iperf else 16});
    assert(cfg.cache_tx_buf_num == 32 && enabled);
    assert(cfg.feature_caps == (1ULL << {bit}));
    assert(cfg.dynamic_tx_buf_num == 0);
    assert(cfg.static_rx_buf_num == {16 if iperf else 10} && cfg.dynamic_rx_buf_num == {64 if iperf else 32});
    assert(cfg.ampdu_rx_enable == 1 && cfg.ampdu_tx_enable == 1);
    assert(cfg.rx_ba_win == {32 if iperf else 6});
    puts("PASS: actual init enables the pinned IDF cache capability, not only its size; DMA/RX/AMPDU settings preserved");
}}
'''
with tempfile.TemporaryDirectory() as temp:
    src = Path(temp) / 'cache.c'
    exe = Path(temp) / 'cache'
    src.write_text(code)
    subprocess.run(['cc', '-std=gnu11', '-fsanitize=address,undefined', str(src), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
