#!/usr/bin/env python3
# Copyright (c) 2026 Paulneja. GPLv3, see LICENSE. https://github.com/paulneja/Linux-on-esp32-S3
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

work = Path(sys.argv[1]).resolve(strict=True)
if work != Path('/work'):
    raise SystemExit('Run inside the isolated build container')
repo = work / 'Linux-on-esp32-S3'
build = work / 'refs/esp32-linux-build/build'
host = build / 'build-buildroot-arduino_nano_esp32/host'
out = work / 'artifacts'
experiment = repo / 'experiments/mmu-poc/out'
firmware = build / 'esp-hosted/esp_hosted_ng/esp/esp_driver/network_adapter/build'
board = repo / 'new-files/board/espressif/esp32s3'

# Provenance, read here rather than at the point of use because
# /etc/esp-image-version is written into the rootfs long before the rest of the
# manifest is assembled. Both files are produced by build/reproduce.sh before
# the container starts.
source_commit = (work / 'source-commit.txt').read_text().strip()
container_id = (work / 'container-image-id.txt').read_text().strip()

# The release this image belongs to, read out of CHANGELOG.md instead of
# written here. That file is already where a release number is decided --
# build/plot-releases.py parses the same headings -- so a literal here would be
# a second copy to keep in step, and the one thing a version string must not do
# is disagree with the changelog it came from.
release = re.search(r'^## (\d[\w.+-]*)\b', (repo / 'CHANGELOG.md').read_text(), re.M)
assert release, 'CHANGELOG.md has no "## <version>" heading to name this image with'
version = release.group(1)

def run(*args):
    subprocess.run([str(arg) for arg in args], check=True)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def elf_stack_size(path):
    """PT_GNU_STACK p_memsz for a 32-bit little-endian ELF, or 0 if absent."""
    data = path.read_bytes()
    if len(data) < 52 or data[:4] != b'\x7fELF' or data[4] != 1 or data[5] != 1:
        return 0
    phoff, = struct.unpack_from('<I', data, 28)
    phentsize, phnum = struct.unpack_from('<HH', data, 42)
    for n in range(phnum):
        base = phoff + n * phentsize
        if base + 32 > len(data):
            break
        p_type, = struct.unpack_from('<I', data, base)
        if p_type == 0x6474e551:          # PT_GNU_STACK
            p_memsz, = struct.unpack_from('<I', data, base + 20)
            return p_memsz
    return 0

parts = {}
for line in (repo / 'new-files/esp-hosted/network_adapter/partition_table.esp32s3.16m8r').read_text().splitlines():
    if not line or line.startswith('#'): continue
    fields = [field.strip() for field in line.split(',')]
    parts[fields[0]] = {'offset': int(fields[3], 0), 'size': int(fields[4], 0)}

# The kernel's partitions come from the device tree now, not from the table in
# the flash: the firmware that used to serve that read is gone, so the kernel
# is told the layout instead of parsing it. That makes the two independent, and
# a disagreement between them is silent -- the kernel mounts whatever sits at
# the offset the DT names, which is how "Cannot open root device" happens with
# a partition table that looks perfectly correct. So compare them here.
#
# The DT is read out of the built xipImage rather than out of the source, so
# this compares what the kernel will actually use against what gets flashed.
# It walks the flattened tree properly -- header, struct block, strings block --
# rather than pattern-matching bytes, because a regex that silently matches
# nothing would make this check pass on exactly the input it exists to catch.
def dtb_partitions(image):
    found = {}
    pos = 0
    while True:
        pos = image.find(b'\xd0\x0d\xfe\xed', pos)
        if pos < 0:
            break
        hdr = image[pos:pos + 40]
        if len(hdr) < 40:
            break
        magic, total, off_struct, off_strings = struct.unpack_from('>IIII', hdr, 0)
        # A real DTB is small and self-consistent; this skips the false
        # positives the magic turns up inside ordinary data.
        if not (0 < total <= len(image) - pos):
            pos += 4
            continue
        blob = image[pos:pos + total]
        structs = blob[off_struct:off_strings]
        strings = blob[off_strings:]

        def string_at(off):
            end = strings.find(b'\x00', off)
            return strings[off:end].decode('ascii', 'replace')

        i, path, props = 0, [], {}
        while i + 4 <= len(structs):
            token = struct.unpack_from('>I', structs, i)[0]
            i += 4
            if token == 1:                      # FDT_BEGIN_NODE
                end = structs.find(b'\x00', i)
                path.append(structs[i:end].decode('ascii', 'replace'))
                i = (end + 4) & ~3
                props.setdefault(tuple(path), {})
            elif token == 2:                    # FDT_END_NODE
                if path:
                    path.pop()
            elif token == 3:                    # FDT_PROP
                length, nameoff = struct.unpack_from('>II', structs, i)
                i += 8
                props.setdefault(tuple(path), {})[string_at(nameoff)] = \
                    structs[i:i + length]
                i = (i + length + 3) & ~3
            else:                               # FDT_END or malformed
                break

        for node, prop in props.items():
            if 'label' in prop and len(prop.get('reg', b'')) == 8:
                label = prop['label'].split(b'\x00')[0].decode()
                offset, size = struct.unpack('>II', prop['reg'])
                found.setdefault(label, {'offset': offset, 'size': size})
        pos += 4
    return found


