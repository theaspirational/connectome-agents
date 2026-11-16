"""roles/connectome_kitchen without a host, a kitchen or Ansible: what its files are, and what its three scripts say.

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
        self.assertEqual(sorted(line.split()[2] for line in pointers.splitlines() if line.strip().startswith('step sc ')), ['daemon-reload', 'start', 'stop', 'stop'])

    def test_operations_gates_and_defaults(self):
        main = yaml.safe_load((ROLE / 'tasks/main.yml').read_text())
        first = main[0]['ansible.builtin.assert']['that']
        self.assertIn("connectome_kitchen_operation in ['name', 'plan', 'prepare', 'verify', 'start', 'rollback', 'readback']", first)
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
        for operation in ('name', 'plan', 'prepare', 'verify', 'start', 'rollback', 'readback'):
            self.assertIn('| `%s` |' % operation, doc)

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
        render = lambda new, same, env, files: environment.from_string(expression).render(
            connectome_kitchen_secret_files_new=new, ck_result={'same_environment': same, 'environment_file': env, 'secret_files': files}).strip()
        self.assertEqual(render([], True, True, {'A': True}), 'same-environment')
        self.assertEqual(render(['B'], True, True, {'A': True, 'B': True}), 'same-environment')
        self.assertEqual(render(['B'], False, False, {'A': True, 'B': None}), 'new-secret-files')
        for new, env, files in (([], False, {'A': True, 'B': None}), (['B'], False, {'A': False, 'B': None}), (['B'], False, {'A': None, 'B': None}), (['B'], False, {'A': True, 'B': False}),
                                (['B'], None, {'A': True, 'B': None}), (['B'], False, {'A': True}), (['B'], False, {}), (['B', 'C'], False, {'A': True, 'B': None}), ([], False, {'A': True})):
            self.assertEqual(render(new, False, env, files), 'ANOTHER-ENVIRONMENT', (new, env, files))
        start = (ROLE / 'tasks/start.yml').read_text()
        self.assertIn("- ck_environment != 'ANOTHER-ENVIRONMENT' or (connectome_kitchen_secrets_differ_ok | bool)", start)
        self.assertEqual(yaml.safe_load((ROLE / 'defaults/main.yml').read_text())['connectome_kitchen_secret_files_new'], [])

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


if __name__ == '__main__':
    unittest.main()
