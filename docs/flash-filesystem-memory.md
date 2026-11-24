# Writable flash and internal SRAM: measured memory costs

2026-11-17. JFFS2 is the practical first filesystem for these small raw-NOR
partitions: it is already enabled and avoids adding UBI or a new filesystem
port. This is not a claim that it uses the least RAM under every workload.
The measurements below are target-compiled type sizes and source formulas;
total resident RAM and persistence still require board measurements.

Only new research files were created. No kernel configuration, memory bank,
partition table, filesystem, cached build stamp or board state was changed.

## Reproducible target measurements

Artifacts and exact source snapshots are under
`build-output/flash-rw-research/fs-memory-probe/`. `measurements.json` records
object/source/configuration SHA256 hashes; `symbol-sizes.txt` records the ELF
symbol sizes. `fsprobe.c` and the zlib probes were built against the actual
configured Linux 7.2.4 Xtensa headers using the preserved toolchain, with the
configured kernel volume mounted read-only. No target code was executed.

Current relevant configuration: 32-bit pointers, 4-KiB pages, FLATMEM,
JFFS2 built-in, `JFFS2_FS_WRITEBUFFER=y`, `JFFS2_CMODE_NONE=y`, zlib/rtime
enabled, summary/xattrs disabled, UBI disabled. Sizes depend on these settings.

| Target object or constant | Bytes |
| --- | ---: |
| `struct page` | 32 |
| `jffs2_raw_node_ref` | 8 |
| `jffs2_inode_cache` | 24 |
| `jffs2_eraseblock` | 52 |
| `jffs2_full_dirent` | 24, plus name allocation |
| `jffs2_full_dnode` | 16 |
| `jffs2_node_frag` | 24 |
| `jffs2_sb_info` | 400 |
| JFFS2 inode object, including its VFS inode | 376 |
| VFS superblock | 784 |
| GC task structure | 1,280 |
| Kernel stack for each GC task | 8,192 |
| Raw-ref allocation block: 30 usable refs plus link slot | 248 |
| JFFS2 zlib inflate workspace | 42,284 |
| JFFS2 zlib deflate workspace if enabled | 267,980 |

These are requested object sizes, not slab/page allocation sizes. Do not add
the VFS inode again to the JFFS2 inode object. Slab rounding, per-CPU caches,
thread bookkeeping, dentries, instantiated inodes, file page cache and temporary
scan/write/GC buffers add costs. Each writable mount creates its own GC task.

## Erase geometry changes both RAM and usable flash

The proposed `/etc` is 448 KiB (`0x070000`), and `/home` is 4.75 MiB
(`0x4c0000`). The driver must expose an erase geometry it actually implements;
filesystem images must use the same geometry. These are comparison scenarios,
not a change to the present MTD driver.

| Region | Erase size | Blocks | Eraseblock array | Refblocks if each block is cleanmarked/referenced | Write reserve |
| --- | ---: | ---: | ---: | ---: | ---: |
| `/etc` | 64 KiB | 7 | 364 B | 1,736 B | 192 KiB, 3 blocks |
| `/home` | 64 KiB | 76 | 3,952 B | 18,848 B | 256 KiB, 4 blocks |
| `/etc` | 4 KiB | 112 | 5,824 B | 27,776 B | 28 KiB, 7 blocks |
| `/home` | 4 KiB | 1,216 | 63,232 B | 301,568 B | 228 KiB, 57 blocks |

Across both partitions the eraseblock arrays request 4,316 B at 64 KiB versus
69,056 B at 4 KiB. The illustrative one-refblock-per-marked-block scenario adds
20,584 B versus 329,344 B. Smaller eraseblocks therefore have a substantial RAM
cost even before the file-node population is considered. The refblock column
is a conditional scenario, not a universal mount measurement: all-erased blocks,
dirty regions and the actual node distribution affect allocations.

The reason is visible in the actual source: each eraseblock has its own ref
chain, and its first required reference allocates a 248-byte block even if only
one of its 30 usable entries is needed. In-band cleanmarkers written after
erasure obtain such a reference. Additional refs scale with live and obsolete
on-flash nodes, not just live file sizes. Directory names and fragmentation
also add variable costs.

Both mounts use a minimum 128-entry inode-cache hash table: 512 B each.
The source computes the normal-write reserve as:

