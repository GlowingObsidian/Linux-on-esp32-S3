/* SPDX-License-Identifier: GPL-2.0-only */
/* Included by the SRAM guard owner. The read-only root-flash driver remains
 * unchanged; these two MTDs expose only the validated etc/home intervals. */
#include <linux/mtd/mtd.h>
#include "esp32s3-flash-nor.h"

struct fn_partition {
	struct mtd_info mtd;
	struct fg_device *fg;
	u32 offset;
	u32 users;
};
extern const u8 esp32s3_nor_blob_start[], esp32s3_nor_blob_end[];
extern const u8 esp32s3_nor_entry[];

static int fn_error(u32 status)
{
	return !status ? 0 : status == 3 ? -ETIMEDOUT :
		status == 5 ? -EBUSY : -EIO;
}

/* Caller holds fg->lock across staging, both-CPU entry and verification. */
static int fn_execute(struct fg_device *fg, u32 op, u32 address,
		      u32 length, const u8 *input, u8 *output)
{
	struct fg_control __iomem *c = fg->sram + FG_CONTROL;
	struct fn_task __iomem *t = fg->nor + FN_TASK;
	u32 probe[16], cpu, i;
	int ret;

	memset_io(c, 0, sizeof(*c));
	memset_io(t, 0, sizeof(*t));
	writel(op, &t->operation);
	writel(address, &t->address);
	writel(length, &t->length);
	writel(FN_TIMEOUT_CYCLES, &t->timeout);
	writel(fg->nor_jedec, &t->jedec);
	memset_io(t->data, 0xff, FN_FIFO_BYTES);
	if (input)
		memcpy_toio(t->data, input, length);
	writel(FN_BASE + FN_TASK, &c->nor_task);
	writel(FN_BASE + FG_ALIAS + fg->nor_entry_offset, &c->nor_entry);
	for (cpu = 0; cpu < 2; cpu++) {
		fg->checked[cpu] = false;
		writel(FG_CANARY, fg->sram + FG_STACK(cpu));
	}
	for (i = 0; i < ARRAY_SIZE(probe); i++)
		WRITE_ONCE(probe[i], 0x5a631c09U ^ (i * 0x314159U));
	fg->mode = 4;
	cpus_read_lock();
	if (num_online_cpus() != 2 || !cpu_online(0) || !cpu_online(1)) {
		ret = -ENODEV;
		goto out;
	}
	asm volatile("memw\n\tisync" ::: "memory");
	ret = stop_machine_cpuslocked(fg_cpu, fg, cpu_online_mask);
	fg->runs++;
	fg->flash_operations += readl(&t->changed);
	for (cpu = 0; cpu < 2; cpu++) {
		u32 sp = readl(&c->sp[cpu]), pc = readl(&c->pc[cpu]);
		u32 low = FG_BASE + FG_STACK(cpu);
		pc = (pc & 0x3fffffff) | (fg->iram & 0xc0000000);
		fg->checked[cpu] = readl(&c->ready[cpu]) == 1 &&
			sp >= low + 64 && sp < low + FG_STACK_BYTES &&
			pc >= fg->iram + FG_CACHE_CODE &&
			pc < fg->iram + FG_CACHE_CODE + fg->cache_bytes &&
			readl(fg->sram + FG_STACK(cpu)) == FG_CANARY;
		if (!ret && !fg->checked[cpu])
			ret = -EIO;
		if (!ret)
			ret = fn_error(readl(&c->result[cpu]));
	}
	fg->psram_ok = true;
	for (i = 0; i < ARRAY_SIZE(probe); i++)
		if (READ_ONCE(probe[i]) != (0x5a631c09U ^ (i * 0x314159U)))
			fg->psram_ok = false;
	if (!ret && (!fg->psram_ok || readl(&c->stage) != 5 ||
	    readl(&c->peer_off) != 1 || readl(&c->irq_off[0]) ||
	    readl(&c->irq_off[1]) || readl(&c->nmi[0]) || readl(&c->nmi[1])))
		ret = -EIO;
	for (i = 0; i < 3; i++)
		if (!ret && (fg_cache_fault_status(i, readl(&c->errors_before[i])) ||
		    fg_cache_fault_status(i, readl(&c->errors_after[i]))))
			ret = -EIO;
	if (!ret)
		ret = fn_error(readl(&t->result));
	if (!ret && !readl(&t->restored))
		ret = -EIO;
	if (!ret && readl(&t->changed) && readl(&t->cache_syncs) != 2)
		ret = -EIO;
	if (!ret && op == FN_IDENTIFY) {
		fg->nor_jedec = readl(&t->jedec);
		fg->nor_id_ok = FN_JEDEC_SUPPORTED(fg->nor_jedec);
	}
	if (!ret && output)
		memcpy_fromio(output, t->data, length);
out:
	cpus_read_unlock();
	fg->last_result = ret;
	if (ret && readl(&t->changed))
		fg->nor_rw = false;
	return ret;
}