# The check that the device tree and the partition table agree lives further
# down, after the kernel image has been copied into artifacts/. It has to: it
# reads the DTB out of that image, and reading it here would read whatever the
# previous build left behind -- which is a check that passes on a stale file
# and fails on a correct one, the exact opposite of what it is for.

elf = (firmware / 'network_adapter.elf').read_bytes()
shoff, = struct.unpack_from('<I', elf, 0x20)
shsize, shnum = struct.unpack_from('<HH', elf, 0x2e)
sections = [struct.unpack_from('<10I', elf, shoff + n * shsize) for n in range(shnum)]
vectors = None
for _, kind, _, _, offset, size, link, _, _, entsize in sections:
    if kind != 2: continue
    names = elf[sections[link][4]:sections[link][4] + sections[link][5]]
    for pos in range(offset, offset + size, entsize):
        name, address = struct.unpack_from('<II', elf, pos)
        if names[name:].split(b'\0', 1)[0] == b'space_for_vectors': vectors = address
config = (experiment / 'linux-fork/.config').read_text()
match = re.search(r'^CONFIG_VECTORS_ADDR=(0x[0-9a-fA-F]+)$', config, re.M)
assert match and vectors == int(match[1], 16), 'Firmware/kernel vector mismatch'
load = re.search(r'^CONFIG_KERNEL_LOAD_ADDRESS=(0x[0-9a-fA-F]+)$', config, re.M)
assert load, 'kernel has no CONFIG_KERNEL_LOAD_ADDRESS'
expected_load = 0x42000000 + parts['linux']['offset']
assert int(load[1], 16) == expected_load, (
    'kernel is linked at %s but the linux partition starts at %s. Moving the '
    'partition means moving CONFIG_KERNEL_LOAD_ADDRESS with it, or the board '
    'loops in the bootloader before Linux prints anything.'
    % (load[1], hex(expected_load)))
assert 'CONFIG_XTENSA_NOMMU_FORK=y' in config
assert '# CONFIG_XTENSA_VARIANT_MMU is not set' in config
assert not re.search(r'^CONFIG_MMU=[ym]$', config, re.M)
autoconf = (experiment / 'linux-fork/include/generated/autoconf.h').read_text()
assert not re.search(r'^#define CONFIG_MMU\s', autoconf, re.M)
run('python3', repo / 'experiments/mmu-poc/fork/check-kernel-config.py',
    '--require-fork',
    board / 'nano_esp32_linux.config', experiment / 'linux-fork/.config')

