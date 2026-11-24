#!/usr/bin/env python3
"""Final gates for the combined native-NOR and SD-free flash-state candidate."""
from pathlib import Path
import argparse
import binascii
import hashlib
import json
import re
import struct

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('artifacts',type=Path)
b=p.parse_args().artifacts
r=Path(__file__).resolve().parent.parent
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

image=(b/'linux.bin').read_bytes()
dt=(b/'board.dtb').read_bytes()
assert len(image)<=0x3d0000 and dt in image
header=struct.unpack_from('>10I',dt)
assert header[0]==0xd00dfeed
pos,strings,stack,nodes=header[2],header[3],[],{}
while True:
    token=struct.unpack_from('>I',dt,pos)[0];pos+=4
    if token==1:
        end=dt.index(0,pos);stack.append(dt[pos:end].decode());pos=(end+4)&~3
    elif token==2: stack.pop()
    elif token==3:
        length,noff=struct.unpack_from('>II',dt,pos);pos+=8
        end=dt.index(0,strings+noff)
        name=dt[strings+noff:end].decode()
        nodes.setdefault('/'+'/'.join(stack[1:]),{})[name]=dt[pos:pos+length]
        pos=(pos+length+3)&~3
    elif token==4: continue
    elif token==9: break
    else: raise AssertionError(token)
args=nodes['/chosen']['bootargs'].rstrip(b'\0').decode()
assert 'root=mtd:rootfs rootfstype=cramfs ro' in args
assert not any(word.startswith('init=') or word.startswith('rdinit=') for word in args.split())
assert nodes['/flash-guard@3fcc8000']['reg']==struct.pack('>4I',0x3fcc8000,0x2000,0x3fcc7000,0x1000)
assert nodes['/wifi_blob']['reg']==struct.pack('>II',0x3fcce800,0xdf00)
assert nodes['/wifi_blob']['status']==b'okay\0'
assert not any(b'mmc-spi-slot' in v.get('compatible',b'') for v in nodes.values())
assert nodes['/soc/flash@42000000/partitions/partition@510000']['reg']==struct.pack('>II',0x510000,0x6f0000)
cfg=(b/'kernel.config').read_text()
for symbol in ('CONFIG_SMP=y','CONFIG_XTENSA_NOMMU_FORK=y','CONFIG_ESP32_WIFI_BLOB=y',
               'CONFIG_MTD_ESP32_FLASH_GUARD_TEST=y','CONFIG_MTD_ESP32S3_NOR=y',
               'CONFIG_CRAMFS_MTD=y','CONFIG_JFFS2_FS=y','CONFIG_JFFS2_CMODE_NONE=y'):
    assert symbol in cfg,symbol
assert 'CONFIG_ESP32_GDMA=y' not in cfg and 'CONFIG_ESP32_WIFI_SHMEM=y' not in cfg
for symbol in ('CONFIG_MMC','CONFIG_MMC_BLOCK','CONFIG_MMC_SPI'):
    assert not re.search(r'^'+symbol+r'=[ym]$',cfg,re.M),symbol
assert 'CONFIG_BUILTIN_DTB_NAME="arduino-nano-esp32-flash-rw"' in cfg
for line in (b/'kernel-inputs.sha256').read_text().splitlines():
    digest,path=line.split(maxsplit=1)
    path=Path(path.strip().replace('/work/Linux-on-esp32-S3',str(r)))
    assert sha(path)==digest,('stale kernel input',path)

proof=[]
for path in (b/'vmlinux',r/'build-output/wifi-smp-audit/base/firmware.elf'):
    elf=path.read_bytes();ph=struct.unpack_from('<I',elf,28)[0]
    psz,n=struct.unpack_from('<HH',elf,42)
    for i in range(n):
        typ,off,va,pa,fs,ms,flags,align=struct.unpack_from('<8I',elf,ph+i*psz)
        if typ!=1 or not ms: continue
        ranges=[(va,va+ms)]
        lo,hi=max(va,0x40378000),min(va+ms,0x403e0000)
        if lo<hi:ranges.append((lo-0x6f0000,hi-0x6f0000))
        for lo,hi in ranges:
            assert not(lo<0x3fcca000 and hi>0x3fcc7000),('SRAM overlap',path,hex(lo),hex(hi))
    proof.append(dict(file=str(path),sha256=sha(path)))

