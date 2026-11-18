#!/usr/bin/python3 -I
"""What a new build is beside the one an agent runs, and what the agent shows after it was started on it. Reads only.

    kitchen-inspect.py compare  CONFIG.json     before the start: the new build against the running one
    kitchen-inspect.py baseline CONFIG.json     before the start: what the running process shows, kept for `readback`
    kitchen-inspect.py readback CONFIG.json     after the start: recipe keys, MCP children, the context budget
    kitchen-inspect.py alone    CONFIG.json     a first install, before the start: the new build by itself

A first install (`first_install` in CONFIG.json) has no build that runs: `alone` stands where `compare` and `baseline`
stand, and `readback` then holds nothing to a process before the start and says so.

CONFIG.json is written by the role (roles/connectome_kitchen/tasks) and holds paths, names and expectations only.

It prints words, names, counts and numbers. It never prints a text value of a recipe, a secret, or a line of the
agent's journal: of a recipe it prints the names of leaves and, of a leaf, a value only when that is true, false, null
or a number; of the journal it prints the name of an MCP server with a word for what the line says of it, and the
numbers of the last `[plan-vs-actual]` line.

Exit status: 0 every line is `ok`; 5 a line says `LOOK` (something differs from what was expected, or could not be
seen yet); 2 unusable invocation or configuration.
"""
import hashlib, json, os, re, subprocess, sys

ABSENT = object()
looks = 0


def say(word, text):
    global looks
    if word == 'LOOK':
        looks += 1
    print('%-5s %s' % (word, text))


def check(label, held, detail=''):
    say('ok' if held else 'LOOK', label + ('' if held or not detail else ': ' + detail))


def show(value):
    if value is ABSENT:
        return 'absent'
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value)
    return '<%s>' % type(value).__name__


def walk(a, b, path, out):
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            walk(a.get(key, ABSENT), b.get(key, ABSENT), path + '/' + key.replace('~', '~0').replace('/', '~1'), out)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for index, (x, y) in enumerate(zip(a, b)):
            walk(x, y, '%s/%d' % (path, index), out)
    elif a is ABSENT or b is ABSENT or type(a) is not type(b) or a != b:
        out.append((path, '%s: %s -> %s' % (path, show(a), show(b))))


def pointer(document, path):
    """The value at an RFC 6901 pointer, or ABSENT."""
    value = document
    for token in path.split('/')[1:]:
        token = token.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
            value = value[int(token)]
        else:
            return ABSENT
    return value


def read(path):
    try:
        with open(path, 'rb') as stream:
            return stream.read()
    except OSError:
        return None


def renamed(data, old, new):
    return None if data is None else data.replace(old.encode(), new.encode())


def leaves(old_bytes, new_bytes):
    found = []
    walk(json.loads(old_bytes), json.loads(new_bytes), '', found)
    return found


def by_name(lock):
    """The lock with its lists of named things as records by name: one server more is then one leaf with the name of
    that server, and not a list of another length."""
    lock = dict(lock) if isinstance(lock, dict) else {}
    for key in ('external', 'requirements', 'credentials'):
        rows = lock.get(key)
        if not isinstance(rows, list):
            continue
        names = [row if isinstance(row, str) else row.get('name') if isinstance(row, dict) else None for row in rows]
        if all(isinstance(name, str) for name in names) and len(set(names)) == len(names):
            lock[key] = {name: True if isinstance(row, str) else row for name, row in zip(names, rows)}
    return lock


