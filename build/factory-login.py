#!/usr/bin/env python3
"""Put the board's login back to how a fresh flash leaves it.

The test harness turns SSH on to reach the board, so after a run the password is
whatever the run left and a way in is listening. This sets the password back to
the image default, turns SSH off and forgets the choice, so the next console
login finds the board as a fresh flash leaves it: console only, nothing on the
network.

There is no second copy to fix up any more: S03keepconfig used to keep one in
/home, and /home is now part of the read-only rootfs, so the change below is
the only one that exists and it lasts until the next reboot.
"""
import argparse
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('probe', ROOT / 'experiments/mmu-poc/serial-probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

parser = argparse.ArgumentParser()
parser.add_argument('port')
args = parser.parse_args()

pw = probe.DEFAULT_PASSWORD
c = probe.Console(args.port)
c.login()
c.command(f'printf "{pw}\\n{pw}\\n" | passwd -a sha256 root >/dev/null 2>&1', 30)
c.command('remote-login off >/dev/null', 30)
c.command('rm -f /etc/remote-login /home/.etc-backup/remote-login', 10)
c.command('sync', 60)
state = c.command('remote-login status; ls /etc/remote-login 2>&1; '
                  "h=$(sed -n 's/^root:\\([^:]*\\):.*/\\1/p' /etc/shadow); s=${h#\\$5\\$}; "
                  f'[ "$(mkpasswd -m sha256 {pw} ${{s%%\\$*}})" = "$h" ] && echo factory-$((1))-hash', 30, check=False)
c.port.write(b'exit\n')
c.close()
if 'Listening: nothing' not in state or 'No such file' not in state or 'factory-1-hash' not in state:
    raise SystemExit('factory-login: the board did not end up at the factory login:\n' + state)
print(f'root is back to the image default, nothing listens on the network, '
      'and the board is once again console only by default')
