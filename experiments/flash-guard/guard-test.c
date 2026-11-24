// SPDX-License-Identifier: GPL-2.0-only
/* Manual SRAM rendezvous (1), strict cache gate (2), or a cache gate with
 * explicit acknowledgement of the observed CPU0 status latch (3).
 * All modes leave flash commands and the read-only MTD backend untouched.
 */
#include <linux/cpu.h>
#include <linux/io.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/slab.h>
#include <linux/stop_machine.h>
#include <asm/traps.h>
#include "esp32s3-flash-guard-layout.h"

struct fg_device {
	void __iomem *sram;
	void __iomem *cache;
	unsigned long iram;
	struct mutex lock;
	u32 code_bytes, cache_bytes, runs, mode;
	int last_result;
	bool checked[2], psram_ok;
	u32 setup_before[3], setup_after[3];
	void __iomem *nor;
	void *nor_partitions;
	u32 nor_bytes, nor_entry_offset, nor_jedec, flash_operations;
	bool nor_id_ok, nor_rw, nor_scratch_ok;
};
extern const u8 esp32_flash_guard_template_start[];
extern const u8 esp32_flash_guard_template_end[];
extern const u8 esp32_flash_cache_template_start[];
extern const u8 esp32_flash_cache_template_end[];

static __always_inline u32 fg_mask_all(void)
{
	u32 saved;

	asm volatile("rsr %0, intenable\n\tmovi a8, 0\n\twsr a8, intenable\n\trsync"
		     : "=a" (saved) : : "a8", "memory");
	return saved;
}

static __always_inline void fg_restore_all(u32 saved)
{
	asm volatile("wsr %0, intenable\n\trsync" : : "a" (saved) : "memory");
}

static int fg_cpu(void *arg)
{
	struct fg_device *fg = arg;
	unsigned int cpu = raw_smp_processor_id();
	typedef int (*fg_entry)(struct fg_control *, void *, unsigned int, u32);
	bool cache = fg->mode != 1;
	fg_entry entry = (fg_entry)(fg->iram + (cache ? FG_CACHE_CODE : 0));
	struct fg_control *control = (void __force *)(fg->sram + FG_CONTROL);
	void *stack = (void __force *)(fg->sram + FG_STACK(cpu) + FG_STACK_BYTES);
	u32 enabled = 0, ps = 0, windows;
	int ret;

	if (cpu > 1)
		return -EINVAL;
	if (cache) {
		/* stop_machine masks only LOCKLEVEL=3 on this S3. Mask inputs
		 * 4/5 as well, BEFORE spilling windows. No external call may
		 * occur between this spill and the relocated entry except entry.
		 * True NMI routes/pending state are checked by the SRAM guard.
		 */
		enabled = fg_mask_all();
		asm volatile("rsr %0, ps" : "=a" (ps));
		writel(enabled, &control->irq_saved[cpu]);
		writel(ps, &control->ps[cpu]);
		spill_registers();
		asm volatile("rsr %0, windowstart" : "=a" (windows));
		writel(windows, &control->windows[cpu]);
	}
	ret = entry(control, stack, cpu, fg->mode == 4 ? 1250000000U :
		    (cache ? FG_RENDEZVOUS_CYCLES : 120000000));
	if (cache) {
		u32 after_ps;

		asm volatile("rsr %0, ps" : "=a" (after_ps));
		fg_restore_all(enabled);
		/* CALLINC may vary; interrupt level, EXCM and WOE must not. */
		if ((ps ^ after_ps) & 0x4001f)
			return -EIO;
	}
	return ret == 0 ? 0 : (ret == 3 ? -ETIMEDOUT : (ret == 5 ? -EBUSY : -EIO));
}

static ssize_t result_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct fg_device *fg = dev_get_drvdata(dev);
	struct fg_control __iomem *c = fg->sram + FG_CONTROL;
	ssize_t n;

	mutex_lock(&fg->lock);
	n = sysfs_emit(buf, "runs=%u mode=%u result=%d code_bytes=%u cache_code_bytes=%u cpu0_internal=%u cpu1_internal=%u flash_operations=%u cache_changes=%u cache_stage=%u cache_off_cycles=%u peer_cache_off=%u psram_ok=%u cpu0_status=%u cpu1_status=%u status_acknowledged=%08x\n",
		fg->runs, fg->mode, fg->last_result, fg->code_bytes, fg->cache_bytes,
		fg->checked[0], fg->checked[1], fg->flash_operations, readl(&c->attempt), readl(&c->stage),
		readl(&c->hold_cycles), readl(&c->peer_off), fg->psram_ok,
		readl(&c->result[0]), readl(&c->result[1]), readl(&c->acknowledged));
	mutex_unlock(&fg->lock);
	return n;
}
static DEVICE_ATTR_RO(result);