static int fn_read(struct mtd_info *mtd, loff_t from, size_t length,
		   size_t *retlen, u_char *buffer)
{
	struct fn_partition *p = container_of(mtd, struct fn_partition, mtd);
	*retlen = 0;
	if (from < 0 || from > mtd->size || length > mtd->size - from)
		return -EINVAL;
	mutex_lock(&p->fg->lock);
	memcpy(buffer, (const void *)(0x42000000UL + p->offset + (unsigned long)from), length);
	*retlen = length;
	mutex_unlock(&p->fg->lock);
	return 0;
}

static int fn_program(struct fg_device *fg, u32 address, size_t length,
		      const u8 *buffer, size_t *retlen)
{
	u8 verify[FN_FIFO_BYTES];
	int ret;
	*retlen = 0;
	while (length) {
		size_t chunk = min_t(size_t, length, FN_FIFO_BYTES);
		u32 i;
		chunk = min_t(size_t, chunk, 256 - (address & 255));
		for (i = 0; i < chunk; i++) {
			u8 old = READ_ONCE(*(const u8 *)(0x42000000UL + address + i));
			if ((~old & buffer[i]) != 0)
				return -EINVAL;
		}
		ret = fn_execute(fg, FN_PROGRAM, address, chunk, buffer, NULL);
		if (ret)
			return ret;
		/* Check independent SPI1 readback and the invalidated XIP alias. */
		ret = fn_execute(fg, FN_READ, address, chunk, NULL, verify);
		if (ret || memcmp(buffer, verify, chunk) ||
		    memcmp(buffer, (const void *)(0x42000000UL + address), chunk)) {
			fg->nor_rw = false;
			return ret ? ret : -EIO;
		}
		address += chunk;
		buffer += chunk;
		length -= chunk;
		*retlen += chunk;
	}
	return 0;
}

static int fn_write(struct mtd_info *mtd, loff_t to, size_t length,
		    size_t *retlen, const u_char *buffer)
{
	struct fn_partition *p = container_of(mtd, struct fn_partition, mtd);
	int ret;
	*retlen = 0;
	if (to < 0 || to > mtd->size || length > mtd->size - to)
		return -EINVAL;
	mutex_lock(&p->fg->lock);
	ret = p->fg->nor_rw ? fn_program(p->fg, p->offset + to, length, buffer, retlen) : -EROFS;
	mutex_unlock(&p->fg->lock);
	return ret;
}

static int fn_erased(u32 address)
{
	u32 i;
	const u32 *data = (const void *)(0x42000000UL + address);
	for (i = 0; i < FN_ERASE_BYTES / sizeof(*data); i++)
		if (READ_ONCE(data[i]) != 0xffffffffU)
			return -EIO;
	return 0;
}

static int fn_erase(struct mtd_info *mtd, struct erase_info *info)
{
	struct fn_partition *p = container_of(mtd, struct fn_partition, mtd);
	u64 off;
	int ret = 0;
	if (info->addr > mtd->size || info->len > mtd->size - info->addr ||
	    (info->addr | info->len) & (FN_ERASE_BYTES - 1))
		return -EINVAL;
	mutex_lock(&p->fg->lock);
	if (!p->fg->nor_rw) {
		ret = -EROFS;
		goto out;
	}
	for (off = info->addr; off < info->addr + info->len; off += FN_ERASE_BYTES) {
		ret = fn_execute(p->fg, FN_ERASE, p->offset + off, 0, NULL, NULL);
		if (!ret)
			ret = fn_erased(p->offset + off);
		if (ret) {
			info->fail_addr = off;
			p->fg->nor_rw = false;
			break;
		}
	}
out:
	mutex_unlock(&p->fg->lock);
	return ret;
}

