#!/usr/bin/env python3
"""usr/sbin/remote-login and ssh-server on the host, against a fake /etc."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SH = os.environ.get('BOARD_SH', 'sh')
SBIN = ROOT / 'new-files/board/espressif/esp32s3/rootfs_overlay/usr/sbin'
# Lines from an older image, which still had a web server and a telnet server.
# This image has neither, and /etc survives a flash, so the lines have to be
# tolerated and then dropped.
OLD_WEB = '#80\tstream\ttcp\tnowait\twww-data\t/usr/sbin/httpd\thttpd -i -h /home/www\n'
OLD_TELNET = '#telnet\tstream\ttcp\tnowait\troot\t/usr/sbin/telnetd\ttelnetd -i\n'
OLD = ('ssh\tstream\ttcp\tnowait\troot\t/usr/sbin/dropbear\tdropbear -i -R -I 600\n'
       + OLD_TELNET)
# A line remote-login does not own, so it must survive every rewrite. busybox
# inetd builtins are written as "internal" plus the builtin's name, and this
# image enables the echo one (CONFIG_FEATURE_INETD_SUPPORT_BUILTIN_ECHO), so it
# is a line a user could genuinely have added.
FOREIGN = 'echo\tstream\ttcp\tnowait\troot\tinternal\techo\n'


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='remote-login-'))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.etc = self.dir / 'etc'
        self.etc.mkdir()
        self.home = self.dir / 'root'
        self.bin = self.dir / 'bin'
        self.bin.mkdir()
        self.dropbear = self.dir / 'dropbear'
        self.dropbear.write_text('')
        self.dropbear.chmod(0o755)
        self.inetd = self.etc / 'inetd.conf'
        self.inetd.write_text('')
        self.fake('id', 'case "$*" in "-u") echo 0 ;; *) exit 0 ;; esac')
        self.fake('killall', 'echo "$@" >> %s/killall.log' % self.dir)
        self.fake('remote-login', f'exec {SH} {SBIN / "remote-login"} "$@"')

    def fake(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def run_sbin(self, name, *args):
        env = {**os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}',
               'REMOTE_LOGIN_ETC': str(self.etc), 'REMOTE_LOGIN_DROPBEAR': str(self.dropbear),
               'REMOTE_LOGIN_HOME': str(self.home)}
        return subprocess.run([SH, str(SBIN / name), *args], capture_output=True,
                              text=True, env=env, timeout=20)

    def ok(self, name, *args):
        r = self.run_sbin(name, *args)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def lines(self):
        return [line.split('\t') for line in self.inetd.read_text().splitlines() if line]

    def line(self, program):
        found = [f for f in self.lines() if f[5] == '/usr/sbin/' + program]
        self.assertEqual(len(found), 1, self.inetd.read_text())
        return found[0]

    def listening(self, program):
        return not self.line(program)[0].startswith('#')


class RemoteLoginTests(Base):
    def test_ssh_on_the_default_port_takes_password_and_keys(self):
        r = self.ok('remote-login', 'ssh')
        self.assertIn('SSH on port 22 (password or key)', r.stdout)
        ssh = self.line('dropbear')
        self.assertEqual((ssh[0], ssh[6]), ('22', 'dropbear -i -R -I 600'))
        self.assertIn('-HUP inetd', (self.dir / 'killall.log').read_text())

    def test_port_moves_ssh_and_survives_switching(self):
        self.ok('remote-login', 'ssh')
        self.ok('remote-login', 'port', 'ssh', '2222')
        self.assertEqual(self.line('dropbear')[0], '2222')
        self.assertTrue(self.listening('dropbear'))
        self.ok('remote-login', 'off')
        self.ok('remote-login', 'ssh')
        self.assertEqual(self.line('dropbear')[0], '2222')
        self.assertIn('SSH_PORT=2222', (self.etc / 'remote-login.conf').read_text())
        self.ok('remote-login', 'port', 'ssh', 'default')
        self.assertEqual(self.line('dropbear')[0], '22')

    def test_auth_modes_pick_the_dropbear_flags(self):
        self.ok('remote-login', 'ssh')
        for mode, args, text in (('password', 'dropbear -i -R -I 600 -n', 'password only'),
                                 ('key', 'dropbear -i -R -I 600 -s', 'keys only'),
                                 ('both', 'dropbear -i -R -I 600', 'password or key')):
            r = self.ok('remote-login', 'auth', mode)
            self.assertIn(text, r.stdout)
            self.assertEqual(self.line('dropbear')[6], args)
            self.assertIn(f'SSH_AUTH={mode}', (self.etc / 'remote-login.conf').read_text())

    def test_keys_only_warns_without_a_key_and_not_with_one(self):
        r = self.ok('remote-login', 'auth', 'key')
        self.assertIn('authorized_keys yet', r.stderr)
        (self.home / '.ssh').mkdir(parents=True)
        (self.home / '.ssh/authorized_keys').write_text('ssh-ed25519 AAAA me\n')
        r = self.ok('remote-login', 'auth', 'key')
        self.assertEqual(r.stderr, '')

    def test_bad_ports_change_nothing(self):
        self.ok('remote-login', 'ssh')
        before = self.inetd.read_text()
        for port in ('0', '65536', 'abc', '022', '-1', ''):
            r = self.run_sbin('remote-login', 'port', 'ssh', port)
            self.assertNotEqual(r.returncode, 0, port)
        self.assertEqual(self.inetd.read_text(), before)
        self.assertFalse((self.etc / 'remote-login.conf').exists())

    def test_hand_edited_settings_take_effect_on_apply(self):
        self.ok('remote-login', 'ssh')
        (self.etc / 'remote-login.conf').write_text('SSH_PORT=2200\nSSH_AUTH=key\nJUNK\n')
        self.ok('remote-login', 'apply')
        ssh = self.line('dropbear')
        self.assertEqual((ssh[0], ssh[6]), ('2200', 'dropbear -i -R -I 600 -s'))

    def test_status_names_ports_and_auth(self):
        self.assertIn('Listening: nothing', self.ok('remote-login', 'status').stdout)
        self.ok('remote-login', 'ssh')
        self.ok('remote-login', 'port', 'ssh', '2022')
        self.ok('remote-login', 'auth', 'password')
        out = self.ok('remote-login').stdout
        self.assertIn('Listening: ssh', out)
        self.assertIn('SSH: port 2022, password only', out)

    def test_an_old_inetd_conf_is_rewritten_without_duplicates(self):
        self.inetd.write_text(OLD_WEB + OLD)
        (self.etc / 'remote-login').write_text('ssh\n')
        self.ok('remote-login', 'apply')
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(self.line('dropbear')[0], '22')
        self.assertTrue(self.listening('dropbear'))

    def test_a_line_from_the_old_web_server_is_dropped(self):
        # /etc is jffs2 and outlives a flash, so a board updated from an image
        # that had a web server still names /usr/sbin/httpd. This image has no
        # such program, so the line is removed rather than left to fail.
        self.inetd.write_text(OLD_WEB)
        self.ok('remote-login', 'apply')
        self.assertEqual([f for f in self.lines() if f[5] == '/usr/sbin/httpd'], [])
        self.assertEqual(len(self.lines()), 1)

    def test_a_line_from_the_old_telnet_server_is_dropped(self):
        # Same reasoning as the web server above: this image builds no telnetd,
        # so a line naming one is removed rather than left to fork a name that
        # no longer exists.
        self.inetd.write_text(OLD_TELNET)
        self.ok('remote-login', 'apply')
        self.assertEqual([f for f in self.lines() if f[5] == '/usr/sbin/telnetd'], [])
        self.assertEqual(len(self.lines()), 1)

    def test_a_line_remote_login_does_not_own_is_left_alone(self):
        self.inetd.write_text(FOREIGN)
        self.ok('remote-login', 'ssh')
        self.ok('remote-login', 'port', 'ssh', '2222')
        self.ok('remote-login', 'off')
        self.assertIn(FOREIGN, self.inetd.read_text())
        self.assertEqual(len(self.lines()), 2)
        self.assertFalse(self.listening('dropbear'))

    def test_usage_on_nonsense(self):
        for args in (['port'], ['port', 'web', '81'], ['auth', 'maybe'], ['reboot'],
                     ['telnet']):
            r = self.run_sbin('remote-login', *args)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn('usage:', r.stderr)


class SshServerTests(Base):
    def test_on_and_off_round_trip(self):
        self.ok('remote-login', 'port', 'ssh', '2222')
        self.ok('ssh-server', 'on')
        self.assertTrue(self.listening('dropbear'))
        self.assertEqual(self.line('dropbear')[0], '2222')
        self.assertIn('SSH: enabled', self.ok('ssh-server', 'status').stdout)
        self.assertIn('Listening: ssh', self.ok('remote-login', 'status').stdout)
        self.ok('ssh-server', 'off')
        self.assertFalse(self.listening('dropbear'))
        self.assertIn('SSH: disabled', self.ok('ssh-server', 'status').stdout)

    def test_off_with_nothing_else_leaves_nothing(self):
        self.ok('ssh-server', 'on')
        self.assertEqual((self.etc / 'remote-login').read_text().split(), ['ssh'])
        self.ok('ssh-server', 'off')
        self.assertEqual((self.etc / 'remote-login').read_text().split(), ['off'])
        self.assertFalse(self.listening('dropbear'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
