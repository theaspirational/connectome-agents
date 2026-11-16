"""roles/connectome_kitchen without a host, a kitchen or Ansible: what its files are, and what its three scripts say.

The role on a sandbox, through ansible-playbook and a real kitchen, is tests/kitchen/trial.py.
"""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest
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
            self.assertIs(json.loads(done.stdout.splitlines()[-1][7:])['same_environment'], False)
        self.assertEqual(subprocess.run([sys.executable, '-I', str(ROLE / 'files/kitchen-step.py'), 'restart', str(runner)], capture_output=True).returncode, 2)
        self.assertEqual(subprocess.run([sys.executable, '-I', str(ROLE / 'files/kitchen-step.py'), 'check', str(self.work / 'absent')], capture_output=True).returncode, 2)

    def builds(self, new_recipe):
        root = self.work / 'root'
        recipe = {'agent': {'systemPrompt': 'SECRET-PROMPT-TEXT', 'strategy': {'keep': 1, 'dropped': True, 'curve': 0}}, 'mcpServers': {}, 'path': '/x/builds/%s/app' % OLD}
        for build, document in ((OLD, recipe), (NEW, new_recipe(json.loads(json.dumps(recipe).replace(OLD, NEW))))):
            for target in (root / 'builds' / build / 'cook-recipe.json', root / 'cook-config' / build / 'recipes/cook-recipe.json'):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(document))
            for leaf, text in (('connectome-x.service', 'ExecStart=/x/builds/%s/run.sh\n'), ('run.sh', 'exec bun /x/builds/%s/app\n'), ('.prepared', '%s'), ('connectome.lock', '{"createdAt": "%s", "engines": {"bun": "1"}}')):
                (root / 'builds' / build / leaf).write_text(text % build)
        config = {'old': OLD, 'new': NEW, 'root': str(root), 'unit': 'connectome-x.service', 'recipe_changes': ['/agent/strategy/curve', '/agent/strategy/dropped'], 'recipe_absent': ['/agent/strategy/dropped', '/agent/strategy/curve'],
                  'recipe_present': ['/agent/strategy/keep'], 'as_account': [], 'environment': [], 'systemctl': '/bin/false', 'journalctl': '/bin/false', 'baseline_file': str(self.work / 'baseline.json'), 'baseline_since': '-1h'}
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


if __name__ == '__main__':
    unittest.main()