# Staged in the container's own /tmp, deliberately not under `work`. `work` is
# a bind mount from the build host, and Docker Desktop's macOS file sharing
# silently drops setuid and setgid on bind mounts: chmod 4755 there reads back
# 0755. Staged under `work`, the SUID bit on /bin/busybox was gone before
# mkcramfs ran, while every check that read the tree earlier in this function
# still saw it set. Container-local, the bits stick. The assertion after the
# chmod below is the guard, so a filesystem that eats setuid fails here rather
# than shipping.
with tempfile.TemporaryDirectory(prefix='final-rootfs-') as directory:
    tree = Path(directory) / 'tree'
    run(host / 'bin/cramfsck', '-x', tree, out / 'rootfs.cramfs')
    # Paths this image used to carry and no longer does. Every stage before this
    # one is add-only -- it extracts a tree, copies into it and repacks -- so a
    # file deleted from rootfs_overlay/ survives in the image extracted above and
    # ships anyway, with the build reporting success. That is how
    # usr/sbin/first-login outlived its own removal. The last repack is the one
    # place that cannot fall behind the source tree, and the assertion is what
    # turns a survivor into a failure rather than a file nobody notices.
    # retired-paths.txt says more about why.
    #
    # Done before /etc is written out to its own jffs2 partition below, so a
    # retired path under /etc leaves both that partition and the cramfs.
    retired = [line.split('#', 1)[0].strip().strip('/')
               for line in (board / 'retired-paths.txt').read_text().splitlines()]
    retired = [relative for relative in retired if relative]
    for relative in retired:
        doomed = tree / relative
        if doomed.is_symlink() or doomed.is_file():
            doomed.unlink()
        elif doomed.is_dir():
            shutil.rmtree(doomed)
    # is_symlink() as well as exists(): a dangling symlink reports absent to
    # exists(), and the path entry would still be in the image.
    survived = [relative for relative in retired
                if (tree / relative).is_symlink() or (tree / relative).exists()]
    assert not survived, 'image still carries retired paths: %s' % survived
    # The baseline cramfs can predate trim-target.sh's getty correction.
    # An empty tty1 init ID inherits /dev/console (ttyS0), stealing UART input.
    run('python3', repo / 'build/configure-consoles.py', tree / 'etc/inittab')
    run('python3', repo / 'build/install-default-tools.py', tree,
        experiment / 'programs/default-tools', board / 'rootfs_overlay')
    passwd = tree / 'etc/passwd'
    entries = passwd.read_text().splitlines()
    roots = [n for n, entry in enumerate(entries) if entry.startswith('root:')]
    assert len(roots) == 1
    fields = entries[roots[0]].split(':')
    fields[-2:] = ['/home/root', '/usr/bin/user-shell']
    # A cached cramfs can predate the move away from a separate /home image.
    # The login account's declared home must exist in the final rootfs.
    (tree / 'home/root').mkdir(parents=True, exist_ok=True, mode=0o700)
    entries[roots[0]] = ':'.join(fields)
    passwd.write_text('\n'.join(entries) + '\n')
    # This image ships no web server, and www-data exists only to run one, so
    # trim-target.sh removes the account it inherits from buildroot's skeleton.
    # Asserted here because the account is invisible from inside the running
    # system whenever nothing tries to log in as it.
    assert not any(entry.startswith('www-data:') for entry in entries)
    shells = tree / 'etc/shells'
    allowed = shells.read_text().splitlines()
    if '/usr/bin/user-shell' not in allowed:
        shells.write_text('\n'.join(allowed + ['/usr/bin/user-shell']) + '\n')
    assert not (tree / 'etc/wpa_supplicant.conf').exists()
    # buildroot's board directory carries dropbear host keys that are tracked
    # in a public repository. trim-target.sh removes them so -R generates one
    # per board; this is the check that it actually happened.
    keys = sorted(str(path.relative_to(tree))
                  for path in tree.glob('etc/dropbear/*_host_key'))
    assert not keys, 'image carries dropbear private host keys: %s' % keys
    # A zero PT_GNU_STACK means the program runs on kernel.default_stack_size,
    # and on NOMMU that allocation has no guard page, so an overflow is silent
    # corruption rather than a SIGSEGV. Record which programs rely on it --
    # asserting would be wrong, because shared libraries legitimately have no
    # PT_GNU_STACK and the executable's value is what governs. Only real
    # programs are listed, so the set is small enough to read in a diff.
    stackless = sorted(
        str(path.relative_to(tree))
        for directory in ('bin', 'sbin', 'usr/bin', 'usr/sbin')
        for path in (tree / directory).glob('*')
        if path.is_file() and not path.is_symlink() and '.so' not in path.name
        and path.open('rb').read(4) == b'\x7fELF' and elf_stack_size(path) == 0)
    assert not (tree / 'usr/bin/sqlite3').exists()
    assert not (tree / 'usr/bin/sudo').exists()
    assert not (tree / 'usr/bin/doas').exists()
    assert (tree / 'bin/sh').readlink() == Path('busybox')
    (tree / 'bin/busybox').chmod(0o4755)
    assert (tree / 'bin/busybox').stat().st_mode & 0o7777 == 0o4755, (
        'bin/busybox is not setuid: the staging tree is on a filesystem that '
        'drops setuid (Docker Desktop drops setuid and setgid on macOS bind '
        'mounts). BusyBox is the one SUID binary in the image -- /bin/sh is a '
        'symlink to it -- so packing it 0755 ships an image that fails '
        'test-home-users.py and behaves wrongly for anyone who is not root.')
    (tree / 'etc/shadow').chmod(0o600)

    # The image's own identity. Nothing on an ESP32-S3 reports which board it
    # is: there is no board ID EEPROM, and a device tree is only as specific as
    # whoever wrote it. Ours does carry one -- patches/linux/0039 sets
    # model = "Arduino Nano ESP32" on the board's root node -- but the kernel
    # reads that to describe the hardware, not to name the release, and it says
    # nothing about which build of it is running. So the name and the version
    # have to travel with the image.
    # /usr/share/esp/board-facts.sh reads BOARD, VERSION and COMMIT out of this
    # file and omits any it does not find, which is what keeps `neofetch` honest
    # about never inventing a value. The login banner sources that same file, so
    # the two cannot drift into disagreeing about the same board -- though the
    # banner itself prints only the uname line, the licence notice and the
    # last-login stamp, and leaves the board's own fields to `neofetch`.
    #
    # The commit is shortened because the file's reader is somebody at a serial
    # console. The full value, and the container image id, stay in
    # build-manifest.json where they are read by machines.
    #
    # Deliberately no build timestamp: the project builds reproducibly
    # (SOURCE_DATE_EPOCH is fixed in build/container-build.sh), and a wall-clock
    # stamp here would make two builds of one tree produce two different images.
    sha_part, _, dirty = source_commit.partition('-')
    short_commit = sha_part[:7] + ('-' + dirty if dirty else '')
    (tree / 'etc/esp-image-version').write_text(
        '# Written at build time by build/package-final.py. Editing it on the\n'
        '# board is undone by the next flash, which is the point of it being\n'
        '# generated from the sources rather than typed.\n'
        'BOARD=Arduino Nano ESP32\n'
        'VERSION=%s\n'
        'COMMIT=%s\n' % (version, short_commit))
    # -x zlib -x rtime is load-bearing.  mkfs.jffs2 compresses by default, and
    # the firmware's own jffs2 reader (tiny_jffs2_reader.c) returns a node's raw
    # bytes without decompressing -- "compression is not supported ATM".  A
    # compressed /etc/board.dtb therefore reaches parse_fdt() as a zlib stream
    # whose first four bytes are not d0 0d fe ed, and the board silently boots
    # the tree compiled into xipImage instead.  Any file over one node (~4 KiB)
    # in this partition is affected; board.dtb is 7 KB.
    # No etc.jffs2 and no home.jffs2. There is no etc or home partition any
    # more: /etc and /home are part of the read-only cramfs, so there is
    # nothing to build a separate jffs2 image for. The mkfs.jffs2 calls that
    # used to be here are gone with the partitions.
    run(host / 'bin/mkcramfs', '-X', '-q', tree, out / 'rootfs.cramfs')
    run(host / 'bin/cramfsck', out / 'rootfs.cramfs')

