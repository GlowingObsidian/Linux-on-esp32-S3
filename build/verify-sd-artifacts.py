#!/usr/bin/env python3
"""Verify the built SD profile, embedded DT, image layout and retained userspace."""
from pathlib import Path
import hashlib
import json
import struct
import sys

root=Path(__file__).resolve().parent.parent
art=Path(sys.argv[1]) if len(sys.argv)>1 else root/'build-output/sd-rootfs-poc'
profile=root/'new-files/board/espressif/esp32s3/sd-rootfs'
def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for x in iter(lambda:f.read(1024*1024),b''):h.update(x)
 return h.hexdigest()
def dtprops(blob):
 magic,total,off,strings=struct.unpack_from('>4I',blob)
 assert magic==0xd00dfeed and total==len(blob)
 names=blob[strings:];p=off;path=[];props={}
 while p+4<=strings:
  token=struct.unpack_from('>I',blob,p)[0];p+=4
  if token==1:
   end=blob.index(0,p);path.append(blob[p:end].decode());p=(end+4)&~3
   props.setdefault('/'.join(path),{})
  elif token==2:path.pop()
  elif token==3:
   length,name=struct.unpack_from('>II',blob,p);p+=8
   key=names[name:names.index(0,name)].decode()
   props['/'.join(path)][key]=blob[p:p+length];p=(p+length+3)&~3
  elif token==4:continue
  elif token==9:break
  else:raise AssertionError(token)
 return props
conf={}
for line in (art/'kernel.config').read_text().splitlines():
 if line.startswith('CONFIG_'):
  k,v=line.split('=',1);conf[k]=v
for k in ('SMP','XTENSA_NOMMU_FORK','MMC','MMC_BLOCK','MMC_SPI','EXT2_FS','NOMMU_EXEC_DROP_FILE_CACHE'):
 assert conf.get('CONFIG_'+k)=='y',k
for k in ('ESP32_GDMA','ESP32_REGS','FB','FB_TFT','VT','ESP32_WIFI_BLOB'):
 assert conf.get('CONFIG_'+k,'n')=='n',k
assert conf['CONFIG_NR_CPUS']=='2'
assert conf['CONFIG_KERNEL_LOAD_ADDRESS']=='0x42140000'
hybrid=(art/'root-mode').exists() and (art/'root-mode').read_text().strip()=='hybrid'
pio=(art/'transfer-mode').exists() and (art/'transfer-mode').read_text().strip()=='pio'
expected_dtb='arduino-nano-esp32-hybrid-pio' if hybrid else ('arduino-nano-esp32-sd-pio' if pio else 'arduino-nano-esp32-sd')
assert conf['CONFIG_BUILTIN_DTB_NAME']=='"'+expected_dtb+'"'
if hybrid:
 assert pio and conf.get('CONFIG_CRAMFS_MTD')=='y'
 assert conf.get('CONFIG_EXT4_FS')=='y' and conf.get('CONFIG_JBD2')=='y'
kernel=(art/'linux.bin').read_bytes();dtb=(art/'board.dtb').read_bytes()
assert kernel.count(dtb)==1,'DTB must be embedded in the packaged kernel'
props=dtprops(dtb)
boot=props['/chosen']['bootargs'].rstrip(b'\0').decode()
words=('root=mtd:rootfs','rootfstype=cramfs','ro','init=/sbin/hybrid-init','console=ttyS0,115200n8') if hybrid else ('root=/dev/mmcblk0p1','rootfstype=ext2','rootwait','rw','console=ttyS0,115200n8')
for word in words:
 assert word in boot.split(),word
assert 'tty1' not in boot and 'fbcon' not in boot
assert 'rootflags=' not in boot, 'initial rootflags are filesystem options, not generic mount flags'
assert not any('panel@' in path for path in props)
spi=props['/soc/spi@60024000']
if pio:
 assert struct.unpack('>2I',spi['reg'])==(0x60024000,0x100)
 assert spi['reg-names']==b'spi\0'
 assert spi['clock-names']==b'spi\0' and spi['reset-names']==b'reset\0'
else:
 assert struct.unpack('>6I',spi['reg'])==(0x60024000,0x100,0x6003f000,0x1000,0x3fccc700,0x2040)