def compare(c):
    old, new, root, unit = c['old'], c['new'], c['root'], c['unit']
    a, b = os.path.join(root, 'builds', old), os.path.join(root, 'builds', new)
    cooked = lambda build: os.path.join(root, 'cook-config', build, 'recipes', 'cook-recipe.json')
    check('the new build is prepared', read(os.path.join(b, '.prepared')) == new.encode())

    def same(label, first, second, optional=False):
        x, y = renamed(read(first), old, new), read(second)
        if x is None and y is None and optional:
            return say('ok', label + ' (absent in both)')
        check(label, x is not None and x == y, 'DIFFERS' if x is not None and y is not None else 'a file is missing')
    same('unit-same-but-for-the-build-name', os.path.join(a, unit), os.path.join(b, unit))
    same('launcher-same-but-for-the-build-name', os.path.join(a, 'run.sh'), os.path.join(b, 'run.sh'))
    same('cook-env-same-but-for-the-build-name', os.path.join(root, 'cook-config', old, '.env'), os.path.join(root, 'cook-config', new, '.env'), True)
    same('bunfig-same-but-for-the-build-name', os.path.join(a, 'bunfig.toml'), os.path.join(b, 'bunfig.toml'), True)
    # Under adapter contract /2 Bun installs from an empty cache of the build's own and may resolve a range that no
    # lockfile pins to a newer version than the running build has. This line says whether it did.
    same('bun-lock-same-but-for-the-build-name', os.path.join(a, 'app', 'bun.lock'), os.path.join(b, 'app', 'bun.lock'), True)
    expected = sorted(c['recipe_changes'])
    for label, first, second in (('staged recipe', os.path.join(a, 'cook-recipe.json'), os.path.join(b, 'cook-recipe.json')), ('cooked recipe', cooked(old), cooked(new))):
        x, y = renamed(read(first), old, new), read(second)
        if x is None or y is None:
            check(label + ': both files are there', False)
            continue
        found = leaves(x, y)
        for _, line in found:
            print('      %s %s' % (label, line))
        got = sorted(path for path, _ in found)
        check('%s differs from the running build\'s in exactly the %d expected leaves' % (label, len(expected)), got == expected,
              'unexpected %s; missing %s' % (sorted(set(got) - set(expected)), sorted(set(expected) - set(got))))
    x, y = renamed(read(os.path.join(a, 'connectome.lock')), old, new), read(os.path.join(b, 'connectome.lock'))
    if x is None or y is None:
        check('lock: both files are there', False)
    else:
        found = []
        walk(by_name(json.loads(x)), by_name(json.loads(y)), '', found)
        # The lock names its recipe by digest. That leaf differs whenever the recipe does. Where each lock names the staged
        # recipe of its own build, whose leaves are held above, the leaf says nothing more and is not counted here.
        own = True
        for label, build, lock in (('running', a, x), ('new', b, y)):
            staged, named = read(os.path.join(build, 'cook-recipe.json')), pointer(json.loads(lock), '/recipe/sha256')
            own = own and staged is not None and named == hashlib.sha256(staged).hexdigest()
            if named is not ABSENT:
                check('the lock of the %s build names its staged recipe by digest' % label, staged is not None and named == hashlib.sha256(staged).hexdigest())
        allowed = sorted(c.get('lock_changes', []))
        stops = sorted(path for path, _ in found if not (own and path == '/recipe/sha256') and (re.match(r'/(external|engines|requirements|credentials|connectomeHost|recipe)(/|$)', path)
                                                                                         or re.match(r'/hostDependencies/[^/]+/(version|sha256)$', path)))
        for path, line in found:
            print('      lock %s' % line)
        check('the lock differs in %s of the external servers, engines, requirements, credentials, host or pinned packages' % ('exactly the %d expected leaves' % len(allowed) if allowed else 'no leaf'),
              stops == allowed, 'unexpected %s; missing %s' % (sorted(set(stops) - set(allowed)), sorted(set(allowed) - set(stops))))
    keys(c, b, cooked(new))
    for build in (old, new):
        print('      web-ui-%s %s' % ('built' if os.path.exists(os.path.join(root, 'builds', build, 'app', 'dist', 'web')) else 'absent', build[:8]))


def alone(c):
    """A first install: no build runs, so there is nothing to compare the new build with. What it is by itself."""
    new, root, unit = c['new'], c['root'], c['unit']
    build = os.path.join(root, 'builds', new)
    cooked = os.path.join(root, 'cook-config', new, 'recipes', 'cook-recipe.json')
    check('the new build is prepared', read(os.path.join(build, '.prepared')) == new.encode())
    for leaf in ('run.sh', unit, 'cook-recipe.json', 'connectome.lock'):
        check('the build has its %s' % leaf, read(os.path.join(build, leaf)) is not None)
    staged, lock = read(os.path.join(build, 'cook-recipe.json')), read(os.path.join(build, 'connectome.lock'))
    try:
        named = pointer(json.loads(lock), '/recipe/sha256') if lock is not None else ABSENT
    except ValueError:
        named = ABSENT
    check('the lock of the new build names its staged recipe by digest',
          named is not ABSENT and staged is not None and named == hashlib.sha256(staged).hexdigest())
    keys(c, build, cooked)
    try:
        servers = json.loads(staged).get('mcpServers') if staged is not None else None
    except ValueError:
        servers = None
    servers = servers if isinstance(servers, dict) else {}
    print('      %d MCP servers in the staged recipe: %s' % (len(servers), ', '.join(sorted(servers)) or 'none'))
    for name in sorted(c.get('servers_connected', [])):
        check('MCP %s, which readback will hold to a connected line, is a server of the recipe' % name, name in servers)
    print('      web-ui-%s %s' % ('built' if os.path.exists(os.path.join(build, 'app', 'dist', 'web')) else 'absent', new[:8]))
    print('      first install: there is no running build to compare with, and no process to keep a baseline of')


