#!/usr/bin/env python3
"""Offline health: simulate manager EnvironmentFile delivery to real cook run.sh.

This is not a live systemd qualification. No raw child output is forwarded.
"""
import os
from pathlib import Path
import shlex
import subprocess
import sys

environment = {key: os.environ[key] for key in ('PATH', 'HOME') if key in os.environ}
for line in Path('secrets.env').read_text().splitlines():
    name, _, value = line.partition('=')
    environment[name] = shlex.split(value)[0]
result = subprocess.run([str(Path('run.sh').resolve())], env=environment,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
sys.exit(result.returncode)
