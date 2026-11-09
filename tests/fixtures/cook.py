#!/usr/bin/env python3
"""Offline adapter stub. It deliberately has no acquisition/network code."""
import hashlib
import json
from pathlib import Path
import sys

recipe, out, state = map(Path, sys.argv[1:])
spec = json.loads(recipe.read_text())
assert 'memory' not in spec['config']['modules']
assert all(c.get('enabled', True) for c in spec['config']['connectors'].values())
assert all(m.get('enabled', True) for m in spec['config']['modules'].values())
with (out.parent.parent / 'cook-calls').open('a') as stream:
    stream.write(out.name + '\n')
fail_once = out.parent.parent / 'cook-fail-once'
if fail_once.exists():
    fail_once.unlink()
    (out / 'partial').write_text('incomplete')
    sys.exit(1)
(out / 'run.sh').write_text('#!/bin/sh\nexit 0\n')
(out / 'run.sh').chmod(0o755)
names = set(spec['config'].get('required_secrets', []))
for section in ('connectors', 'modules'):
    for entry in spec['config'][section].values():
        names.update(entry.get('required_secrets', []))
lock = {
    'version': 2, 'backend': 'host',
    'state': {'path': str(state)},
    'cook': {'version': '0.0.0-test-only'},
    'recipe': {'path': str(recipe), 'sha256': hashlib.sha256(recipe.read_bytes()).hexdigest()},
    'recipePath': str(recipe), 'createdAt': '2026-11-09T00:00:00.000Z',
    'build': {'id': hashlib.sha256(recipe.read_bytes()).hexdigest(), 'dir': str(out)},
    'connectomeHost': {'url': 'https://example.invalid/runtime.git',
                       'ref': spec['pins']['runtime']['connectome-host'],
                       'commit': spec['pins']['runtime']['connectome-host']},
    'components': [], 'localExtensions': [],
    'requirements': [{'name': n, 'envName': n, 'origin': 'process-env'} for n in sorted(names)],
    'credentials': sorted(names), 'outputs': [],
    'launch': {'kind': 'script', 'script': 'run.sh'},
}
(out / 'connectome.lock').write_text(json.dumps(lock))
