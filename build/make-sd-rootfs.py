#!/usr/bin/env python3
"""Populate a writable ext2 SD image in Docker; never writes a physical disk."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

repo=Path(__file__).resolve().parent.parent
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--rootfs',type=Path,required=True,help='verified working CramFS to migrate')
parser.add_argument('--firmware-elf',type=Path,required=True)
parser.add_argument('--cache',type=Path,default=Path('/work/refs/esp32-linux-build/build'))
parser.add_argument('--output',type=Path,default=repo/'build-output/sd-rootfs-poc')
parser.add_argument('--scratch',type=Path,default=repo/'experiments/mmu-poc/out')
parser.add_argument('--memory-userspace',type=Path,help='verified smaller userspace candidates; original tools retained')
a=parser.parse_args()
def run(*args,**kwargs):
 return subprocess.run([str(x) for x in args],check=True,**kwargs)
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()
# No initialized firmware section may overlap the DMA pool or its IRAM alias.
elf=a.firmware_elf.read_bytes()
assert elf[:6]==b'\x7fELF\x01\x01'
phoff=struct.unpack_from('<I',elf,28)[0]
phsize,phnum=struct.unpack_from('<HH',elf,42)
pool=(0x3fccc700,0x3fcce740)
for n in range(phnum):
 typ,off,va,pa,filesz,memsz,flags,align=struct.unpack_from('<8I',elf,phoff+n*phsize)
 if typ!=1:continue
 for delta in (0,0x6f0000):
  assert va+memsz<=pool[0]+delta or va>=pool[1]+delta, f'firmware LOAD overlaps DMA SRAM: {va:#x}'
assert pool[1]<=0x3fcdc700, 'DMA SRAM crosses firmware ROM-reserved boundary'
a.output.mkdir(parents=True,exist_ok=True)
host=a.cache/'build-buildroot-arduino_nano_esp32/host'
with tempfile.TemporaryDirectory(prefix='sd-root-image-',dir=a.scratch) as temp:
 w=Path(temp);tree=w/'root'
 run(host/'bin/cramfsck','-x',tree,a.rootfs)
 for needed in ('sbin/init','bin/busybox','bin/bash','usr/bin/fork-test-dynamic','usr/bin/fork-smp-test','usr/bin/lscpu'):
  # Bash can live under /usr/bin; do not replace the working shell.
  if needed=='bin/bash' and (tree/'usr/bin/bash').exists():continue
  assert (tree/needed).exists() or (tree/needed).is_symlink(), f'missing {needed}'
 run('python3',repo/'build/configure-consoles.py','--serial-only',tree/'etc/inittab')
 memory_profile=None
 if a.memory_userspace:
  run('python3',repo/'build/optimize-sd-userspace.py',tree,a.memory_userspace,
      '--report',a.output/'memory-userspace.json')
  memory_profile=json.loads((a.output/'memory-userspace.json').read_text())
 (tree/'etc/fstab').write_text('''/dev/root / ext2 rw,noatime,noauto 0 0
proc /proc proc defaults 0 0
devpts /dev/pts devpts defaults,gid=5,mode=620,ptmxmode=0666 0 0
sysfs /sys sysfs defaults 0 0
tmpfs /run tmpfs mode=0755,nosuid,nodev 0 0
tmpfs /dev/shm tmpfs mode=1777,nosuid,nodev 0 0
''')
 (tree/'tmp').mkdir(exist_ok=True);(tree/'tmp').chmod(0o1777)
 (tree/'etc/sd-rootfs-poc').write_text('Full writable SD rootfs POC; kernel in onboard flash.\n')
 # Use part of any nominal 1 GB card without assuming its exact sector count.
 image_bytes=896*1024*1024;offset=1024*1024;fsbytes=image_bytes-offset
 filesystem=w/'rootfs.ext2'
 with filesystem.open('wb') as f:f.truncate(fsbytes)
 run('mke2fs','-q','-F','-t','ext2','-b','4096','-I','128','-m','0',
     '-O','none,filetype,sparse_super,large_file','-L','linux-root','-d',tree,filesystem)
 run('e2fsck','-f','-n',filesystem)
 # Verify contents from the finished filesystem, not just the staging tree.
 result=run('debugfs','-R','cat /etc/inittab',filesystem,capture_output=True,text=True)
 assert 'ttyS0::respawn:' in result.stdout and 'tty1::respawn:' not in result.stdout
 result=run('debugfs','-R','stat /tmp',filesystem,capture_output=True,text=True)
 assert '01777' in result.stdout, result.stdout
 if memory_profile:
  # Verify actual filesystem contents, not just the source staging directory.
  for name,key in [('bin/busybox-core','busybox_core_sha256'),
                   (memory_profile['bash_path'].lstrip('/'),'bash_sha256'),
                   ('bin/busybox','full_busybox_sha256'),
                   ('usr/libexec/bash-original','original_bash_sha256')]:
   dest=w/'verify-binary'
   if dest.exists():dest.unlink()
   run('debugfs','-R',f'dump /{name} {dest}',filesystem,capture_output=True)
   assert sha(dest)==memory_profile[key], name
  for name in memory_profile['service_symlinks']:
   result=run('debugfs','-R',f'stat /{name}',filesystem,capture_output=True,text=True)
   assert 'busybox-core' in result.stdout, name
 image=a.output/'sd-rootfs.img'
 with image.open('wb') as out,filesystem.open('rb') as src:
  out.truncate(image_bytes)
  mbr=bytearray(512)
  # Linux primary partition, LBA 2048 to end of image. CHS saturated.
  mbr[446:462]=struct.pack('<B3sB3sII',0,b'\xfe\xff\xff',0x83,b'\xfe\xff\xff',offset//512,fsbytes//512)
  mbr[510:512]=b'\x55\xaa';out.seek(0);out.write(mbr)
  # Preserve holes instead of writing almost 900 MiB of zeroes to the Mac.
  pos=0
  while pos<fsbytes:
   try:data=os.lseek(src.fileno(),pos,os.SEEK_DATA)
   except OSError as e:
    if e.errno==6:break
    raise
   end=min(os.lseek(src.fileno(),data,os.SEEK_HOLE),fsbytes)
   src.seek(data);out.seek(offset+data)
   while data<end:
    block=src.read(min(1024*1024,end-data));assert block
    out.write(block);data+=len(block)
   pos=end
 manifest={'profile':'full-sd-rootfs-poc','image_bytes':image_bytes,
   'partition_offset':offset,'filesystem':'ext2','read_only':False,'spi_max_hz':20000000,
   'dma_pool':[hex(x) for x in pool],'firmware_elf_sha256':sha(a.firmware_elf),
   'source_rootfs_sha256':sha(a.rootfs),'sd_image_sha256':sha(image),
   'display':False,'hardware_verified':False}
 if memory_profile:manifest['memory_userspace']=memory_profile
 (a.output/'sd-rootfs.json').write_text(json.dumps(manifest,indent=2)+'\n')
 print(json.dumps(manifest,indent=2))