static ssize_t cache_state_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct fg_device *fg = dev_get_drvdata(dev);
	struct fg_control __iomem *c = fg->sram + FG_CONTROL;
	ssize_t n;

	mutex_lock(&fg->lock);
	n = sysfs_emit(buf, "i_before=%08x i_after=%08x d_before=%08x d_after=%08x i_off=%08x d_off=%08x buses_off=%08x,%08x state_off=%08x errors_before=%08x,%08x,%08x errors_after=%08x,%08x,%08x irq_off=%08x,%08x windows=%08x,%08x before_valid=%u off_valid=%u after_valid=%u errors_ack=%08x,%08x,%08x\n",
		readl(&c->before[0]), readl(&c->after[0]),
		readl(&c->before[4]), readl(&c->after[4]),
		readl(&c->suspended[0]), readl(&c->suspended[1]),
		readl(&c->suspended[2]), readl(&c->suspended[3]), readl(&c->state_off),
		readl(&c->errors_before[0]), readl(&c->errors_before[1]), readl(&c->errors_before[2]),
		readl(&c->errors_after[0]), readl(&c->errors_after[1]), readl(&c->errors_after[2]),
		readl(&c->irq_off[0]), readl(&c->irq_off[1]),
		readl(&c->windows[0]), readl(&c->windows[1]),
		!!(readl(&c->snapshots) & 1), !!(readl(&c->snapshots) & 2),
		!!(readl(&c->snapshots) & 4), readl(&c->errors_ack[0]),
		readl(&c->errors_ack[1]), readl(&c->errors_ack[2]));
	mutex_unlock(&fg->lock);
	return n;
}
static DEVICE_ATTR_RO(cache_state);

static void fg_read_errors(struct fg_device *fg, u32 errors[3])
{
	errors[0] = readl(fg->cache + 0xe4);
	errors[1] = readl(fg->cache + 0xf0);
	errors[2] = readl(fg->cache + 0xfc);
}

static ssize_t cache_live_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct fg_device *fg = dev_get_drvdata(dev);
	u32 errors[3];
	u32 mmu_content, mmu_vaddr;
	ssize_t n;

	mutex_lock(&fg->lock);
	fg_read_errors(fg, errors);
	/* TRM/IDF: faulting MMU entry and virtual address, read without W1C. */
	mmu_content = readl(fg->cache + 0x120);
	mmu_vaddr = readl(fg->cache + 0x124);
	n = sysfs_emit(buf, "i_ctrl=%08x i_bus=%08x d_ctrl=%08x d_bus=%08x errors=%08x,%08x,%08x setup_before=%08x,%08x,%08x setup_after=%08x,%08x,%08x mmu_fault_content=%08x mmu_fault_vaddr=%08x\n",
		readl(fg->cache + 0x60), readl(fg->cache + 0x64),
		readl(fg->cache), readl(fg->cache + 4),
		errors[0], errors[1], errors[2],
		fg->setup_before[0], fg->setup_before[1], fg->setup_before[2],
		fg->setup_after[0], fg->setup_after[1], fg->setup_after[2],
		mmu_content, mmu_vaddr);
	mutex_unlock(&fg->lock);
	return n;
}
static DEVICE_ATTR_RO(cache_live);

#ifdef CONFIG_MTD_ESP32S3_NOR
#include "esp32s3-flash-nor-main.inc"
#endif

