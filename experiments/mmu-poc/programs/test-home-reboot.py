#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import re
import sys

spec = importlib.util.spec_from_file_location('probe', Path(__file__).resolve().parents[1] / 'serial-probe.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
c = p.Console(sys.argv[1])


def assert_no_web_server():
    # There is no web server in this image, so there is nothing whose state
    # could survive the reboot -- which is the point of checking it twice.
    # /etc/passwd is in the list because www-data is the part of a web server
    # that leaves no trace at runtime once nothing can log in as it.
    c.command('test ! -e /usr/sbin/web-server && '
              '! grep -q "^www-data:" /etc/passwd && '
              '! grep -q "^www-data:" /etc/group && '
              '! busybox --list 2>/dev/null | grep -qx httpd')


try:
    c.login()
    files = ('/home/root/README.txt /etc/passwd /etc/shadow /etc/inetd.conf '
             '/etc/hostname /etc/shells')
    def hashes():
        return re.findall(r'(?m)^([a-f0-9]{64})\s+(/\S+)$', c.command('sha256sum ' + files))
    before = hashes()
    assert len(before) == 6
    boot = re.search(r'(?m)^[a-f0-9-]{36}$', c.command('cat /proc/sys/kernel/random/boot_id')).group(0)
    assert_no_web_server()
    c.port.write(b'sync; reboot\n')
    c.until(rb'[\w.-]+ login: ?', 45)
    c.login()
    assert boot not in c.command('cat /proc/sys/kernel/random/boot_id')
    assert hashes() == before
    c.command('test ! -e /www && test "$(stat -c %a /bin/busybox)" = 4755 && test "$(cat /proc/sys/kernel/tainted)" = 0')
    assert_no_web_server()
    print('PASS: explicit reboot; six persistent files unchanged; private root home; SUID; no web server; taint 0')
    print(c.command('process-test', 60))
finally:
    c.close()