def keys(c, build, cooked):
    for label, path in (('staged recipe', os.path.join(build, 'cook-recipe.json')), ('cooked recipe', cooked)):
        data = read(path)
        if data is None:
            check(label + ' of the new build can be read', False)
            continue
        document = json.loads(data)
        for leaf in c['recipe_absent']:
            check('%s: %s is absent' % (label, leaf), pointer(document, leaf) is ABSENT, 'present: ' + show(pointer(document, leaf)))
        for leaf in c['recipe_present']:
            check('%s: %s is present (%s)' % (label, leaf, show(pointer(document, leaf))), pointer(document, leaf) is not ABSENT)


def as_account(c, argv):
    return subprocess.run(c['as_account'] + ['/usr/bin/env', '-i'] + c['environment'] + argv, capture_output=True, text=True, errors='replace')


def prop(c, name):
    return as_account(c, [c['systemctl'], '--user', 'show', '-p', name, '--value', c['unit']]).stdout.strip()


def descendants(pid):
    """Every process below `pid`, as (pid, argv)."""
    children = {}
    for entry in os.listdir('/proc'):
        if entry.isdigit():
            try:
                with open('/proc/%s/stat' % entry) as stream:
                    parent = int(stream.read().rsplit(') ', 1)[1].split()[1])
                children.setdefault(parent, []).append(int(entry))
            except (OSError, ValueError, IndexError):
                pass
    found, pending = [], [pid]
    while pending:
        for child in children.get(pending.pop(), []):
            try:
                with open('/proc/%d/cmdline' % child, 'rb') as stream:
                    argv = [word.decode('utf-8', 'replace') for word in stream.read().split(b'\0') if word]
            except OSError:
                argv = []
            found.append((child, argv))
            pending.append(child)
    return found


def marks_of(entry):
    """The absolute paths a server's recipe entry starts its process with: those of its arguments, or else its command."""
    return [arg for arg in entry.get('args', []) if isinstance(arg, str) and arg.startswith('/')] or [entry['command']]


def child_of(name, servers, free):
    """The process of `free` that is the server `name`, as (pid, argv), or None; and whether it was found below a wrapper.
    First a process whose command line holds every path of the entry. Where the entry starts a wrapper that replaces
    itself with the server (python3 wrapper.py NODE ENTRY CONFIG becomes NODE ENTRY ...), no command line holds them all:
    then a process whose every absolute path is one of the entry's, one of them a path no other server's entry holds."""
    marks = marks_of(servers[name])
    match = next((item for item in free if all(mark in item[1] for mark in marks)), None)
    if match:
        return match, False
    others = {mark for other, entry in servers.items() if other != name and isinstance(entry, dict) and isinstance(entry.get('command'), str)
              for mark in marks_of(entry) + [entry['command']]}
    own = set(marks) - others
    for item in free:
        paths = [word for word in item[1] if word.startswith('/')]
        if paths and all(path in marks for path in paths) and any(path in own for path in paths):
            return item, True
    return None, False


MCPL = re.compile(r'MCPL server "([^"\s]{1,80})" (.{0,40})')
WORDS = (('reconnected successfully', 'connected'), ('connected', 'connected'), ('initial connect failed', 'FAILED'), ('reconnect failed', 'FAILED'),
         ('disconnected', 'DISCONNECTED'), ('transport closed', 'DISCONNECTED'), ('capabilities masked', None), ('this host closed', None))
PLAN = re.compile(r'\[plan-vs-actual\] (.*)')