static ssize_t fn_identify_store(struct device *dev, const char *buf, size_t count)
{
	struct fg_device *fg = dev_get_drvdata(dev);
	int ret;
	mutex_lock(&fg->lock);
	fg->nor_id_ok = false;
	ret = fn_execute(fg, FN_IDENTIFY, 0, 0, NULL, NULL);
	mutex_unlock(&fg->lock);
	if (!ret)
		dev_dbg(dev, "SPI1 read-only identification: JEDEC=%06x supported=%u; writes locked=%u\n",
			fg->nor_jedec, fg->nor_id_ok, !fg->nor_rw);
	return ret ? ret : count;
}

static ssize_t nor_state_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct fg_device *fg = dev_get_drvdata(dev);
	struct fn_task __iomem *t = fg->nor + FN_TASK;
	ssize_t ret;
	mutex_lock(&fg->lock);
	ret = sysfs_emit(buf, "jedec=%06x supported=%u scratch_passed=%u rw_enabled=%u flash_operations=%u operation=%u address=%08x length=%u result=%u commands=%u changed=%u cache_syncs=%u spi_restored=%u status_before=%02x status_after=%02x code_bytes=%u preflight_reg=%08x preflight_value=%08x\n",
		fg->nor_jedec, fg->nor_id_ok, fg->nor_scratch_ok, fg->nor_rw,
		fg->flash_operations, readl(&t->operation), readl(&t->address),
		readl(&t->length), readl(&t->result), readl(&t->commands),
		readl(&t->changed), readl(&t->cache_syncs), readl(&t->restored),
		readl(&t->status_before), readl(&t->status_after), fg->nor_bytes,
		readl(&t->preflight_register), readl(&t->preflight_value));
	mutex_unlock(&fg->lock);
	return ret;
}
static DEVICE_ATTR_RO(nor_state);

static ssize_t scratch_test_store(struct device *dev, struct device_attribute *attr,
				 const char *buf, size_t count)
{
	struct fg_device *fg = dev_get_drvdata(dev);
	struct fn_partition *part = fg->nor_partitions;
	u8 pattern[FN_FIFO_BYTES];
	u32 address, i;
	size_t written;
	int ret = kstrtou32(buf, 0, &address);
	if (ret || address != FN_SCRATCH)
		return -EINVAL;
	mutex_lock(&fg->lock);
	fg->nor_scratch_ok = false;
	if (!fg->nor_id_ok || fg->nor_rw || part[0].users || part[1].users) {
		ret = -EBUSY;
		goto out;
	}
	/* Explicitly destructive manual gate. Operator backs up this exact
	 * 64-KiB interval before requesting it; never run during probe/boot. */
	ret = fn_execute(fg, FN_ERASE, address, 0, NULL, NULL);
	if (!ret)
		ret = fn_erased(address);
	for (i = 0; i < sizeof(pattern); i++)
		pattern[i] = (i * 37U) ^ 0xa5;
	if (!ret)
		ret = fn_program(fg, address + 1, sizeof(pattern), pattern, &written);
	if (!ret)
		ret = fn_program(fg, address + 0xf0, sizeof(pattern), pattern, &written);
	if (!ret)
		ret = fn_execute(fg, FN_ERASE, address, 0, NULL, NULL);
	if (!ret)
		ret = fn_erased(address);
	fg->nor_scratch_ok = !ret;
out:
	mutex_unlock(&fg->lock);
	dev_info(dev, "manual NOR scratch at %08x: result=%d; rw remains locked\n", address, ret);
	return ret ? ret : count;
}
static DEVICE_ATTR_WO(scratch_test);

static ssize_t rw_enable_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct fg_device *fg = dev_get_drvdata(dev);
	return sysfs_emit(buf, "%u\n", READ_ONCE(fg->nor_rw));
}

