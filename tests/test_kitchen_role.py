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
        first = main[0]['ansible.builtin.assert']['that']
        self.assertIn("connectome_kitchen_operation in ['name', 'plan', 'prepare', 'verify', 'start', 'rollback', 'readback', 'clear']", first)
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