def journal(c, since):
    """Of the unit's journal since a time: per MCP server the word of its last line that says something of its connection,
    and the numbers of the last `[plan-vs-actual]` line. No line is returned or printed."""
    done = as_account(c, [c['journalctl'], '--user', '-u', c['unit'], '--since', since, '-o', 'cat', '--no-pager', '-q'])
    servers, plan, lines = {}, None, 0
    for line in done.stdout.splitlines():
        lines += 1
        match = MCPL.search(line)
        if match:
            for text, word in WORDS:
                if match.group(2).startswith(text):
                    if word:
                        servers[match.group(1)] = word
                    break
        match = PLAN.search(line)
        if match and 'dryRun=true' not in match.group(1):
            plan = {key: value for key, value in re.findall(r'\b(hardBudget|floor|planned|actual|budgetMet|exhausted)=([0-9]+|true|false)\b', match.group(1))}
    return {'answered': done.returncode == 0, 'lines': lines, 'servers': servers, 'plan': plan}


def baseline(c):
    pid = prop(c, 'MainPID')
    seen = journal(c, c['baseline_since'])
    record = {'main_pid': pid, 'hard_budget': (seen['plan'] or {}).get('hardBudget'), 'servers': seen['servers'], 'journal_answered': seen['answered'],
              'children': len(descendants(int(pid))) if pid.isdigit() and int(pid) else None}
    with open(c['baseline_file'], 'w') as stream:
        json.dump(record, stream, indent=1)
    print('      baseline kept in %s: MainPID %s, %s processes below it, hardBudget %s, MCP servers named in the journal: %s' % (
        c['baseline_file'], pid, record['children'], record['hard_budget'], ', '.join('%s=%s' % pair for pair in sorted(seen['servers'].items())) or 'none'))
    check('the journal of the unit can be read', seen['answered'])
    check('a [plan-vs-actual] line of the running process was found (the budget to hold the new process to)', record['hard_budget'] is not None,
          'none since %s; readback will then have no budget to compare with' % c['baseline_since'])


