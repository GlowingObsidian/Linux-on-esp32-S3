#!/usr/bin/env python3
"""Host tests for usr/sbin/bootlog and etc/init.d/S00bootlog: verbose is recorded
and re-applied at every boot, quiet leaves no marker and so lasts a single boot,
the buffer is replayed only when the command line really carried `quiet`, and a
board with no marker pays nothing and prints nothing.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SH = os.environ.get('BOARD_SH', 'sh')
OVERLAY = ROOT / 'new-files/board/espressif/esp32s3/rootfs_overlay'
BOOTLOG = OVERLAY / 'usr/sbin/bootlog'
INIT = OVERLAY / 'etc/init.d/S00bootlog'
# What /proc/sys/kernel/printk looks like on the booted board: console level,
# default message level, minimum console level, default console level. The
# console level is CONFIG_CONSOLE_LOGLEVEL_DEFAULT=7, because the shipped
# command line carries no `quiet`; no script has run yet at that point.
PRINTK = '7\t4\t1\t7\n'
# ...and a command line with `quiet` on it, for the boards that carry one.
QUIET_CMDLINE = 'earlycon=esp32s3uart console=ttyS0,115200n8 quiet\n'


class BootlogTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='bootlog-'))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.flags = self.dir / 'etc/bootlog'
        self.level = self.dir / 'printk'
        self.level.write_text(PRINTK)
        self.cmdline = self.dir / 'cmdline'
        self.cmdline.write_text(QUIET_CMDLINE.replace(' quiet', ''))
        # bootlog refuses to run as anyone but root, which is right on the
        # board and impossible here; the check itself is exercised separately.
        self.stub('id', 'echo 0')

    def env(self):
        return {**os.environ, 'BOOTLOG_DIR': str(self.flags), 'BOOTLOG_LEVEL': str(self.level),
                'BOOTLOG_CMDLINE': str(self.cmdline),
                'PATH': f'{self.dir}/bin:' + os.environ['PATH']}

    def run_script(self, script, *args, code=0):
        result = subprocess.run([SH, str(script), *args], capture_output=True,
                                text=True, env=self.env(), timeout=30)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def stub(self, name, body):
        (self.dir / 'bin').mkdir(exist_ok=True)
        path = self.dir / 'bin' / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)

    def stub_dmesg(self, text='[    0.000000] Linux version 6.11.0\n'):
        self.stub('dmesg', f'printf %s {text!r}')

    def console_level(self):
        # On the board this is procfs: writing one number sets the console
        # level and the file still reads back four fields. A plain file just
        # takes the number, so read the first field either way.
        return self.level.read_text().split('\t')[0].strip()

    # -- the never-told board ----------------------------------------------

    def test_a_board_that_was_never_told_says_so(self):
        out = self.run_script(BOOTLOG, 'status')
        self.assertIn('setting: quiet', out)
        self.assertIn('this boot only', out)

    def test_the_init_script_does_nothing_and_says_nothing_when_off(self):
        self.stub_dmesg()
        out = self.run_script(INIT, 'start')
        self.assertEqual(out, '')
        self.assertEqual(self.console_level(), '7', 'an off board must not be touched')

    # -- turning it on -----------------------------------------------------

    def test_verbose_raises_the_level_now_and_records_it(self):
        self.run_script(BOOTLOG, 'verbose')
        self.assertEqual(self.console_level(), '8')
        self.assertTrue((self.flags / 'verbose').exists())
        self.assertIn('setting: verbose', self.run_script(BOOTLOG, 'status'))

    def test_the_init_script_replays_the_buffer_when_the_command_line_asked(self):
        self.run_script(BOOTLOG, 'verbose')
        self.level.write_text(PRINTK)  # a reboot puts the level back
        self.cmdline.write_text(QUIET_CMDLINE)
        self.stub_dmesg('[    0.000000] Linux version 6.11.0\n[    1.3] Run /sbin/init\n')
        out = self.run_script(INIT, 'start')
        self.assertEqual(self.console_level(), '8')
        self.assertIn('Linux version 6.11.0', out, 'the early log has to be replayed')
        self.assertIn('Run /sbin/init', out)

    def test_a_live_boot_is_not_printed_twice(self):
        # This image's command line has no `quiet`, so the boot already reached
        # the console as it happened and replaying here would double every line.
        self.run_script(BOOTLOG, 'verbose')
        self.level.write_text(PRINTK)
        self.assertNotIn('quiet', self.cmdline.read_text())
        self.stub_dmesg('[    0.000000] Linux version 6.11.0\n[    1.3] Run /sbin/init\n')
        out = self.run_script(INIT, 'start')
        self.assertEqual(self.console_level(), '8')
        self.assertIn('bootlog: verbose', out)
        self.assertNotIn('Linux version 6.11.0', out, 'a live boot is not replayed')

    # -- turning it off again ----------------------------------------------

    def test_quiet_lowers_the_level_and_forgets_the_setting(self):
        self.run_script(BOOTLOG, 'verbose')
        self.run_script(BOOTLOG, 'quiet')
        self.assertEqual(self.console_level(), '4')
        self.assertFalse((self.flags / 'verbose').exists())
        self.assertIn('setting: quiet', self.run_script(BOOTLOG, 'status'))

    def test_quiet_leaves_no_marker_so_the_next_boot_returns_to_the_default(self):
        # That absence is why quiet lasts one boot: there is nothing for the
        # init script to re-apply, so the kernel's own default comes back.
        self.run_script(BOOTLOG, 'verbose')
        self.run_script(BOOTLOG, 'quiet')
        self.assertEqual(list(self.flags.iterdir()), [])
        self.assertEqual(self.run_script(INIT, 'start'), '')

    def test_the_setting_survives_into_a_later_boot(self):
        self.run_script(BOOTLOG, 'verbose')
        self.assertIn('bootlog: verbose', self.run_script(INIT, 'status'))
        self.run_script(BOOTLOG, 'quiet')
        self.assertIn('bootlog: quiet', self.run_script(INIT, 'status'))

    # -- the edges ---------------------------------------------------------

    def test_on_and_off_are_accepted_as_aliases(self):
        self.run_script(BOOTLOG, 'on')
        self.assertEqual(self.console_level(), '8')
        self.run_script(BOOTLOG, 'off')
        self.assertEqual(self.console_level(), '4')

    def test_it_refuses_to_run_as_anyone_but_root(self):
        self.stub('id', 'echo 1000')
        out = self.run_script(BOOTLOG, 'verbose', code=1)
        self.assertIn('must be run as root', out)
        self.assertFalse((self.flags / 'verbose').exists())
        self.assertEqual(self.console_level(), '7')

    def test_an_unknown_word_is_refused_with_the_usage(self):
        out = self.run_script(BOOTLOG, 'louder', code=1)
        self.assertIn('usage: bootlog', out)

    def test_an_unreadable_printk_does_not_take_the_boot_down(self):
        self.level.unlink()
        self.stub_dmesg()
        self.run_script(BOOTLOG, 'verbose')          # must not fail
        self.assertTrue((self.flags / 'verbose').exists())
        self.run_script(INIT, 'start')               # nor here, on the next boot


if __name__ == '__main__':
    unittest.main(verbosity=2)
