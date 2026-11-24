#!/usr/bin/env python3
"""Check actual SPI DMA callbacks with emulated SRAM/descriptors and faults.

Does not prove electrical transfers, hardware register timing, or cache behavior.
"""
from pathlib import Path
import importlib.util
import subprocess
import tempfile

spec = importlib.util.spec_from_file_location('spi_pio', Path(__file__).with_name('test-esp32-spi.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)  # also runs the unchanged PIO regression suite
constants = '\n'.join(line for line in m.s.splitlines() if line.startswith(('#define SPI_', '#define ESP32_SPI_FIFO_SIZE', '#define GDMA_')))
shim = m.shim
shim = shim[:shim.index('static int esp32_spi_dma_transfer(')]
shim = shim.replace('void *gdma; void *dev; bool dma_failed; };', 'u8 *gdma, *dma_sram; u32 dma_phys; int irq; unsigned done; void *dev; bool dma_failed; u32 dma_chunks, dma_bytes, dma_errors; };')
shim += r'''
#define dev_warn(d,...) do { (void)(d); fprintf(stderr,__VA_ARGS__); } while(0)
#define dev_err_ratelimited(d,...) do { (void)(d); fprintf(stderr,__VA_ARGS__); } while(0)
#define min_t(t,a,b) ((t)(a)<(t)(b)?(t)(a):(t)(b))
#define writel(v,p) (*(u32 *)(p)=(v))
#define readl(p) (*(u32 *)(p))
#define memset_io memset
#define WRITE_ONCE(p,v) ((p)=(v))
#define wmb() ((void)0)
#define rmb() ((void)0)
#define reinit_completion(p) (*(p)=0)
#define wait_for_completion_timeout(p,t) (*(p))
#define synchronize_irq(i) ((void)0)
#define readl_poll_timeout(p,v,c,d,t) ((v)=readl(p), (c)?0:-ETIMEDOUT)
static unsigned dma_chunks;
static bool drop_irq, bad_rx_length, drop_eof, descriptor_error;
static bool held_tx_owner, bad_tx_length, drop_rx_eof;
static unsigned inject_fifo_error;
'''
registers = r'''
static u32 esp32_spi_read(struct esp32_spi *spi, int off) {
    jiffies++; return *(u32 *)(spi->regs+off);
}
static void esp32_spi_write(struct esp32_spi *spi, int off, u32 value) {
    if (off == SPI_DMA_INT_CLR_REG) {
        *(u32 *)(spi->regs+SPI_DMA_INT_RAW_REG) &= ~value; return;
    }
    *(u32 *)(spi->regs+off)=value;
    if (off != SPI_CMD_REG || stuck || (fail_after && commands>=fail_after)) return;
    *(u32 *)(spi->regs+off)=0;
    if (value != SPI_USR) return;
    commands++;
    if (!missing_done) *(u32 *)(spi->regs+SPI_DMA_INT_RAW_REG) |= SPI_TRANS_DONE_INT;
    if (!(readl(spi->regs+SPI_DMA_CONF_REG)&SPI_DMA_TX_ENA)) {
        assert(readl(spi->regs+SPI_DMA_CONF_REG)==0); return;
    }
    assert((readl(spi->regs+SPI_DMA_CONF_REG)&SPI_DMA_MASTER_FLAGS)==SPI_DMA_MASTER_FLAGS);
    assert(!(readl(spi->regs+SPI_DMA_INT_RAW_REG)&SPI_DMA_FIFO_ERRORS));
    assert((readl(spi->gdma+GDMA_IN_CONF0)&GDMA_IN_BURST)==GDMA_IN_BURST);
    assert((readl(spi->gdma+GDMA_OUT_CONF0)&GDMA_OUT_BURST)==GDMA_OUT_BURST);
    assert((readl(spi->regs+SPI_DMA_INT_ENA_REG)&SPI_DMA_FIFO_ERRORS)==SPI_DMA_FIFO_ERRORS);
    u32 control=readl(spi->dma_sram+GDMA_TX_DESC);
    unsigned len=FIELD_GET(GDMA_LENGTH,control);
    assert(len && len<=4092 && len==FIELD_GET(GDMA_SIZE,control));
    assert(control&GDMA_OWNER && control&GDMA_DESC_EOF);
    assert(readl(spi->dma_sram+GDMA_TX_DESC+4)==spi->dma_phys+GDMA_TX_BUF);
    assert(readl(spi->gdma+GDMA_OUT_LINK)==(((spi->dma_phys+GDMA_TX_DESC)&GDMA_LINK_ADDR)|GDMA_OUT_START));
    assert(!(readl(spi->gdma+GDMA_IN_CONF0)&BIT(4))); /* SPI, never memcpy mode */
    assert(readl(spi->regs+SPI_DMA_INT_ENA_REG)&SPI_TRANS_DONE_INT);
    dma_chunks++;
    if (readl(spi->regs+SPI_DMA_CONF_REG)&SPI_DMA_RX_ENA) {
        u32 rx=readl(spi->dma_sram+GDMA_RX_DESC);
        assert(FIELD_GET(GDMA_SIZE,rx)==len && FIELD_GET(GDMA_LENGTH,rx)==0);
        assert(rx&GDMA_OWNER && !(rx&GDMA_DESC_EOF));
        assert(readl(spi->dma_sram+GDMA_RX_DESC+4)==spi->dma_phys+GDMA_RX_BUF);
        assert(readl(spi->gdma+GDMA_IN_LINK)==(((spi->dma_phys+GDMA_RX_DESC)&GDMA_LINK_ADDR)|GDMA_IN_START));
        assert(GDMA_TX_BUF+len<=GDMA_RX_BUF);
        memcpy(spi->dma_sram+GDMA_RX_BUF,spi->dma_sram+GDMA_TX_BUF,len);
        writel(FIELD_PREP(GDMA_SIZE,len)|FIELD_PREP(GDMA_LENGTH,len-bad_rx_length),spi->dma_sram+GDMA_RX_DESC);
        writel((drop_eof || drop_rx_eof)?0:GDMA_RX_EOF,spi->gdma+GDMA_IN_RAW);
    }
    /* Reproduce hardware log: OUT_DONE=1 with no OUT_TOTAL_EOF. */
    writel((control & ~(GDMA_OWNER | GDMA_LENGTH)) |
           (held_tx_owner?GDMA_OWNER:0) |
           FIELD_PREP(GDMA_LENGTH,len-bad_tx_length),spi->dma_sram+GDMA_TX_DESC);
    writel((descriptor_error?GDMA_OUT_ERROR:0) | (drop_eof?0:BIT(0)),spi->gdma+GDMA_OUT_RAW);
    if (inject_fifo_error) {
        *(u32 *)(spi->regs+SPI_DMA_INT_RAW_REG) |= inject_fifo_error;
        *(u32 *)(spi->regs+SPI_DMA_INT_RAW_REG) &= ~SPI_TRANS_DONE_INT;
        *(u32 *)(spi->regs+SPI_CMD_REG)=SPI_USR; /* stalled master */
    }
    if (!drop_irq) spi->done=1;
}
'''
callbacks='\n'.join(m.function(n) for n in (
    'esp32_spi_init','esp32_spi_prepare_message','esp32_spi_set_cs',
    'esp32_spi_wait_bit','esp32_spi_clock','esp32_spi_prepare_transfer',
    'esp32_spi_start_transfer','esp32_spi_wait','esp32_spi_dma_reset',
    'esp32_spi_dma_desc','esp32_spi_dma_hw_init','esp32_spi_dma_transfer','esp32_spi_transfer_one'))
extra = r'''
    stuck=false; missing_done=false;
    u32 gdma[1024]={0}, sram[(GDMA_SRAM_SIZE+8)/4]={0};
    spi.gdma=(u8 *)gdma; spi.dma_sram=(u8 *)sram; spi.dma_phys=0x3fccc700;
    assert(esp32_spi_dma_hw_init(&spi)==-EIO);
    writel(GDMA_DATE_VALUE,spi.gdma+GDMA_DATE);
    assert(esp32_spi_dma_hw_init(&spi)==0);
    assert(readl(spi.gdma+GDMA_MISC_CONF)&GDMA_CLOCK_EN);
    assert(readl(spi.gdma+GDMA_IN_PERI)==0 && readl(spi.gdma+GDMA_OUT_PERI)==0);
    puts("PASS DMA: internal clock enabled, hardware identity checked, SPI2 selected");
    u8 tx_dma[9002],rx_dma[9002];
    for(unsigned i=0;i<sizeof(tx_dma);i++) tx_dma[i]=(u8)(i*19+7);
    unsigned sizes[]={64,512,4092,4096,8192,9000};
    for(unsigned k=0;k<sizeof(sizes)/sizeof(*sizes);k++) {
        memset(rx_dma,0,sizeof(rx_dma)); rx_dma[0]=0xa5; rx_dma[sizes[k]+1]=0x5a;
        t.len=sizes[k]; t.tx_buf=tx_dma+1; t.rx_buf=rx_dma+1;
        unsigned before=dma_chunks;
        assert(esp32_spi_transfer_one(&host,&dev,&t)==0);
        assert(dma_chunks-before==(t.len+4091)/4092);
        assert(memcmp(tx_dma+1,rx_dma+1,t.len)==0 && rx_dma[0]==0xa5 && rx_dma[t.len+1]==0x5a);
        t.tx_buf=NULL;
        assert(esp32_spi_transfer_one(&host,&dev,&t)==0);
        for(unsigned i=1;i<=t.len;i++) assert(rx_dma[i]==0xff);
        t.tx_buf=tx_dma+1; t.rx_buf=NULL;
        assert(esp32_spi_transfer_one(&host,&dev,&t)==0);
    }
    t.len=7; t.tx_buf=tx_dma; t.rx_buf=rx_dma;
    unsigned old_chunks=dma_chunks;
    assert(esp32_spi_transfer_one(&host,&dev,&t)==0 && dma_chunks==old_chunks);
    assert(memcmp(tx_dma,rx_dma,7)==0 && readl(spi.regs+SPI_DMA_CONF_REG)==0);
    t.len=512;
    assert(esp32_spi_transfer_one(&host,&dev,&t)==0 && dma_chunks==old_chunks+1);
    assert(memcmp(tx_dma,rx_dma,512)==0);
    puts("PASS DMA: disjoint SRAM, descriptor bounds, multi-chunk payloads and DMA/PIO/DMA transitions");
    t.len=512; t.tx_buf=tx_dma; t.rx_buf=rx_dma;
    drop_irq=true;
    assert(esp32_spi_transfer_one(&host,&dev,&t)==-ETIMEDOUT);
    assert(readl(spi.regs+SPI_DMA_INT_ENA_REG)==0 && readl(spi.regs+SPI_DMA_CONF_REG)==0);
    assert(spi.dma_failed);
    unsigned chunks_before=dma_chunks;
    drop_irq=false;
    assert(esp32_spi_transfer_one(&host,&dev,&t)==0);
    assert(dma_chunks==chunks_before && memcmp(tx_dma,rx_dma,t.len)==0);
    puts("PASS: failing DMA returns error, subsequent request uses PIO without replaying failed data");
    spi.dma_failed=false; descriptor_error=true;
    assert(esp32_spi_transfer_one(&host,&dev,&t)==-EIO);
    spi.dma_failed=false; descriptor_error=false; bad_rx_length=true;
    assert(esp32_spi_transfer_one(&host,&dev,&t)==-EIO);
    spi.dma_failed=false; bad_rx_length=false; drop_eof=true;
    assert(esp32_spi_transfer_one(&host,&dev,&t)==-ETIMEDOUT);
    spi.dma_failed=false; drop_eof=false; drop_rx_eof=true;
    assert(esp32_spi_transfer_one(&host,&dev,&t)==-ETIMEDOUT);
    spi.dma_failed=false; drop_rx_eof=false; held_tx_owner=true;
    assert(esp32_spi_transfer_one(&host,&dev,&t)==-EIO);
    spi.dma_failed=false; held_tx_owner=false; bad_tx_length=true;
    assert(esp32_spi_transfer_one(&host,&dev,&t)==-EIO);
    spi.dma_failed=false; bad_tx_length=false;
    assert(esp32_spi_transfer_one(&host,&dev,&t)==0);
    assert(memcmp(tx_dma,rx_dma,t.len)==0);
    puts("PASS DMA: OUT_DONE without total EOF; lost IRQ, TX ownership/length, short RX, missing TX done/RX EOF, errors and recovery");
    unsigned bytes_before=spi.dma_bytes, success_before=spi.dma_chunks;
    *(u32 *)(spi.regs+SPI_DMA_INT_RAW_REG)|=SPI_DMA_FIFO_ERRORS;
    assert(esp32_spi_transfer_one(&host,&dev,&t)==0); /* stale error cleared */
    assert(spi.dma_bytes==bytes_before+t.len && spi.dma_chunks==success_before+1);
    for(unsigned mask=1;mask<=2;mask++) {
        unsigned errors_before=spi.dma_errors, bytes=spi.dma_bytes;
        inject_fifo_error=mask;
        assert(esp32_spi_transfer_one(&host,&dev,&t)==-EIO);
        assert(spi.dma_errors==errors_before+1 && spi.dma_bytes==bytes);
        assert(spi.dma_failed && readl(spi.regs+SPI_DMA_INT_ENA_REG)==0);
        spi.dma_failed=false; inject_fifo_error=0;
    }
    assert(esp32_spi_transfer_one(&host,&dev,&t)==0);
    puts("PASS DMA: stale FIFO errors cleared, fresh TX underrun/RX overrun fail a stalled master, burst configuration and successful-byte/error counters");
'''
tests=m.tests.rsplit('}',1)[0]+extra+'}\n'
with tempfile.TemporaryDirectory(prefix='esp32-spi-dma-test-') as tmp:
    binary=str(Path(tmp)/'dma-test')
    subprocess.run(['cc','-std=gnu17','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
                    '-fsanitize=address,undefined','-x','c','-','-o',binary],
                   input=shim+constants+'\n'+registers+callbacks+tests,text=True,check=True)
    subprocess.run([binary],check=True)
