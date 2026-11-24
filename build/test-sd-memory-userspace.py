#!/usr/bin/env python3
"""Check root-aware symlinks, retained utilities, and candidate hash gates."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('memory', Path(__file__).with_name('optimize-sd-userspace.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def elf(size):
    data = bytearray(84)
    data[:6] = b'\x7fELF\x01\x01'
    struct.pack_into('<H', data, 18, 94)
    struct.pack_into('<I', data, 28, 52)
    struct.pack_into('<HH', data, 42, 32, 1)
    struct.pack_into('<8I', data, 52, 1, 0, 0, 0, 84, size, 5, 4096)
    return data


class MemoryImageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'root'
        self.candidates = Path(self.temp.name) / 'candidates'
        for name in ('bin', 'sbin', 'usr/bin', 'usr/sbin', 'usr/lib', 'etc'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.candidates.mkdir()
        for name in ('bin/busybox', 'bin/bash', 'usr/lib/libncurses.so.6.4'):
            (self.root / name).write_bytes(elf(1000))
        for name, target in [('sbin/init', '/bin/busybox'), ('sbin/getty', '../bin/busybox'),
                             ('bin/sh', 'busybox'), ('bin/login', 'busybox')]:
            (self.root / name).symlink_to(target)
        (self.root / 'usr/bin/login').write_text('custom login')
        for name, report in [('busybox-core-measure.bin', 'core-busybox-result.json'),
                             ('bash-static-terminal', 'static-terminal-result.json')]:
            (self.candidates / name).write_bytes(elf(100))
            (self.candidates / report).write_text(json.dumps({'sha256': m.digest(self.candidates / name)}))

    def test_resident_links_preserve_full_utilities_and_original_shell(self):
        old = (self.root / 'bin/bash').read_bytes()
        report = m.optimize(self.root, self.candidates)
        for name in ('sbin/init', 'sbin/getty', 'bin/sh', 'bin/login'):
            self.assertEqual(m.resolve_in_root(self.root, name), self.root / 'bin/busybox-core')
        self.assertEqual((self.root / 'usr/bin/login').read_text(), 'custom login')
        self.assertEqual((self.root / 'usr/libexec/bash-original').read_bytes(), old)
        self.assertEqual((self.root / 'bin/busybox').read_bytes(), elf(1000))
        self.assertEqual(report['potential_loaded_segment_saving_bytes'], 2800)

    def test_absolute_directory_link_stays_inside_image(self):
        (self.root / 'alias').symlink_to('/bin')
        self.assertEqual(m.resolve_in_root(self.root, 'alias/bash'), self.root / 'bin/bash')

    def test_cycle_rejected(self):
        (self.root / 'loop').symlink_to('/loop')
        with self.assertRaises(ValueError):
            m.resolve_in_root(self.root, 'loop')

    def test_modified_candidate_rejected(self):
        (self.candidates / 'bash-static-terminal').write_bytes(elf(101))
        with self.assertRaises(AssertionError):
            m.optimize(self.root, self.candidates)


unittest.main()
