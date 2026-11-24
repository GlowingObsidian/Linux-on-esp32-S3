#!/usr/bin/env python3
"""Verify a patch stack in scratch, updating only files whose bytes change.

Avoid rewriting common headers on incremental builds when adding one patch.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('kernel',type=Path)
p.add_argument('patches',nargs='+',type=Path)
a=p.parse_args()
files=set()
for patch in a.patches:
 for line in patch.read_text().splitlines():
  if line.startswith(('--- a/','+++ b/')):
   path=Path(line[6:].split('\t')[0])
   assert not path.is_absolute() and '..' not in path.parts
   files.add(path)
with tempfile.TemporaryDirectory(prefix='esp32-patch-stack-') as temp:
 stage=Path(temp)
 for name in files:
  src=a.kernel/name;dst=stage/name
  if src.exists():
   dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)
 def apply(patch,*flags,check=True):
  return subprocess.run(['git','-C',str(stage),'apply',*flags,str(patch.resolve())],
                        check=check,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
 for patch in reversed(a.patches):
  if apply(patch,'--reverse','--check',check=False).returncode==0:
   apply(patch,'--reverse')
 for patch in a.patches:
  apply(patch,'--check');apply(patch)
 changed=[]
 for name in sorted(files):
  src=stage/name;dst=a.kernel/name
  if src.exists():
   data=src.read_bytes()
   if not dst.exists() or dst.read_bytes()!=data:
    dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(data);changed.append(str(name))
  elif dst.exists():dst.unlink();changed.append(str(name))
 print(f'Verified {len(a.patches)} patches; updated {len(changed)} source files')
 for name in changed:print('  '+name)