for name in ('bootloader.bin', 'partition-table.bin', 'network_adapter.bin'):
    shutil.copyfile(work / 'base-images' / name, out / name)

# The binary table is what the board reads; the CSV is what everything here
# computes from. They have disagreed before, and the kernel derives the rootfs
# XIP address from the table, so a mismatch panics with "Cannot open root
# device" rather than failing the build.
table = (out / 'partition-table.bin').read_bytes()
installed = {}
for pos in range(0, len(table) - 31, 32):
    magic, _, _, offset, size, label, _ = struct.unpack_from('<HBBII16sI', table, pos)
    if magic != 0x50AA:
        break
    installed[label.split(b'\0', 1)[0].decode('ascii')] = {'offset': offset, 'size': size}
assert installed == parts, (
    'partition-table.bin does not match partition_table.esp32s3.16m8r:\n'
    '  table: %s\n  csv:   %s' % (installed, parts))
shutil.copyfile(experiment / 'real-bins/xipImage-fork-quiet', out / 'xipImage')

# Now that the kernel this build produced is in artifacts/, check that the
# partitions it carries in its device tree are the ones the flash will be laid
# out with. This has to come after the copy above: reading out/xipImage before
# it would read the previous build's kernel, which is a check that passes on a
# stale file and fails on a correct one -- the opposite of what it is for.
dt_parts = dtb_partitions((out / 'xipImage').read_bytes())
assert dt_parts, 'no fixed-partitions found in the device tree in xipImage'
assert dt_parts == parts, (
    'the device tree partitions and partition_table.esp32s3.16m8r disagree:\n'
    '  DT:  %s\n  CSV: %s\n'
    'The kernel mounts what the DT describes, so a mismatch means it reads the '
    'wrong region.' % (dt_parts, parts))

