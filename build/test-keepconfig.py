#!/usr/bin/env python3
"""Check factory and persistent-state startup under the inherited CI test name."""
from pathlib import Path
import subprocess
import sys

subprocess.run([sys.executable, str(Path(__file__).with_name('test-flash-init.py'))],
               check=True)