```
2 + ceil((floor(flash_size / 50) + nr_eraseblocks * 100) / erase_size)
```

This reserves flash space for deletion and GC progress; it is not a RAM
allocation. With 64-KiB blocks, `/etc` has seven blocks, clears the five-block
mount minimum, but has roughly 256 KiB available above its 192-KiB normal-write
reserve, before nodes and cleanmarkers. Verify that the desired `/etc` content
and update workload fit. Keeping large blocks on `/home` is attractive for RAM.
Using smaller blocks for `/etc` could recover capacity at a memory cost, if the
native MTD design supports separate geometries. For example, 4-KiB `/etc` plus
64-KiB `/home` adds 31,500 B of array/refblock requests relative to 64 KiB on
both, under the same marked-block assumption, while reducing `/etc`'s reserve
by 164 KiB. That tradeoff needs workload data rather than a blanket rule.

Source snapshots: `fs/jffs2/nodelist.h:87-105,277-298`, `malloc.c:46-56,168-217`,
`erase.c:419-460`, `scan.c:619-624`, `build.c:311-341,374-390`,
`fs.c:494-511,539-566`, `background.c:35-58`.

## Zlib is a global cost already present in the baseline

JFFS2's `inf_strm` and `def_strm` are static globals, initialized once when the
filesystem implementation is registered. They are **not per-mount workspaces**.
The configured local `compr_zlib.c` patch already skips allocating the 267,980-B
deflate workspace when `CMODE_NONE` is selected, and rejects a forced zlib write
compression path when that workspace is absent. It retains inflate so existing
compressed factory data can be read.

The inflate request is 42,284 B, with at least 45,056 B (44 KiB) of page-rounded
backing allocation. It exists even with no JFFS2 mount, so do not charge it twice
when comparing the incremental cost of `/etc` and `/home`. The existing
uncompressed-write configuration saves approximately 262 KiB of deflate requests
relative to enabling that global workspace; that saving has already occurred.

A new image containing only uncompressed/rtime nodes could potentially omit
JFFS2 zlib and its inflate workspace. This requires proving that no preserved
filesystem data needs zlib decompression; a mount option does not change the
compression of existing nodes. No compressed data or zlib support was removed.

Ordinary byte-programmable NOR with `MTD_BIT_WRITEABLE` uses no JFFS2 writebehind
buffer. Enabling `JFFS2_FS_WRITEBUFFER` compiles support and enlarges control
structures, but does not itself allocate a NAND/OOB buffer on ordinary NOR.
The native MTD flags must describe the chip/program restrictions honestly:
ECC'd NOR/DataFlash/UBI/NAND paths have different setup. Disabling writebuffer
support may reduce code and small per-mount structures, but is not a large
buffer saving for this raw-NOR path.

Source snapshots: `fs/jffs2/compr_zlib.c:31-77,87-96,215-233`,
`super.c:363-407`, `compr.c:365-388`, `os-linux.h:75-142`,
`fs.c:684-739`, `lib/zlib_inflate/inflate.c:28-30`,
`lib/zlib_inflate/infutil.h:22-29`, `lib/zlib_deflate/deflate.c:103-116,1130-1143`,
`lib/zlib_deflate/defutil.h:243-255`.

## Why UBIFS or littlefs is not an immediate replacement

UBIFS plus UBI is valuable for larger flash: its index can avoid keeping every
raw node reference in RAM, and UBI handles mapping and wear management. It adds
the UBI mapping/wear tables, a physical-eraseblock scratch buffer and UBI thread,
plus UBIFS per-mount state, journal heads, tree caches, a background thread and
two LEB-sized buffers on a writable mount. The configured sources request
`sbuf=vmalloc(leb_size)` and `ileb_buf=vmalloc(leb_size)`, and UBI requests
`peb_buf=vmalloc(peb_size)`. Those alone make it unsuitable to assume lower RAM
for a small, lightly populated filesystem. A workload benchmark is still needed
before making a general JFFS2/UBIFS RAM claim.