symbols={v[2]:int(v[0],16) for line in (b/'System.map').read_text().splitlines() if len(v:=line.split())>=3}
elf=(b/'vmlinux').read_bytes()
ph=struct.unpack_from('<I',elf,28)[0];psz,n=struct.unpack_from('<HH',elf,42)
templates={}
for name,limit in (('esp32_flash_guard_template',0x100),('esp32_flash_cache_template',0xd00)):
    low,high=symbols[name+'_start'],symbols[name+'_end']
    assert 0<high-low<=limit
    for i in range(n):
        typ,off,va,pa,fs,ms,flags,align=struct.unpack_from('<8I',elf,ph+i*psz)
        if typ==1 and va<=low and high<=va+fs:
            blob=elf[off+low-va:off+high-va]
            assert image[low-0x42140000:high-0x42140000]==blob
            templates[name]=dict(bytes=len(blob),sha256=hashlib.sha256(blob).hexdigest())
    assert name in templates
callback=(b/'guard-callback.dis').read_text()
mask=re.search(r'wsr\.intenable\s+a8',callback)
spill=re.search(r'\bcall8\s+',callback)
window=callback.index('rsr.windowstart');entry=callback.index('callx8')
assert mask and spill and mask.start()<spill.start()<window<entry
assert not re.search(r'\bcall(?:0|4|8|12|x0|x4|x8|x12)\b',callback[window:entry])
saved_irq_register=re.search(r'rsr\.intenable\s+(a\d+)',callback)[1]
assert re.search(r'wsr\.intenable\s+'+saved_irq_register+r'\b',callback[entry:])

meta=json.loads((b/'flash-rootfs.json').read_text())
assert sha(b/'rootfs.cramfs')==meta['rootfs_sha256']
assert (b/'rootfs.cramfs').stat().st_size==meta['rootfs_bytes']<=0x6f0000
assert meta['source_rootfs_sha256']=='8e7feaa631fa24e7c2a4ab6e10ef5740745b22d45f6133e49c83cf109e12c619'
root=b/'rootfs-check/root'
for script,dest in (('flash-init.sh','sbin/flash-state-init'),('flash-state-enable.sh','usr/sbin/flash-state-enable')):
    assert (root/dest).read_bytes()==(r/'build'/script).read_bytes()
    assert (root/dest).stat().st_mode & 0o111
assert sha(root/'bin/bash')==meta['bash_sha256'] and sha(root/'bin/busybox')==meta['busybox_sha256']
assert not (root/'etc/.flash-rw-verified').exists()
assert (root/'sbin/init').is_symlink() and (root/'sbin/init').readlink()==Path('../bin/busybox')
assert not (root/'sbin/hybrid-init').exists() and not (root/'sbin/flash-init').exists()
inittab=(root/'etc/inittab').read_text()
assert inittab.count('::sysinit:/sbin/flash-state-init\n')==1
assert inittab.index('/sbin/flash-state-init') < inittab.index('/bin/mount -a') < inittab.index('/etc/init.d/rcS')
fstab=(root/'etc/fstab').read_text()
assert 'ext4' not in fstab and 'mmcblk' not in fstab
assert not re.search(r'^\s*(auto|allow-hotplug).*wlan0', (root/'etc/network/interfaces').read_text(), re.M)