files = {'factory': 'network_adapter.bin', 'linux': 'xipImage',
         'rootfs': 'rootfs.cramfs'}
for partition, filename in files.items():
    assert (out / filename).stat().st_size <= parts[partition]['size'], (partition, filename)

run('esptool', '--chip', 'esp32s3', 'merge_bin', '-o', out / 'linux-esp32s3-native-full.bin',
    '--flash_mode', 'dio', '--flash_freq', '80m', '--flash_size', '16MB', '--fill-flash-size', '16MB',
    '0x0', out / 'bootloader.bin', '0x8000', out / 'partition-table.bin',
    *[arg for partition, filename in files.items() for arg in (hex(parts[partition]['offset']), out / filename)])
full = (out / 'linux-esp32s3-native-full.bin').read_bytes()
assert len(full) == 16 * 1024 * 1024
for offset, filename in ((0, 'bootloader.bin'), (0x8000, 'partition-table.bin')):
    payload = (out / filename).read_bytes()
    assert full[offset:offset + len(payload)] == payload, filename
for partition, filename in files.items():
    payload = (out / filename).read_bytes()
    start = parts[partition]['offset']
    assert full[start:start + len(payload)] == payload
# No factory /home to check: /home is inside the cramfs now, so the region the
# old assertion covered does not exist. What replaced it is the cramfs check
# below, which is the filesystem the board actually mounts.
manifest = json.loads((out / 'rootfs.json').read_text())
manifest.update(image_bytes=(out / 'rootfs.cramfs').stat().st_size,
                free_bytes=parts['rootfs']['size'] - (out / 'rootfs.cramfs').stat().st_size,
                sha256=sha(out / 'rootfs.cramfs'))
(out / 'rootfs.json').write_text(json.dumps(manifest, indent=2) + '\n')
checksums = {name: sha(out / name) for name in ['bootloader.bin', 'partition-table.bin', *files.values(), 'linux-esp32s3-native-full.bin']}
(out / 'SHA256SUMS').write_text(''.join(value + '  ' + name + '\n' for name, value in checksums.items()))
inventory = {
    'source_commit': source_commit,
    'container_image_id': container_id,
    'vectors_addr': hex(vectors), 'partitions': parts, 'sha256': checksums,
    'build_method': 'Pinned source builds; cached toolchain and unchanged program builds may be reused',
    'kernel_input_fingerprint': (experiment / 'linux-fork/.fork-kernel-inputs.sha256').read_text().strip(),
    'board_verification': 'pending',
    'default_stack_binaries': stackless,
}
configurations = {
    'toolchain.config': build / 'crosstool-NG/.config',
    'buildroot.config': build / 'build-buildroot-arduino_nano_esp32/.config',
    'firmware.config': firmware.parent / 'sdkconfig',
    'kernel.config': experiment / 'linux-fork/.config',
    'busybox.config': experiment / 'programs/busybox-netcat/.config',
}
(out / 'configs').mkdir(exist_ok=True)
inventory['configuration_sha256'] = {}
for name, path in configurations.items():
    shutil.copyfile(path, out / 'configs' / name)
    inventory['configuration_sha256'][name] = sha(path)
(out / 'build-manifest.json').write_text(json.dumps(inventory, indent=2) + '\n')
shutil.copyfile(repo / 'build/sources.lock', out / 'sources.lock')
print(json.dumps(inventory, indent=2))
