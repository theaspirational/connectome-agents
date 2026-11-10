"""Synthetic cook CLI for catalog role wiring, NOT upstream qualification."""
import hashlib
import json
from pathlib import Path
import sys

args = sys.argv[1:]
assert args[0] == 'install'
recipe = Path(args[1])
build = Path(args[args.index('--out') + 1])
layout = json.loads(Path(args[args.index('--layout') + 1]).read_text())
settings = json.loads(recipe.read_text())
unit = args[args.index('--user-unit') + 1]
(build / 'run.sh').write_text('#!/bin/sh\nexit 0\n')
(build / unit).write_text('[Service]\nWorkingDirectory=' + layout['state'] + '\nExecStart=/bin/sh ' + str(build / 'run.sh') +
                         '\n[Install]\nWantedBy=default.target\n')
pin = args[args.index('--ch-ref') + 1]
lock = {'version': 2, 'backend': 'host', 'cook': {'version': '0.0.0-test-only'},
        'recipe': {'path': str(recipe), 'sha256': hashlib.sha256(recipe.read_bytes()).hexdigest()},
        'recipePath': str(recipe), 'createdAt': '2026-11-09T00:00:00.000Z',
        'build': {'id': 'a' * 64, 'dir': str(build)},
        'connectomeHost': {'url': args[args.index('--ch-repo') + 1], 'ref': pin, 'commit': pin},
        'components': [], 'localExtensions': [], 'outputs': [], 'hostDependencies': [],
        'requirements': [], 'credentials': [], 'layout': layout, 'state': {'path': layout['state']},
        'launch': {'kind': 'script', 'script': 'run.sh', 'userUnit': unit},
        'engines': {name: {'path': args[args.index('--' + name + '-path') + 1],
                          'realPath': args[args.index('--' + name + '-path') + 1], 'version': '24.1.0' if name == 'node' else '1.0.0'}
                    for name in ('bun', 'node')}}
external = [{'name': name, 'command': entry['command']} for name, entry in sorted(settings['mcpServers'].items())]
if external:
    lock['external'] = external
for name in settings.get('requirements', {}):
    lock['requirements'].append({'name': name, 'envName': name, 'origin': 'unresolved'})
    lock['credentials'].append(name)
(build / 'connectome.lock').write_text(json.dumps(lock))
