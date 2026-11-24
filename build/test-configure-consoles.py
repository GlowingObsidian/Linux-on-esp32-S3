#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('consoles', Path(__file__).with_name('configure-consoles.py'))
consoles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(consoles)


class ConsoleTests(unittest.TestCase):
    def test_flashed_stale_display_entry_cannot_share_the_serial_console(self):
        text = ('ttyS0::respawn:/sbin/getty -L ttyS0 115200 vt100\n'
                '::respawn:/sbin/getty -L tty1 0 linux\n')
        self.assertEqual(consoles.normalize(text).splitlines(), list(consoles.ENTRIES))

    def test_upstream_console_alias_and_duplicate_entries_are_replaced(self):
        text = ('console::respawn:/sbin/getty -L console 0 vt100\n'
                'tty1::respawn:/sbin/getty -L tty1 0 vt100\n'
                '::respawn:/sbin/getty -L /dev/tty1 0 linux\n')
        self.assertEqual(consoles.normalize(text).splitlines(), list(consoles.ENTRIES))

    def test_unrelated_init_actions_and_other_ports_survive(self):
        text = ('::sysinit:/etc/init.d/rcS\n'
                'ttyS1::respawn:/sbin/getty -L ttyS1 9600 vt100\n'
                '::shutdown:/bin/umount -a -r\n')
        self.assertTrue(consoles.normalize(text).startswith(text))

    def test_repackaging_is_idempotent(self):
        text = consoles.normalize('# startup\n')
        self.assertEqual(consoles.normalize(text), text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