static ssize_t test_store(struct device *dev, struct device_attribute *attr,
			  const char *buf, size_t count)
{
	struct fg_device *fg = dev_get_drvdata(dev);
	struct fg_control __iomem *c = fg->sram + FG_CONTROL;
	u32 *probe = NULL;
	unsigned int value, cpu, i;
	int ret;

#ifdef CONFIG_MTD_ESP32S3_NOR
	if (!kstrtouint(buf, 0, &value) && value == 4)
		return fn_identify_store(dev, buf, count);
#endif

	ret = kstrtouint(buf, 0, &value);
	if (ret || (value != 1 && value != 2 && value != 3))
		return -EINVAL;
	mutex_lock(&fg->lock);
	fg->mode = value;
	fg->psram_ok = false;
	memset_io(c, 0, sizeof(*c));
	for (cpu = 0; cpu < 2; cpu++) {
		fg->checked[cpu] = false;
		writel(FG_CANARY, fg->sram + FG_STACK(cpu));
	}
	writel(value == 3, &c->ack_request);
	if (value != 1) {
		/* A temporary dirty PSRAM page exercises tag/data preservation.
		 * It is never touched while caches are suspended.
		 */
		probe = kmalloc(4096, GFP_KERNEL);
		if (!probe) {
			ret = -ENOMEM;
			goto unlock;
		}
		for (i = 0; i < 1024; i++)
			probe[i] = 0x5639a721 ^ (i * 0x10201);
	}
	cpus_read_lock();
	if (num_online_cpus() != 2 || !cpu_online(0) || !cpu_online(1)) {
		ret = -ENODEV;
		goto out;
	}
	asm volatile("memw\n\tisync" ::: "memory");
	ret = stop_machine_cpuslocked(fg_cpu, fg, cpu_online_mask);
	for (cpu = 0; cpu < 2; cpu++) {
		u32 sp = readl(&c->sp[cpu]);
		u32 pc = readl(&c->pc[cpu]);
		u32 status = readl(&c->result[cpu]);
		u32 ready = readl(&c->ready[cpu]);
		unsigned long low = FG_BASE + FG_STACK(cpu);
		unsigned long code = fg->iram + (value != 1 ? FG_CACHE_CODE : 0);
		u32 bytes = value != 1 ? fg->cache_bytes : fg->code_bytes;

		pc = (pc & 0x3fffffff) | (fg->iram & 0xc0000000);
		fg->checked[cpu] = ready == 1 && sp >= low + 32 &&
			sp < low + FG_STACK_BYTES && pc >= code && pc < code + bytes &&
			readl(fg->sram + FG_STACK(cpu)) == FG_CANARY;
		if (!fg->checked[cpu] && !ret)
			ret = -EIO;
		if (status && !ret)
			ret = status == 3 ? -ETIMEDOUT : (status == 5 ? -EBUSY : -EIO);
	}
	if (value != 1) {
		fg->psram_ok = true;
		for (i = 0; i < 1024; i++)
			if (probe[i] != (0x5639a721 ^ (i * 0x10201)))
				fg->psram_ok = false;
		if (!ret && (!fg->psram_ok || readl(&c->stage) != 5 ||
		    readl(&c->attempt) != 1 || readl(&c->mutated) != 3 ||
		    readl(&c->peer_off) != 1 || readl(&c->irq_off[0]) ||
		    readl(&c->irq_off[1]) || readl(&c->nmi[0]) || readl(&c->nmi[1]) ||
		    (readl(&c->suspended[0]) & 1) || (readl(&c->suspended[1]) & 1) ||
		    (readl(&c->suspended[2]) & 3) != 3 || (readl(&c->suspended[3]) & 3) != 3 ||
		    readl(&c->state_off) != 0x1001 || readl(&c->hold_cycles) < FG_HOLD_CYCLES))
			ret = -EIO;
		for (i = 0; i < 3; i++) {
			u32 allowed = value == 3 && i == 1 ? readl(&c->acknowledged) : 0;

			if (!ret && (fg_cache_fault_status(i, readl(&c->errors_before[i]) & ~allowed) ||
			    fg_cache_fault_status(i, readl(&c->errors_after[i])) ||
			    fg_cache_fault_status(i, readl(&c->errors_ack[i]))))
				ret = -EIO;
		}
		if (!ret && readl(&c->acknowledged) &&
		    (value != 3 || readl(&c->acknowledged) != 2 ||
		     readl(&c->errors_before[1]) != 2))
			ret = -EIO;
	}
	fg->runs++;
out:
	cpus_read_unlock();
	kfree(probe);
unlock:
	fg->last_result = ret;
	mutex_unlock(&fg->lock);
	if (ret == -EBUSY && value != 1 && readl(&c->stage) == 1)
		dev_warn(dev, "cache preflight blocked before cache changes; saved status=%08x,%08x,%08x ack=%08x errno=%d\n",
			readl(&c->errors_before[0]), readl(&c->errors_before[1]),
			readl(&c->errors_before[2]), readl(&c->acknowledged), ret);
	else if (ret)
		dev_warn(dev, "non-writing guard mode %u failed: %d\n", value, ret);
	else if (value != 1)
		dev_dbg(dev, "cache suspend/resume passed on both CPUs; PSRAM intact, no flash commands\n");
	else
		dev_dbg(dev, "internal-memory dry run passed on both CPUs; no flash/cache changes\n");
	return ret ? ret : count;
}
static DEVICE_ATTR_WO(test);

static struct attribute *fg_attrs[] = {
	&dev_attr_result.attr, &dev_attr_cache_state.attr,
	&dev_attr_cache_live.attr, &dev_attr_test.attr,
#ifdef CONFIG_MTD_ESP32S3_NOR
	&dev_attr_nor_state.attr, &dev_attr_scratch_test.attr,
	&dev_attr_rw_enable.attr,
#endif
	NULL,
};
static const struct attribute_group fg_group = { .attrs = fg_attrs };

