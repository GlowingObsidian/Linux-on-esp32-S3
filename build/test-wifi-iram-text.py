#!/usr/bin/env python3
"""Exercise actual kernel text helpers with the SRAM profile enabled/disabled."""
from pathlib import Path
import subprocess
import sys
import tempfile

p = Path(sys.argv[1])
arch = (p / 'sections.h').read_text()
generic = (p / 'generic-sections.h').read_text()
start = arch.index('#define arch_is_kernel_text(addr)')
macro = arch[start:arch.index('#endif', start)]

def function(name):
    pos = generic.index(name + '(')
    start = generic.rfind('\n', 0, pos) + 1
    end = generic.index('{', pos) + 1
    depth = 1
    while depth:
        depth += (generic[end] == '{') - (generic[end] == '}')
        end += 1
    return generic[start:end] + '\n'

shim = '''#include <stdbool.h>
#include <assert.h>
#include <stdio.h>
#define _stext ((char *)0x42140000UL)
#define _etext ((char *)0x42400000UL)
#define _end ((char *)0x42500000UL)
#define __init_begin ((char *)0x3d820000UL)
#define __init_end ((char *)0x3d830000UL)
#define _esp32_wifi_iram_start ((char *)0x40390000UL)
#define _esp32_wifi_iram_end ((char *)0x40398400UL)
'''
for enabled in [False, True]:
    code = shim + (macro if enabled else '#define arch_is_kernel_text(addr) false\n')
    code += function('__is_kernel_text') + function('__is_kernel')
    code += '''int main(void) {
        assert(__is_kernel_text(0x42140000UL));
        assert(!__is_kernel_text(0x42400000UL));
        assert(__is_kernel(0x42400000UL));
        assert(__is_kernel(0x3d820000UL));
        assert(!__is_kernel(0x3d830000UL));
        assert(!__is_kernel_text(0x4038ffffUL));
        assert(!__is_kernel_text(0x40398400UL));
        assert(!__is_kernel_text(0x40399000UL));
        assert(!__is_kernel(0x60000000UL));
'''
    code += f'''assert(__is_kernel_text(0x40390000UL) == {int(enabled)});
        assert(__is_kernel_text(0x403983ffUL) == {int(enabled)});
        assert(__is_kernel(0x40390000UL) == {int(enabled)});
        puts("PASS: normal text/init bounds and exact SRAM text bounds; profile {'on' if enabled else 'off'}");
    }}'''
    with tempfile.TemporaryDirectory() as temp:
        src = Path(temp) / 'text.c'
        exe = Path(temp) / 'text'
        src.write_text(code)
        subprocess.run(['cc', '-std=gnu11', '-fsanitize=address,undefined', str(src), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
