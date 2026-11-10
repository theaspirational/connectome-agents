#!/usr/bin/env python3
"""Fake user service manager; tests select the failure target via a file."""
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
args = sys.argv[2:]
scope = 'user'
if args[0] in ('--system', '--user'):
    scope = args[0][2:]
    args = args[1:]
log = root / 'systemctl-calls' if root.exists() else root.parent / 'preflight-systemctl-calls'
with log.open('a') as stream:
    stream.write(json.dumps(sys.argv[2:]) + '\n')
if (root.parent / 'forbid-unit-activation').exists():
    assert args[0] not in ('enable', 'start', 'restart', 'stop', 'disable'), args
states_path = root.parent / 'unit-states.json'
states = json.loads(states_path.read_text()) if states_path.exists() else {}
key = scope + ':' + args[-1]
if (root.parent / 'track-unit-state').exists():
    if args[0] in ('start', 'restart'):
        states[key] = 'active'
        states_path.write_text(json.dumps(states))
    if args[0] == 'stop':
        states[key] = 'inactive'
        states_path.write_text(json.dumps(states))
if args[0] == 'is-active':
    if key in states or (root.parent / 'track-unit-state').exists():
        value = states.get(key, 'inactive')
        if '--quiet' not in args:
            print(value)
        sys.exit(0 if value == 'active' else 3)
    fail = root / 'fail-health'
    if fail.exists() and fail.read_text() == str((root / 'current').resolve()):
        sys.exit(3)

if args[0] == 'show':
    if 'ActiveState' in args:
        print(states.get(key, 'inactive'))
        sys.exit(0)
    current = (root / 'current').resolve()
    reject = root / 'reject-env'
    if not reject.exists() or reject.read_text() != str(current):
        env = current / 'secrets.env'
        spec = current / 'deployment.json'
        if spec.is_file():
            config = json.loads(spec.read_text())['config']
            if 'recipe_file' not in config and config.get('catalog'):
                env = root / 'state' / (current.name + '.env')
        print(str(env) + ' (ignore_errors=no)')

sys.exit(0)
