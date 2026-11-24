#!/usr/bin/env python3
"""Select measured SD userspace candidates without removing installed tools."""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import os
import shutil
import struct


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_in_root(root, name):
    """Resolve image symlinks against the image root, including absolute links."""
    pending = list(PurePosixPath('/' + str(name).lstrip('/')).parts[1:])
    current = PurePosixPath('/')
    links = 0
    while pending:
        part = pending.pop(0)
        if part == '..':
            current = current.parent
            continue
        if part == '.':
            continue
        path = root / str(current / part).lstrip('/')
        if path.is_symlink():
            links += 1
            if links > 40:
                raise ValueError('Symlink loop')
            target = PurePosixPath(os.readlink(path))
            if target.is_absolute():
                current = PurePosixPath('/')
                pending = list(target.parts[1:]) + pending
            else:
                pending = list(target.parts) + pending
        else:
            current /= part
    return root / str(current).lstrip('/')


def load_bytes(path):
    data = path.read_bytes()
    assert data[:6] == b'\x7fELF\x01\x01'
    assert struct.unpack_from('<H', data, 18)[0] == 94
    offset = struct.unpack_from('<I', data, 28)[0]
    size, count = struct.unpack_from('<HH', data, 42)
    return sum(struct.unpack_from('<8I', data, offset + i * size)[5]
               for i in range(count)
               if struct.unpack_from('<I', data, offset + i * size)[0] == 1)


def optimize(root, candidates):
    root = root.resolve()
    full = resolve_in_root(root, '/bin/busybox')
    bash = resolve_in_root(root, '/bin/bash')
    assert full.is_file() and bash.is_file()
    assert bash.relative_to(root).as_posix() in ('bin/bash', 'usr/bin/bash')
    full_sha = digest(full)
    baseline_bash_sha = digest(bash)
    old_bytes = load_bytes(full) + load_bytes(bash)
    old_bytes += load_bytes(resolve_in_root(root, '/usr/lib/libncurses.so.6.4'))
    for name, report in [('busybox-core-measure.bin', 'core-busybox-result.json'),
                         ('bash-static-terminal', 'static-terminal-result.json')]:
        assert digest(candidates / name) == json.loads((candidates / report).read_text())['sha256']
    new_bytes = load_bytes(candidates / 'busybox-core-measure.bin')
    new_bytes += load_bytes(candidates / 'bash-static-terminal')
    assert new_bytes < old_bytes
    core = root / 'bin/busybox-core'
    assert not core.exists() and not core.is_symlink()
    shutil.copyfile(candidates / 'busybox-core-measure.bin', core)
    core.chmod(0o755)
    backup = root / 'usr/libexec/bash-original'
    backup.parent.mkdir(parents=True, exist_ok=True)
    assert not backup.exists() and not backup.is_symlink()
    shutil.copy2(bash, backup)
    shutil.copyfile(candidates / 'bash-static-terminal', bash)
    bash.chmod(0o755)
    changed = []
    for name in ('sbin/init', 'sbin/getty', 'bin/login', 'usr/bin/login',
                 'sbin/syslogd', 'sbin/klogd', 'usr/sbin/inetd', 'bin/sh'):
        path = root / name
        if path.is_symlink() and resolve_in_root(root, name) == full:
            path.unlink()
            path.symlink_to(os.path.relpath(core, path.parent))
            changed.append(name)
    assert {'sbin/init', 'bin/sh'} <= set(changed)
    assert digest(full) == full_sha and digest(backup) == baseline_bash_sha
    result = {
        'profile': 'resident-busybox-core-and-static-terminal-bash',
        'hardware_verified': False,
        'full_busybox_retained': True,
        'full_busybox_sha256': full_sha,
        'original_bash_backup': '/usr/libexec/bash-original',
        'original_bash_sha256': baseline_bash_sha,
        'bash_path': '/' + bash.relative_to(root).as_posix(),
        'bash_sha256': digest(bash),
        'busybox_core_sha256': digest(core),
        'service_symlinks': changed,
        'old_loaded_segment_bytes': old_bytes,
        'new_loaded_segment_bytes': new_bytes,
        'potential_loaded_segment_saving_bytes': old_bytes - new_bytes,
        'networking_configuration_changed': False,
    }
    (root / 'etc/sd-memory-profile.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('tree', type=Path)
    p.add_argument('candidates', type=Path)
    p.add_argument('--report', type=Path, required=True)
    args = p.parse_args()
    result = optimize(args.tree, args.candidates)
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
