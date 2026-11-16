#!/usr/bin/env python3
"""Sandbox trial of roles/connectome_kitchen: every operation through ansible-playbook, on a case of kitchen's deploy corpus.

    trial.py --kitchen CLONE --ansible ANSIBLE_DIR --scratch DIR [--corpus DIR] --case NAME
             [--scenario full|fail-start|short-lived|rotated-at-start]
             [--new-recipe FILE --expect-leaf POINTER ... --changed-leaf POINTER ... --lock-leaf POINTER ... --new-secret-file SECRET=ENV ...]

Not part of `scripts/test.sh`: it needs a built kitchen clone (with its test harness), ansible-core and Node.

What is real: the role and its five scripts; ansible-playbook; kitchen's runner and engine (scripts/takeover.mjs,
src/engine.ts) and its adapter; the case's layers, pins and recipe, moved under a sandbox root; and, for a case that
holds the files of a real build, that build's real cook lock.
What stands in, as in kitchen's own tests of its runner: the host model and the adapter seam of the corpus
(test/corpus/takeover_driver.mjs), cook (a replay of the real lock, or the corpus stand-in), the service manager
(fake_manager.py, which keeps real processes), the journal (a file), and cook's copy of the recipe for the host
(a copy of the staged recipe, made here). No account, no systemd, no network.

The build that runs at the start is made by the Python role's engine with the role's cook command, as the corpus makes
it: for the engine that is a build with another name than its own, which is what a build of an earlier adapter contract
is too.

`--new-recipe` puts another recipe in place before the new build is asked for (the running build keeps the case's own),
and cook is then the corpus stand-in, since a real lock can only be replayed for the recipe it was made from.
`--expect-leaf` names a leaf of the recipe that goes, `--changed-leaf` one that comes or changes, `--lock-leaf` a leaf of
the lock that follows from them. `--new-secret-file` adds a secret file to the agent layer and a placeholder under its
name to the sandbox's secrets file, after the running build was made: the running deployment then has no such file.
Prints one line a check; exit status 1 if one failed.
"""
import argparse, copy, hashlib, json, os, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
results = []


