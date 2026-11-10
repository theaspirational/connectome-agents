#!/usr/bin/env python3
"""Offline static verifier double; never talks to a host user manager."""
from pathlib import Path
import sys

assert sys.argv[1:3] == ['--user', 'verify']
assert sys.argv[3:]
for unit in sys.argv[3:]:
    assert '[Service]' in Path(unit).read_text()