assert struct.unpack('>3I',spi['cs-gpios'])[1:]==(21,1)
assert struct.unpack('>3I',spi['interrupts'])==(21,9,0)
mmc=props['/soc/spi@60024000/mmc@0']
assert mmc['compatible']==b'mmc-spi-slot\0'
assert struct.unpack('>I',mmc['spi-max-frequency'])[0]==20000000
parts={}
for line in (profile/('partitions-hybrid.csv' if hybrid else 'partitions.csv')).read_text().splitlines():
 if not line.strip() or line.startswith('#'):continue
 fields=[x.strip() for x in line.split(',')]
 parts[fields[0]]=(int(fields[3],0),int(fields[4],0))
dtparts={p['label'].rstrip(b'\0').decode():struct.unpack('>2I',p['reg']) for path,p in props.items() if path.startswith('/soc/flash@42000000/partitions/partition@')}
assert parts==dtparts and (('rootfs' in parts)==hybrid),(parts,dtparts)
assert parts['linux'][0]==0x140000 and len(kernel)<=parts['linux'][1]
if hybrid:
 image=art/'rootfs.cramfs';manifest=json.loads((art/'hybrid-rootfs.json').read_text())
 assert digest(image)==manifest['rootfs_sha256']
 assert image.read_bytes()[:4]==b'\x45\x3d\xcd\x28'
 assert parts['rootfs']==(0x510000,0x6f0000) and image.stat().st_size<=parts['rootfs'][1]
 assert 'read-only' in props['/soc/flash@42000000/partitions/partition@510000']
 table=(art/'partition-table.bin').read_bytes();encoded={}
 for off in range(0,len(table),32):
  magic,typ,sub,start,size,label,flags=struct.unpack_from('<HBBII16sI',table,off)
  if magic!=0x50aa:break
  encoded[label.rstrip(b'\0').decode()]=(start,size)
 assert encoded==parts,(encoded,parts)
 names=('linux.bin','board.dtb','rootfs.cramfs','partition-table.bin')
 if manifest.get('journal_required'):
  data_manifest=json.loads((art/'sd-data.json').read_text())
  data_image=art/'sd-data.img'
  assert digest(data_image)==data_manifest['sd_image_sha256']
  assert data_image.stat().st_size==data_manifest['image_bytes']==939524096
  with data_image.open('rb') as f:
   mbr=f.read(512)
   assert mbr[510:512]==b'\x55\xaa' and mbr[450]==0x83
   start,count=struct.unpack_from('<II',mbr,454)
   assert start==2048 and (start+count)*512==data_image.stat().st_size
   f.seek(start*512+1024);sb=f.read(1024)
  assert struct.unpack_from('<H',sb,56)[0]==0xef53
  assert struct.unpack_from('<H',sb,58)[0]&1
  assert struct.unpack_from('<I',sb,92)[0]&4  # journal enabled
  assert struct.unpack_from('<I',sb,100)[0]&0x400  # metadata checksums
  assert struct.unpack_from('<I',sb,224)[0]==8  # internal journal inode
  manifest['sd_data']=data_manifest
  readback=art/'card-readback.sha256'
  manifest['sd_card_readback_verified']=readback.exists() and readback.read_text().split()[0]==data_manifest['sd_image_sha256']
  names+=('sd-data.img',)
else:
 image=art/'sd-rootfs.img';manifest=json.loads((art/'sd-rootfs.json').read_text())
 assert digest(image)==manifest['sd_image_sha256']
 with image.open('rb') as f:
  mbr=f.read(512);assert mbr[510:512]==b'\x55\xaa'
  typ=mbr[450];start,count=struct.unpack_from('<II',mbr,454)
  assert typ==0x83 and start==2048 and (start+count)*512==image.stat().st_size
  f.seek(start*512+1024+56);assert f.read(2)==b'\x53\xef'
 readback=art/'card-readback.sha256'
 manifest['sd_card_readback_verified']=readback.exists() and readback.read_text().split()[0]==manifest['sd_image_sha256']
 names=('linux.bin','board.dtb','sd-rootfs.img')
manifest['transfer_mode']='pio-baseline' if pio else 'dma-with-pio-recovery'
manifest['flash_verified']=False
manifest['boot_verified']=False
manifest.update(kernel_sha256=digest(art/'linux.bin'),kernel_bytes=len(kernel),
                board_dtb_sha256=digest(art/'board.dtb'),bootargs=boot,
                software_verified=True,hardware_verified=False)
(art/'integration-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
(art/'SHA256SUMS').write_text(''.join(f'{digest(art/name)}  {name}\n' for name in names))
print('PASS: SMP/fork config, embedded DTB, GPIO CS, 20 MHz, IRQ routing, root mount, partition layout and image hashes')
