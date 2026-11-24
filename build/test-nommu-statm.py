#!/usr/bin/env python3
"""Execute the real statm function on non-contiguous FDPIC and shared XIP VMAs."""
from pathlib import Path
import re,subprocess,sys,tempfile
s=Path(sys.argv[1]).read_text();a=s.index('unsigned long task_statm(');b=s.index('\n/*',a);fn=s[a:b]
code=r'''
#include <assert.h>
#include <stdio.h>
#define VM_EXEC 1
#define VM_WRITE 2
#define VM_MAYSHARE 4
#define PAGE_SHIFT 12
#define PAGE_MASK (~4095UL)
#define PAGE_ALIGN(x) (((x)+4095)&PAGE_MASK)
struct vm_region {unsigned long vm_start,vm_end;};
struct vm_area_struct {unsigned long vm_start,vm_end,vm_flags;struct vm_region*vm_region;struct vm_area_struct*next;};
struct mm_struct {struct vm_area_struct*first;unsigned long start_code,end_code,start_data,start_stack;};
#define VMA_ITERATOR(v,mm,start) struct vm_area_struct*v=(mm)->first
#define for_each_vma(v,p) for((p)=(v);(p);(p)=(p)->next)
#define mmap_read_lock(mm) ((void)0)
#define mmap_read_unlock(mm) ((void)0)
#define vma_pages(v) (((v)->vm_end-(v)->vm_start)>>12)
#define is_nommu_shared_mapping(f) ((f)&VM_MAYSHARE)
#define kobjsize(p) 64UL
'''+fn+r'''
int main(void){
 struct vm_region rs[]={ {0x3da00000,0x3da04000},{0x3df00000,0x3df10000},{0x42500508,0x42520508} };
 struct vm_area_struct v[]={ {rs[0].vm_start,rs[0].vm_end,VM_WRITE,&rs[0],&v[1]}, {rs[1].vm_start,rs[1].vm_end,VM_WRITE,&rs[1],&v[2]}, {rs[2].vm_start,rs[2].vm_end,VM_EXEC|VM_MAYSHARE,&rs[2],0} };
 struct mm_struct mm={v,rs[2].vm_start,rs[2].vm_end,rs[1].vm_start,rs[0].vm_end};
 unsigned long shared=999,text=999,data=999,resident=999;
 assert(task_statm(&mm,&shared,&text,&data,&resident)==52);
 assert(resident==52&&text==32&&data==20&&shared==32);
 mm.start_stack=0x3d900000;mm.start_data=0x3df80000;
 assert(task_statm(&mm,&shared,&text,&data,&resident)==52&&data==20);
 v[1].vm_flags|=VM_EXEC;
 assert(task_statm(&mm,&shared,&text,&data,&resident)==52&&text==48&&data==20);
 mm.first=0;assert(!task_statm(&mm,&shared,&text,&data,&resident));
 assert(!shared&&!text&&!data&&!resident);
 puts("PASS actual NOMMU statm: stack-before-data, address gaps, shared XIP, RWX classification and empty mm");
}
'''
with tempfile.TemporaryDirectory()as d:
 p=Path(d);(p/'test.c').write_text(code)
 subprocess.run(['cc','-Wall','-Wextra','-fsanitize=address,undefined',str(p/'test.c'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True)
