#!/usr/bin/env python3
"""Use small, releasable NOMMU libc heap extents on the 8 MiB S3 target."""
from pathlib import Path
import sys
p=Path(sys.argv[1])/'libc/stdlib/malloc/malloc.h'
s=p.read_text()
if '# define MALLOC_HEAP_EXTEND_SIZE\t(2*MALLOC_PAGE_SIZE)' in s:
 print('NOMMU small-heap policy already applied');raise SystemExit(0)
changes={
 '# define MALLOC_HEAP_EXTEND_SIZE\t65536':'# define MALLOC_HEAP_EXTEND_SIZE\t(2*MALLOC_PAGE_SIZE)',
 '#define MALLOC_UNMAP_THRESHOLD\t(8*MALLOC_PAGE_SIZE)':'#ifdef __ARCH_USE_MMU__\n#define MALLOC_UNMAP_THRESHOLD\t(8*MALLOC_PAGE_SIZE)\n#else\n#define MALLOC_UNMAP_THRESHOLD\tMALLOC_PAGE_SIZE\n#endif',
 '#define MALLOC_MIN_SIZE\t\t(2*MALLOC_PAGE_SIZE)':'#ifdef __ARCH_USE_MMU__\n#define MALLOC_MIN_SIZE\t\t(2*MALLOC_PAGE_SIZE)\n#else\n#define MALLOC_MIN_SIZE\t\tMALLOC_PAGE_SIZE\n#endif'}
for before,after in changes.items():
 assert s.count(before)==1,('unexpected libc source',before)
 s=s.replace(before,after)
if p.is_symlink():p.unlink()
p.write_text(s)
print('Applied 8 KiB NOMMU growth, 4 KiB release threshold/reserve; MMU policy unchanged')
