#!/usr/bin/env python3
"""Run the actual SPI clock/PIO callbacks against fake registers, with ASan/UBSan.

This checks software bounds and error propagation; it does not test SD wiring.
"""
from pathlib import Path
import subprocess
import sys
import tempfile

repo = Path(__file__).resolve().parent.parent
source = Path(sys.argv[1]) if len(sys.argv) > 1 else repo / 'experiments/mmu-poc/out/linux-fork/drivers/spi/spi-esp32.c'
s = source.read_text()
def function(name):
    at = s.index(name + '(')
    start = s.rfind('\n', 0, at) + 1
    if s[max(0, start-12):start].endswith('static int\n'):
        start -= len('static int\n')
    brace = s.index('{', at)
    depth, end = 1, brace + 1
    while depth:
        depth += (s[end] == '{') - (s[end] == '}')
        end += 1
    return s[start:end]

constants = '\n'.join(line for line in s.splitlines() if line.startswith('#define SPI_') or line.startswith('#define ESP32_SPI_FIFO_SIZE'))
shim = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
typedef uint32_t u32;
typedef uint8_t u8;
#define BIT(n) (1U << (n))
#define GENMASK(h,l) ((~0U >> (31-(h))) & (~0U << (l)))
#define FIELD_PREP(m,v) (((u32)(v) << __builtin_ctz(m)) & (m))
#define FIELD_GET(m,v) (((v) & (m)) >> __builtin_ctz(m))
#define DIV_ROUND_UP(n,d) (((n) + (d)-1)/(d))
#define max_t(t,a,b) ((t)(a) > (t)(b) ? (t)(a) : (t)(b))
#define min(a,b) ((a)<(b)?(a):(b))
#define WARN_ON(x) ((void)(x))
#define WARN_ON_ONCE(x) ((void)(x))
#define pr_warn(...) ((void)0)
#define SPI_CPHA BIT(0)
#define SPI_CPOL BIT(1)
#define SPI_CS_HIGH BIT(2)
#define SPI_LSB_FIRST BIT(3)
#define SPI_NBITS_SINGLE 1
#define HZ 10
#define time_after(a,b) ((long)((b)-(a)) < 0)
#define cpu_relax() ((void)0)
static unsigned long jiffies;
static unsigned commands, fail_after;
static bool stuck, missing_done;
struct esp32_spi { u8 *regs; u32 *clk; u32 speed_hz; void *gdma; void *dev; bool dma_failed; };
struct spi_controller { struct esp32_spi *data; };
struct spi_device { struct spi_controller *controller; u32 mode; bool gpio; };
struct spi_message { struct spi_device *spi; };
struct spi_transfer { const void *tx_buf; void *rx_buf; unsigned len, speed_hz, bits_per_word, rx_nbits, tx_nbits, effective_speed_hz; };
#define spi_controller_get_devdata(h) ((h)->data)
#define spi_get_chipselect(d,i) 0
#define spi_get_csgpiod(d,i) ((d)->gpio)
#define clk_get_rate(c) (*(c))
#define memcpy_toio memcpy
#define memcpy_fromio memcpy
'''
registers = r'''
static u32 esp32_spi_read(struct esp32_spi *spi, int off) {
    jiffies++;
    return *(u32 *)(spi->regs+off);
}
static void esp32_spi_write(struct esp32_spi *spi, int off, u32 value) {
    if (off == SPI_DMA_INT_CLR_REG) {
        *(u32 *)(spi->regs+SPI_DMA_INT_RAW_REG) &= ~value;
        return;
    }
    *(u32 *)(spi->regs+off)=value;
    if (off == SPI_CMD_REG) {
        if (stuck || (fail_after && commands >= fail_after)) return;
        *(u32 *)(spi->regs+off)=0;
        if (value == SPI_USR) {
            commands++;
            if (!missing_done) *(u32 *)(spi->regs+SPI_DMA_INT_RAW_REG) |= SPI_TRANS_DONE_INT;
        }
    }
}
'''
shim += '''
static int esp32_spi_dma_transfer(struct esp32_spi *s, struct spi_device *d, struct spi_transfer *t) { (void)s; (void)d; (void)t; return -ENOSYS; }
''' if 'static int esp32_spi_dma_transfer(' in s else ''
callbacks = '\n'.join(function(n) for n in (
    'esp32_spi_prepare_message', 'esp32_spi_set_cs', 'esp32_spi_wait_bit',
    'esp32_spi_clock', 'esp32_spi_prepare_transfer', 'esp32_spi_start_transfer',
    'esp32_spi_wait', 'esp32_spi_transfer_one'))
tests = r'''
int main(void) {
    u32 reg, actual;
    assert(esp32_spi_clock(80000000,400000,&reg,&actual)==0 && actual==400000);
    assert(esp32_spi_clock(80000000,20000000,&reg,&actual)==0 && actual==20000000);
    assert(esp32_spi_clock(80000000,0,&reg,&actual)==-EINVAL);
    assert(esp32_spi_clock(80000000,10000,&reg,&actual)==-EINVAL);
    for (u32 rate=78125; rate<=80000000; rate+=13731) {
        assert(esp32_spi_clock(80000000,rate,&reg,&actual)==0);
        u32 div=(FIELD_GET(SPI_CLKDIV_PRE,reg)+1)*(FIELD_GET(SPI_CLKCNT_N,reg)+1);
        assert(actual==80000000/div && (uint64_t)rate*div>=80000000);
        assert(FIELD_GET(SPI_CLKCNT_H,reg)<=FIELD_GET(SPI_CLKCNT_N,reg));
    }
    puts("PASS: 400 kHz initialization, 20 MHz data, and divider range never overclock");
    u32 regs[64]={0}, clock=80000000;
    struct esp32_spi spi={.regs=(u8 *)regs,.clk=&clock};
    struct spi_controller host={&spi};
    struct spi_device dev={&host,0,false};
    struct spi_message msg={&dev};
    struct spi_transfer t={.speed_hz=400000,.bits_per_word=8,.rx_nbits=1,.tx_nbits=1};
    unsigned lengths[]={1,3,4,63,64,65,512};
    for (unsigned k=0; k<sizeof(lengths)/sizeof(*lengths); k++) {
        u8 tx[514],rx[514];
        for (unsigned i=0;i<sizeof(tx);i++) tx[i]=(u8)i;
        memset(rx,0,sizeof(rx)); rx[0]=rx[sizeof(rx)-1]=0x5a;
        t.len=lengths[k]; t.tx_buf=tx+1; t.rx_buf=rx+1;
        assert(esp32_spi_transfer_one(&host,&dev,&t)==0);
        assert(memcmp(tx+1,rx+1,t.len)==0 && rx[0]==0x5a && rx[t.len+1]==(t.len==512?0x5a:0));
        t.tx_buf=NULL; memset(rx+1,0,t.len);
        assert(esp32_spi_transfer_one(&host,&dev,&t)==0);
        for (unsigned i=1;i<=t.len;i++) assert(rx[i]==0xff);
        t.tx_buf=tx+1; t.rx_buf=NULL;
        assert(esp32_spi_transfer_one(&host,&dev,&t)==0);
    }
    puts("PASS: RX-only idle bytes, TX-only, full duplex, odd tails and 512-byte transfers");
    esp32_spi_set_cs(&dev,false);
    assert(regs[SPI_MISC_REG/4]&SPI_CS_KEEP_ACTIVE);
    assert(esp32_spi_prepare_message(&host,&msg)==0 && (regs[SPI_MISC_REG/4]&SPI_CS_KEEP_ACTIVE));
    dev.gpio=true;
    assert(esp32_spi_prepare_message(&host,&msg)==0);
    assert((regs[SPI_MISC_REG/4]&SPI_CS_DIS)==SPI_CS_DIS);
    u32 saved=regs[SPI_MISC_REG/4]; esp32_spi_set_cs(&dev,false);
    assert(saved==regs[SPI_MISC_REG/4]);
    puts("PASS: native CS held across messages, GPIO CS disables hardware CS");
    t.len=1; u8 byte=0; t.tx_buf=&byte; stuck=true;
    assert(esp32_spi_transfer_one(&host,&dev,&t)==-ETIMEDOUT);
    stuck=false; missing_done=true; regs[SPI_DMA_INT_RAW_REG/4]=0;
    assert(esp32_spi_transfer_one(&host,&dev,&t)==-EIO);
    puts("PASS: command timeout and missing completion propagate errors");
}
'''
with tempfile.TemporaryDirectory(prefix='esp32-spi-test-') as tmp:
    binary = str(Path(tmp) / 'spi-test')
    subprocess.run(['cc', '-std=gnu17', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter', '-fsanitize=address,undefined',
                    '-x', 'c', '-', '-o', binary], input=shim+constants+'\n'+registers+callbacks+tests,
                   text=True, check=True)
    subprocess.run([binary], check=True)
