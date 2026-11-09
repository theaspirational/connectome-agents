#!/usr/bin/env python3
"""Fake user service manager; tests select the failure target via a file."""
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
args = sys.argv[2:]
with (root / 'systemctl-calls').open('a') as stream:
    stream.write(json.dumps(args) + '\n')
if args[0] == 'is-active':
    fail = root / 'fail-health'
    if fail.exists() and fail.read_text() == str((root / 'current').resolve()):
        sys.exit(3)
sys.exit(0)