def ok(label, held, detail=''):
    print(('ok   ' if held else 'FAIL ') + label + ('' if held or detail == '' else '\n       ' + str(detail)[-1500:]), flush=True)
    results.append(bool(held))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--kitchen', required=True)
    parser.add_argument('--ansible', required=True, help='a directory with bin/ansible-playbook and lib/')
    parser.add_argument('--scratch', required=True)
    parser.add_argument('--corpus', help='a deploy corpus; default: the synthetic one of the clone')
    parser.add_argument('--case', required=True)
    parser.add_argument('--scenario', default='full', choices=('full', 'fail-start', 'short-lived', 'rotated-at-start'))
    parser.add_argument('--new-recipe')
    parser.add_argument('--expect-leaf', action='append', default=[])
    parser.add_argument('--changed-leaf', action='append', default=[])
    parser.add_argument('--lock-leaf', action='append', default=[])
    parser.add_argument('--new-secret-file', action='append', default=[])
    parser.add_argument('--budget-reported-only', action='store_true', help='readback reports the planner budget before and after and holds none')
    parser.add_argument('--connected', action='append', default=[], help='an MCP server whose journal line must say connected at readback')
    parser.add_argument('--node', default=shutil.which('node'))
    args = parser.parse_args()
    kitchen, scratch, node = Path(args.kitchen).resolve(), Path(args.scratch).resolve(), args.node
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(kitchen / 'test/corpus'), str(kitchen / 'test/differential')]
    import corpus, ts_port
    ts_port.initialize()
    for name in ('connectome_deploy', 'connectome_cook', 'kitchen_v1'):
        __import__(name)
    ts_port.remove(scratch)
    scratch.mkdir(parents=True)
    where = Path(args.corpus).resolve() if args.corpus else corpus.located('synthetic', scratch / 'synthetic')
    case = next(case for case in corpus.cases(where) if case.name == args.case)
    case.data = copy.deepcopy(case.data)
    case.data['layers']['agent']['activation'] = 'start'
    world = corpus.World(case, scratch / 'sandbox')
    world.build()
    role = world.role_apply(world.operator_files())
    sandbox, root, unit_dir, home = world.sandbox, Path(world.root), Path(world.unit_dir), world.home
    name, old = world.effective['name'], role['request_id']
    unit, state = 'connectome-%s.service' % name, Path(world.effective['state_dir'])
    me, uid = __import__('pwd').getpwuid(os.geteuid()).pw_name, os.geteuid()
    replayed = world.replay is not None
    print('case %s: deployment %s, the running build %s, %s' % (case.name, name, old[:8], 'a replay of a real cook lock' if replayed else 'the corpus stand-in for cook'))

    # The service manager, with real processes.
    (sandbox / 'knobs').mkdir()
    fake = sandbox / 'fake-systemctl'
    corpus.write(fake, '#!/bin/sh\nexec %s %s %s %s %s %s "$@"\n' % (sys.executable, HERE / 'fake_manager.py', sandbox, root, unit_dir, state), 0o755)
    sc = lambda *words: subprocess.run([str(fake), '--user', *words], capture_output=True, text=True)
    show = lambda prop: sc('show', '-p', prop, '--value', unit).stdout.strip()
    calls = lambda: [json.loads(line)[0] for line in (sandbox / 'fake-manager-calls').read_text().splitlines() if json.loads(line)[0] in ('restart', 'start', 'stop', 'daemon-reload')]

    def cooked(build):
        """Cook's copy of the recipe for the host, where the stand-in made none: the staged recipe's bytes."""
        target = root / 'cook-config' / build / 'recipes/cook-recipe.json'
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / 'builds' / build / 'cook-recipe.json', target)
    cooked(old)
    sc('daemon-reload'); sc('start', unit)
    pid0 = show('MainPID')
    journal = sandbox / 'journal.txt'
    servers = sorted(json.loads((root / 'builds' / old / 'cook-recipe.json').read_text()).get('mcpServers') or {})
    lines = lambda budget: ''.join('MCPL server "%s" reconnected successfully\n' % server for server in servers) + \
        '[plan-vs-actual] planned=10 actual=10 delta=+0 (+0.0%%) budgetMet=true exhausted=false moves=0 floor=10 hardBudget=%d queuedRelief=0\n' % budget
    journal.write_text(lines(280800))
    stub_journal = sandbox / 'fake-journalctl'
    corpus.write(stub_journal, '#!/bin/sh\nexec /usr/bin/cat %s\n' % journal, 0o755)

    # The kitchen install the role is pointed at, and what stands in for Node: kitchen's own test driver around its runner,
    # and name_seam.mjs around the role's helper, both with the host model of the corpus.
    install = sandbox / 'kitchen-install'
    install.mkdir()
    for leaf in ('dist', 'scripts'):
        (install / leaf).symlink_to(kitchen / leaf)
    commit = subprocess.run(['git', '-C', str(kitchen), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    (install / 'KITCHEN_COMMIT').write_text(commit + '\n')
    stub = sandbox / 'node-stub'
    # Knob secrets-at-start: its bytes become the secrets file at the moment the runner is started for `start`, after
    # the role held the environment and before the engine reads it. Once.
    corpus.write(stub, '#!/bin/sh\nscript=$1; shift\nknob=%s/knobs/secrets-at-start\ncase "$script" in\n'
                 '  */takeover.mjs) if [ "$1" = start ] && [ -f $knob ]; then /usr/bin/cat $knob > %s; /usr/bin/rm $knob; fi\n'
                 '    exec %s %s "$1" "$3" %s %s ;;\n'
                 '  */kitchen-name.mjs) exec %s %s %s "$2" %s %s ;;\n'
                 # The role's own comparison of the environment files needs no seam: it is run as it is on a host.
                 '  */roles/connectome_kitchen/files/kitchen-secrets.mjs) exec %s "$script" "$@" ;;\nesac\necho "node-stub: unexpected script $script" >&2; exit 64\n' % (
                     sandbox, world.effective['secrets_file'], node, kitchen / 'test/corpus/takeover_driver.mjs', sandbox, world.account, node, HERE / 'name_seam.mjs', kitchen, sandbox, world.account, node), 0o755)

    # The layers as files and variables, as an inventory gives them to the Python role.
    inputs = scratch / 'inputs'
    (inputs / 'connectors').mkdir(parents=True)
    raw = world.raw_layers
    (inputs / 'defaults.yml').write_text(json.dumps(raw['defaults']))
    for key, entry in raw['catalog'].items():
        (inputs / 'connectors' / (key + '.yml')).write_text(json.dumps({key: entry}))
    (inputs / 'versions.yml').write_text(json.dumps(world.versions))
    recipe_path = Path(raw['agent']['recipe_file']['path'])
    changes, agent = [], copy.deepcopy(raw['agent'])
    if args.new_recipe:
        text = world.moved(Path(args.new_recipe).read_bytes().decode('utf-8')).encode('utf-8')
        recipe_path.write_bytes(text)
        agent['recipe_file']['sha256'] = hashlib.sha256(text).hexdigest()
        changes = sorted(args.expect_leaf + args.changed_leaf)
        # What a host-installed server of the new recipe points at, where the case's own recipe has no such server
        # (as the corpus lays it out for the case: test/corpus/corpus.py, World.build).
        import connectome_deploy
        for server in (json.loads(text).get('mcpServers') or {}).values():
            if isinstance(server, dict) and isinstance(server.get('source'), dict) and 'external' in server['source']:
                for value in [server['command']] + [connectome_deploy.path_like_arg(raw) for raw in server.get('args', [])]:
                    if value and value.startswith(str(world.sandbox) + '/') and not Path(value).exists():
                        corpus.write(value, '#!/bin/sh\n# stand-in of the trial: never executed\nexit 64\n', 0o755 if value == server['command'] else 0o644)
        # A real lock is the lock of one recipe. For another recipe cook is the corpus stand-in.
        corpus.write(world.tools_moved['runtime'], '#!%s -I\nimport runpy, sys\nsys.dont_write_bytecode = True\nrunpy.run_path(%r, run_name="__main__")\n' % (
            sys.executable, str(kitchen / 'test/corpus/stand_in_cook.py')), 0o755)
    new_files = []
    for pair in args.new_secret_file:
        secret, env = pair.split('=')
        agent['secret_files'] = list(agent.get('secret_files') or []) + [{'secret': secret, 'env': env}]
        secrets_file = Path(world.effective['secrets_file'])
        secrets_file.write_text(json.dumps({**json.loads(secrets_file.read_text()), secret: 'placeholder-of-the-trial-' + secret}, sort_keys=True))
        new_files.append(env)
    world.raw_layers['agent'], world.layers[3] = agent, agent
    import connectome_config
    world.effective = connectome_config.merge_layers(copy.deepcopy(world.layers))
    variables = {
        'connectome_kitchen_home': str(install), 'connectome_kitchen_commit': commit, 'connectome_kitchen_node': str(stub),
        'connectome_kitchen_account': me, 'connectome_kitchen_account_home': str(home), 'connectome_kitchen_account_uid': uid, 'connectome_kitchen_operator_uid': uid,
        'connectome_kitchen_runtime_dir': str(sandbox), 'connectome_kitchen_systemctl': str(fake), 'connectome_kitchen_as_account': [], 'connectome_kitchen_journalctl': str(stub_journal),
        'connectome_base': world.base, 'connectome_root': world.root, 'connectome_unit_dir': world.unit_dir,
        'connectome_private': raw['private'], 'connectome_group': raw['group'], 'connectome_agent': agent,
        'connectome_versions_file': str(inputs / 'versions.yml'), 'connectome_cook_command': world.role_command,
        'connectome_kitchen_defaults_file': str(inputs / 'defaults.yml'), 'connectome_kitchen_catalog_dir': str(inputs / 'connectors'),
        'connectome_kitchen_live_build': old, 'connectome_kitchen_build': '', 'connectome_kitchen_save_dir': str(sandbox / 'save'),
        'connectome_kitchen_watch_seconds': 3, 'connectome_kitchen_recipe_changes': changes,
        'connectome_kitchen_recipe_absent': args.expect_leaf if args.new_recipe else [], 'connectome_kitchen_recipe_present': args.changed_leaf if args.new_recipe else [],
        'connectome_kitchen_lock_changes': args.lock_leaf, 'connectome_kitchen_secret_files_new': new_files, 'connectome_kitchen_servers_connected': args.connected,
        'connectome_kitchen_budget_reported_only': args.budget_reported_only,
    }
    play = scratch / 'play.yml'
    play.write_text('- hosts: localhost\n  connection: local\n  gather_facts: false\n  roles:\n    - role: %s\n' % (REPO / 'roles/connectome_kitchen'))
    ansible = Path(args.ansible).resolve()
    environment = {'PATH': '/usr/bin:/bin', 'HOME': str(scratch / 'controller-home'), 'PYTHONPATH': str(ansible / 'lib'), 'ANSIBLE_NOCOLOR': '1', 'ANSIBLE_LOCAL_TEMP': str(scratch / 'ansible-local'),
                   'ANSIBLE_REMOTE_TEMP': str(scratch / 'ansible-remote'), 'LANG': 'C.UTF-8', 'KITCHEN_BACK_WAIT': '6', 'KITCHEN_BACK_SETTLE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    (scratch / 'controller-home').mkdir()
    log = scratch / 'ansible.log'

    def op(operation, **extra):
        (scratch / 'vars.json').write_text(json.dumps({**variables, 'connectome_kitchen_operation': operation, **extra}))
        done = subprocess.run([sys.executable, str(ansible / 'bin/ansible-playbook'), '-i', 'localhost,', str(play), '-e', '@' + str(scratch / 'vars.json')],
                              env=environment, capture_output=True, text=True, timeout=900, cwd=str(scratch))
        text = done.stdout + done.stderr
        with log.open('a') as stream:
            stream.write('\n===== %s %s: exit %d\n%s' % (operation, json.dumps(extra), done.returncode, text))
        return done.returncode, text.replace(str(sandbox), '<S>')
    pointers = lambda: (os.readlink(root / 'current'), os.readlink(unit_dir / unit), (unit_dir / (unit + '.d') / '90-connectome-env.conf').read_text(),
                        os.readlink(unit_dir / 'default.target.wants' / unit), (root / 'state/ownership.json').read_text())
    at = lambda build: pointers()[0] == str(root / 'builds' / build) and pointers()[1] == pointers()[3] == str(root / 'builds' / build / 'managed-units' / unit) and build in pointers()[2]
    idle = lambda offset=0: 'IDLE %s %s' % (name, time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(time.time() + offset)))
    before = pointers()
    tree = corpus.snapshot(sandbox)

    # name: asked of the engine, held against the oracle's formula with the word of the adapter contract.
    status, out = op('name')
    line = next((line for line in out.splitlines() if 'connectome_kitchen_build: ' in line), '')
    new = line.split('connectome_kitchen_build: ')[1].strip(' ",') if line else ''
    world.node, world.own_name = node, True
    expected = world.request_id()
    ok('name: exit 0 and a build name', status == 0 and len(new) == 64, out)
    ok('name: the adapter contract of this kitchen is kitchen-cook-adapter/2', world.contract == 'kitchen-cook-adapter/2', world.contract)
    ok('name: it is the oracle\'s formula over this request with that word (%s), and not the running build\'s' % new[:8], new == expected and new != old, (new, expected))
    variables['connectome_kitchen_build'] = new
    build = root / 'builds' / new
    changed = lambda: {path for path, row in corpus.snapshot(sandbox).items() if tree.get(path) != row and not path.startswith(str(home / 'kitchen-takeover'))
                       and not path.startswith(str(sandbox / 'save')) and Path(path).name not in corpus.VOLATILE + ('fake-manager-calls', 'fake-manager.json')}
    ok('name: nothing of the deployment was written', changed() == set() and pointers() == before, sorted(changed())[:8])

    status, out = op('plan', connectome_kitchen_build='0' * 63 + '1')
    ok('plan with another pinned name: refused, nothing written', status != 0 and changed() == set() and pointers() == before, out)
    status, out = op('plan')
    ok('plan: exit 0, the plan names the build and says there is a build to make', status == 0 and '"prepare":{"build_dir":"<S>' in out.replace('\\"', '"') and changed() == set(), out)
    status, out = op('verify')
    ok('verify before prepare: refused (the build is not there to hold against the running one)', status != 0 and pointers() == before, out)
    status, out = op('start', connectome_kitchen_idle=idle())
    ok('start before prepare: refused, no restart', status != 0 and pointers() == before and show('MainPID') == pid0 and 'verify has not run' in out, out)

    if new_files:
        # The secrets file without the new secret: what a host shows where the agent has not filled it in yet.
        secrets_file, filled = Path(world.effective['secrets_file']), Path(world.effective['secrets_file']).read_text()
        secrets_file.write_text(json.dumps({key: value for key, value in json.loads(filled).items() if key not in [pair.split('=')[0] for pair in args.new_secret_file]}, sort_keys=True))
        status, out = op('prepare')
        # Whether the build is made without the secret is the engine's affair (here it is: prepare reads no secret). What
        # must hold: the running deployment is as it was, and no verify passes, so no start can follow.
        ok('prepare while the secrets file lacks the new secret (exit %d, %s under the build\'s name): the running deployment is as it was' % (status, 'a directory' if build.exists() else 'nothing'),
           pointers()[:4] == before[:4] and show('MainPID') == pid0, out)
        status, out = op('verify')
        ok('verify while it lacks it: refused, nothing marked for a start', status != 0 and ('does not hold every declared name' in out or 'is not prepared' in out) and not (sandbox / 'save/verified').exists(), out)
        secrets_file.write_text(filled)
    status, out = op('prepare')
    after = pointers()
    ok('prepare: exit 0, the build is prepared', status == 0 and (build / '.prepared').is_file() and (build / '.prepared').read_text() == new, out)
    ok('prepare: the pointer, the unit link, the drop-in and the enable link are as they were; the process too', after[:4] == before[:4] and show('MainPID') == pid0 and set(calls()) <= {'daemon-reload', 'start'} and calls().count('start') == 1)
    ok('prepare: the runner saw the running deployment untouched', '"untouched_by_this_run":true' in out.replace('\\"', '"'), out)
    save = sandbox / 'save'
    ok('prepare: what rollback needs was saved before the engine wrote, by the operator, 0700', (save / 'links').read_text().split() == [before[0], before[1], before[3]] and (save / 'ownership.json').read_text() == before[4]
       and (save / '90-connectome-env.conf').read_text() == before[2] and (save.stat().st_mode & 0o777) == 0o700)
    if replayed and not args.new_recipe:
        real = json.loads((case.directory / case.data['cook_build']['directory'] / 'connectome.lock').read_text())
        made = json.loads((build / 'connectome.lock').read_text())
        ok('prepare: the new build\'s lock is the real lock replayed (cook %s, %d host dependencies, %d external servers)' % (
            real.get('cook', {}).get('version'), len(real.get('hostDependencies', [])), len(real.get('external', []) or [])),
           made.get('cook') == real.get('cook') and len(made.get('hostDependencies', [])) == len(real.get('hostDependencies', [])) and (made.get('external') is None) == (real.get('external') is None))
    cooked(new)
    servers = sorted(json.loads((build / 'cook-recipe.json').read_text()).get('mcpServers') or {})
    status, out = op('prepare')
    ok('prepare again: exit 0, the save is kept and nothing is built twice', status == 0 and 'already saved' in out and (save / 'ownership.json').read_text() == before[4], out)

    status, out = op('verify')
    ok('verify: exit 0', status == 0, out)
    # Two cooks made the two builds where a real lock runs and the new recipe is another one: the running build's unit and
    # launcher are the real build's, the new build's the stand-in's. There the two lines must say LOOK, and do.
    mixed = replayed and bool(args.new_recipe)
    for word in ('unit-same-but-for-the-build-name', 'launcher-same-but-for-the-build-name'):
        ok('verify: %s %s' % ('LOOK (the builds are of two cooks)' if mixed else 'ok', word), (('LOOK  ' if mixed else 'ok    ') + word) in out, [line for line in out.splitlines() if word[:20] in line])
    for word in ('staged recipe differs from the running build\'s in exactly the %d expected leaves' % len(changes), 'cooked recipe differs from the running build\'s in exactly the %d expected leaves' % len(changes)):
        ok('verify: ok ' + word, ('ok    ' + word) in out, [line for line in out.splitlines() if word[:20] in line])
    for leaf in args.expect_leaf if args.new_recipe else []:
        ok('verify: %s is absent from the new build\'s recipe' % leaf, 'ok    staged recipe: %s is absent' % leaf in out)
    for leaf in args.changed_leaf if args.new_recipe else []:
        ok('verify: %s is present in the new build\'s recipe' % leaf, 'ok    cooked recipe: %s is present' % leaf in out)
    if not mixed:
        word = 'the lock differs in %s of the external servers' % ('exactly the %d expected leaves' % len(args.lock_leaf) if args.lock_leaf else 'no leaf')
        ok('verify: ok ' + word, ('ok    ' + word) in out and 'ok    the lock of the new build names its staged recipe by digest' in out, [line for line in out.splitlines() if 'lock' in line])
    if new_files:
        ok('verify: the secrets file gives the new process the running one\'s secret files and the new %s, not there yet' % ', '.join(new_files),
           'and the new ones named (%s), which are not there yet' % ', '.join(sorted(new_files)) in out and not any((root / 'state/secret-files' / env).exists() for env in new_files), out)
    else:
        ok('verify: the secrets file gives the new process the running one\'s environment', 'gives the new process the environment the running one has' in out, out)
    ok('verify: nothing of the running deployment changed', pointers()[:4] == before[:4] and show('MainPID') == pid0)

    status, out = op('start')
    ok('start without the agent\'s IDLE: refused, no restart', status != 0 and 'needs its own word that it is idle' in out and show('MainPID') == pid0 and at(old), out)
    status, out = op('start', connectome_kitchen_idle=idle(-3600))
    ok('start with an IDLE an hour old: refused, no restart', status != 0 and show('MainPID') == pid0 and at(old), out)
    status, out = op('start', connectome_kitchen_idle='IDLE someone-else ' + idle().split(' ')[2])
    ok('start with another agent\'s IDLE: refused, no restart', status != 0 and show('MainPID') == pid0 and at(old), out)
    status, out = op('rollback', connectome_kitchen_idle=idle())
    ok('rollback while the agent runs the saved build: exit 0, nothing changed, nothing restarted', status == 0 and 'Nothing to put back' in out and show('MainPID') == pid0 and pointers() == after, out)
    ok('rollback there says what it is: a prepare that was never started, not a way back that did not finish',
       'ownership.json is not the saved one (as after a prepare that was never started' in out and 'DID NOT FINISH' not in out and not (save / 'back.incomplete').exists(), out)
    status, out = op('clear')
    ok('clear before any start: refused (it is for after a complete way back), nothing changed', status != 0 and 'clear is for after a complete way back' in out and pointers() == after and show('MainPID') == pid0, out)

    # A direct secret that changed since the running process was started: never taken with the new files, never shown.
    import connectome_deploy
    direct = connectome_deploy.direct_secret_names(world.effective)
    secrets_file, filled = Path(world.effective['secrets_file']), Path(world.effective['secrets_file']).read_text()
    if direct:
        target = next((name for name in direct if 'ANTHROPIC' in name), direct[0])
        rotated, was = 'ROTATED-VALUE-OF-THE-TRIAL', json.loads(filled)[target]
        shown = lambda text: rotated in text or (len(was) >= 12 and was in text)
        secrets_file.write_text(json.dumps({**json.loads(filled), target: rotated}, sort_keys=True))
        seen = len(calls())
        status, out = op('verify')
        ok('verify with the direct secret %s changed: it says ANOTHER environment and names that name as changed' % target, status == 0 and 'ANOTHER environment or secret file' in out and 'changed: %s;' % target in out, out)
        status, out = op('start', connectome_kitchen_idle=idle())
        ok('start with the direct secret %s changed%s: refused, no restart, the name said' % (target, ' beside the named new file' if new_files else ''),
           status != 0 and 'another environment or secret file' in out and 'changed: %s;' % target in out and show('MainPID') == pid0 and at(old) and calls()[seen:] == [], (calls()[seen:], out))
        keep = home / 'kitchen-takeover' / name
        ok('neither value of %s is in what the plays printed, in the runner\'s log or in the request' % target,
           not shown(out) and not shown(log.read_text()) and not any(shown(path.read_text(errors='replace')) for path in keep.rglob('*') if path.is_file() and 'cook-recipe' not in path.name))
        secrets_file.write_text(json.dumps({key: value for key, value in json.loads(filled).items() if key != target}, sort_keys=True))
        status, out = op('start', connectome_kitchen_idle=idle())
        ok('start with %s gone from the secrets file: refused, no restart' % target, status != 0 and show('MainPID') == pid0 and at(old) and calls()[seen:] == [], (calls()[seen:], out))
        secrets_file.write_text(filled)
        status, out = op('verify')
        ok('verify with the secrets file as it was: exit 0, no name changed', status == 0 and 'changed: none;' in out and 'ANOTHER environment' not in out, out)
    else:
        print('note the case declares no direct secret: the changed-direct-secret checks do not apply')

    if new_files:
        status, out = op('start', connectome_kitchen_idle=idle(), connectome_kitchen_secret_files_new=[])
        ok('start with the new secret file not named as new: refused, no restart', status != 0 and 'another environment or secret file' in out and show('MainPID') == pid0 and at(old), out)
        status, out = op('verify')
        ok('verify again, with it named: exit 0', status == 0, out)

    if args.scenario == 'fail-start':
        (sandbox / 'knobs' / ('fail-start-' + new[:8])).write_text('')
        seen = len(calls())
        status, out = op('start', connectome_kitchen_idle=idle())
        ok('a start that fails: the play fails and says the engine put the previous build back', status != 0 and 'NOT STARTED' in out and 'the engine put the previous build back and restarted it' in out, out)
        ok('a start that fails: the agent runs the previous build in a new process; the role\'s own way back did not run', at(old) and show('ActiveState') == 'active' and show('MainPID') not in ('0', pid0)
           and 'putting code and configuration back' not in out and calls()[seen:].count('stop') == 0, calls()[seen:])
        return finish(sc, unit, log)
    if args.scenario == 'short-lived':
        (sandbox / 'knobs' / ('short-lived-' + new[:8])).write_text('')
        status, out = op('start', connectome_kitchen_idle=idle())
        ok('a build that does not stay up: the play fails and says it rolled back', status != 0 and 'ROLLED BACK' in out, out)
        ok('a build that does not stay up: the agent runs the previous build again, pointers and ownership.json as saved', at(old) and show('ActiveState') == 'active' and pointers() == before, pointers()[:2])
        status, out = op('start', connectome_kitchen_idle=idle(), connectome_kitchen_auto_rollback=False)
        ok('the same with auto_rollback false: refused before a restart, since ownership.json is the saved one (verify has to run again)', status != 0 and at(old), out)
        return finish(sc, unit, log)

    if args.scenario == 'rotated-at-start':
        # The secrets file changes after the role held it and before the engine reads it: the engine starts the new build
        # with a value nobody held. What it wrote is held after the restart, and that is a failed start.
        old_env = (root / 'builds' / old / 'secrets.env').read_bytes()
        (sandbox / 'knobs/secrets-at-start').write_text(json.dumps({**json.loads(filled), target: rotated}, sort_keys=True))
        seen = len(calls())
        status, out = op('start', connectome_kitchen_idle=idle())
        ok('a secrets file that changes between the check and the start: the play fails, names %s as changed in what the engine wrote, and rolls back' % target,
           status != 0 and 'ROLLED BACK' in out and 'the secrets file changed between the check and the start' in out and ('(changed: %s; added: ' % target) in out and not (sandbox / 'knobs/secrets-at-start').exists(), out)
        ok('there: the agent runs the previous build again on its own environment file, pointers and ownership.json as saved; no value shown',
           at(old) and show('ActiveState') == 'active' and pointers() == before and (root / 'builds' / old / 'secrets.env').read_bytes() == old_env and calls()[seen:] == ['daemon-reload', 'restart', 'stop', 'daemon-reload', 'start']
           and not shown(out) and not shown(log.read_text()), calls()[seen:])
        secrets_file.write_text(filled)
        return finish(sc, unit, log)

    journal.write_text(lines(280800))
    seen = len(calls())
    status, out = op('start', connectome_kitchen_idle=idle())
    ok('start: exit 0', status == 0 and 'STARTED: ' in out, out)
    pid1 = show('MainPID')
    ok('start: one daemon-reload and one restart, no stop', calls()[seen:] == ['daemon-reload', 'restart'], calls()[seen:])
    ok('start: the agent runs the new build in another process, the same after the wait', at(new) and show('ActiveState') == 'active' and pid1 not in ('0', pid0))
    ok('start: verify afterwards finds nothing to change', 'verify after the start: nothing to change' in out, out)
    written = "environment file as written, against the previous build's: changed: none; added: %s; removed: none" % (', '.join(sorted(new_files)) or 'none')
    ok('start: the environment file the engine wrote differs from the previous build\'s in %s' % ('exactly the named new ' + ', '.join(sorted(new_files)) if new_files else 'no name'), written in out, out)
    for env in new_files:
        ok('start: the new secret file %s was written by the engine, for the account alone' % env, (root / 'state/secret-files' / env).is_file() and ((root / 'state/secret-files' / env).stat().st_mode & 0o777) == 0o600)
    status, out = op('readback')
    ok('readback: exit 0, every line ok', status == 0 and 'readback: every line ok' in out, out)
    ok('readback: one child process for each of the %d MCP servers of the recipe' % len(servers), all(('ok    MCP %s: a child process is there' % server) in out or ('ok    MCP %s (no child process' % server) in out for server in servers), out)
    if args.budget_reported_only:
        ok('readback: the recipe\'s budget and the planner\'s hardBudget before and after are reported, none held',
           'hardBudget of the planner: 280800 before the start, 280800 now (reported, not held' in out and 'recipe: contextBudgetTokens=' in out and 'hardBudget is the one of the process' not in out, out)
    else:
        ok('readback: hardBudget is the one from before the start', 'ok    hardBudget is the one of the process before the start (280800)' in out, out)
    for server in args.connected:
        ok('readback: the journal says %s is connected' % server, 'ok    MCP %s: its last connection line in the journal says connected' % server in out, out)
        journal.write_text(lines(280800).replace('MCPL server "%s" reconnected successfully\n' % server, ''))
        status, out = op('readback')
        ok('readback with no line of %s in the journal: LOOK' % server, 'LOOK  MCP %s: its last connection line in the journal says connected: no line yet' % server in out, out)
    journal.write_text(lines(302400).replace('reconnected successfully', 'reconnect failed: text that must not be shown', 1))
    status, out = op('readback')
    ok('readback with another budget and a failed server: LOOK lines, and no text of the journal line', ('LOOK  hardBudget' not in out and '280800 before the start, 302400 now' in out if args.budget_reported_only else 'LOOK  hardBudget' in out)
       and 'LOOK  MCP ' in out and 'must not be shown' not in out and 'readback: LOOK lines above' in out, out)
    status, out = op('start', connectome_kitchen_idle=idle())
    ok('start a second time: refused, no restart', status != 0 and show('MainPID') == pid1 and at(new), out)

    status, out = op('rollback')
    ok('rollback without the agent\'s IDLE: refused, no restart', status != 0 and show('MainPID') == pid1 and at(new), out)
    status, out = op('clear')
    ok('clear while the agent runs the new build: refused, nothing removed', status != 0 and 'clear is for after a complete way back' in out and all((root / 'state/secret-files' / env).is_file() for env in new_files) and show('MainPID') == pid1, out)
    seen = len(calls())
    status, out = op('rollback', connectome_kitchen_idle=idle())
    ok('rollback: exit 0', status == 0 and 'the agent runs the saved build' in out, out)
    ok('rollback: the pointers, the drop-in and ownership.json are what prepare saved; the agent runs the previous build', pointers() == before and at(old) and show('ActiveState') == 'active' and show('MainPID') not in ('0', pid1))
    ok('rollback: one stop, one daemon-reload, one start', calls()[seen:] == ['stop', 'daemon-reload', 'start'], calls()[seen:])
    ok('rollback: the agent\'s state directory, both builds and the secrets file are untouched', (build / '.prepared').is_file() and (root / 'builds' / old / '.prepared').is_file())
    status, out = op('plan')
    ok('after rollback, plan refuses the new build directory as unmanaged (ownership.json is the saved one)', status != 0 and 'unmanaged build directory' in out, out)
    shutil.rmtree(build); shutil.rmtree(root / 'cook-config' / new, ignore_errors=True); shutil.rmtree(root / 'cook-credentials' / new, ignore_errors=True)
    status, out = op('plan')
    ok('with that directory removed, plan is as before the window: exit 0, a build to make', status == 0, out)

    # The retry, as docs/kitchen.md words it: the three directories are gone; clear; plan, prepare, verify, start.
    files = root / 'state/secret-files'
    listed = lambda: sorted(path.name for path in files.iterdir()) if files.is_dir() else []
    was_listed = (save / 'secret-files').read_text().split()
    pid2, seen = show('MainPID'), len(calls())
    if new_files:
        ok('after rollback the secret file the start wrote is still there, and the saved deployment does not declare it', listed() == sorted(was_listed + new_files), listed())
        if was_listed:
            status, out = op('clear', connectome_kitchen_secret_files_new=was_listed[:1])
            ok('clear of a secret file that was there before the window (%s): refused, nothing removed' % was_listed[0], status != 0 and 'was a secret file before the window' in out and listed() == sorted(was_listed + new_files), out)
        digests = {leaf: hashlib.sha256((files / leaf).read_bytes()).hexdigest() for leaf in was_listed}
        status, out = op('clear')
        ok('clear: exit 0, it removed exactly %s; every secret file from before the window is byte for byte there; nothing restarted' % ', '.join(new_files),
           status == 0 and listed() == was_listed and digests == {leaf: hashlib.sha256((files / leaf).read_bytes()).hexdigest() for leaf in was_listed}
           and all('removed <S>' in out and ('/state/secret-files/%s (introduced by this window' % env) in out for env in new_files) and show('MainPID') == pid2 and calls()[seen:] == [] and pointers() == before, out)
    status, out = op('clear')
    ok('clear with nothing of this window there: exit 0, nothing removed, nothing restarted', status == 0 and 'removed ' not in out and listed() == was_listed and show('MainPID') == pid2 and calls()[seen:] == [] and pointers() == before, out)
    status, out = op('plan')
    ok('retry: plan exit 0', status == 0, out)
    status, out = op('prepare')
    ok('retry: prepare exit 0, the build is prepared again and the first save is kept', status == 0 and (build / '.prepared').is_file() and 'already saved' in out and (save / 'ownership.json').read_text() == before[4] and show('MainPID') == pid2, out)
    cooked(new)
    status, out = op('verify')
    ok('retry: verify exit 0, with the environment %s' % ('of the running process and the named new file, not there' if new_files else 'of the running process'),
       status == 0 and (('and the new ones named (%s), which are not there yet' % ', '.join(sorted(new_files))) if new_files else 'gives the new process the environment the running one has') in out, out)
    journal.write_text(lines(280800))
    seen = len(calls())
    status, out = op('start', connectome_kitchen_idle=idle())
    pid3 = show('MainPID')
    ok('retry: start exit 0, with no bypass of the secrets check', status == 0 and 'STARTED: ' in out and written in out, out)
    ok('retry: one daemon-reload and one restart; the agent runs the new build in another process', calls()[seen:] == ['daemon-reload', 'restart'] and at(new) and show('ActiveState') == 'active' and pid3 not in ('0', pid2), calls()[seen:])
    for env in new_files:
        ok('retry: the start wrote %s again, for the account alone' % env, (files / env).is_file() and ((files / env).stat().st_mode & 0o777) == 0o600)

    # A way back that fails at ownership.json, and the next one: it must put ownership.json back before it says done.
    state_mode = (root / 'state').stat().st_mode & 0o777
    os.chmod(root / 'state', 0o500)
    try:
        status, out = op('rollback', connectome_kitchen_idle=idle())
    finally:
        os.chmod(root / 'state', state_mode)
    ok('a way back that cannot write ownership.json: exit 62, the play fails, and it is kept as unfinished',
       status != 0 and 'back: exit 62' in out and 'STEP FAILED' in out and (save / 'back.incomplete').exists() and at(old) and pointers()[4] != before[4], out)
    seen = len(calls())
    status, out = op('rollback', connectome_kitchen_agent_is_down=True)
    ok('the next way back: it says the earlier one did not finish, puts all five back, and only then exit 0',
       status == 0 and 'AN EARLIER WAY BACK DID NOT FINISH' in out and 'ownership.json are the saved ones' in out and pointers() == before and at(old) and show('ActiveState') == 'active' and not (save / 'back.incomplete').exists(), out)
    ok('the next way back: one stop, one daemon-reload, one start', calls()[seen:] == ['stop', 'daemon-reload', 'start'], calls()[seen:])
    pid4, seen = show('MainPID'), len(calls())
    status, out = op('rollback', connectome_kitchen_idle=idle())
    ok('rollback once more: nothing to put back, ownership.json is the saved one, nothing restarted', status == 0 and 'Nothing to put back' in out and 'ownership.json is the saved one.' in out and show('MainPID') == pid4 and calls()[seen:] == [], out)
    return finish(sc, unit, log)


def finish(sc, unit, log):
    sc('stop', unit)
    print('%d of %d checks passed; the plays are in %s' % (sum(results), len(results), log))
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
