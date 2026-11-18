"""roles/connectome_kitchen without a host, a kitchen or Ansible: what its files are, and what its scripts say.

The role on a sandbox, through ansible-playbook and a real kitchen, is tests/kitchen/trial.py.
"""
import hashlib, importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / 'roles/connectome_kitchen'
OLD, NEW = 'a' * 64, 'b' * 64


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Files(unittest.TestCase):
    def tasks(self):
        found = []

        def walk(node):
            if isinstance(node, list):
                for item in node:
                    walk(item)
            elif isinstance(node, dict):
                found.append(node)
                for key in ('block', 'rescue', 'always'):
                    walk(node.get(key, []))
        for path in sorted((ROLE / 'tasks').glob('*.yml')):
            walk(yaml.safe_load(path.read_text()))
        return found

    def test_the_role_never_reaches_the_python_engine(self):
        text = ''.join(path.read_text() for path in sorted(ROLE.rglob('*')) if path.is_file() and path.suffix in ('.yml', '.sh', '.mjs', '.py') and path.parent.name != 'filter_plugins')
        for word in ('connectome_release', 'cook-adapter.py', 'connectome_deploy', 'roles/connectome_agent', 'import_role', 'include_role'):
            self.assertNotIn(word, text)
        modules = {key for task in self.tasks() for key in task if key.startswith('ansible.builtin.') or '.' not in key and key in ('connectome_release',)}
        self.assertEqual(modules, {'ansible.builtin.' + name for name in ('assert', 'set_fact', 'shell', 'command', 'include_vars', 'include_tasks', 'file', 'stat', 'copy', 'debug', 'fail')})
        # The one thing taken from the Python role is the catalog step of the layers, a function without effects.
        plugin = (ROLE / 'filter_plugins/connectome_kitchen.py').read_text()
        self.assertIn('connectome_agent/module_utils/connectome_config.py', plugin)
        self.assertIn("'connectome_kitchen_public_catalog': _config.public_catalog", plugin)

    def test_no_task_asks_the_service_manager_to_change_anything(self):
        for task in self.tasks():
            for module in ('ansible.builtin.command', 'ansible.builtin.shell'):
                words = json.dumps(task.get(module, ''))
                for verb in ('restart', 'daemon-reload', ' stop', ' start ', 'enable', 'kill'):
                    self.assertNotIn(verb, words.replace('kitchen-step', '').replace("'start'", ''), task.get('name'))
        # Restarts come from two places only: the runner of kitchen in start, and the way back.
        pointers = (ROLE / 'files/kitchen-pointers.sh').read_text()
        self.assertEqual(sorted(line.split()[2] for line in pointers.splitlines() if line.strip().startswith('step sc ')), ['daemon-reload', 'start', 'stop'])

    def test_operations_gates_and_defaults(self):
        main = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())
        first = next(task for task in main if task['name'] == 'Refuse an unusable invocation before anything is read')['ansible.builtin.assert']['that']
        # The eight operations of a deployment that runs; `remove` is there for a first install alone.
        self.assertIn("connectome_kitchen_operation in ['name', 'plan', 'prepare', 'verify', 'start', 'rollback', 'readback', 'clear'] or (connectome_kitchen_operation == 'remove' and connectome_kitchen_first_install | bool)", first)
        self.assertIn('not ansible_check_mode', first)
        self.assertIn("ansible_connection == 'local'", first)
        names = [task['name'] for task in main]
        gates = [names.index(name) for name in ('Refuse to restart an agent from inside its own unit', "Refuse a restart without the agent's own IDLE for this run")]
        acts = [names.index(name) for name in ('Start the agent on the prepared build', 'Put the previous build back')]
        self.assertLess(max(gates), min(acts))
        for name in ('Refuse to restart an agent from inside its own unit', "Refuse a restart without the agent's own IDLE for this run"):
            self.assertTrue(str(main[names.index(name)]['when']).startswith('ck_restarts'))
        defaults = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())
        self.assertEqual((defaults['connectome_kitchen_idle'], defaults['connectome_kitchen_agent_is_down'], defaults['connectome_kitchen_auto_rollback'], defaults['connectome_kitchen_secrets_differ_ok']), ('', False, True, False))
        self.assertEqual(defaults['connectome_kitchen_environment'][3:5], ['PATH=/usr/bin:/bin', 'LANG=C.UTF-8'])
        self.assertEqual(len(defaults['connectome_kitchen_environment']), 7)
        doc = (ROOT / 'docs/kitchen.md').read_text()
        # clear restarts nothing and asks nothing of the engine: it passes neither gate and builds no request.
        self.assertIn("ck_restarts: \"{{ connectome_kitchen_operation in ['start', 'rollback'] }}\"", (ROLE / 'tasks/main.yml').read_text())
        self.assertIn("ck_engine: \"{{ connectome_kitchen_operation in ['name', 'plan', 'prepare', 'verify', 'start'] }}\"", (ROLE / 'tasks/main.yml').read_text())
        for operation in ('name', 'plan', 'prepare', 'verify', 'start', 'rollback', 'readback', 'clear'):
            self.assertIn('| `%s` |' % operation, doc)

    def test_a_first_install_is_a_mode_of_its_own_and_leaves_the_takeover_tasks_as_they_were(self):
        main = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())
        first = next(task for task in main if task['name'] == 'Refuse an unusable invocation before anything is read')['ansible.builtin.assert']['that']
        # No live build with it, a live build without it; no rollback and no clear with it; remove only with it.
        for line in ("connectome_kitchen_live_build is match('^[0-9a-f]{64}$') or (connectome_kitchen_first_install | bool)", "connectome_kitchen_live_build == '' or not (connectome_kitchen_first_install | bool)",
                     "not (connectome_kitchen_first_install | bool) or connectome_kitchen_operation not in ['rollback', 'clear']", "connectome_kitchen_operation in ['name', 'plan'] or connectome_kitchen_build != ''"):
            self.assertIn(line, first)
        defaults = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())
        self.assertEqual((defaults['connectome_kitchen_first_install'], defaults['connectome_kitchen_operator_word']), (False, ''))
        self.assertNotIn('connectome_kitchen_remove_state', defaults)
        self.assertEqual(main[0]['ansible.builtin.assert']['that'], 'connectome_kitchen_remove_state is not defined')
        names = [task['name'] for task in main]
        when = lambda name: str(main[names.index(name)]['when'])
        # The gates: the executor for a removal too, and the operator's own word where no agent can say IDLE. Both before any act.
        word = "Refuse a first start or a removal without the operator's own dated word for this run"
        self.assertEqual(when('Refuse to restart an agent from inside its own unit'), 'ck_restarts or ck_removes')
        self.assertEqual(when(word), 'ck_first and (ck_restarts or ck_removes)')
        self.assertIn('not ck_first', when("Refuse a restart without the agent's own IDLE for this run"))
        acts = [names.index(name) for name in ('Start the agent on the prepared build', 'Start the agent for the first time', 'Stop the agent and remove its first install', 'Put the previous build back')]
        self.assertLess(names.index(word), min(acts))
        gate = main[names.index(word)]
        self.assertIn("ck_word: \"{{ 'FIRST-START' if ck_restarts else 'REMOVE' }}\"", (ROLE / 'tasks/main.yml').read_text())
        self.assertIn("connectome_kitchen_operator_word is match('^' + ck_word + ' ' + ck_name + ' [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z$')", gate['ansible.builtin.assert']['that'])
        # Each task that saves, compares with or marks against a running build is the takeover's alone, and each of the
        # first install's is its own: none of them registers a result under a name the other reads.
        takeover = ('Keep what rollback needs (the links, the drop-in, ownership.json)', 'Show what was kept', 'Hold the new build against the running one, and keep the baseline of the process for readback',
                    'Hold what verify found, for the start', 'Say where verify leaves it', 'Start the agent on the prepared build')
        own = ('Hold that nothing is deployed, and keep the marker of a first install', 'Show what was held', 'Stop unless nothing is deployed', 'Hold the new build by itself',
               'Hold what verify found of the first build, for the start', 'Say where verify leaves a first install', 'Start the agent for the first time')
        for name in takeover:
            self.assertTrue(when(name).endswith(' and not ck_first'), name)
        for name in own:
            self.assertTrue(when(name).endswith(' and ck_first'), name)
        registered = lambda chosen: {task['register'] for task in main if task['name'] in chosen and 'register' in task}
        self.assertEqual((registered(takeover), registered(own)), ({'ck_saved'}, {'ck_first_holds'}))
        self.assertEqual(when('Stop the agent and remove its first install'), 'ck_removes')
        # The first start and the removal use the first install's own script and never the pointers of a running deployment.
        for leaf in ('start-first.yml', 'remove.yml'):
            text = (ROLE / 'tasks' / leaf).read_text()
            self.assertNotIn('ck_pointers', text)
            self.assertNotIn('kitchen-secrets', text)
        self.assertNotIn('ck_first', (ROLE / 'tasks/start.yml').read_text() + (ROLE / 'tasks/rollback.yml').read_text())
        # The runner is told that nothing is deployed with null, and with the live build's name otherwise.
        self.assertIn("live_build: \"{{ none if (connectome_kitchen_first_install | bool) else connectome_kitchen_live_build }}\"", (ROLE / 'tasks/request.yml').read_text())
        runner = yaml.safe_load((ROLE / 'tasks/runner.yml').read_text())
        for task in runner:
            if 'kitchen-secrets' in json.dumps(task) or task.get('register') == 'ck_compared':
                self.assertEqual(task['when'], "ck_mode == 'verify' and not ck_first", task['name'])
        # What changes the service manager for a first install: its one script, with a stop, a reload and no start.
        script = (ROLE / 'files/kitchen-first.sh').read_text()
        asked = sorted({line.split('sc ', 1)[1].split()[0] for line in script.splitlines() if not line.lstrip().startswith('#') and ('step sc ' in line or line.lstrip().startswith('sc ') or '; sc ' in line)})
        self.assertEqual(asked, ['daemon-reload', 'reset-failed', 'stop'])
        for verb in (' start ', 'restart', 'enable', 'disable', 'kill', 'mask'):
            self.assertNotIn(verb, '\n'.join(line for line in script.splitlines() if 'sc ' in line and not line.lstrip().startswith('#')))
        doc = (ROOT / 'docs/kitchen.md').read_text()
        for text in ('## A first install', '| `remove` |', 'connectome_kitchen_first_install', 'connectome_kitchen_operator_word', 'connectome_kitchen_remove_state', 'FIRST-START', 'REMOVE'):
            self.assertIn(text, doc)

    def test_engines_from_the_cook_command(self):
        plugin = load(ROLE / 'filter_plugins/connectome_kitchen.py', 'kitchen_filter')
        argv = ['/usr/bin/python3', '/x/cook-adapter.py', '--runtime', '/b/bun', '--entrypoint', '/c/bin/cook', '--bun-path', '/b/bun', '--node-path', '/n/node', '--spec', '{recipe}']
        self.assertEqual(plugin.engines(argv), {'cook': {'runtime': '/b/bun', 'entrypoint': '/c/bin/cook', 'bun': '/b/bun', 'node': '/n/node'}})
        for bad in ([], argv[:4], [word if word != '/n/node' else 'node' for word in argv], None):
            with self.assertRaises(ValueError):
                plugin.engines(bad)