static ssize_t rw_enable_store(struct device *dev, struct device_attribute *attr,
			      const char *buf, size_t count)
{
	struct fg_device *fg = dev_get_drvdata(dev);
	int ret = 0;
	mutex_lock(&fg->lock);
	if (sysfs_streq(buf, "0"))
		fg->nor_rw = false;
	else if (fg->nor_id_ok && ((sysfs_streq(buf, "1") && fg->nor_scratch_ok) ||
		 sysfs_streq(buf, "resume")))
		fg->nor_rw = true;
	else
		ret = -EPERM;
	mutex_unlock(&fg->lock);
	return ret ? ret : count;
}
static DEVICE_ATTR_RW(rw_enable);

static int fn_get_device(struct mtd_info *mtd)
{
	struct fn_partition *p = container_of(mtd, struct fn_partition, mtd);
	mutex_lock(&p->fg->lock);
	p->users++;
	mutex_unlock(&p->fg->lock);
	return 0;
}

static void fn_put_device(struct mtd_info *mtd)
{
	struct fn_partition *p = container_of(mtd, struct fn_partition, mtd);
	mutex_lock(&p->fg->lock);
	if (p->users)
		p->users--;
	mutex_unlock(&p->fg->lock);
}

static void fn_unregister(void *data)
{
	struct fn_partition *part = data;
	mtd_device_unregister(&part[1].mtd);
	mtd_device_unregister(&part[0].mtd);
}

static int fn_probe(struct platform_device *pdev, struct fg_device *fg)
{
	struct resource *res = platform_get_resource(pdev, IORESOURCE_MEM, 1);
	struct fn_partition *part;
	u32 i;
	int ret;
	static_assert(FN_TASK + sizeof(struct fn_task) <= FN_BYTES);
	if (!res || res->start != FN_BASE || resource_size(res) != FN_BYTES)
		return -EINVAL;
	fg->nor = devm_ioremap_resource(&pdev->dev, res);
	if (IS_ERR(fg->nor))
		return PTR_ERR(fg->nor);
	fg->nor_bytes = esp32s3_nor_blob_end - esp32s3_nor_blob_start;
	fg->nor_entry_offset = esp32s3_nor_entry - esp32s3_nor_blob_start;
	if (!fg->nor_bytes || fg->nor_bytes > FN_TASK || fg->nor_entry_offset >= fg->nor_bytes)
		return -E2BIG;
	memcpy_toio(fg->nor, esp32s3_nor_blob_start, fg->nor_bytes);
	for (i = 0; i < fg->nor_bytes; i++)
		if (readb(fg->nor + i) != esp32s3_nor_blob_start[i])
			return -EIO;
	asm volatile("memw\n\tisync" ::: "memory");
	memset_io(fg->nor + FN_TASK, 0, sizeof(struct fn_task));
	part = devm_kcalloc(&pdev->dev, 2, sizeof(*part), GFP_KERNEL);
	if (!part)
		return -ENOMEM;
	fg->nor_partitions = part;
	for (i = 0; i < 2; i++) {
		struct mtd_info *mtd = &part[i].mtd;
		part[i].fg = fg;
		part[i].offset = i ? FN_HOME_BASE : FN_ETC_BASE;
		mtd->name = i ? "home" : "etc";
		mtd->size = i ? FN_HOME_BYTES : FN_ETC_BYTES;
		mtd->type = MTD_NORFLASH;
		mtd->flags = MTD_CAP_NORFLASH;
		mtd->erasesize = FN_ERASE_BYTES;
		mtd->writesize = 1;
		mtd->writebufsize = FN_FIFO_BYTES;
		mtd->owner = THIS_MODULE;
		mtd->dev.parent = &pdev->dev;
		mtd->_read = fn_read;
		mtd->_write = fn_write;
		mtd->_erase = fn_erase;
		mtd->_get_device = fn_get_device;
		mtd->_put_device = fn_put_device;
		ret = mtd_device_register(mtd, NULL, 0);
		if (ret) {
			if (i)
				mtd_device_unregister(&part[0].mtd);
			return ret;
		}
	}
	return devm_add_action_or_reset(&pdev->dev, fn_unregister, part);
}