def readback(c):
    root, new, unit = c['root'], c['new'], c['unit']
    build = os.path.join(root, 'builds', new)
    cooked = os.path.join(root, 'cook-config', new, 'recipes', 'cook-recipe.json')
    state = {name: prop(c, name) for name in ('ActiveState', 'SubState', 'MainPID', 'NRestarts', 'ExecMainStartTimestamp', 'EnvironmentFiles')}
    print('      unit: ' + ' '.join('%s=%s' % (name, state[name]) for name in ('ActiveState', 'SubState', 'MainPID', 'NRestarts')))
    check('the unit is active and running', (state['ActiveState'], state['SubState']) == ('active', 'running'))
    check('its environment file is the new build\'s', state['EnvironmentFiles'].startswith(os.path.join(build, 'secrets.env')))
    check('current names the new build', os.path.realpath(os.path.join(root, 'current')) == os.path.realpath(build))
    first = bool(c.get('first_install'))
    try:
        with open(c['baseline_file']) as stream:
            before = json.load(stream)
    except (OSError, ValueError):
        before = None
    if first:
        # Nothing ran before a first start: there is no baseline, and none is missed.
        before = None
        print('      first install: no process ran before this one; nothing is held to a baseline')
    else:
        check('a baseline of the process before the start is there', before is not None, c['baseline_file'])
    pid = int(state['MainPID']) if state['MainPID'].isdigit() else 0
    if before:
        check('it is another process than before the start', bool(pid) and str(pid) != before['main_pid'])

    # Recipe keys, present or absent, in the bytes the engine staged and in the copy the process was started with.
    keys(c, build, cooked)
    data = read(cooked)
    recipe = json.loads(data) if data is not None else {}
    argv = []
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as stream:
            argv = [word.decode('utf-8', 'replace') for word in stream.read().split(b'\0') if word]
    except OSError:
        pass
    check('the command line of the process names the new build\'s cooked recipe', cooked in argv)

    # MCP children: every server the recipe starts as a process has one below the main process.
    below = descendants(pid) if pid else []
    free = list(below)
    seen = journal(c, state['ExecMainStartTimestamp'] or c['baseline_since'])
    servers = recipe.get('mcpServers') if isinstance(recipe.get('mcpServers'), dict) else {}
    print('      %d processes below the main process; %d MCP servers in the recipe; %d journal lines since the start' % (len(below), len(servers), seen['lines']))
    for name in sorted(servers):
        entry = servers[name] if isinstance(servers[name], dict) else {}
        word = seen['servers'].get(name)
        if entry.get('enabled') is False:
            print('      MCP %s: disabled in the recipe' % name)
            continue
        if not isinstance(entry.get('command'), str):
            # A server reached over a socket has no child process: the journal is all there is.
            check('MCP %s (no child process: reached over a connection): the journal says nothing against it (%s)' % (name, word or 'no line'), word in (None, 'connected'))
            continue
        match, wrapped = child_of(name, servers, free)
        if match:
            free.remove(match)
        check('MCP %s: a child process is there (pid %s) and the journal says nothing against it (%s)' % (
            name, ('%d, started by the wrapper the recipe names' if wrapped else '%d') % match[0] if match else 'none', word or 'no line'),
              match is not None and word in (None, 'connected'))
    # A server the window is for (a new one, or one that did not start before): no line against it is not enough.
    for name in sorted(c.get('servers_connected', [])):
        check('MCP %s: its last connection line in the journal says connected' % name, seen['servers'].get(name) == 'connected',
              (seen['servers'].get(name) or 'no line yet') + ('' if name in servers else '; and the recipe has no server of that name'))
    for name in sorted(set(seen['servers']) - set(servers)):
        print('      MCP %s is named in the journal and not in the recipe: %s' % (name, seen['servers'][name]))
    check('the journal of the unit can be read', seen['answered'])

    # The context budget the process works with, as its own planner states it, against the one before the start.
    agent = recipe.get('agent') if isinstance(recipe.get('agent'), dict) else {}
    strategy = agent.get('strategy') if isinstance(agent.get('strategy'), dict) else {}
    print('      recipe: contextBudgetTokens=%s maxTokens=%s recentWindowTokens=%s overBudgetGraceRatio=%s' % (
        show(agent.get('contextBudgetTokens', ABSENT)), show(agent.get('maxTokens', ABSENT)), show(strategy.get('recentWindowTokens', ABSENT)), show(strategy.get('overBudgetGraceRatio', ABSENT))))
    plan = seen['plan']
    if plan is None:
        say('LOOK', 'no [plan-vs-actual] line since the start yet: the process has not planned a context. Run readback again after its first turn')
    else:
        print('      planner: ' + ' '.join('%s=%s' % (key, plan[key]) for key in sorted(plan)))
        if before and before.get('hard_budget') is not None and c.get('budget_reported_only'):
            # A window that changes the budget of the recipe. What the planner then works with also depends on settings
            # the process keeps in its own state, which nothing here reads or writes: both numbers are said, none is held.
            print('      hardBudget of the planner: %s before the start, %s now (reported, not held to the one before)' % (before['hard_budget'], plan.get('hardBudget')))
        elif before and before.get('hard_budget') is not None:
            check('hardBudget is the one of the process before the start (%s)' % before['hard_budget'], plan.get('hardBudget') == before['hard_budget'], 'now %s' % plan.get('hardBudget'))
        elif first:
            print('      hardBudget of the planner: %s (first install: reported; there is no process before the start to hold it to)' % plan.get('hardBudget'))
        else:
            say('LOOK', 'hardBudget=%s, and there is no value from before the start to hold it to' % plan.get('hardBudget'))


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ('compare', 'baseline', 'readback', 'alone'):
        sys.stderr.write(__doc__)
        return 2
    try:
        with open(sys.argv[2]) as stream:
            c = json.load(stream)
        for key in ('old', 'new', 'root', 'unit', 'recipe_changes', 'recipe_absent', 'recipe_present', 'as_account', 'environment', 'systemctl', 'journalctl', 'baseline_file', 'baseline_since'):
            c[key]
    except (OSError, ValueError, KeyError) as error:
        sys.stderr.write('kitchen-inspect: unusable configuration (%s)\n' % type(error).__name__)
        return 2
    {'compare': compare, 'baseline': baseline, 'readback': readback, 'alone': alone}[sys.argv[1]](c)
    print('kitchen-inspect %s: %s' % (sys.argv[1], 'every line ok' if not looks else '%d line(s) marked LOOK' % looks))
    return 5 if looks else 0


if __name__ == '__main__':
    sys.exit(main())
