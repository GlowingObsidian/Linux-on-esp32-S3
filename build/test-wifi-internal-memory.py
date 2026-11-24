#!/usr/bin/env python3
"""Test the real OSI SRAM callbacks: bounds, exhaustion and realloc ownership."""
from pathlib import Path
import tempfile, subprocess, sys
repo=Path(__file__).resolve().parent.parent
s=Path(sys.argv[1] if len(sys.argv)>1 else repo/'build-output/wifi-smp-audit/current/esp_blob.c').read_text()
def fun(n):
 p=s.index(n+'(');a=s.rfind('\n',0,p)+1;b=s.index('{',p)+1;d=1
 while d:d+=(s[b]=='{')-(s[b]=='}');b+=1
 return s[a:b]
shim=r'''
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <assert.h>
#include <stdio.h>
typedef int64_t s64;
#define min(a,b) ((a)<(b)?(a):(b))
#define check_add_overflow __builtin_add_overflow
#define check_mul_overflow __builtin_mul_overflow
#define pr_err_ratelimited(...) ((void)0)
struct gen_pool { _Alignas(32) unsigned char mem[4096]; bool used[128]; };
static struct gen_pool pool;
static bool gen_pool_has_addr(struct gen_pool*p,unsigned long a,size_t n){
 return a>=(uintptr_t)p->mem && a+n<=(uintptr_t)p->mem+sizeof(p->mem);
}
static size_t gen_pool_size(struct gen_pool*p){return sizeof(p->mem);}
static size_t gen_pool_avail(struct gen_pool*p){size_t n=0;for(unsigned i=0;i<128;i++)n+=!p->used[i];return n*32;}
static unsigned long gen_pool_alloc(struct gen_pool*p,size_t n){
 n=(n+31)/32;for(unsigned i=0;i+n<=128;i++){
  unsigned j=0;while(j<n&&!p->used[i+j])j++;
  if(j==n){for(j=0;j<n;j++)p->used[i+j]=true;return (uintptr_t)(p->mem+i*32);}
 }return 0;
}
static void gen_pool_free(struct gen_pool*p,unsigned long a,size_t n){
 unsigned i=(a-(uintptr_t)p->mem)/32;assert((a-(uintptr_t)p->mem)%32==0);
 n=(n+31)/32;assert(i+n<=128);for(unsigned j=0;j<n;j++){assert(p->used[i+j]);p->used[i+j]=false;}
}
#define GFP_KERNEL 0
static void *kzalloc(size_t n,int flags){return calloc(1,n);}
static void *external;static size_t external_size;static s64 tracked;
static size_t ksize(void*p){assert(p==external);return external_size;}
static void kfree(void*p){assert(p==external);free(p);external=NULL;}
static void osi_heap_track(s64 n){tracked+=n;}
'''
a=s.index('static struct gen_pool *osi_internal_pool;');b=s.index('/* A zero-byte kmalloc',a)
code=shim+s[a:b]
main=r'''
int main(void){
 assert(!osi_malloc_internal(32));osi_internal_pool=&pool;
 void*p=osi_calloc_internal(3,19);assert(p && !((uintptr_t)p%32));
 for(unsigned i=0;i<57;i++)assert(!((unsigned char*)p)[i]);
 memset(p,0x5a,57);void*q=osi_realloc_internal(p,100);assert(q);
 for(unsigned i=0;i<57;i++)assert(((unsigned char*)q)[i]==0x5a);
 size_t free_before=gen_pool_avail(&pool);
 assert(!osi_realloc_internal(q,4096));assert(gen_pool_avail(&pool)==free_before);
 assert(!osi_malloc_internal(SIZE_MAX));assert(!osi_calloc_internal(SIZE_MAX,2));
 assert(!osi_realloc_internal(q,0));assert(gen_pool_avail(&pool)==4096);
 external=malloc(43);external_size=43;tracked=43;memset(external,0x3c,43);
 p=osi_realloc_internal(external,80);assert(p && !external && !tracked);
 for(unsigned i=0;i<43;i++)assert(((unsigned char*)p)[i]==0x3c);
 osi_free_internal(p);
 void*all[128];unsigned n=0;
 while((p=osi_malloc_internal(1)))all[n++]=p;
 assert(n==64 && !gen_pool_avail(&pool));
 for(unsigned i=0;i<n;i++)osi_free_internal(all[i]);
 assert(gen_pool_avail(&pool)==4096);
 puts("PASS: SRAM alignment, zeroing, overflow, exhaustion, realloc preservation and PSRAM migration");
}
'''
with tempfile.TemporaryDirectory() as d:
 src=Path(d)/'test.c';out=Path(d)/'test';src.write_text(code+main)
 subprocess.run(['cc','-std=gnu11','-fsanitize=address,undefined',str(src),'-o',str(out)],check=True)
 subprocess.run([str(out)],check=True)