static int fg_copy(struct fg_device *fg, unsigned int offset, const u8 *src, u32 bytes)
{
	unsigned int i;

	memcpy_toio(fg->sram + offset, src, bytes);
	for (i = 0; i < bytes; i++)
		if (readb(fg->sram + offset + i) != src[i])
			return -EIO;
	return 0;
}

static int fg_probe(struct platform_device *pdev)
{
	struct resource *res = platform_get_resource(pdev, IORESOURCE_MEM, 0);
	struct fg_device *fg;
	int ret;

	static_assert(offsetof(struct fg_control, release) == FGC_FINAL);
	static_assert(offsetof(struct fg_control, result) == FGC_RESULT);
	static_assert(offsetof(struct fg_control, sp) == FGC_SP);
	static_assert(offsetof(struct fg_control, pc) == FGC_PC);
	static_assert(offsetof(struct fg_control, elapsed) == FGC_ELAPSED);
	static_assert(offsetof(struct fg_control, abort_request) == FGC_ABORT);
	static_assert(offsetof(struct fg_control, before) == FGC_BEFORE);
	static_assert(offsetof(struct fg_control, after) == FGC_AFTER);
	static_assert(offsetof(struct fg_control, errors_before) == FGC_ERRORS_BEFORE);
	static_assert(offsetof(struct fg_control, errors_after) == FGC_ERRORS_AFTER);
	static_assert(offsetof(struct fg_control, suspended) == FGC_SUSPENDED);
	static_assert(offsetof(struct fg_control, peer_off) == FGC_PEER_OFF);
	static_assert(offsetof(struct fg_control, ack_request) == FGC_ACK_REQUEST);
	static_assert(offsetof(struct fg_control, errors_ack) == FGC_ERRORS_ACK);
	static_assert(offsetof(struct fg_control, acknowledged) == FGC_ACKNOWLEDGED);
	static_assert(offsetof(struct fg_control, snapshots) == FGC_SNAPSHOTS);
	static_assert(offsetof(struct fg_control, nor_task) == FGC_NOR_TASK);
	static_assert(offsetof(struct fg_control, nor_entry) == FGC_NOR_ENTRY);
	static_assert(sizeof(struct fg_control) == FGC_BYTES);
	static_assert(FG_CONTROL + FGC_BYTES <= FG_STACK(0));
	if (!res || res->start != FG_BASE || resource_size(res) != FG_BYTES)
		return -EINVAL;
	fg = devm_kzalloc(&pdev->dev, sizeof(*fg), GFP_KERNEL);
	if (!fg)
		return -ENOMEM;
	fg->sram = devm_ioremap_resource(&pdev->dev, res);
	if (IS_ERR(fg->sram))
		return PTR_ERR(fg->sram);
	fg->cache = devm_ioremap(&pdev->dev, 0x600c4000, 0x158);
	if (!fg->cache)
		return -ENOMEM;
	fg->iram = FG_BASE + FG_ALIAS;
	fg->last_result = -ENODATA;
	fg->code_bytes = esp32_flash_guard_template_end - esp32_flash_guard_template_start;
	fg->cache_bytes = esp32_flash_cache_template_end - esp32_flash_cache_template_start;
	if (!fg->code_bytes || fg->code_bytes > FG_CACHE_CODE ||
	    !fg->cache_bytes || fg->cache_bytes > FG_CONTROL - FG_CACHE_CODE)
		return -E2BIG;
	mutex_init(&fg->lock);
	fg_read_errors(fg, fg->setup_before);
	ret = fg_copy(fg, 0, esp32_flash_guard_template_start, fg->code_bytes);
	if (!ret)
		ret = fg_copy(fg, FG_CACHE_CODE, esp32_flash_cache_template_start, fg->cache_bytes);
	if (ret)
		return ret;
	asm volatile("memw\n\tisync" ::: "memory");
	fg_read_errors(fg, fg->setup_after);
	memset_io(fg->sram + FG_CONTROL, 0, sizeof(struct fg_control));
	platform_set_drvdata(pdev, fg);
#ifdef CONFIG_MTD_ESP32S3_NOR
	ret = fn_probe(pdev, fg);
	if (ret)
		return ret;
#endif
	return devm_device_add_group(&pdev->dev, &fg_group);
}
static const struct of_device_id fg_match[] = {
	{ .compatible = "espressif,esp32s3-flash-guard-test" }, {},
};
MODULE_DEVICE_TABLE(of, fg_match);
static struct platform_driver fg_driver = {
	.probe = fg_probe,
	.driver = { .name = "esp32s3-flash-guard-test", .of_match_table = fg_match },
};
module_platform_driver(fg_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Non-writing ESP32-S3 SRAM and cache suspend/resume test");
