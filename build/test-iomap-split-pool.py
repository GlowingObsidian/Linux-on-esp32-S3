#!/usr/bin/env python3
"""Exercise actual iomap initialization and clone bodies with allocation failures."""
from pathlib import Path
import re, subprocess, sys, tempfile
io = Path(sys.argv[1]).read_text()
bio = Path(sys.argv[2]).read_text()
def function(text, name):
    m = re.search(r'^[^\n]*\b' + name + r'\([^;]*?\)\n\{', text, re.M)
    assert m, name
    start = text.index('{', m.start()); depth = 1; end = start + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}'); end += 1
    return text[m.start():end]
init = function(io, 'iomap_ioend_init')
clone = function(bio, 'bio_alloc_clone')
split = function(bio, 'bio_split')
assert len(re.findall(r'&iomap_ioend_split_bioset\b', io)) == 2, 'Review new split-pool users'
assert 'bio_alloc_clone(bio->bi_bdev, bio, gfp, bs)' in split
assert re.search(r'bio_alloc_bioset\(bdev,\s*0,\s*bio_src->bi_opf,\s*gfp,\s*bs\)', clone)
prelude = r'''
#include <assert.h>
#include <stddef.h>
#include <stdio.h>
#define __init
#define PAGE_SIZE 4096
#define SECTOR_SIZE 512
#define BIOSET_NEED_BVECS 1
#define ENOMEM 12
static int writeback_enabled=1;
#define CONFIG_FS_IOMAP_WRITEBACK writeback_enabled
#define IS_ENABLED(x) (x)
typedef unsigned gfp_t;
struct block_device { int id; };
struct bio { unsigned bi_opf; void *bi_io_vec; };
struct iomap_ioend { int head; struct bio io_bio; };
struct bio_set { unsigned count, flags; int ready; };
static struct bio_set iomap_ioend_bioset, iomap_ioend_split_bioset;
static unsigned calls, exits, fail_init, fail_alloc, fail_clone, puts;
static struct bio result;
static int bioset_init(struct bio_set *bs, unsigned count, unsigned pad, unsigned flags) {
    (void)pad; calls++; if (calls == fail_init) return -ENOMEM;
    bs->count=count; bs->flags=flags; bs->ready=1; return 0;
}
static void bioset_exit(struct bio_set *bs) { assert(bs->ready); bs->ready=0; exits++; }
static struct bio *bio_alloc_bioset(struct block_device *d, unsigned vecs, unsigned op, gfp_t g, struct bio_set *bs) {
    (void)d; (void)op; (void)g; assert(bs->ready && !vecs);
    return fail_alloc ? NULL : &result;
}
static int __bio_clone(struct bio *dst, struct bio *src, gfp_t g) {
    (void)g; if (fail_clone) return -ENOMEM; dst->bi_opf=src->bi_opf; return 0;
}
static void bio_put(struct bio *b) { assert(b==&result); puts++; }
'''.replace('unsigned calls, exits, fail_init, fail_alloc, fail_clone, puts;', 'unsigned calls, exits, fail_init, fail_alloc, fail_clone, put_count;').replace('puts++;','put_count++;')
checks = r'''
int main(void) {
    writeback_enabled=0; assert(iomap_ioend_init()==0); assert(!calls && !exits); writeback_enabled=1;
    assert(iomap_ioend_init()==0);
    assert(calls==2 && !exits);
    assert(iomap_ioend_bioset.count==32 && iomap_ioend_split_bioset.count==32);
    assert(iomap_ioend_bioset.flags==BIOSET_NEED_BVECS);
    assert(iomap_ioend_split_bioset.flags==0);
    unsigned vectors[8]={0}; struct bio source={.bi_opf=17,.bi_io_vec=vectors};
    struct bio *b=bio_alloc_clone(NULL,&source,0,&iomap_ioend_split_bioset);
    assert(b==&result && b->bi_io_vec==vectors && b->bi_opf==17);
    fail_alloc=1; assert(!bio_alloc_clone(NULL,&source,0,&iomap_ioend_split_bioset));
    assert(!put_count); fail_alloc=0; fail_clone=1;
    assert(!bio_alloc_clone(NULL,&source,0,&iomap_ioend_split_bioset)); assert(put_count==1);
    bioset_exit(&iomap_ioend_split_bioset); bioset_exit(&iomap_ioend_bioset);
    for (fail_init=1;fail_init<=2;fail_init++) {
        calls=exits=0; assert(iomap_ioend_init()==-ENOMEM);
        assert(exits==fail_init-1);
    }
    puts("PASS split vector sharing, zero-vector allocation, unchanged bio reserves and both initialization failure paths");
}
'''
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp); (p/'test.c').write_text(prelude+'\n'+init+'\n'+clone+'\n'+checks)
    subprocess.run(['cc','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-g',str(p/'test.c'),'-o',str(p/'test')],check=True)
    subprocess.run([str(p/'test')],check=True)
