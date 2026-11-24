#!/usr/bin/env python3
"""usr/sbin/home-init, which seeds /home/root and /etc/shells on first boot.

The web seeding this test used to cover is gone: this image ships no web
server, so home-init no longer unpacks anything. What is left is the writable
/home gate, the root home and its README, and the shells line -- all of which
still run on every boot of a fresh /home, so all of which are still tested.
"""
import os
from pathlib import Path
import stat
import subprocess
import tempfile

repo = Path(__file__).resolve().parents[3]
source = repo / 'new-files/board/espressif/esp32s3/rootfs_overlay/usr/sbin/home-init'
with tempfile.TemporaryDirectory(prefix='esp32-init-test-') as directory:
    root = Path(directory)
    for name in ('home', 'etc', 'share', 'bin'):
        (root / name).mkdir()
    (root / 'etc/shells').write_text('/bin/sh\n')
    (root / 'share/README.txt').write_text('Welcome\n')
    mounts = root / 'mounts'
    for name, body in {'id': 'echo 0'}.items():
        target = root / 'bin' / name
        target.write_text('#!/bin/sh\n' + body + '\n')
        target.chmod(0o755)
    text = source.read_text()
    for old, new in (
        ('/usr/share/esp32-home', str(root / 'share')),
        ('/etc/shells', str(root / 'etc/shells')),
        ('/proc/mounts', str(mounts)),
        ('/home', str(root / 'home')),
    ):
        assert old in text, f'home-init no longer mentions {old}'
        text = text.replace(old, new)
    script = root / 'init.sh'
    script.write_text(text)
    env = {**os.environ, 'PATH': str(root / 'bin') + ':' + os.environ['PATH']}

    def run():
        return subprocess.run(['sh', script], env=env, capture_output=True)

    home = root / 'home/root'
    shells = root / 'etc/shells'

    def mount(option):
        # The substituted script compares against the substituted path, so the
        # fake mounts file has to name the same one.
        mounts.write_text(f'/dev/mtdblock1 {root / "home"} jffs2 {option} 0 0\n')

    # /home comes from a jffs2 partition; without it mounted there is nowhere
    # to install defaults, and the script must say so rather than write into
    # the read-only cramfs underneath.
    mounts.write_text('/dev/mtdblock0 / jffs2 rw 0 0\n')
    result = run()
    assert result.returncode != 0, 'a missing /home mount was accepted'
    assert b'is not mounted; defaults not installed' in result.stderr, result.stderr
    assert not home.exists(), 'something was written with no /home mounted'

    # Mounted, but read-only, is the same answer.
    mount('ro')
    result = run()
    assert result.returncode != 0
    assert not home.exists()

    mount('rw,relatime')
    result = run()
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(home.stat().st_mode) == 0o700, oct(stat.S_IMODE(home.stat().st_mode))
    readme = home / 'README.txt'
    assert readme.read_text() == 'Welcome\n'
    assert stat.S_IMODE(readme.stat().st_mode) == 0o600, oct(stat.S_IMODE(readme.stat().st_mode))
    assert shells.read_text().count('/usr/bin/user-shell') == 1

    # A second boot must not duplicate the shells line, and must not overwrite
    # a README the user has edited.
    readme.write_text('mine\n')
    assert run().returncode == 0
    assert shells.read_text() == '/bin/sh\n/usr/bin/user-shell\n'
    assert readme.read_text() == 'mine\n'

    # A shells file that already names the shell is left exactly as it is.
    shells.write_text('/bin/sh\n/usr/bin/user-shell\n/usr/bin/bash\n')
    assert run().returncode == 0
    assert shells.read_text() == '/bin/sh\n/usr/bin/user-shell\n/usr/bin/bash\n'

    # /home/root pointing somewhere else is refused: following it would install
    # the README and its mode, and the shell, outside the home partition.
    readme.unlink()
    home.rmdir()
    (root / 'elsewhere').mkdir()
    home.symlink_to(root / 'elsewhere')
    result = run()
    assert result.returncode != 0, 'a symlinked /home/root was followed'
    assert not (root / 'elsewhere/README.txt').exists()
    assert not readme.exists()

print('PASS: /home mount gate, mode 700 home, mode 600 README, README not clobbered, '
      '/etc/shells once, symlinked /home/root refused')
