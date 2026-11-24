#!/usr/bin/env python3
"""Exercise the actual NOMMU copy path and its bounded cache-reclaim policy."""
from pathlib import Path
import subprocess,sys,tempfile
s=Path(sys.argv[1]).read_text()
def function(name):
 i=s.index(name+'(');a=s.rfind('\n',0,i)+1;b=s.index('{',i);d=1;e=b+1
 while d:d+=(s[e]=='{')-(s[e]=='}');e+=1
 return s[a:e]
shim=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
typedef unsigned gfp_t;
#define PAGE_SHIFT 12
#define PAGE_ALLOC_COSTLY_ORDER 3
#define GFP_KERNEL 1U
#define __GFP_RETRY_MAYFAIL 2U
#define VM_EXEC 1U
#define VM_WRITE 2U
#define VM_SHARED 4U
#define VM_MAPPED_COPY 8U
#define NOMMU_MAP_DIRECT 1U
#define IS_ENABLED(x) policy
#define WARN_ON_ONCE(x) (x)
#define vm_flags_set(v,f) ((v)->vm_flags|=(f))
#define is_nommu_shared_mapping(f) ((f)&VM_SHARED)
#define vma_set_anonymous(v) ((void)(v))
#define atomic_long_add(n,p) (*(p)+=(n))
#define pr_err(...) ((void)0)
#define show_mem() ((void)0)
static bool policy=true,fail_alloc;
static long mmap_pages_allocated;
static unsigned sysctl_nr_trim_pages=1,allocs,reads,drains,evictions,frees;
static unsigned last_gfp;
static unsigned long first_index,last_index;
static int read_result=-9999,direct_result;
static unsigned char ram[2*1024*1024];
struct task { int pid; const char *comm; } task={1,"test"};
#define current (&task)
struct file { void *f_mapping; };
struct vm_region { unsigned long vm_flags,vm_start,vm_end,vm_top; };
struct vm_area_struct { struct file *vm_file; struct vm_region *vm_region; unsigned long vm_flags,vm_pgoff,vm_start,vm_end; };
static int get_order(unsigned long len) { unsigned n=0;while((4096UL<<n)<len)n++;return n; }
static void *alloc_pages_exact(unsigned long len,gfp_t gfp) {
 assert(len<=sizeof(ram));allocs++;last_gfp=gfp;return fail_alloc?NULL:ram;
}
static int mmap_file(struct file *f,struct vm_area_struct *v) {(void)f;(void)v;return direct_result;}
static int kernel_read(struct file *f,void *dst,unsigned long len,long long *pos) {
 (void)f;assert(*pos==3*4096);reads++;
 int n=read_result==-9999?(int)len:read_result;
 if(n>=0)memset(dst,0x69,n);return n;
}
static void free_page_series(unsigned long start,unsigned long end) {assert(end>start);frees++;}
static void lru_add_drain(void) {assert(reads);drains++;}
static unsigned long invalidate_mapping_pages(void *mapping,unsigned long first,unsigned long last) {
 assert(mapping==(void*)0x1234 && reads && drains && read_result!=-EIO);
 evictions++;first_index=first;last_index=last;return last-first+1;
}
#define loff_t long long
'''
tests=r'''
int main(void) {
 struct file file={(void*)0x1234};struct vm_region region={0};
 struct vm_area_struct vma={.vm_file=&file,.vm_region=&region,.vm_flags=VM_EXEC,.vm_pgoff=3};
 memset(ram,0xa5,sizeof(ram));
 assert(do_mmap_private(&vma,&region,733184,0)==0);
 assert(last_gfp==(GFP_KERNEL|__GFP_RETRY_MAYFAIL));
 assert(allocs==1 && reads==1 && evictions==1 && first_index==3 && last_index==181);
 assert(vma.vm_flags&VM_MAPPED_COPY && ram[733183]==0x69);
 puts("PASS: costly immutable text gets bounded reclaim, then evicts only its copied file range");
 unsigned ev=evictions;
 vma.vm_flags=VM_EXEC|VM_WRITE;
 assert(do_mmap_private(&vma,&region,733184,0)==0 && last_gfp==GFP_KERNEL && evictions==ev);
 vma.vm_flags=VM_EXEC|VM_SHARED;
 assert(do_mmap_private(&vma,&region,733184,0)==0 && last_gfp==GFP_KERNEL && evictions==ev);
 vma.vm_flags=VM_EXEC;vma.vm_file=NULL;
 assert(do_mmap_private(&vma,&region,733184,0)==0 && last_gfp==GFP_KERNEL && evictions==ev);
 vma.vm_file=&file;policy=false;vma.vm_flags=VM_EXEC;
 assert(do_mmap_private(&vma,&region,733184,0)==0 && last_gfp==GFP_KERNEL && evictions==ev);
 policy=true;vma.vm_flags=VM_EXEC|VM_SHARED;direct_result=0;
 unsigned before=allocs;
 assert(do_mmap_private(&vma,&region,4096,NOMMU_MAP_DIRECT)==0 && allocs==before && evictions==ev);
 puts("PASS: writable/shared/anonymous, disabled policy and direct XIP preserve existing allocation behavior");
 vma.vm_flags=VM_EXEC;read_result=512;memset(ram,0xa5,4096);
 assert(do_mmap_private(&vma,&region,4096,0)==0 && last_gfp==GFP_KERNEL);
 assert(ram[511]==0x69 && ram[512]==0 && ram[4095]==0 && evictions==ev+1);
 ev=evictions;read_result=-EIO;
 assert(do_mmap_private(&vma,&region,4096,0)==-EIO && frees==1 && evictions==ev);
 assert(region.vm_top==0 && region.vm_start==0 && vma.vm_start==0);
 fail_alloc=true;before=reads;
 assert(do_mmap_private(&vma,&region,733184,0)==-ENOMEM && reads==before && evictions==ev);
 puts("PASS: short reads zero-fill; read/allocation failures retain error handling and never evict source data");
}
'''
with tempfile.TemporaryDirectory(prefix='nommu-exec-memory-') as tmp:
 binary=str(Path(tmp)/'test')
 subprocess.run(['cc','-std=gnu17','-Wall','-Wextra','-Werror','-Wno-sign-compare','-fsanitize=address,undefined','-x','c','-','-o',binary],input=shim+function('nommu_reclaim_exec_cache')+function('do_mmap_private')+tests,text=True,check=True)
 subprocess.run([binary],check=True)