class Scripts(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)

    def test_pointers_refuses_before_any_change(self):
        script = ['/bin/bash', str(ROLE / 'files/kitchen-pointers.sh')]
        done = subprocess.run(script + ['back'], env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
        self.assertEqual((done.returncode, 'REFUSED, nothing changed' in done.stdout), (60, True))
        env = {'PATH': '/usr/bin:/bin', **{key: 'x' for key in ('KITCHEN_NAME', 'KITCHEN_ACCOUNT', 'KITCHEN_UID', 'KITCHEN_OPERATOR_UID', 'KITCHEN_ACCOUNT_HOME', 'KITCHEN_RUNTIME_DIR', 'KITCHEN_ROOT',
                                                              'KITCHEN_UNIT_DIR', 'KITCHEN_SAVE', 'KITCHEN_SYSTEMCTL')}, 'KITCHEN_OLD': OLD}
        for words in ([], ['runs'], ['runs', 'not-a-build'], ['back', 'extra'], ['restart'], ['mark', NEW, 'a b', 'c']):
            done = subprocess.run(script + words, env=env, capture_output=True, text=True)
            self.assertEqual(done.returncode, 60, words)
        done = subprocess.run(script + ['look'], env={**env, 'KITCHEN_OLD': 'short'}, capture_output=True, text=True)
        self.assertEqual((done.returncode, 'not a build name' in done.stdout), (60, True))
        # Another operator than the one named: refused, whatever the mode.
        done = subprocess.run(script + ['back'], env={**env, 'KITCHEN_OPERATOR_UID': str(os.geteuid() + 1)}, capture_output=True, text=True)
        self.assertEqual((done.returncode, 'run it as the operator' in done.stdout), (60, True))

    def test_first_install_inspection_requires_a_recipe_digest(self):
        root = self.work / 'deployment'
        build = root / 'builds' / NEW
        build.mkdir(parents=True)
        unit = 'connectome-test.service'
        for leaf, text in (('.prepared', NEW), ('run.sh', ''), (unit, ''), ('cook-recipe.json', '{}')):
            (build / leaf).write_text(text)
        config = self.work / 'config.json'
        config.write_text(json.dumps(dict(root=str(root), new=NEW, unit=unit, old='', first_install=True,
                                              recipe_changes=[], recipe_absent=[], recipe_present=[], as_account=[], environment=[],
                                              systemctl='/bin/false', journalctl='/bin/false', baseline_file='', baseline_since='')))
        for lock in ('{}', 'not-json'):
            (build / 'connectome.lock').write_text(lock)
            done = subprocess.run([sys.executable, '-I', str(ROLE / 'files/kitchen-inspect.py'), 'alone', str(config)],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 5, done.stdout + done.stderr)
            self.assertIn('LOOK  the lock of the new build names its staged recipe by digest', done.stdout)

    def test_step_shows_the_closed_part_only(self):
        runner = self.work / 'runner'
        lines = [{'step': 'start', 'environment_names': ['HOME']},
                 {'step': 'plan answered', 'status': 0, 'core': {'changed': True, 'prepare': {'build_dir': '/r/builds/' + NEW, 'changed': False}, 'activate': {'changed': True}}},
                 {'step': 'secrets file', 'environment_file_of_current_build_is_what_start_would_write': True, 'secret_files_are_what_start_would_write': {'A': True}},
                 {'step': 'engine failure that is not a refusal', 'error': 'TEXT-OF-AN-EXCEPTION'},
                 {'step': 'before', 'target': 'x' * 2000}, {'step': 'running deployment', 'untouched_by_this_run': True, 'pad': 'y' * 2000}, {'step': 'end', 'status': 3}]
        runner.write_text('#!/bin/sh\ncat >&2 <<"EOF"\n%s\ntakeover: the runner refused\nnot json\nEOF\nexit 3\n' % '\n'.join(json.dumps(line) for line in lines))
        runner.chmod(0o755)
        done = subprocess.run([sys.executable, '-I', str(ROLE / 'files/kitchen-step.py'), 'verify', str(runner)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        out = done.stdout.splitlines()
        result = json.loads(out[-1][len('result '):])
        self.assertEqual((result['rc'], result['same_environment'], result['unclosed'], result['plan']['prepare']['build_dir']), (3, True, 1, '/r/builds/' + NEW))
        self.assertEqual((result['environment_file'], result['secret_files']), (True, {'A': True}))
        self.assertNotIn('TEXT-OF-AN-EXCEPTION', done.stdout)
        self.assertNotIn('environment_names', done.stdout)
        self.assertIn('takeover: the runner refused', out)
        self.assertIn('runner verify: exit 3 (the runner refused; the last line says why)', out)
        self.assertTrue(all(len(line) <= 901 for line in out[:-1]))
        # A secret file that is not what start would write, or no secrets line at all: not the same environment.
        for change in ({'A': False}, {'A': None}):
            lines[2]['secret_files_are_what_start_would_write'] = change
            runner.write_text('#!/bin/sh\ncat >&2 <<"EOF"\n%s\nEOF\n' % '\n'.join(json.dumps(line) for line in lines))
            done = subprocess.run([sys.executable, '-I', str(ROLE / 'files/kitchen-step.py'), 'verify', str(runner)], capture_output=True, text=True)
            result = json.loads(done.stdout.splitlines()[-1][7:])
            self.assertIs(result['same_environment'], False)
            self.assertEqual(result['secret_files'], change)
        # Only yes, no and null leave the step, whatever the runner wrote.
        lines[2] = {'step': 'secrets file', 'environment_file_of_current_build_is_what_start_would_write': 'TEXT', 'secret_files_are_what_start_would_write': {'A': 'TEXT'}}
        runner.write_text('#!/bin/sh\ncat >&2 <<"EOF"\n%s\nEOF\n' % '\n'.join(json.dumps(line) for line in lines))
        done = subprocess.run([sys.executable, '-I', str(ROLE / 'files/kitchen-step.py'), 'verify', str(runner)], capture_output=True, text=True)
        result = json.loads(done.stdout.splitlines()[-1][7:])
        self.assertEqual((result['same_environment'], result['environment_file'], result['secret_files']), (False, None, {'A': None}))
        self.assertEqual(subprocess.run([sys.executable, '-I', str(ROLE / 'files/kitchen-step.py'), 'restart', str(runner)], capture_output=True).returncode, 2)
        self.assertEqual(subprocess.run([sys.executable, '-I', str(ROLE / 'files/kitchen-step.py'), 'check', str(self.work / 'absent')], capture_output=True).returncode, 2)

    def builds(self, new_recipe, lock=None, lock_changes=()):
        root = self.work / 'root'
        recipe = {'agent': {'systemPrompt': 'SECRET-PROMPT-TEXT', 'strategy': {'keep': 1, 'dropped': True, 'curve': 0}}, 'mcpServers': {}, 'path': '/x/builds/%s/app' % OLD}
        for build, document in ((OLD, recipe), (NEW, new_recipe(json.loads(json.dumps(recipe).replace(OLD, NEW))))):
            for target in (root / 'builds' / build / 'cook-recipe.json', root / 'cook-config' / build / 'recipes/cook-recipe.json'):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(document))
            for leaf, text in (('connectome-x.service', 'ExecStart=/x/builds/%s/run.sh\n'), ('run.sh', 'exec bun /x/builds/%s/app\n'), ('.prepared', '%s'), ('connectome.lock', '{"createdAt": "%s", "engines": {"bun": "1"}}')):
                (root / 'builds' / build / leaf).write_text(text % build)
            if lock:
                digest = hashlib.sha256((root / 'builds' / build / 'cook-recipe.json').read_bytes()).hexdigest()
                (root / 'builds' / build / 'connectome.lock').write_text(json.dumps(lock(build == NEW, digest)))
        config = {'old': OLD, 'new': NEW, 'root': str(root), 'unit': 'connectome-x.service', 'recipe_changes': ['/agent/strategy/curve', '/agent/strategy/dropped'], 'recipe_absent': ['/agent/strategy/dropped', '/agent/strategy/curve'],
                  'recipe_present': ['/agent/strategy/keep'], 'lock_changes': list(lock_changes), 'as_account': [], 'environment': [], 'systemctl': '/bin/false', 'journalctl': '/bin/false', 'baseline_file': str(self.work / 'baseline.json'), 'baseline_since': '-1h'}
        (self.work / 'inspect.json').write_text(json.dumps(config))
        return subprocess.run([sys.executable, '-I', str(ROLE / 'files/kitchen-inspect.py'), 'compare', str(self.work / 'inspect.json')], capture_output=True, text=True)

    def test_compare_names_leaves_and_never_a_text_value(self):
        def drop(document):
            del document['agent']['strategy']['dropped'], document['agent']['strategy']['curve']
            return document
        done = self.builds(drop)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for line in ('ok    unit-same-but-for-the-build-name', 'ok    launcher-same-but-for-the-build-name', "ok    staged recipe differs from the running build's in exactly the 2 expected leaves",
                     "ok    cooked recipe differs from the running build's in exactly the 2 expected leaves", 'ok    staged recipe: /agent/strategy/dropped is absent',
                     'ok    cooked recipe: /agent/strategy/keep is present (1)', '      staged recipe /agent/strategy/dropped: true -> absent', 'kitchen-inspect compare: every line ok'):
            self.assertIn(line, done.stdout.splitlines())

        def more(document):
            drop(document)
            document['agent']['systemPrompt'] = 'ANOTHER-SECRET-TEXT'
            document['agent']['strategy']['keep'] = 2
            return document
        done = self.builds(more)
        self.assertEqual(done.returncode, 5)
        self.assertIn('      staged recipe /agent/systemPrompt: <str> -> <str>', done.stdout.splitlines())
        self.assertIn('      staged recipe /agent/strategy/keep: 1 -> 2', done.stdout.splitlines())
        self.assertIn("LOOK  staged recipe differs from the running build's in exactly the 2 expected leaves: unexpected ['/agent/strategy/keep', '/agent/systemPrompt']; missing []", done.stdout.splitlines())
        self.assertNotIn('SECRET', done.stdout)
        # A key that was to go and is still there.
        done = self.builds(lambda document: document)
        self.assertEqual(done.returncode, 5)
        self.assertIn('LOOK  staged recipe: /agent/strategy/dropped is absent: present: true', done.stdout.splitlines())

    def test_compare_reads_the_lock_by_name(self):
        def drop(document):
            del document['agent']['strategy']['dropped'], document['agent']['strategy']['curve']
            return document
        no, some = 'the lock differs in no leaf of the external servers, engines, requirements, credentials, host or pinned packages', \
            'the lock differs in exactly the 2 expected leaves of the external servers, engines, requirements, credentials, host or pinned packages'

        def lock(more, wrong=False):
            return lambda new, digest: {'recipe': {'sha256': 'f' * 64 if wrong and new else digest}, 'credentials': ['A_KEY'] + (['B_FILE'] if new and more else []),
                                        'external': [{'name': name, 'command': '/c'} for name in ['a'] + (['b'] if new and more else []) + ['c']]}
        # Another recipe, so another digest of it in the lock: not a leaf to stop at, as long as each lock names its own staged recipe.
        done = self.builds(drop, lock(False))
        self.assertEqual(done.returncode, 0, done.stdout)
        for line in ('      lock /recipe/sha256: <str> -> <str>', 'ok    the lock of the new build names its staged recipe by digest', 'ok    ' + no):
            self.assertIn(line, done.stdout.splitlines())
        # One server and one credential more: two leaves by their names, a LOOK unless they are the expected ones.
        done = self.builds(drop, lock(True))
        self.assertEqual(done.returncode, 5)
        self.assertIn("LOOK  %s: unexpected ['/credentials/B_FILE', '/external/b']; missing []" % no, done.stdout.splitlines())
        done = self.builds(drop, lock(True), ['/external/b', '/credentials/B_FILE'])
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn('ok    ' + some, done.stdout.splitlines())
        self.assertIn('      lock /external/b: absent -> <dict>', done.stdout.splitlines())
        # An expected leaf that is not there, and a lock that names another recipe than the staged one.
        done = self.builds(drop, lock(False), ['/external/b', '/credentials/B_FILE'])
        self.assertIn("LOOK  %s: unexpected []; missing ['/credentials/B_FILE', '/external/b']" % some, done.stdout.splitlines())
        done = self.builds(drop, lock(False, wrong=True))
        self.assertEqual(done.returncode, 5)
        self.assertIn('LOOK  the lock of the new build names its staged recipe by digest', done.stdout.splitlines())
        self.assertIn("LOOK  %s: unexpected ['/recipe/sha256']; missing []" % no, done.stdout.splitlines())

    def test_new_secret_files_are_the_only_other_environment_a_start_takes(self):
        runner = yaml.safe_load((ROLE / 'tasks/runner.yml').read_text())
        expression = next(task for task in runner if 'ck_environment' in task.get('ansible.builtin.set_fact', {}))['ansible.builtin.set_fact']['ck_environment']
        try:
            import jinja2
        except ImportError:
            self.skipTest('no jinja2 here; tests/kitchen/trial.py holds the same through ansible-playbook')
        environment = jinja2.Environment()
        environment.filters['dict2items'] = lambda record: [{'key': key, 'value': value} for key, value in record.items()]  # as ansible.builtin.dict2items
        names = lambda changed=(), added=(), removed=(), files=('A', 'B'), compared=True: {'compared': compared, 'same': ['K'], 'changed': list(changed), 'added': list(added), 'removed': list(removed), 'secret_files': list(files)}
        render = lambda new, same, env, files, secrets: environment.from_string(expression).render(
            connectome_kitchen_secret_files_new=new, ck_result={'same_environment': same, 'environment_file': env, 'secret_files': files}, ck_secrets=secrets).strip()
        self.assertEqual(render([], True, True, {'A': True}, names()), 'same-environment')
        self.assertEqual(render(['B'], True, True, {'A': True, 'B': True}, names()), 'same-environment')
        self.assertEqual(render(['B'], False, False, {'A': True, 'B': None}, names(added=['B'])), 'new-secret-files')
        self.assertEqual(render(['B', 'C'], False, False, {'A': True, 'B': None, 'C': None}, names(added=['B', 'C'], files=['A', 'B', 'C'])), 'new-secret-files')
        # What the runner alone cannot tell (the review's F1): with the new file named and absent, and every other
        # secret file as it was, a direct name that changed, came or went is another environment.
        for secrets in (names(changed=['ANTHROPIC_API_KEY'], added=['B']), names(added=['B', 'ANTHROPIC_BASE_URL']), names(added=['B'], removed=['OLD_KEY']), names(added=[]), names(added=['ANTHROPIC_BASE_URL']),
                        names(added=['B'], files=['A']), names(added=['B'], compared=False), {'compared': False, 'reason': 'x'}):
            self.assertEqual(render(['B'], False, False, {'A': True, 'B': None}, secrets), 'ANOTHER-ENVIRONMENT', secrets)
        # The runner says the same and the names do not, or nothing could be compared: not the same environment.
        for secrets in (names(changed=['K']), names(added=['B']), names(removed=['K']), {'compared': False, 'reason': 'x'}):
            self.assertEqual(render([], True, True, {'A': True}, secrets), 'ANOTHER-ENVIRONMENT', secrets)
        for new, env, files in (([], False, {'A': True, 'B': None}), (['B'], False, {'A': False, 'B': None}), (['B'], False, {'A': None, 'B': None}), (['B'], False, {'A': True, 'B': False}),
                                (['B'], None, {'A': True, 'B': None}), (['B'], False, {'A': True}), (['B'], False, {}), (['B', 'C'], False, {'A': True, 'B': None}), ([], False, {'A': True})):
            self.assertEqual(render(new, False, env, files, names(added=['B'])), 'ANOTHER-ENVIRONMENT', (new, env, files))
        start = (ROLE / 'tasks/start.yml').read_text()
        self.assertIn("- ck_environment != 'ANOTHER-ENVIRONMENT' or (connectome_kitchen_secrets_differ_ok | bool)", start)
        # After the restart, what the engine wrote is held to the names held before; a difference is a failed start.
        tasks = yaml.safe_load(start)
        block = next(task for task in tasks if 'block' in task)['block']
        held = next(task for task in block if task['name'].startswith('Stop here unless the environment file the engine wrote'))
        self.assertEqual(held['ansible.builtin.assert']['that'], ['ck_written.compared', 'ck_held_secrets.compared', 'ck_written.changed == ck_held_secrets.changed',
                                                               'ck_written.added == ck_held_secrets.added', 'ck_written.removed == ck_held_secrets.removed'])
        self.assertEqual(held['when'], 'not (connectome_kitchen_secrets_differ_ok | bool)')
        for task in [task for task in runner if 'kitchen-secrets.mjs' in json.dumps(task)] + [task for task in block if 'kitchen-secrets.mjs' in json.dumps(task)]:
            self.assertIs(task['no_log'], True)
        self.assertEqual(yaml.safe_load((ROLE / 'defaults/main.yml').read_text())['connectome_kitchen_secret_files_new'], [])

    def secrets(self, layer, values, old_env, mode='proposed', new_env=None):
        """files/kitchen-secrets.mjs over a stand-in of kitchen's three modules: one layer, no merge."""
        node = shutil.which('node')
        if not node:
            self.skipTest('no node here; tests/kitchen/trial.py runs the helper with a real kitchen')
        dist, root = self.work / 'install/dist', self.work / 'root'
        (dist / 'pylike').mkdir(parents=True, exist_ok=True)
        (dist / 'package.json').write_text('{"type": "module"}')
        (dist / 'pylike/pyjson.js').write_text('const m = v => Array.isArray(v) ? v.map(m) : v && typeof v === "object" ? new Map(Object.entries(v).map(([k, x]) => [k, m(x)])) : v;\nexport const loads = text => m(JSON.parse(String(text)));\n')
        (dist / 'config.js').write_text('export const merge_layers = layers => layers[layers.length - 1];\n')
        (dist / 'deploy.js').write_text('export const direct_secret_names = config => [...(config.get("required_secrets") ?? [])].sort();\n'
                                        'export class Deployment { static _systemd_path(value) { if (/[%\\s]/u.test(value)) throw new Error("unsafe"); return value; } }\n')
        for build, text in ((OLD, old_env), (NEW, new_env)):
            if text is not None:
                (root / 'builds' / build).mkdir(parents=True, exist_ok=True)
                (root / 'builds' / build / 'secrets.env').write_text(text)
        if values is not None:
            (self.work / 'secrets.json').write_text(values if isinstance(values, str) else json.dumps(values))
        (self.work / 'request.json').write_text(json.dumps({'request': {'root': str(root), 'layers': [{'secrets_file': str(self.work / 'secrets.json'), **layer}]}, 'expect': {'live_build': OLD, 'request_id': NEW}}))
        done = subprocess.run([node, str(ROLE / 'files/kitchen-secrets.mjs'), str(self.work / 'install'), str(self.work / 'request.json'), mode], capture_output=True, text=True)
        return done, root

    def test_the_environment_is_held_name_by_name_and_no_value_leaves(self):
        layer = {'required_secrets': ['ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL'], 'secret_files': [{'secret': 'ZULIP_RC', 'env': 'ZULIP_RC_PATH'}, {'secret': 'GITLAB_TOKEN', 'env': 'GITLAB_TOKEN_FILE'}]}
        values = {'ANTHROPIC_API_KEY': 'VALUE-ONE with "quotes" and \\ a backslash', 'ANTHROPIC_BASE_URL': 'VALUE-TWO', 'ZULIP_RC': 'VALUE-THREE\nof two lines', 'GITLAB_TOKEN': 'VALUE-FOUR'}
        done, root = self.secrets(layer, values, '')
        old = 'ANTHROPIC_API_KEY="VALUE-ONE with \\"quotes\\" and \\\\ a backslash"\nANTHROPIC_BASE_URL="VALUE-TWO"\nZULIP_RC_PATH="%s/state/secret-files/ZULIP_RC_PATH"\n' % root
        # One secret file more, and nothing else.
        done, root = self.secrets(layer, values, old)
        self.assertEqual((done.returncode, done.stderr), (0, ''))
        self.assertEqual(json.loads(done.stdout), {'mode': 'proposed', 'against': OLD, 'compared': True, 'same': ['ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'ZULIP_RC_PATH'], 'changed': [],
                                                   'added': ['GITLAB_TOKEN_FILE'], 'removed': [], 'secret_files': ['GITLAB_TOKEN_FILE', 'ZULIP_RC_PATH']})
        # The review's F1: a direct value that changed beside it is named as changed. No value is printed.
        for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL'):
            done, root = self.secrets(layer, {**values, name: 'VALUE-ROTATED'}, old)
            answer = json.loads(done.stdout)
            self.assertEqual((done.returncode, answer['changed'], answer['added'], answer['removed']), (0, [name], ['GITLAB_TOKEN_FILE'], []))
            self.assertNotIn('VALUE-', done.stdout + done.stderr)
        # A direct name that comes or goes, and a name of the old file the request no longer has.
        done, root = self.secrets({**layer, 'required_secrets': ['ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'MORE']}, {**values, 'MORE': 'VALUE-FIVE'}, old)
        self.assertEqual(json.loads(done.stdout)['added'], ['GITLAB_TOKEN_FILE', 'MORE'])
        done, root = self.secrets({**layer, 'required_secrets': ['ANTHROPIC_API_KEY']}, values, old)
        self.assertEqual(json.loads(done.stdout)['removed'], ['ANTHROPIC_BASE_URL'])
        # What the engine wrote, against the previous build's.
        done, root = self.secrets(layer, None, old, 'written', old.replace('VALUE-TWO', 'VALUE-ROTATED') + 'GITLAB_TOKEN_FILE="/x"\n')
        answer = json.loads(done.stdout)
        self.assertEqual((done.returncode, answer['mode'], answer['changed'], answer['added'], answer['removed']), (0, 'written', ['ANTHROPIC_BASE_URL'], ['GITLAB_TOKEN_FILE'], []))
        self.assertNotIn('VALUE-', done.stdout + done.stderr)
        # Nothing to compare: exit 3 and one closed line. Never the text of a failure, which may quote a value.
        for bad_values, bad_old in (('VALUE-THAT-IS-NOT-JSON', old), ({'ANTHROPIC_API_KEY': 'VALUE-ONE'}, old), ({**values, 'ANTHROPIC_API_KEY': 5}, old), ({**values, 'ANTHROPIC_BASE_URL': 'VALUE\ntwo lines'}, old),
                                    (values, None), (values, 'VALUE-LINE-THAT-IS-NO-ASSIGNMENT\n'), (values, old + old), (values, old.rstrip('\n'))):
            shutil.rmtree(self.work / 'root', ignore_errors=True)
            done, root = self.secrets(layer, bad_values, bad_old)
            answer = json.loads(done.stdout)
            self.assertEqual((done.returncode, answer['compared'], sorted(answer)), (3, False, ['compared', 'mode', 'reason']), done.stdout)
            self.assertNotIn('VALUE', done.stdout + done.stderr)
        node = shutil.which('node')
        self.assertEqual(subprocess.run([node, str(ROLE / 'files/kitchen-secrets.mjs'), str(self.work / 'install'), str(self.work / 'request.json'), 'other'], capture_output=True).returncode, 2)
        text = (ROLE / 'files/kitchen-secrets.mjs').read_text()
        for word in ('writeFile', 'appendFile', 'createHash', 'console.', 'error.message)', 'String(error'):
            self.assertNotIn(word, text)

    def deployment(self, at=NEW, active=True, fail_once=None):
        """A deployment as files, with a service manager that is a file too: no process, no systemd. For `back` and `clear`."""
        w = self.work
        root, units, save, bins = w / 'root', w / 'units', w / 'save', w / 'bin'
        unit = 'connectome-x.service'
        for path in (root / 'state/secret-files', units / 'default.target.wants', units / (unit + '.d'), save, bins):
            path.mkdir(parents=True)
        for build in (OLD, NEW):
            (root / 'builds' / build / 'managed-units').mkdir(parents=True)
            for leaf in ('.prepared', 'run.sh', 'managed-units/' + unit):
                (root / 'builds' / build / leaf).write_text('x')
        (root / 'builds' / OLD / 'secrets.env').write_text('A_KEY="VALUE"\nZULIP_RC_PATH="/p"\n')
        (root / 'builds' / NEW / 'secrets.env').write_text('A_KEY="VALUE"\nGITLAB_TOKEN_FILE="/q"\nZULIP_RC_PATH="/p"\n')
        target = lambda build, index: str(root / 'builds' / build) if index == 0 else str(root / 'builds' / build / 'managed-units' / unit)
        for index, link in enumerate((root / 'current', units / unit, units / 'default.target.wants' / unit)):
            link.symlink_to(target(at, index))
        (save / 'links').write_text(''.join(target(OLD, index) + '\n' for index in range(3)))
        (save / '90-connectome-env.conf').write_text('EnvironmentFile=%s/builds/%s/secrets.env\n' % (root, OLD))
        (units / (unit + '.d') / '90-connectome-env.conf').write_text('EnvironmentFile=%s/builds/%s/secrets.env\n' % (root, at))
        (save / 'ownership.json').write_text('{"saved": true}')
        (save / 'secret-files').write_text('ZULIP_RC_PATH\n')
        (root / 'state/ownership.json').write_text('{"saved": false, "with": "%s"}' % NEW)
        (root / 'state/secret-files/ZULIP_RC_PATH').write_text('OLD-SECRET')
        if at == NEW:
            (root / 'state/secret-files/GITLAB_TOKEN_FILE').write_text('NEW-SECRET')
        (w / 'manager.json').write_text(json.dumps({'active': active, 'build': at}))
        (w / 'systemctl').write_text("""#!/usr/bin/python3
import json, sys
from pathlib import Path
w = Path(__file__).resolve().parent
d = json.loads((w / 'manager.json').read_text()); a = [x for x in sys.argv[1:] if x != '--user']
with (w / 'calls').open('a') as stream: stream.write(a[0] + '\\\\n')
if a[0] == 'show':
    b = w / 'root/builds' / d['build']
    print({'LoadState': 'loaded', 'ActiveState': 'active' if d['active'] else 'inactive', 'SubState': 'running' if d['active'] else 'dead', 'MainPID': '77' if d['active'] else '0', 'NRestarts': '0',
           'FragmentPath': str(b / 'managed-units/connectome-x.service'), 'EnvironmentFiles': str(b / 'secrets.env') + ' (ignore_errors=no)'}.get(a[a.index('-p') + 1], ''))
elif a[0] == 'stop': d['active'] = False
elif a[0] == 'start': d['active'] = True
elif a[0] == 'daemon-reload': d['build'] = (w / 'root/current').resolve().name
(w / 'manager.json').write_text(json.dumps(d))
""".replace('\\\\n', '\\n'))
        (w / 'systemctl').chmod(0o755)
        if fail_once:
            (bins / 'install').write_text('#!/bin/bash\nif [[ "$*" == *"/save/%s "* ]] && [ ! -e "${0%%/*}/failed-once" ]; then touch "${0%%/*}/failed-once"; echo "simulated failure" >&2; exit 1; fi\nexec /usr/bin/install "$@"\n' % fail_once)
            (bins / 'install').chmod(0o755)
        me = __import__('pwd').getpwuid(os.geteuid()).pw_name
        env = {'PATH': '%s:/usr/bin:/bin' % bins, 'KITCHEN_NAME': 'x', 'KITCHEN_ACCOUNT': me, 'KITCHEN_UID': str(os.geteuid()), 'KITCHEN_OPERATOR_UID': str(os.geteuid()), 'KITCHEN_ACCOUNT_HOME': str(w / 'home'),
               'KITCHEN_RUNTIME_DIR': str(w), 'KITCHEN_ROOT': str(root), 'KITCHEN_UNIT_DIR': str(units), 'KITCHEN_SAVE': str(save), 'KITCHEN_OLD': OLD, 'KITCHEN_SYSTEMCTL': str(w / 'systemctl'), 'KITCHEN_AS_ACCOUNT': '',
               'KITCHEN_BACK_WAIT': '0', 'KITCHEN_BACK_SETTLE': '0'}
        calls = lambda: [verb for verb in (w / 'calls').read_text().split() if verb not in ('show', 'reset-failed')] if (w / 'calls').exists() else []
        run = lambda *words: subprocess.run(['/bin/bash', str(ROLE / 'files/kitchen-pointers.sh'), *words], env=env, capture_output=True, text=True)
        back = lambda: (os.readlink(root / 'current') == target(OLD, 0) and os.readlink(units / unit) == target(OLD, 1) and os.readlink(units / 'default.target.wants' / unit) == target(OLD, 1)
                        and (units / (unit + '.d') / '90-connectome-env.conf').read_bytes() == (save / '90-connectome-env.conf').read_bytes(), (root / 'state/ownership.json').read_bytes() == (save / 'ownership.json').read_bytes())
        return run, root, save, calls, back

    def test_a_way_back_is_done_only_with_ownership_too(self):
        # The review's F3: the way back fails at ownership.json; the next one must put it back before it says done.
        for failing in ('ownership.json', '90-connectome-env.conf'):
            shutil.rmtree(self.work); self.work.mkdir()
            run, root, save, calls, back = self.deployment(fail_once=failing)
            first = run('back')
            self.assertEqual((first.returncode, (save / 'back.incomplete').exists(), back()[1]), (62, True, False), first.stdout)
            looked = run('look')
            self.assertEqual((looked.returncode, 'AN EARLIER WAY BACK DID NOT FINISH' in looked.stdout), (1, True), looked.stdout)
            self.assertEqual(run('clear', 'GITLAB_TOKEN_FILE').returncode, 60)
            again = run('back')
            self.assertEqual((again.returncode, back(), (save / 'back.incomplete').exists()), (0, (True, True), False), again.stdout)
            self.assertIn('AN EARLIER WAY BACK DID NOT FINISH', again.stdout)
            self.assertIn('The links, the drop-in and ownership.json are the saved ones.', again.stdout)
            self.assertEqual(calls(), ['stop', 'stop', 'daemon-reload', 'start'])
            # And once it is done: nothing to put back, nothing restarted.
            third = run('back')
            self.assertEqual((third.returncode, 'Nothing to put back' in third.stdout, 'ownership.json is the saved one.' in third.stdout, calls()[4:]), (0, True, True, []), third.stdout)

    def test_a_way_back_put_right_by_hand_is_completed_without_a_restart(self):
        run, root, save, calls, back = self.deployment(fail_once='ownership.json')
        self.assertEqual(run('back').returncode, 62)
        shutil.copyfile(save / 'ownership.json', root / 'state/ownership.json')
        subprocess.run([str(self.work / 'systemctl'), '--user', 'daemon-reload']); subprocess.run([str(self.work / 'systemctl'), '--user', 'start', 'connectome-x.service'])
        seen = len(calls())
        done = run('back')
        self.assertEqual((done.returncode, 'the way back is complete. Nothing changed, nothing restarted.' in done.stdout, (save / 'back.incomplete').exists(), calls()[seen:]), (0, True, False, []), done.stdout)

    def test_a_build_never_started_is_not_an_unfinished_way_back(self):
        # The links and the drop-in are the saved ones and ownership.json holds what prepare added: nothing is put back.
        run, root, save, calls, back = self.deployment(at=OLD)
        before = (root / 'state/ownership.json').read_bytes()
        for mode in ('look', 'back'):
            done = run(mode)
            self.assertEqual((done.returncode, 'Nothing to put back' in done.stdout, 'ownership.json is not the saved one (as after a prepare that was never started' in done.stdout), (0, True, True), done.stdout)
        self.assertEqual(((root / 'state/ownership.json').read_bytes(), calls(), (save / 'back.incomplete').exists()), (before, [], False))
        # The same with the unit down: only the unit is started, and ownership.json is the engine's and is left.
        shutil.rmtree(self.work); self.work.mkdir()
        run, root, save, calls, back = self.deployment(at=OLD, active=False)
        done = run('back')
        self.assertEqual((done.returncode, 'only the unit is started' in done.stdout, 'Nothing but the unit was touched.' in done.stdout, calls(), back()), (0, True, True, ['stop', 'daemon-reload', 'start'], (True, False)), done.stdout)

    def test_clear_removes_exactly_the_secret_file_the_window_introduced(self):
        # The review's F2: after a start and a way back, the file the start wrote is not declared by the saved deployment.
        run, root, save, calls, back = self.deployment()
        files = root / 'state/secret-files'
        listed = lambda: sorted(path.name for path in files.iterdir())
        refused = run('clear', 'GITLAB_TOKEN_FILE')
        self.assertEqual((refused.returncode, 'clear is for after a complete way back' in refused.stdout, listed()), (60, True, ['GITLAB_TOKEN_FILE', 'ZULIP_RC_PATH']), refused.stdout)
        self.assertEqual(run('back').returncode, 0)
        seen = len(calls())
        for words, reason in ((['ZULIP_RC_PATH'], 'was a secret file before the window'), (['GITLAB_TOKEN_FILE', 'ZULIP_RC_PATH'], 'was a secret file before the window'), (['A_KEY'], 'the environment file of the saved build names A_KEY'),
                              (['../x'], 'usage'), (['gitlab'], 'usage')):
            refused = run('clear', *words)
            self.assertEqual((refused.returncode, reason in refused.stdout, listed()), (60, True, ['GITLAB_TOKEN_FILE', 'ZULIP_RC_PATH']), refused.stdout)
        (files / 'OTHER_FILE').symlink_to(files / 'ZULIP_RC_PATH')
        refused = run('clear', 'GITLAB_TOKEN_FILE', 'OTHER_FILE')
        self.assertEqual((refused.returncode, 'is not a regular file' in refused.stdout, len(listed())), (60, True, 3), refused.stdout)
        (files / 'OTHER_FILE').unlink()
        done = run('clear', 'GITLAB_TOKEN_FILE')
        self.assertEqual((done.returncode, listed(), (files / 'ZULIP_RC_PATH').read_text(), calls()[seen:]), (0, ['ZULIP_RC_PATH'], 'OLD-SECRET', []), done.stdout)
        self.assertIn('removed %s/GITLAB_TOKEN_FILE' % files, done.stdout)
        self.assertNotIn('SECRET', done.stdout.replace('SECRET_', ''))
        again = run('clear', 'GITLAB_TOKEN_FILE')
        self.assertEqual((again.returncode, 'GITLAB_TOKEN_FILE: not there, nothing to remove' in again.stdout, listed()), (0, True, ['ZULIP_RC_PATH']), again.stdout)
        self.assertEqual((run('clear').returncode, listed(), back()), (0, ['ZULIP_RC_PATH'], (True, True)))
        # A save of an earlier role has no list of what was there: nothing is removed on a guess.
        (save / 'secret-files').unlink()
        (files / 'GITLAB_TOKEN_FILE').write_text('NEW-SECRET')
        refused = run('clear', 'GITLAB_TOKEN_FILE')
        self.assertEqual((refused.returncode, 'has no list of the secret files' in refused.stdout, listed()), (60, True, ['GITLAB_TOKEN_FILE', 'ZULIP_RC_PATH']), refused.stdout)

    def nothing_deployed(self, active=False):
        """An account where nothing is deployed, as files, with a service manager that is a file too. For kitchen-first.sh."""
        w = self.work
        home = w / 'home'
        root, units, save, state, secrets = home / 'share/connectome/x', home / 'units', w / 'save', home / 'state/x', home / 'secrets/x.json'
        for path in (home, state, secrets.parent):
            path.mkdir(parents=True)
        secrets.write_text('{"A_KEY": "VALUE"}')
        (state / 'credentials').mkdir()
        (w / 'manager.json').write_text(json.dumps({'active': active}))
        (w / 'systemctl').write_text("""#!/usr/bin/python3
import json, os, sys
from pathlib import Path
w = Path(__file__).resolve().parent
d = json.loads((w / 'manager.json').read_text()); a = [x for x in sys.argv[1:] if x != '--user']
with (w / 'calls').open('a') as stream: stream.write(a[0] + '\\n')
link = w / 'home/units/connectome-x.service'
if a[0] == 'show':
    print({'LoadState': 'loaded' if os.path.lexists(link) else 'not-found', 'ActiveState': 'active' if d['active'] else 'inactive', 'SubState': 'running' if d['active'] else 'dead', 'MainPID': '77' if d['active'] else '0',
           'NRestarts': '0', 'FragmentPath': str(link) if os.path.lexists(link) else '', 'EnvironmentFiles': (os.readlink(w / 'home/share/connectome/x/current') + '/secrets.env (ignore_errors=no)') if d['active'] and os.path.islink(w / 'home/share/connectome/x/current') else ''}.get(a[a.index('-p') + 1], ''))
elif a[0] == 'stop':
    if d.get('stop_fails'): sys.exit(1)
    d['active'] = False
    if d.get('redirect_wants_after_stop'):
        wants = link.parent / 'default.target.wants' / link.name
        wants.unlink()
        wants.symlink_to('/unexpected')
    if d.get('replace_root_after_stop'):
        import shutil
        root = w / 'home/share/connectome/x'
        shutil.rmtree(root)
        root.mkdir()
        (root / 'foreign').touch()
elif a[0] == 'daemon-reload' and d.get('reload_fails'):
    sys.exit(1)
(w / 'manager.json').write_text(json.dumps(d))
""".replace('\\\\n', '\\n'))
        (w / 'systemctl').chmod(0o755)
        me = __import__('pwd').getpwuid(os.geteuid()).pw_name
        env = {'PATH': '/usr/bin:/bin', 'KITCHEN_NAME': 'x', 'KITCHEN_ACCOUNT': me, 'KITCHEN_UID': str(os.geteuid()), 'KITCHEN_OPERATOR_UID': str(os.geteuid()), 'KITCHEN_ACCOUNT_HOME': str(home),
               'KITCHEN_RUNTIME_DIR': str(w / 'runtime'), 'KITCHEN_ROOT': str(root), 'KITCHEN_UNIT_DIR': str(units), 'KITCHEN_SAVE': str(save), 'KITCHEN_SYSTEMCTL': str(w / 'systemctl'), 'KITCHEN_AS_ACCOUNT': '',
               'KITCHEN_STATE_DIR': str(state), 'KITCHEN_SECRETS_FILE': str(secrets), 'KITCHEN_PROTECTED_PATHS': '[]'}
        calls = lambda: [verb for verb in (w / 'calls').read_text().split() if verb not in ('show', 'reset-failed')] if (w / 'calls').exists() else []
        run = lambda *words, **more: subprocess.run(['/bin/bash', str(ROLE / 'files/kitchen-first.sh'), *words], env={**env, **more}, capture_output=True, text=True)
        unit = 'connectome-x.service'

        def install(build=NEW):
            """What the engine leaves after a first start: the build, its bookkeeping, the pointer, the two links, the drop-in."""
            there = root / 'builds' / build
            (there / 'managed-units').mkdir(parents=True)
            for leaf in ('.prepared', 'run.sh', 'secrets.env', 'managed-units/' + unit):
                (there / leaf).write_text('x')
            (root / 'state/secret-files').mkdir(parents=True, exist_ok=True)
            (root / 'state/secret-files/ZULIP_RC_PATH').write_text('COPY-OF-A-SECRET')
            (units / 'default.target.wants').mkdir(parents=True, exist_ok=True)
            (units / (unit + '.d')).mkdir(exist_ok=True)
            dropin = units / (unit + '.d') / '90-connectome-env.conf'
            dropin.write_text('[Service]\nEnvironment=DATA_DIR=%s\nEnvironmentFile=%s/secrets.env\n' % (state, there))
            (root / 'current').symlink_to(there)
            for link in (units / unit, units / 'default.target.wants' / unit):
                link.symlink_to(there / 'managed-units' / unit)
            source = there / 'managed-units' / unit
            entries = [dict(path=str(there), kind='directory')]
            for path in (root / 'current', units / unit, units / 'default.target.wants' / unit):
                entries.append(dict(path=str(path), kind='link', target=os.readlink(path)))
            for path in (dropin, source):
                entries.append(dict(path=str(path), kind='file', sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
            (root / 'state/ownership.json').write_text(json.dumps(dict(version=1, entries=entries)))
            (w / 'manager.json').write_text(json.dumps({'active': True}))
        four = lambda: [os.path.lexists(path) for path in (root / 'current', units / unit, units / 'default.target.wants' / unit, units / (unit + '.d'))]
        return run, install, four, calls, (root, units, save, state, secrets)

    def test_first_refuses_before_any_change(self):
        script = ['/bin/bash', str(ROLE / 'files/kitchen-first.sh')]
        done = subprocess.run(script + ['remove'], env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
        self.assertEqual((done.returncode, 'REFUSED, nothing changed' in done.stdout), (60, True))
        run, install, four, calls, (root, units, save, state, secrets) = self.nothing_deployed()
        for words in ([], ['hold'], ['hold', 'not-a-build'], ['runs'], ['nothing', 'extra'], ['remove', 'everything'], ['remove', 'state', 'more'], ['back'], ['save'], ['mark', NEW, 'a b', 'c']):
            self.assertEqual(run(*words).returncode, 60, words)
        # Another operator; a root that is not the deployment's own directory in the home of the account; a path that climbs.
        self.assertEqual(run('nothing', KITCHEN_OPERATOR_UID=str(os.geteuid() + 1)).returncode, 60)
        home = str(self.work / 'home')
        for more in ({'KITCHEN_ROOT': str(self.work / 'outside-home' / 'x')}, {'KITCHEN_ROOT': home + '/share/connectome/other'}, {'KITCHEN_ROOT': home + '/share/../x'}, {'KITCHEN_ROOT': home + '/x/'}, {'KITCHEN_ROOT': home},
                     {'KITCHEN_UNIT_DIR': '/etc/systemd/user'}, {'KITCHEN_STATE_DIR': '/var/lib/x'}, {'KITCHEN_NAME': '../x'}):
            done = run('remove', NEW, **more)
            self.assertEqual((done.returncode, 'REFUSED, nothing changed' in done.stdout), (60, True), more)
        self.assertEqual((calls(), save.exists(), secrets.read_text()), ([], False, '{"A_KEY": "VALUE"}'))

    def test_a_first_install_is_held_only_where_nothing_is_deployed(self):
        run, install, four, calls, (root, units, save, state, secrets) = self.nothing_deployed()
        self.assertEqual(run('nothing').returncode, 0)
        # Without the marker nothing is marked, held or removed: it is not a deployment this role installed from nothing.
        for words in (['mark', NEW, 'first-environment', 'inspection-ok'], ['held', NEW]):
            done = run(*words)
            self.assertEqual((done.returncode, 'holds no marker of a first install' in done.stdout), (60, True), words)
        done = run('hold', NEW)
        self.assertEqual((done.returncode, json.loads((save / 'first-install').read_text())['build'], save.stat().st_mode & 0o777), (0, NEW, 0o700), done.stdout)
        self.assertIn('state directory %s: 1 entries (credentials )' % state, done.stdout)  # names, for the operator to see what a fresh agent starts on
        self.assertIn('already held', run('hold', NEW).stdout)
        (root / 'builds').mkdir()
        # Not a first install: another build; a pointer; a unit link, also one that names nothing; a drop-in; a unit that runs; a takeover's save.
        (root / 'builds' / OLD).mkdir()
        done = run('hold', NEW)
        self.assertEqual((done.returncode, 'different build' in done.stdout), (60, True), done.stdout)
        (root / 'builds' / OLD).rmdir()
        units.mkdir()
        for path, make in ((root / 'current', lambda p: p.symlink_to('/nowhere')), (units / 'connectome-x.service', lambda p: p.symlink_to('/nowhere')),
                           (units / 'default.target.wants', lambda p: (p.mkdir(), (p / 'connectome-x.service').symlink_to('/nowhere'))), (units / 'connectome-x.service.d', lambda p: p.mkdir())):
            make(path)
            done = run('hold', NEW)
            self.assertEqual((done.returncode, 'is installed already: this is not a first install' in done.stdout, run('nothing').returncode), (60, True, 1), path)
            shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
        (self.work / 'manager.json').write_text(json.dumps({'active': True}))
        done = run('hold', NEW)
        self.assertEqual((done.returncode, 'is not down' in done.stdout, run('nothing').returncode), (60, True, 1))
        (self.work / 'manager.json').write_text(json.dumps({'active': False}))
        (save / 'links').write_text('x\n')
        done = run('hold', NEW)
        self.assertEqual((done.returncode, 'holds the save of a takeover' in done.stdout), (60, True))
        (save / 'links').unlink()
        # verify marks; the start is held to the mark, to ownership.json as it was then, and to nothing being installed.
        self.assertEqual(run('held', NEW).returncode, 60)
        (root / 'state').mkdir()
        (root / 'state/ownership.json').write_text('{"version": 1, "entries": []}')
        self.assertEqual(run('mark', NEW, 'first-environment', 'inspection-ok').returncode, 0)
        done = run('held', NEW)
        self.assertEqual((done.returncode, 'verified: %s first-environment inspection-ok; nothing is installed yet' % NEW in done.stdout), (0, True), done.stdout)
        self.assertEqual(run('held', OLD).returncode, 60)
        (root / 'state/ownership.json').write_text('{"version": 1, "entries": []}\n')
        done = run('held', NEW)
        self.assertEqual((done.returncode, 'ownership.json changed since verify' in done.stdout), (60, True))
        self.assertEqual(calls(), [])

    def test_only_initial_prepare_can_claim_newly_created_state(self):
        run, install, four, calls, (root, units, save, state, secrets) = self.nothing_deployed()
        shutil.rmtree(state)
        self.assertEqual(run('hold', NEW).returncode, 0)
        marker = save / 'first-install'
        held = marker.read_bytes()
        self.assertIsNone(json.loads(held)['state_identity'])

        # Emulate state and a prepared build appearing between this invocation's hold and claim.
        state.mkdir()
        build = root / 'builds' / NEW
        build.mkdir(parents=True)
        (root / 'state').mkdir()
        (root / 'state/ownership.json').write_text(json.dumps(
            dict(version=1, entries=[dict(path=str(build), kind='directory')])))
        for mode in ('hold', 'claim'):
            # Ordinary retry cannot acquire authority from an interrupted initial prepare.
            done = run(mode, NEW)
            self.assertEqual((done.returncode, marker.read_bytes()), (60, held), done.stdout)
            self.assertIn('state directory was not held or was replaced', done.stdout)
        done = run('claim-initial', NEW)
        self.assertEqual(done.returncode, 0, done.stdout)
        claimed = marker.read_bytes()
        self.assertEqual(json.loads(claimed)['state_identity'], [state.stat().st_dev, state.stat().st_ino])
        state.rename(state.with_name('old-state'))
        state.mkdir()
        for mode in ('claim', 'claim-initial'):
            done = run(mode, NEW)
            self.assertEqual((done.returncode, marker.read_bytes()), (60, claimed), done.stdout)
            self.assertIn('state directory was not held or was replaced', done.stdout)

    def test_remove_reports_incomplete_and_stops_if_post_stop_validation_fails(self):
        run, install, four, calls, (root, units, save, state, secrets) = self.nothing_deployed()
        self.assertEqual(run('hold', NEW).returncode, 0)
        install()
        manager = self.work / 'manager.json'
        manager.write_text(json.dumps({'active': True, 'redirect_wants_after_stop': True}))
        wants = units / 'default.target.wants/connectome-x.service'
        marker = save / 'first-install'
        held = marker.read_bytes()
        (state / 'keep').touch()
        done = run('remove', NEW)
        self.assertEqual((done.returncode, calls(), four()), (62, ['stop'], [True] * 4), done.stdout)
        self.assertIn('VALIDATION FAILED: unowned or redirected link', done.stdout)
        self.assertIn('Cleanup is incomplete', done.stdout)
        self.assertNotIn('nothing changed', done.stdout)
        self.assertFalse(json.loads(manager.read_text())['active'])
        self.assertEqual((os.readlink(wants), marker.read_bytes(), root.exists(), (state / 'keep').exists()),
                         ('/unexpected', held, True, True))

    def test_remove_takes_a_first_install_away_and_nothing_else(self):
        run, install, four, calls, (root, units, save, state, secrets) = self.nothing_deployed()
        self.assertEqual(run('hold', NEW).returncode, 0)
        install()
        (state / 'what-the-agent-wrote').write_text('x')
        (units / 'other.service').write_text('[Unit]\n')
        done = run('runs', NEW)
        self.assertEqual((done.returncode, done.stdout.splitlines()[-1]), (0, 'process 77 0'), done.stdout)
        self.assertEqual((run('runs', OLD).returncode, run('nothing').returncode), (1, 1))
        done = run('held', NEW)  # a start that was done is not done again
        self.assertEqual(done.returncode, 60)
        # Not removed: a deployment with a second build; a unit link that names a file outside the deployment; a unit that is a file.
        (root / 'builds' / OLD).mkdir()
        done = run('remove', NEW)
        self.assertEqual((done.returncode, 'different build' in done.stdout, four(), calls()), (60, True, [True] * 4, []), done.stdout)
        (root / 'builds' / OLD).rmdir()
        link = units / 'connectome-x.service'
        target = os.readlink(link)
        for make in (lambda: link.symlink_to('/etc/systemd/user/connectome-x.service'), lambda: link.write_text('[Unit]\n')):
            link.unlink(); make()
            done = run('remove', NEW)
            self.assertEqual((done.returncode, 'unowned or redirected link' in done.stdout, calls(), root.exists()), (60, True, [], True), done.stdout)
        link.unlink(); link.symlink_to(target)
        # A failed stop still removes owned boot enablement; other artifacts remain for retry.
        (self.work / 'manager.json').write_text(json.dumps({'active': True, 'stop_fails': True}))
        done = run('remove', NEW)
        self.assertEqual((done.returncode, 'does not report the unit down' in done.stdout, four(), root.exists()), (62, True, [True, True, False, True], True), done.stdout)
        (self.work / 'manager.json').write_text(json.dumps({'active': True}))
        seen = len(calls())
        done = run('remove', NEW)
        self.assertEqual((done.returncode, four(), root.exists(), calls()[seen:]), (0, [False] * 4, False, ['stop', 'daemon-reload']), done.stdout)
        self.assertIn('removed: the unit is down', done.stdout)
        # Left: the secrets file, the state directory, another unit. The marker is retired. No value is printed.
        self.assertEqual((secrets.read_text(), (state / 'what-the-agent-wrote').is_file(), (units / 'other.service').is_file(), (save / 'first-install').is_file(), (save / 'verified').exists()),
                         ('{"A_KEY": "VALUE"}', True, True, False, False))
        self.assertNotIn('SECRET', done.stdout + done.stderr)
        seen = len(calls())
        done = run('remove', NEW)
        self.assertEqual((done.returncode, 'nothing to remove' in done.stdout, calls()[seen:]), (0, True, []), done.stdout)
        # A drop-in file this install did not put there stays.
        self.assertEqual(run('hold', NEW).returncode, 0)
        install()
        (units / 'connectome-x.service.d/10-operator.conf').write_text('[Service]\nNice=1\n')
        done = run('remove', NEW)
        self.assertEqual((done.returncode, state.exists(), root.exists(), (units / 'connectome-x.service.d/10-operator.conf').is_file(), secrets.read_text()), (0, True, False, True, '{"A_KEY": "VALUE"}'), done.stdout)

    def test_hold_allows_directory_atime_updates(self):
        run, install, four, calls, (root, units, save, state, secrets) = self.nothing_deployed()
        root.mkdir(parents=True)
        os.utime(root, ns=(1, root.stat().st_mtime_ns))
        done = run('hold', NEW)
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(run('remove', NEW).returncode, 0)

    def test_remove_does_not_delete_recreated_state(self):
        run, install, four, calls, (root, units, save, state, secrets) = self.nothing_deployed()
        self.assertEqual(run('hold', NEW).returncode, 0)
        install()
        marker = (save / 'first-install').read_bytes()
        shutil.rmtree(state)
        state.mkdir()
        (state / 'replacement-data').touch()
        refused = run('remove', NEW, 'state')
        self.assertEqual((refused.returncode, calls(), four()), (60, [], [True] * 4), refused.stdout)
        self.assertEqual((save / 'first-install').read_bytes(), marker)
        removed = run('remove', NEW)
        self.assertEqual((removed.returncode, root.exists(), (state / 'replacement-data').is_file()),
                         (0, False, True), removed.stdout)

    def test_remove_refuses_recreated_root_even_with_matching_device_and_inode(self):
        run, install, four, calls, (root, units, save, state, secrets) = self.nothing_deployed()
        self.assertEqual(run('hold', NEW).returncode, 0)
        marker = save / 'first-install'
        held = json.loads(marker.read_bytes())
        shutil.rmtree(root)
        root.mkdir()
        sentinel = root / 'unrelated-data'
        sentinel.touch()
        # Exercise the inode-reuse branch regardless of the allocator's choice:
        # substitute only the old check's fields, leaving the held kernel handle intact.
        held['root_identity'] = [root.stat().st_dev, root.stat().st_ino]
        marker.write_text(json.dumps(held))
        done = run('remove', NEW)
        self.assertEqual((done.returncode, calls(), sentinel.is_file()), (60, [], True), done.stdout)
        self.assertIn('deployment root was replaced', done.stdout)

    def test_remove_reports_incomplete_if_root_is_recreated_after_stop(self):
        run, install, four, calls, (root, units, save, state, secrets) = self.nothing_deployed()
        self.assertEqual(run('hold', NEW).returncode, 0)
        install()
        (self.work / 'manager.json').write_text(json.dumps({'active': True, 'replace_root_after_stop': True}))
        marker = save / 'first-install'
        held = marker.read_bytes()
        done = run('remove', NEW)
        self.assertEqual((done.returncode, calls(), four()), (62, ['stop'], [False, True, True, True]), done.stdout)
        self.assertIn('VALIDATION FAILED: deployment root was replaced', done.stdout)
        self.assertEqual((marker.read_bytes(), (root / 'foreign').is_file()), (held, True))

    def test_remove_refuses_old_marker_without_incarnation_proof(self):
        run, install, four, calls, (root, units, save, state, secrets) = self.nothing_deployed()
        self.assertEqual(run('hold', NEW).returncode, 0)
        install()
        marker = save / 'first-install'
        held = json.loads(marker.read_bytes())
        held.pop('root_handle')
        held['version'] = 2
        marker.write_text(json.dumps(held))
        done = run('remove', NEW)
        self.assertEqual((done.returncode, calls(), four(), root.exists()), (60, [], [True] * 4, True), done.stdout)
        self.assertIn('marker does not match', done.stdout)

    def test_remove_retries_after_unlink_without_readopting_or_bypassing_marker(self):
        run, install, four, calls, (root, units, save, state, secrets) = self.nothing_deployed()
        self.assertEqual(run('hold', NEW).returncode, 0)
        install()
        # A legacy marker grants no authority, even for exactly the old layout.
        marker = save / 'first-install'
        record = marker.read_bytes()
        marker.write_text('first-install x\n')
        self.assertEqual((run('remove', NEW).returncode, calls(), four()), (60, [], [True] * 4))
        marker.write_bytes(record)
        self.assertEqual(run('claim', NEW).returncode, 0)
        # The same directory name with a different inode isn't the claimed build.
        build = root / 'builds' / NEW
        original = self.work / 'original-build'
        build.rename(original)
        build.mkdir()
        self.assertEqual((run('remove', NEW).returncode, calls(), four()), (60, [], [True] * 4))
        build.rmdir()
        original.rename(build)
        manager = self.work / 'manager.json'
        manager.write_text(json.dumps({'active': True, 'reload_fails': True}))
        done = run('remove', NEW)
        self.assertEqual((done.returncode, four(), root.exists(), marker.exists()), (62, [True, False, False, False], True, True), done.stdout)
        manager.write_text(json.dumps({'active': False}))
        done = run('remove', NEW)
        self.assertEqual((done.returncode, root.exists(), marker.exists(), secrets.is_file(), state.is_dir()), (0, False, False, True, True), done.stdout)

    def test_a_first_build_is_held_by_itself_and_readback_holds_nothing_to_a_process_before(self):
        root, unit = self.work / 'root', 'connectome-x.service'
        build, cooked = root / 'builds' / NEW, root / 'cook-config' / NEW / 'recipes'
        for path in (build, cooked):
            path.mkdir(parents=True)
        recipe = json.dumps({'agent': {'systemPrompt': '', 'contextBudgetTokens': 240000, 'name': 'PRIVATE TEXT'}, 'mcpServers': {'zulip': {'command': '/n', 'args': ['/z.js']}, 'shell': {'url': 'ws://x'}}}).encode()
        for leaf, data in (('.prepared', NEW.encode()), ('run.sh', b'x'), (unit, b'x'), ('cook-recipe.json', recipe), ('connectome.lock', json.dumps({'recipe': {'sha256': hashlib.sha256(recipe).hexdigest()}}).encode())):
            (build / leaf).write_bytes(data)
        (cooked / 'cook-recipe.json').write_bytes(recipe)
        config = {'old': '', 'new': NEW, 'root': str(root), 'unit': unit, 'recipe_changes': [], 'recipe_absent': ['/agent/strategy'], 'recipe_present': ['/agent/contextBudgetTokens', '/agent/systemPrompt'],
                  'lock_changes': [], 'servers_connected': ['zulip', 'telegram'], 'as_account': [], 'environment': ['PATH=/usr/bin:/bin'], 'systemctl': '/bin/false', 'journalctl': '/bin/false',
                  'baseline_file': str(self.work / 'baseline.json'), 'baseline_since': '-6h', 'first_install': True}
        (self.work / 'inspect.json').write_text(json.dumps(config))
        run = lambda mode: subprocess.run([sys.executable, '-I', str(ROLE / 'files/kitchen-inspect.py'), mode, str(self.work / 'inspect.json')], capture_output=True, text=True)
        done = run('alone')
        for line in ('ok    the new build is prepared', 'ok    the build has its ' + unit, 'ok    the lock of the new build names its staged recipe by digest', 'ok    staged recipe: /agent/contextBudgetTokens is present (240000)',
                     'ok    cooked recipe: /agent/systemPrompt is present (<str>)', 'ok    staged recipe: /agent/strategy is absent', '      2 MCP servers in the staged recipe: shell, zulip',
                     'ok    MCP zulip, which readback will hold to a connected line, is a server of the recipe', 'LOOK  MCP telegram, which readback will hold to a connected line, is a server of the recipe'):
            self.assertIn(line, done.stdout)
        self.assertEqual((done.returncode, 'PRIVATE' in done.stdout, (self.work / 'baseline.json').exists()), (5, False, False), done.stdout)
        # A recipe that is not the one the lock names, and a build that is not prepared, are LOOK lines.
        (build / 'cook-recipe.json').write_bytes(recipe + b' ')
        (build / '.prepared').unlink()
        done = run('alone')
        self.assertIn('LOOK  the new build is prepared', done.stdout)
        self.assertIn('LOOK  the lock of the new build names its staged recipe by digest', done.stdout)
        # readback of a first install: no baseline is asked for, and the planner's budget is said and held to nothing.
        inspect = load(ROLE / 'files/kitchen-inspect.py', 'kitchen_inspect')
        text = (ROLE / 'files/kitchen-inspect.py').read_text()
        self.assertIn("first = bool(c.get('first_install'))", text)
        journal = self.work / 'journalctl'
        journal.write_text('#!/bin/sh\ncat <<"EOF"\nMCPL server "zulip" connected\n[plan-vs-actual] planned=2 actual=2 budgetMet=true exhausted=false floor=2 hardBudget=280800\nEOF\n')
        journal.chmod(0o755)
        (build / 'cook-recipe.json').write_bytes(recipe)
        for first, expected, unexpected in ((True, ('first install: no process ran before this one', 'hardBudget of the planner: 280800 (first install: reported'), ('a baseline of the process before the start', 'no value from before the start')),
                                            (False, ('LOOK  a baseline of the process before the start is there', 'LOOK  hardBudget=280800, and there is no value from before the start to hold it to'), ('first install',))):
            (self.work / 'inspect.json').write_text(json.dumps({**config, 'journalctl': str(journal), 'first_install': first}))
            done = run('readback')
            for line in expected:
                self.assertIn(line, done.stdout)
            for line in unexpected:
                self.assertNotIn(line, done.stdout)
        del inspect

    def test_the_journal_is_read_and_never_printed(self):
        inspect = load(ROLE / 'files/kitchen-inspect.py', 'kitchen_inspect')
        journal = self.work / 'journalctl'
        journal.write_text('#!/bin/sh\ncat <<"EOF"\nMCPL server "zulip" reconnected successfully\nMCPL server "slack" initial connect failed, will retry: PRIVATE TEXT\n'
                           'MCPL server "slack" reconnected successfully\nMCPL server "github" reconnect failed: PRIVATE TEXT\nMCPL server "odd" says something new\nsomeone wrote: MESSAGE TEXT\n'
                           '[plan-vs-actual] planned=1 actual=1 budgetMet=false exhausted=true floor=1 hardBudget=111 dryRun=true ns=agents/PRIVATE\n'
                           '[plan-vs-actual] planned=2 actual=2 budgetMet=false exhausted=false floor=2 hardBudget=280800 queuedRelief=0 ns=agents/PRIVATE\nEOF\n')
        journal.chmod(0o755)
        seen = inspect.journal({'as_account': [], 'environment': ['PATH=/usr/bin:/bin'], 'journalctl': str(journal), 'unit': 'u'}, '-1h')
        self.assertEqual(seen, {'answered': True, 'lines': 8, 'servers': {'zulip': 'connected', 'slack': 'connected', 'github': 'FAILED'},
                                'plan': {'planned': '2', 'actual': '2', 'budgetMet': 'false', 'exhausted': 'false', 'floor': '2', 'hardBudget': '280800'}})
        self.assertNotIn('PRIVATE', json.dumps(seen))
        self.assertIn("for name in sorted(c.get('servers_connected', [])):", (ROLE / 'files/kitchen-inspect.py').read_text())
        self.assertEqual(yaml.safe_load((ROLE / 'defaults/main.yml').read_text())['connectome_kitchen_servers_connected'], [])
        self.assertIs(yaml.safe_load((ROLE / 'defaults/main.yml').read_text())['connectome_kitchen_budget_reported_only'], False)

    def test_a_server_started_through_a_wrapper_is_found_by_its_own_path(self):
        inspect = load(ROLE / 'files/kitchen-inspect.py', 'kitchen_inspect')
        node, other = '/opt/runtime/bin/node', '/opt/other-runtime/bin/node'
        servers = {
            'chat': {'command': '/usr/bin/python3', 'args': ['-I', '/opt/servers/wrap/chat.py', node, '/opt/servers/chat-1/dist/index.js', '/opt/servers/wrap/chat.json']},
            'beat': {'command': node, 'args': ['/opt/servers/beat-1/dist/index.js', '--stdio']},
            'mail': {'command': other, 'args': ['--flag', '/opt/servers/mail-1/build/index.js', '--stdio']},
            'bare': {'command': '/opt/servers/bare-1/bin/serve'},
            'remote': {'url': 'https://example.invalid/mcp'},
        }
        # The wrapper replaced itself with the server: the command line holds two of the entry's four paths.
        chat, beat, mail, bare = (11, [node, '/opt/servers/chat-1/dist/index.js', '--stdio']), (12, [node, '/opt/servers/beat-1/dist/index.js', '--stdio']), \
            (13, [other, '--flag', '/opt/servers/mail-1/build/index.js', '--stdio']), (14, ['/opt/servers/bare-1/bin/serve'])
        free = [beat, mail, chat, bare, (15, [node]), (16, ['/bin/sh', '-c', 'x'])]
        self.assertEqual(inspect.child_of('chat', servers, free), (chat, True))
        self.assertEqual(inspect.child_of('beat', servers, free), (beat, False))
        self.assertEqual(inspect.child_of('mail', servers, free), (mail, False))
        self.assertEqual(inspect.child_of('bare', servers, free), (bare, False))
        # A wrapper that is still there with every path is the match, and is not called wrapped.
        whole = (17, ['/usr/bin/python3'] + servers['chat']['args'])
        self.assertEqual(inspect.child_of('chat', servers, [chat, whole]), (whole, False))
        # Not a match: no process; a process of the shared runtime alone; another server's process; one with a path the entry does not hold.
        for others in ([], [(15, [node])], [beat, mail, bare], [(18, [node, '/opt/servers/chat-1/dist/index.js', '/tmp/else.js'])], [(19, ['node', 'index.js'])]):
            self.assertEqual(inspect.child_of('chat', servers, others), (None, False), others)
        # A path two servers share names neither of them.
        twins = {'a': {'command': '/usr/bin/python3', 'args': ['/opt/wrap/a.py', node, '/opt/servers/twin/index.js']}, 'b': {'command': '/usr/bin/python3', 'args': ['/opt/wrap/b.py', node, '/opt/servers/twin/index.js']}}
        self.assertEqual(inspect.child_of('a', twins, [(20, [node, '/opt/servers/twin/index.js'])]), (None, False))


if __name__ == '__main__':
    unittest.main()
