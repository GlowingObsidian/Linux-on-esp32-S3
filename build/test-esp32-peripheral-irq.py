#!/usr/bin/env python3
"""Exercise the built IRQ callbacks with fake per-CPU registers and deferred IPIs.

This validates CPU ownership/state transitions, not S3 electrical delivery.
Run inside the build container, where the configured kernel source exists.
"""
from pathlib import Path
import subprocess
import tempfile

repo = Path(__file__).resolve().parent.parent
kernel = repo / 'experiments/mmu-poc/out/linux-fork'
smp = (kernel / 'arch/xtensa/kernel/smp.c').read_text()
intc = (kernel / 'drivers/irqchip/irq-esp32-intc.c').read_text()


def function(source, name):
    name_at = source.index(name + '(')
    start = source.rfind('\n', 0, name_at) + 1
    brace = source.index('{', name_at)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end].replace('asm volatile("rsync" ::: "memory");', '')


shim = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <errno.h>
typedef uint32_t u32;
#define __iomem
#define BIT(n) (1UL << (n))
#define XCHAL_NUM_INTERRUPTS 32
#define ESP32_INTC_DISCONNECTED 6
#define IPI_ENABLE_IRQ 3
#define IRQ_SET_MASK_OK 0
#define pr_debug(...) ((void)0)
#define WARN_ON_ONCE(x) (x)
static unsigned nr_cpu_ids = 2, current_cpu;
static unsigned long intenable[2], irqflags[2], esp32_irq_enable_pending[2];
static unsigned long doorbells[2];
struct cpumask { unsigned long bits; };
static const struct cpumask masks[] = {{1}, {2}};
#define cpumask_of(cpu) (&masks[(cpu)])
#define cpumask_test_cpu(cpu, mask) (!!((mask)->bits & BIT(cpu)))
#define get_cpu() current_cpu
#define put_cpu() ((void)0)
#define per_cpu(name, cpu) name[cpu]
#define set_bit(n,p) (*(p) |= BIT(n))
#define local_irq_save(f) do { (f)=irqflags[current_cpu]; irqflags[current_cpu]=1; } while (0)
#define local_irq_restore(f) do { irqflags[current_cpu]=(f); } while (0)
#define xtensa_get_sr(reg) intenable[current_cpu]
#define xtensa_set_sr(value, reg) do { intenable[current_cpu]=(value); } while (0)
#define writel(value, addr) (*(u32 *)(addr)=(value))
static void send_ipi_message(const struct cpumask *mask, int type) {
    assert(type == IPI_ENABLE_IRQ);
    for (unsigned cpu=0; cpu<2; cpu++)
        if (cpumask_test_cpu(cpu, mask)) doorbells[cpu] |= BIT(type);
}
struct esp32_intc { void *base[2]; };
struct esp32_intc_irq {
    struct esp32_intc *intc;
    unsigned int cpu, source, input;
    unsigned int nr_extra, extra[4];
};
struct irq_data { struct esp32_intc_irq *route; unsigned long effective; };
#define irq_data_get_irq_chip_data(d) ((d)->route)
#define irq_data_update_effective_affinity(d,m) ((d)->effective=(m)->bits)
'''
callbacks = '\n'.join([
    function(smp, 'esp32_enable_local_inputs'),
    function(smp, 'esp32_enable_peripheral_irq'),
    function(intc, 'esp32_intc_write'),
    function(intc, 'esp32_intc_route_write'),
    function(intc, 'esp32_intc_mask'),
    function(intc, 'esp32_intc_unmask'),
    function(intc, 'esp32_intc_set_affinity'),
])
tests = r'''
static void deliver(unsigned cpu) {
    current_cpu=cpu;
    assert(doorbells[cpu] & BIT(IPI_ENABLE_IRQ));
    doorbells[cpu]=0;
    unsigned long pending=esp32_irq_enable_pending[cpu];
    esp32_irq_enable_pending[cpu]=0;
    esp32_enable_local_inputs(pending);
}
int main(void) {
    u32 matrix[2][512]={{0}};
    struct esp32_intc ic={{matrix[0],matrix[1]}};
    struct esp32_intc_irq uart={.intc=&ic,.cpu=0,.source=21,.input=1}, usb={.intc=&ic,.cpu=0,.source=38,.input=13};
    struct irq_data ud={&uart,0}, dd={&usb,0};
    intenable[0]=intenable[1]=BIT(2)|BIT(6);
    current_cpu=1; irqflags[1]=1; /* descriptor lock held on the other CPU */
    esp32_intc_unmask(&dd);
    assert(matrix[0][38]==13 && matrix[1][38]==0);
    assert(intenable[0]==0x44 && intenable[1]==0x44 && irqflags[1]==1);
    esp32_intc_unmask(&ud); /* two enables coalesce without losing bits */
    assert(esp32_irq_enable_pending[0]==(BIT(1)|BIT(13)));
    deliver(0);
    assert(intenable[0]==(0x44|BIT(1)|BIT(13)) && intenable[1]==0x44);
    assert(irqflags[0]==0);
    puts("PASS: remote enables reach the DT target CPU and preserve timer/IPI inputs");

    current_cpu=1;
    esp32_intc_unmask(&dd);
    esp32_intc_mask(&dd); /* mask wins even if its earlier IPI is pending */
    deliver(0);
    assert(matrix[0][38]==ESP32_INTC_DISCONNECTED);
    puts("PASS: masking a source remains effective after a deferred enable completes");

    assert(esp32_intc_set_affinity(&dd,cpumask_of(1),false)==-EINVAL);
    assert(esp32_intc_set_affinity(&dd,cpumask_of(0),false)==IRQ_SET_MASK_OK);
    assert(dd.effective==1);
    current_cpu=0; irqflags[0]=1;
    esp32_intc_unmask(&dd);
    assert(matrix[0][38]==13 && irqflags[0]==1 && doorbells[0]==0);
    puts("PASS: fixed affinity and local enables preserve routing and caller IRQ state");
    struct esp32_intc_irq wifi={.intc=&ic,.cpu=0,.source=0,.input=0,.nr_extra=1,.extra={2}};
    struct irq_data wd={&wifi,0};
    matrix[0][0]=matrix[0][2]=ESP32_INTC_DISCONNECTED;
    esp32_intc_unmask(&wd);
    assert(matrix[0][0]==0 && matrix[0][2]==0);
    assert(matrix[0][21]==1 && matrix[0][38]==13);
    esp32_intc_mask(&wd);
    assert(matrix[0][0]==ESP32_INTC_DISCONNECTED && matrix[0][2]==ESP32_INTC_DISCONNECTED);
    puts("PASS: grouped MAC/BB rows unmask and mask together without changing UART/USB");
    return 0;
}
''' 
with tempfile.TemporaryDirectory(prefix='esp32-irq-test-') as tmp:
    output = str(Path(tmp) / 'irq-test')
    subprocess.run(['cc', '-std=gnu17', '-Wall', '-Wextra',
                    '-Wno-unused-parameter', '-Werror', '-fsanitize=address,undefined',
                    '-x', 'c', '-', '-o', output], input=shim+callbacks+tests,
                   text=True, check=True)
    subprocess.run([output], check=True)
