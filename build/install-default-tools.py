#!/usr/bin/env python3
"""Install the default diagnostics and initialize an empty factory root home."""
import argparse
from pathlib import Path
import shutil

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('tree', type=Path)
parser.add_argument('tools', type=Path)
parser.add_argument('overlay', type=Path)
args = parser.parse_args()
tree, tools, overlay = args.tree, args.tools, args.overlay

def install(source, destination, mode):
    path = tree / destination
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    shutil.copyfile(source, path)
    path.chmod(mode)

for name in ('coremark-single', 'coremark-smp', 'iperf3', 'fastfetch'):
    install(tools / name, f'usr/bin/{name}', 0o755)
command = tree / 'usr/bin/coremark'
command.unlink(missing_ok=True)
command.symlink_to('coremark-smp')
install(tools / 'libatomic.so.1', 'usr/lib/libatomic.so.1', 0o755)
# Prefer the board runtime even if a cached source root carries a stock copy.
other_libatomic = tree / 'lib/libatomic.so.1'
if other_libatomic.exists() or other_libatomic.is_symlink():
    install(tools / 'libatomic.so.1', 'lib/libatomic.so.1', 0o755)
for notice in sorted((tools / 'licenses').iterdir()):
    install(notice, f'usr/share/licenses/{notice.name}/LICENSE', 0o644)
for path in ('etc/profile.d/00-banner.sh', 'etc/xdg/fastfetch/config.jsonc',
             'etc/wpa_supplicant.conf.example'):
    install(overlay / path, path, 0o644)
for relative in ('usr/bin/neofetch', 'usr/share/esp32-home/README.txt',
                 'home/.root-last-login'):
    (tree / relative).unlink(missing_ok=True)

# The input is a staged factory tree. Existing board state is separate.
home = tree / 'home/root'
if home.is_symlink():
    home.unlink()
elif home.is_dir():
    shutil.rmtree(home)
home.mkdir(parents=True, mode=0o700)
assert not any(home.iterdir())