There is also a concrete geometry blocker for this `/etc`: UBIFS requires at
least 17 logical eraseblocks by its minimum constants (runtime requirements can
be higher). Seven 64-KiB physical blocks fail even before UBI reservations.
4-KiB physical eraseblocks cannot meet UBIFS's 15-KiB minimum logical block
size. Making a different virtual erase geometry or changing the partitioning
would be new work and would still require accounting for UBI overhead.

littlefs is designed for embedded storage with bounded caches, but there is no
littlefs filesystem implementation in this pinned kernel's `fs/` tree. A new
kernel port, FUSE/userspace bridge or other integration would have its own
process/cache/error-recovery cost and needs native-MTD compatibility tests.
It is a research alternative, not an already available low-RAM Linux switch.

Source snapshots: `fs/ubifs/ubifs-media.h:52-53,222-262`,
`fs/ubifs/super.c:509-561,818-862,1243-1268`, `fs/ubifs/io.c:1145-1161`,
`drivers/mtd/ubi/build.c:994,1025`, `drivers/mtd/ubi/vtbl.c:490`.

## Internal SRAM can help, but a normal bank currently costs more metadata

The example aligned candidate `0x3fc95000..0x3fcc8000` contains 204 KiB.
Its ownership was not established for general allocations; it is only an
arithmetic example. SRAM cache regions, vectors, firmware/ROM state, Wi-Fi,
SPI/DMA and the guard must remain outside any general bank. Only the correct
DRAM alias should be described as memory, never both aliases.

The earlier report measured 230.25 KiB of extra FLATMEM page metadata using
the unrounded high-address span. The actual allocator rounds the node end up
to `MAX_ORDER_NR_PAGES=1024` pages, or 4 MiB, from current
`ARCH_FORCE_MAX_ORDER=10`. Thus the candidate end rounds to `0x40000000`.
Extending the current PSRAM map end from `0x3e000000` requests **256 KiB more
page metadata**, exceeding the example 204-KiB bank by 52 KiB before any other
cost. This refines the previous unrounded estimate.

Generic `free_unused_memmap()` exists, but immediately returns unless
`CONFIG_HAVE_ARCH_PFN_VALID` is enabled, and that capability is absent in this
configured Xtensa kernel. Its default FLATMEM `pfn_valid()` treats the span as
valid; simply enabling reclamation without replacing validity/ownership logic
is unsafe. Xtensa does not select `ARCH_SPARSEMEM_ENABLE`, so sparse memory is
not an existing configuration option. DISCONTIGMEM is not offered by this
pinned memory-model menu either. NOMMU's identity address mapping alone does
not solve this metadata issue.

A future correct PFN-validity and hole-reclamation implementation could free
much of the gap's metadata. With the current 4-MiB pageblock alignment, the
arithmetic example would reclaim 224 KiB, retaining 32 KiB of incremental
metadata; that would leave roughly 172 KiB of net bank gain before ownership
and allocator costs. This is a design estimate, not implemented behavior, and
the initial 256-KiB allocation would still need to succeed. Sparse-memory
support or adjusted pageblock geometry would be a separate port/refactor.
The bank's own 51 pages need only 1,632 B of metadata in an ideal non-span model,
showing that the address hole is the expensive part.

For the present task, explicitly owned SRAM pools for the guard and necessary
DMA/radio allocations deliver a benefit without expanding FLATMEM. A general
SRAM bank is viable research once ownership and metadata reclamation are proven;
it should not be added as an unmeasured RAM optimization.

Source snapshots: `.config:252,396,404`, `include/linux/mmzone.h:30-45,2241-2260`,
`mm/mm_init.c:1639-1676`, `mm/memblock.c:2154-2230,2403`,
`mm/Kconfig:380-431`, `arch/xtensa/mm/init.c:40-64`,
`arch/xtensa/include/asm/page.h:20-31,139-172`. TRM ownership/alias findings
remain in `docs/writable-flash-research.md` and its saved TRM extracts.

## Board measurements still required

Measure the same boot image before and after each writable mount, first empty,
then seeded with the real `/etc` and `/home` data, then after repeated updates,
GC and reboot. Record `MemAvailable`, `Slab`, `SReclaimable`, `SUnreclaim`, page
cache, the JFFS2 slab caches where exposed, and the GC threads. Compare steady
state and mount/GC peaks under the same workload; file contents, node history
and erase geometry must be recorded. Only those results can establish the
system's actual minimum-memory storage profile.
