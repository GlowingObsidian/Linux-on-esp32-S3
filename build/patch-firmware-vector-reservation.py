#!/usr/bin/env python3
"""Keep IDF code/literals above the Linux vectors and atomic guard."""
from pathlib import Path
import sys

p = Path(sys.argv[1])
s = p.read_text()
old = '  .iram0.text :\n'
new = '''  /* Linux owns 0x40379000..0x4037a000 after handoff. ROM callbacks
   * still execute firmware code, so its literals must also avoid this page. */
  .iram0.text MAX(ALIGN(0x1000), 0x4037a000) :
'''
if new in s:
    print('Firmware vector reservation already applied')
else:
    assert s.count(old) == 1, 'Unexpected IDF linker template'
    p.write_text(s.replace(old, new))
    print('Reserved Linux vector/atomic-guard page from firmware code')