def crc(data):return binascii.crc32(data,0xffffffff)^0xffffffff
seeds=[]
for part in meta['partitions']:
    name=part['name'];data=(b/(name+'.jffs2')).read_bytes()
    offset=int(part['offset'],0)
    assert (offset,len(data))==((0xd0000,0x70000) if name=='etc' else (0xc00000,0x400000))
    assert sha(b/(name+'.jffs2'))==part['sha256']
    used=0;node_count=0;pos=0
    while pos<len(data):
        if data[pos:pos+4]==b'\xff'*4:pos+=4;continue
        magic,kind,length,hcrc=struct.unpack_from('<HHII',data,pos)
        assert magic==0x1985 and length>=12 and pos+length<=len(data)
        assert pos//65536==(pos+length-1)//65536
        node=data[pos:pos+length];assert crc(node[:8])==hcrc
        if kind==0xe002:
            csize,dsize=struct.unpack_from('<II',node,48)
            assert node[56]==0 and csize==dsize and length==68+csize
            assert crc(node[:60])==struct.unpack_from('<I',node,64)[0]
            assert crc(node[68:])==struct.unpack_from('<I',node,60)[0]
        elif kind==0xe001:
            assert length==40+node[28]
            assert crc(node[:32])==struct.unpack_from('<I',node,32)[0]
            assert crc(node[40:])==struct.unpack_from('<I',node,36)[0]
        else:assert kind in (0x2003,0x2004),(name,hex(kind))
        used+=(length+3)&~3;node_count+=1;pos+=(length+3)&~3
    assert used<len(data)-3*65536,(name,'GC reserve')
    seeds.append(dict(name=name,offset=hex(offset),bytes=len(data),seed_node_bytes=used,nodes=node_count,sha256=part['sha256']))
nor=json.loads((b/'nor-fault-tests.json').read_text())
cache=json.loads((b/'cache-fault-tests.json').read_text())
assert nor['passed']>=75 and cache['passed']>=46
host_tests=json.loads((b/'host-test-results.json').read_text())
boot_tests=json.loads((b/'flash-init-tests.json').read_text())
assert len(host_tests)==18 and all(t['passed'] for t in host_tests)
assert boot_tests['passed']==14
manifest=dict(profile='native-nor-smp-xip-etc-home',kernel_sha256=sha(b/'linux.bin'),
    kernel_bytes=len(image),kernel_offset='0x140000',kernel_margin=0x3d0000-len(image),
    rootfs_sha256=meta['rootfs_sha256'],rootfs_bytes=meta['rootfs_bytes'],rootfs_offset='0x510000',
    rootfs_margin=0x6f0000-meta['rootfs_bytes'],dtb_sha256=sha(b/'board.dtb'),
    source_patch_count=38,elf_evidence=proof,templates=templates,
    nor_template_bytes=nor['template_bytes'],nor_template_sha256=nor['template_sha256'],
    nor_instruction_scenarios=nor['passed'],cache_instruction_scenarios=cache['passed'],
    regression_suites_passed=len(host_tests),flash_boot_scenarios_passed=boot_tests['passed'],
    sram={'start':'0x3fcc7000','end':'0x3fcca000','bytes':12288,'runtime_verified':False},
    partitions=seeds,sd_required=False,sd_driver_disabled=True,init='/sbin/init',state_mount_hook='/sbin/flash-state-init',first_boot_writes_locked=True,
    firmware_and_partition_table_write=False,root_read_only=True,
    software_gates_passed=True,flashed=False,hardware_verified=False,
    limitations=['Exact new NOR code and SRAM range require board identification/scratch tests.',
        'Instruction models do not emulate electrical timing, cache silicon or physical flash.',
        'Back up existing flash before installing seeds or requesting the explicit destructive scratch test.',
        'Persistence, reboot without SD and operation under peripheral/network load remain hardware gates.',
        'The call0 object intentionally has a different ELF ABI; only the verified internal callx0 bridge calls it.'])
(b/'integration-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(f'PASS: exact images/inputs, SRAM ownership, IRQ boundary, SD-free root, uncompressed JFFS2 CRCs and GC reserve; kernel margin {manifest["kernel_margin"]}, rootfs margin {manifest["rootfs_margin"]} bytes')
