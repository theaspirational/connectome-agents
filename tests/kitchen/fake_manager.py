#!/usr/bin/env python3
"""Trial only: a user service manager for tests/kitchen/trial.py. Answers what the connectome_kitchen role, its scripts
and the engine ask.

    fake_manager.py SANDBOX ROOT UNIT_DIR STATE_DIR [--user|--system] VERB ...

A started unit is a real process (so /proc/<pid>/cmdline and cwd can be read) whose arguments are the entry and the
recipe copy of the build the loaded unit file lies in. The unit file and the drop-in are read at `daemon-reload` only,
as a real manager does. Knobs are files in SANDBOX/knobs:
  fail-start-<first 8 of a build>   start and restart of a unit loaded from that build fail
  short-lived-<first 8 of a build>  the process of that build ends after one second; `show` then finds it gone and
                                    starts another one (Restart=on-failure), counting NRestarts
  fail-reload                       daemon-reload fails
  fragment-is-target                FragmentPath is the unit link's target, not the link (some managers name the link)
"""
import json, os, signal, subprocess, sys, time
from pathlib import Path

sandbox, root, unit_dir, state_dir = map(Path, sys.argv[1:5])
args = sys.argv[5:]
scope = 'user'
if args and args[0] in ('--user', '--system'):
    scope, args = args[0][2:], args[1:]
knobs = sandbox / 'knobs'
book = sandbox / 'fake-manager.json'
data = json.loads(book.read_text()) if book.exists() else {'loaded': {}, 'units': {}}
with (sandbox / 'fake-manager-calls').open('a') as stream:
    stream.write(json.dumps(args) + '\n')
with (root / 'systemctl-calls').open('a') as stream:  # where the unit test counts the engine's verbs
    stream.write(json.dumps(sys.argv[5:]) + '\n')
words = [word for word in args if not word.startswith('-')]
verb, operands = words[0], words[1:]


def save():
    book.write_text(json.dumps(data, indent=1))
    # The oracle's fake, which the role's own view asks afterwards, reads this file.
    (root.parent / 'unit-states.json').write_text(json.dumps({'user:' + name: ('active' if unit['active'] == 'active' else 'inactive') for name, unit in data['units'].items()}))
    (root.parent / 'track-unit-state').write_text('')


def load(name):
    link = unit_dir / name
    if not link.exists():
        return None
    fragment = Path(os.path.realpath(link))
    env = ''
    dropin = unit_dir / (name + '.d') / '90-connectome-env.conf'
    if dropin.is_file():
        for line in dropin.read_text().splitlines():
            if line.startswith('EnvironmentFile='):
                env = line.split('=', 1)[1]
    return {'fragment': str(fragment), 'env': env}


MAIN = '''
import json, subprocess, sys, time
try:
    servers = json.load(open(sys.argv[3])).get('mcpServers', {})
except (OSError, ValueError):
    servers = {}
for server in servers.values():
    if isinstance(server, dict) and isinstance(server.get('command'), str) and server.get('enabled') is not False:
        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(900)'] + [str(arg) for arg in server.get('args', [])])
time.sleep(float(sys.argv[1]))
'''


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return Path('/proc/%d/stat' % pid).read_text().split(') ')[1][0] != 'Z'
    except OSError:
        return False


def spawn(name):
    loaded = data['loaded'].get(name) or load(name)
    data['loaded'][name] = loaded
    build = Path(loaded['fragment']).parents[1]
    if (knobs / ('fail-start-' + build.name[:8])).exists():
        return False
    seconds = '1' if (knobs / ('short-lived-' + build.name[:8])).exists() else '900'
    # The process of the agent: it starts one child for each MCP server the cooked recipe starts as a process (so that a
    # readback has children to find), then sleeps.
    child = subprocess.Popen([sys.executable, '-c', MAIN, seconds, str(build / 'app/src/index.ts'), str(root / 'cook-config' / build.name / 'recipes/cook-recipe.json')],
                             cwd=state_dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    unit = data['units'].setdefault(name, {'restarts': 0})
    unit.update(active='active', sub='running', pid=child.pid, started=time.strftime('%a %Y-%m-%d %H:%M:%S UTC', time.gmtime()) + ' #%d' % child.pid)
    return True


def stop(name):
    unit = data['units'].get(name)
    if unit and unit.get('pid'):
        try:
            os.killpg(unit['pid'], signal.SIGKILL)
        except OSError:
            pass
    if unit:
        unit.update(active='inactive', sub='dead', pid=0)


def settle(name):
    unit = data['units'].get(name)
    if unit and unit['active'] == 'active' and not alive(unit['pid']):
        unit['restarts'] += 1
        if not spawn(name):
            unit.update(active='failed', sub='failed', pid=0)
        save()


if verb == 'daemon-reload':
    if (knobs / 'fail-reload').exists():
        sys.exit(1)
    for name in set(data['loaded']) | {path.name for path in unit_dir.glob('*.service')}:
        loaded = load(name)
        if loaded:
            data['loaded'][name] = loaded
    save()
elif verb in ('start', 'restart'):
    name = operands[0]
    if verb == 'start' and data['units'].get(name, {}).get('active') == 'active' and alive(data['units'][name]['pid']):
        sys.exit(0)
    stop(name)
    started = spawn(name)
    if not started:
        data['units'].setdefault(name, {'restarts': 0}).update(active='failed', sub='failed', pid=0)
    save()
    sys.exit(0 if started else 1)
elif verb == 'stop':
    stop(operands[0]); save()
elif verb == 'reset-failed':
    pass
elif verb == 'is-active':
    name = operands[0]
    settle(name)
    value = data['units'].get(name, {}).get('active', 'inactive') if scope == 'user' else 'inactive'
    if '--quiet' not in args:
        print(value)
    sys.exit(0 if value == 'active' else 3)
elif verb == 'show':
    name = operands[-1]
    settle(name)
    unit = data['units'].get(name, {'active': 'inactive', 'sub': 'dead', 'pid': 0, 'restarts': 0, 'started': ''})
    loaded = data['loaded'].get(name) or load(name) or {'fragment': '', 'env': ''}
    answers = {'ActiveState': unit['active'], 'SubState': unit['sub'], 'MainPID': str(unit['pid']), 'NRestarts': str(unit['restarts']), 'ExecMainStartTimestamp': unit.get('started', ''),
               'LoadState': 'loaded' if loaded['fragment'] else 'not-found', 'UnitFileState': 'enabled' if (unit_dir / 'default.target.wants' / name).is_symlink() else 'disabled',
               'FragmentPath': (str(unit_dir / name) if not (knobs / 'fragment-is-target').exists() else loaded['fragment']) if loaded['fragment'] else '', 'DropInPaths': str(unit_dir / (name + '.d') / '90-connectome-env.conf') if loaded['env'] else '',
               'EnvironmentFiles': loaded['env'] + ' (ignore_errors=no)' if loaded['env'] else ''}
    asked = [args[index + 1] for index, word in enumerate(args) if word == '-p']
    for key in asked:
        print(answers.get(key, '') if '--value' in args else '%s=%s' % (key, answers.get(key, '')))
else:
    sys.exit(1)
