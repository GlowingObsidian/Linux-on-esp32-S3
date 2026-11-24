#!/usr/bin/env python3
"""Check the first gated Wi-Fi candidate against the working storage layout."""
from pathlib import Path
import hashlib,json,struct,argparse
p=argparse.ArgumentParser();p.add_argument('artifacts',type=Path);a=p.parse_args()
r=Path(__file__).resolve().parent.parent;b=a.artifacts
image=(b/'linux.bin').read_bytes();dt=(b/'board.dtb').read_bytes()
assert len(image)<=0x3d0000 and dt in image,'image size or embedded DT mismatch'
header=struct.unpack_from('>10I',dt);assert header[0]==0xd00dfeed
pos=header[2];strings=header[3];stack=[];nodes={}
while True:
 token=struct.unpack_from('>I',dt,pos)[0];pos+=4
 if token==1:
  end=dt.index(0,pos);name=dt[pos:end].decode();stack.append(name);pos=(end+4)&~3
 elif token==2:stack.pop()
 elif token==3:
  length,noff=struct.unpack_from('>II',dt,pos);pos+=8
  end=dt.index(0,strings+noff);name=dt[strings+noff:end].decode()
  nodes.setdefault('/'+'/'.join(stack[1:]),{})[name]=dt[pos:pos+length];pos=(pos+length+3)&~3
 elif token==4:continue
 elif token==9:break
 else:raise AssertionError(token)
args=nodes['/chosen']['bootargs'].rstrip(b'\0').decode()
assert 'root=mtd:rootfs rootfstype=cramfs ro init=/sbin/hybrid-init' in args
assert nodes['/wifi_blob']['status']==b'okay\0'
start,size=struct.unpack('>II',nodes['/wifi_blob']['reg']);end=start+size
assert (start,size)==(0x3fcce800,0xdf00)
assert start>=0x3fccc700+0x2040 and end<=0x3fcdc700
assert nodes['/soc/flash@42000000/partitions/partition@510000']['reg']==struct.pack('>II',0x510000,0x6f0000)
spi=next(n for n in nodes if n.endswith('/spi@60024000'))
assert nodes[spi]['reg']==struct.pack('>II',0x60024000,0x100),'PIO baseline changed'
config=(b/'kernel.config').read_text()
for symbol in ['CONFIG_SMP=y','CONFIG_XTENSA_NOMMU_FORK=y','CONFIG_ESP32_WIFI_BLOB=y','CONFIG_EXT4_FS=y','CONFIG_CRAMFS_MTD=y']:
 assert symbol in config,symbol
assert 'CONFIG_ESP32_WIFI_SHMEM=y' not in config
assert 'CONFIG_ESP32_GDMA=y' not in config
proof=[]
for elf in [b/'vmlinux',r/'build-output/wifi-smp-audit/base/firmware.elf']:
 data=elf.read_bytes();assert data[:7]==b'\x7fELF\x01\x01\x01'
 off=struct.unpack_from('<I',data,28)[0];ents,num=struct.unpack_from('<HH',data,42)
 for n in range(num):
  typ,fo,vaddr,paddr,filesz,memsz,flags,al=struct.unpack_from('<8I',data,off+n*ents)
  if typ!=1 or not memsz:continue
  x,y=vaddr,vaddr+memsz
  if 0x40370000<=x<0x403e0000:x-=0x6f0000;y-=0x6f0000
  assert not (x<end and y>start),f'SRAM overlap: {elf} {x:x}..{y:x}'
 proof.append({'file':str(elf),'sha256':hashlib.sha256(data).hexdigest()})
base=json.loads((r/'build-output/hybrid-journaled-sd/integration-manifest.json').read_text())
rootfs_sha=base['rootfs_sha256']
rootfs_changed=(b/'rootfs.cramfs').exists()
if rootfs_changed:
 rootfs=(b/'rootfs.cramfs').read_bytes()
 metadata=json.loads((b/'hybrid-rootfs.json').read_text())
 rootfs_sha=hashlib.sha256(rootfs).hexdigest()
 assert rootfs_sha==metadata['rootfs_sha256'] and len(rootfs)==metadata['rootfs_bytes']<=0x6f0000
 assert metadata['source_rootfs_sha256']==base['rootfs_sha256']
manifest={'profile':'hybrid-xip-smp-wifi-station-candidate','kernel_sha256':hashlib.sha256(image).hexdigest(),
 'kernel_bytes':len(image),'partition_remaining_bytes':0x3d0000-len(image),'dtb_sha256':hashlib.sha256(dt).hexdigest(),
 'kernel_offset':'0x140000','rootfs_sha256':rootfs_sha,'rootfs_changed':rootfs_changed,
 'firmware_unchanged_sha256':hashlib.sha256((r/'build-output/smp-peripherals-2026-11-16/network_adapter.bin').read_bytes()).hexdigest(),
 'sram':{'start':hex(start),'bytes':size,'static_elf_and_spi_overlap':False},'elf_evidence':proof,
 'radio_enabled_at_boot':rootfs_changed,'automatic_network_connection':False,
 'software_gates_passed':True,'hardware_verified':False,'flashed':False,
 'limitations':['Native scan/open/WPA2-CCMP station bridge is implemented but not hardware verified; SAE/WEP are unsupported.',
 'IRQ delivery, scanning, association, DHCP, traffic and internal SRAM capacity under load require board testing.',
 'Radio initialization passed on the previous kernel; this exact image is not yet hardware tested.',
 'SPI SD DMA remains disabled; RSA unchanged.']}
(b/'integration-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(f'PASS: embedded DT, hybrid rootfs layout, SMP/forkbank, PIO isolation, SRAM overlap; kernel {len(image)} bytes, margin {0x3d0000-len(image)} bytes')
