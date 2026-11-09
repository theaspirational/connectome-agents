"""Schema/adapter unit tests and a separately identified, genuine offline build."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'roles/connectome_agent/module_utils'))
import connectome_cook as adapter
from connectome_lock import read_lock, LockError, names_only


def fixture(name='v2'):
    return json.loads((ROOT / 'tests/fixtures/locks' / (name + '.json')).read_text())


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


def repository(path, files):
    path.mkdir()
    git(path, 'init', '-q')
    for name, content in files.items():
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
    git(path, 'add', '.')
    git(path, '-c', 'user.name=Example', '-c', 'user.email=' + 'noreply' + '@' + 'example.invalid',
        'commit', '-qm', 'Local integration input')
    return git(path, 'rev-parse', 'HEAD')


def framework_archive(path, pin):
    with tarfile.open(path, 'w:gz', format=tarfile.USTAR_FORMAT) as archive:
        for name, value in {'package.json': json.dumps({'name': adapter.FRAMEWORK,
                             'version': '1.0.0', 'gitHead': pin, 'main': 'index.js'}),
                            'index.js': 'export const example = true;\n'}.items():
            content = value.encode()
            member = tarfile.TarInfo('package/' + name)
            member.size, member.mode = len(content), 0o644
            archive.addfile(member, io.BytesIO(content))
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


class ScratchTest(unittest.TestCase):
    def setUp(self):
        (ROOT / '.cache').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.cache')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.build = self.work / 'build'
        self.build.mkdir(mode=0o700)


class LockTests(ScratchTest):
    def record(self, data=None):
        data = deepcopy(data if data is not None else fixture())
        data['build']['dir'] = str(self.build) if 'dir' in data.get('build', {}) else None
        data['launch']['script'] = str(self.build / 'run.sh')
        (self.build / 'run.sh').write_text('#!/bin/sh\nexit 0\n')
        path = self.build / 'connectome.lock'
        path.write_text(json.dumps(data))
        return path

    def test_recorded_v2_accepted_and_mapped(self):
        lock = read_lock(self.record(), host_pin='c' * 40, required_names=['EXAMPLE_SECRET'])
        self.assertEqual(lock['build'], {'id': 'b' * 64, 'dir': str(self.build)})
        self.assertEqual(lock['requirements'][0]['envName'], 'EXAMPLE_SECRET')
        self.assertEqual(lock['credentials'], ['EXAMPLE_SECRET'])

    def test_recorded_v1_and_malformed_refused(self):
        for name in ('v1', 'malformed-v2'):
            path = self.build / 'connectome.lock'
            path.write_text(json.dumps(fixture(name)))
            with self.subTest(name=name), self.assertRaises(LockError):
                read_lock(path)

    def test_versions_missing_fields_and_bad_types_fail_closed(self):
        cases = [None, [], {}, {'version': 3}]
        for key in fixture():
            record = fixture()
            del record[key]
            cases.append(record)
        for field in ('build', 'credentials', 'requirements', 'launch', 'components', 'outputs', 'cook'):
            for value in (None, 'invalid', 1, [None]):
                record = fixture()
                record[field] = value
                cases.append(record)
        for record in cases:
            path = self.build / 'connectome.lock'
            path.write_text(json.dumps(record))
            with self.subTest(record=record), self.assertRaises(LockError):
                read_lock(path)

    def test_invalid_json_duplicate_fields_and_nonfinite_numbers_refused(self):
        path = self.build / 'connectome.lock'
        for content in ('{', '{"version": 1, "version": 2}', '{"version": NaN}'):
            path.write_text(content)
            with self.subTest(content=content), self.assertRaises(LockError):
                read_lock(path)

    def test_value_looking_secrets_anywhere_refused_without_echo(self):
        cases = []
        for key in ('value', 'values', 'secretValue', 'token', 'api_key'):
            record = fixture()
            record['requirements'][0][key] = 'synthetic-answer'
            cases.append(record)
        record = fixture(); record['credentials'] = [{'name': 'EXAMPLE_SECRET', 'value': 'synthetic-answer'}]
        cases.append(record)
        record = fixture(); record['credentials'] = ['EXAMPLE_SECRET=synthetic-answer']
        cases.append(record)
        record = fixture(); record['connectomeHost']['url'] = 'https://example:synthetic-answer' + '@' + 'example.invalid/repo'
        cases.append(record)
        record = fixture(); record['recipe']['path'] = 'EXAMPLE_SECRET=synthetic-answer'
        record['recipePath'] = record['recipe']['path']
        cases.append(record)
        record = fixture(); record['extra'] = {'secret': 'synthetic-answer'}
        cases.append(record)
        record = fixture(); record['cook']['version'] = 'gh' + 'p_' + 'A' * 30
        cases.append(record)
        for record in cases:
            with self.subTest(record=record):
                with self.assertRaises(LockError) as error:
                    read_lock(self.record(record))
                self.assertNotIn('synthetic-answer', str(error.exception))

    def test_all_public_credential_shapes_refused_in_every_string(self):
        tokens = [prefix + 'A' * 32 for prefix in (
            'gh' + 'p_', 'gh' + 's_', 'gh' + 'o_', 'gh' + 'u_', 'gh' + 'r_',
            'github' + '_pat_', 'glpat' + '-', 'glpat' + '_', 'xox' + 'b-', 'sk' + '-', 'hvs' + '.')]
        tokens += ['12345678' + ':' + 'A' * 32, 'ey' + 'JA.eA.eA',
                   '-----' + 'BEGIN TEST PRIVATE KEY' + '-----', 'vault' + '/example/credential']
        for token in tokens:
            for field in ('version', 'path'):
                record = fixture()
                if field == 'version':
                    record['cook']['version'] = token
                else:
                    record['recipe']['path'] = record['recipePath'] = '/tmp/' + token
                with self.subTest(field=field, shape=tokens.index(token)):
                    with self.assertRaises(LockError) as error:
                        read_lock(self.record(record))
                    self.assertNotIn(token, str(error.exception))
            # Prove the recursive detector itself catches each shape, rather
            # than relying on a version/schema rejection to hide a missed one.
            for value in ({'nested': [token]}, {token: 'name'}):
                with self.assertRaises(LockError):
                    names_only(value)

    def test_every_lock_scalar_has_an_exact_type(self):
        record = fixture()
        record.update({
            'components': [
                {'kind': 'npm', 'key': 'extra', 'role': 'mcp', 'name': 'example',
                 'spec': '^1.0.0', 'version': '1.0.0', 'integrity': 'sha512-ZGVmZw=='},
                {'kind': 'git', 'key': 'other', 'role': 'extension', 'url': '/tmp/repo',
                 'ref': 'main', 'commit': 'e' * 40, 'path': '/tmp/extension', 'install': ''}],
            'localExtensions': [{'name': 'local', 'hostDir': '/tmp/local', 'path': '/tmp/local/index.js'}],
            'state': {'path': '/tmp/state'},
            'engines': {name: {'path': '/tmp/' + name, 'realPath': '/tmp/' + name, 'version': '1.2.3'}
                        for name in ('bun', 'node')},
            'layout': {'schema': 'connectome.host-layout/v1', 'runtime': '/tmp/runtime',
                       'config': '/tmp/config', 'credentials': '/tmp/credentials', 'state': '/tmp/state',
                       'workspace': '/tmp/workspace', 'connectorState': {'extra': {'DATA_DIR': '/tmp/data'}}},
            'health': {'command': ['node', 'health.js']},
            'hostDependencies': [{'name': adapter.FRAMEWORK, 'originalRange': '^1.0.0', 'version': '1.0.0',
                'sha256': 'd' * 64, 'size': 123, 'snapshot': '/tmp/framework.tgz',
                'packageJsonDelta': {
                    'direct': {'section': 'dependencies', 'name': adapter.FRAMEWORK, 'before': '^1.0.0', 'after': 'file:/tmp/framework.tgz'},
                    'override': {'section': 'overrides', 'name': adapter.FRAMEWORK, 'before': None, 'after': 'file:/tmp/framework.tgz'}}}],
        })
        record['cook']['commit'] = 'f' * 40
        record['outputs'].append({'path': 'example.env', 'mode': 0o600,
                                  'sha256Omitted': 'contains-requirement-value'})
        unit = self.build / 'example.service'
        unit.write_text('[Service]\nExecStart=/bin/true\n')
        record['units'] = [{'path': unit.name, 'sha256': hashlib.sha256(unit.read_bytes()).hexdigest()}]
        read_lock(self.record(record))

        def leaves(value, path=()):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield from leaves(child, path + (key,))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    yield from leaves(child, path + (index,))
            else:
                yield path, value

        for path, original in leaves(record):
            for replacement in (None, [], {}, True, 123):
                if type(replacement) is type(original):
                    continue
                # Mutate after record() materializes the real build paths.
                lock_path = self.record(record)
                changed = json.loads(lock_path.read_text())
                parent = changed
                for key in path[:-1]:
                    parent = parent[key]
                parent[path[-1]] = replacement
                lock_path.write_text(json.dumps(changed))
                with self.subTest(path=path, replacement=replacement), self.assertRaises(LockError):
                    read_lock(lock_path)
        for path in (('cook', 'version'), ('engines', 'bun', 'version'),
                     ('components', 0, 'version'), ('hostDependencies', 0, 'version'),
                     ('cook', 'commit'), ('connectomeHost', 'commit'), ('recipe', 'sha256'),
                     ('createdAt',), ('components', 0, 'integrity')):
            changed = deepcopy(record)
            parent = changed
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = 'not-a-valid-format'
            with self.subTest(path=path), self.assertRaises(LockError):
                read_lock(self.record(changed))

    def test_unit_export_and_planned_units_map_with_containment(self):
        unit = self.build / 'example.service'
        unit.write_text('[Service]\nExecStart=/bin/true\n')
        record = fixture()
        record['launch']['userUnit'] = str(unit)
        mapped = read_lock(self.record(record))
        self.assertEqual(mapped['units'], [{'path': unit.name, 'sha256': hashlib.sha256(unit.read_bytes()).hexdigest()}])
        record['launch'].pop('userUnit')
        record['units'] = mapped['units']
        self.assertEqual(read_lock(self.record(record))['units'], mapped['units'])
        unit.write_text('changed')
        with self.assertRaisesRegex(LockError, 'digest'):
            read_lock(self.record(record))
        record.pop('units')
        outside = self.work / 'outside.service'; outside.write_text('outside')
        record['launch']['userUnit'] = str(outside)
        with self.assertRaisesRegex(LockError, 'escapes'):
            read_lock(self.record(record))

    def test_build_host_and_secret_binding(self):
        path = self.record()
        with self.assertRaisesRegex(LockError, 'host pin'):
            read_lock(path, host_pin='e' * 40)
        with self.assertRaisesRegex(LockError, 'omits'):
            read_lock(path, required_names=['OTHER_SECRET'])
        record = fixture(); record['build']['dir'] = str(self.work)
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(LockError, 'directory'):
            read_lock(path)


class AdapterTests(ScratchTest):
    def spec(self):
        return {'config': {'name': 'example', 'agent': {'systemPrompt': 'Example'},
                'modules': {'lessons': {'settings': {}}}, 'connectors': {'extra': {
                    'repo': 'https://example.invalid/extra.git', 'pin_ref': 'connectors.extra',
                    'build': [['printf', '%s', 'a; b'], ['make', 'build']],
                    'settings': {'command': 'node', 'args': ['index.js'],
                                 'env': {'EXAMPLE_SECRET': '${EXAMPLE_SECRET}'}},
                    'required_secrets': ['EXAMPLE_SECRET']}},
                'cook': {'host_repo': 'https://example.invalid/host.git',
                         'framework_archive': framework_archive(self.work / 'framework.tgz', 'd' * 40),
                         'connector_state': {}}},
                'pins': {'runtime': {'cook': adapter.COOK_COMMIT, 'connectome-host': 'c' * 40,
                                    'agent-framework': 'd' * 40}, 'connectors': {'extra': 'e' * 40}}}

    def test_translate_normalized_spec_and_quote_build_argv(self):
        spec = self.spec(); before = deepcopy(spec)
        recipe, names = adapter.recipe_from_spec(spec)
        self.assertEqual(spec, before)
        self.assertEqual(names, ['EXAMPLE_SECRET'])
        server = recipe['mcpServers']['extra']
        self.assertEqual(server['source']['ref'], 'e' * 40)
        self.assertEqual(server['source']['install']['run'], "printf %s 'a; b' && make build")
        self.assertEqual(recipe['requirements']['EXAMPLE_SECRET'], {'exposeAs': 'EXAMPLE_SECRET', 'required': False})
        self.assertEqual(recipe['modules'], {'lessons': True})

    def test_disabled_missing_launch_literal_secret_and_unpinned_ref_refused(self):
        for change in ('disabled', 'launch', 'literal', 'pin', 'module'):
            spec = self.spec(); entry = spec['config']['connectors']['extra']
            if change == 'disabled': entry['enabled'] = False
            if change == 'launch': entry['settings'].pop('command')
            if change == 'literal': entry['settings']['env']['EXAMPLE_SECRET'] = 'synthetic-answer'
            if change == 'pin': spec['pins']['connectors']['extra'] = 'main'
            if change == 'module': spec['config']['modules']['lessons']['repo'] = 'https://example.invalid/module.git'
            with self.subTest(change=change), self.assertRaises((adapter.AdapterError, adapter.DeployError)):
                adapter.recipe_from_spec(spec)

    def invoke(self, spec, **overrides):
        spec_path = self.build / 'deployment.json'
        spec_path.write_text(json.dumps(spec))
        arguments = dict(spec_path=str(spec_path), build_dir=str(self.build),
                         state_dir=str(self.work / 'state'), runtime=sys.executable,
                         entrypoint='/tmp/example/bin/cook', bun_path=sys.executable, node_path=sys.executable)
        arguments.update(overrides)
        return adapter.adapt(**arguments)

    def test_unsafe_sources_rejected_before_any_subprocess(self):
        unsafe = ['https://user:secret' + '@' + 'host/repo',
                  'ssh://git:secret' + '@' + 'host/repo',
                  'https://user%3Asecret' + '@' + 'host/repo',
                  'http://example.invalid/repo', 'git://example.invalid/repo',
                  'file:///tmp/repo', 'ext::command', 'relative/repo',
                  'https://example.invalid/repo?x=y', 'https://example.invalid/repo#fragment',
                  '/tmp/repo?x=y', '/tmp/repo#fragment',
                  'https://example.invalid/repo%0aextra', 'https://example.invalid/repo\nextra',
                  'https://example.invalid/repo\x7fextra', 'https://example.invalid:bad/repo',
                  '/tmp/' + 'glpat' + '-' + 'A' * 32, {}, []]
        for field in ('host', 'connector'):
            for value in unsafe:
                spec = self.spec()
                if field == 'host':
                    spec['config']['cook']['host_repo'] = value
                else:
                    spec['config']['connectors']['extra']['repo'] = value
                with self.subTest(field=field, case=unsafe.index(value)), patch.object(adapter, 'command') as run:
                    with self.assertRaises(adapter.AdapterError):
                        self.invoke(spec)
                    run.assert_not_called()

    def test_repository_allowlist_and_argument_defense(self):
        for value in ('https://example.invalid/repo.git', 'ssh://git' + '@' + 'example.invalid/repo.git',
                      'git' + '@' + 'example.invalid:repo.git',
                      'git' + '@' + 'example.invalid:/absolute/repo.git', 'example.invalid:repo.git', '/tmp/local repo'):
            with self.subTest(source=value):
                adapter.source({'repo': value}, 'e' * 40, '/connectors/extra')
        for key in ('spec_path', 'build_dir', 'state_dir', 'runtime', 'entrypoint', 'bun_path', 'node_path'):
            for value in ('/tmp/invalid\npath', '/tmp/path?query', '/tmp/path#fragment',
                          '/tmp/' + 'gh' + 's_' + 'A' * 32):
                with self.subTest(key=key), patch.object(adapter, 'command') as run:
                    with self.assertRaises(adapter.AdapterError):
                        self.invoke(self.spec(), **{key: value})
                    run.assert_not_called()
        with patch.object(adapter.subprocess, 'run') as run:
            for value in ('invalid\nargument', 'gh' + 's_' + 'A' * 32):
                with self.assertRaises(adapter.AdapterError):
                    adapter.command(['tool', value])
            run.assert_not_called()

    def test_argv_provenance_fields_rejected_before_any_subprocess(self):
        for pin in ('cook', 'connectome-host', 'agent-framework'):
            spec = self.spec()
            spec['pins']['runtime'][pin] = 'gh' + 's_' + 'A' * 32
            with self.subTest(pin=pin), patch.object(adapter, 'command') as run:
                with self.assertRaises(adapter.DeployError):
                    self.invoke(spec)
                run.assert_not_called()
        for value in ('/tmp/invalid\narchive', '/tmp/archive?query', '/tmp/archive#fragment',
                      '/tmp/' + 'glpat' + '-' + 'A' * 32, {}, []):
            spec = self.spec()
            spec['config']['cook']['framework_archive']['path'] = value
            with self.subTest(archive=value), patch.object(adapter, 'command') as run:
                with self.assertRaises(adapter.AdapterError):
                    self.invoke(spec)
                run.assert_not_called()
        spec = self.spec()
        spec['config']['name'] = 'invalid\nname'
        with patch.object(adapter, 'command') as run:
            with self.assertRaises(adapter.DeployError):
                self.invoke(spec)
            run.assert_not_called()

    def test_framework_hash_and_commit_verified(self):
        spec = self.spec()
        self.assertTrue(adapter.framework_input(spec['config'], 'd' * 40).startswith(adapter.FRAMEWORK + '='))
        with self.assertRaisesRegex(adapter.AdapterError, 'gitHead'):
            adapter.framework_input(spec['config'], 'c' * 40)
        spec['config']['cook']['framework_archive']['sha256'] = 'a' * 64
        with self.assertRaisesRegex(adapter.AdapterError, 'hash'):
            adapter.framework_input(spec['config'], 'd' * 40)

    def test_source_checkout_pin_tree_and_cleanliness(self):
        source = self.work / 'source'
        commit = repository(source, {'bin/cook': 'example'})
        env = adapter.clean_environment()
        with patch.object(adapter, 'COOK_COMMIT', commit):
            adapter.verify_cook(str(source / 'bin/cook'), commit, env)
            with self.assertRaisesRegex(adapter.AdapterError, 'requested pin'):
                adapter.verify_cook(str(source / 'bin/cook'), 'a' * 40, env)
            (source / 'bin/cook').write_text('changed')
            with self.assertRaisesRegex(adapter.AdapterError, 'clean'):
                adapter.verify_cook(str(source / 'bin/cook'), commit, env)

    def test_invocation_names_only_fresh_dir_and_post_build_validation(self):
        spec = self.spec()
        spec_path = self.build / 'deployment.json'; spec_path.write_text(json.dumps(spec))
        calls = []
        def materialize(argv, cwd, env):
            # UNIT double only. RealCookTests below never replaces a tool.
            calls.append(argv)
            self.assertEqual(cwd, self.build)
            self.assertNotIn('EXAMPLE_SECRET', env)
            self.assertNotIn('COOK_PASS_ENV', env)
            self.assertNotIn('BUN_OPTIONS', env)
            self.assertNotIn('synthetic-answer', json.dumps(argv))
            layout = json.loads((self.build / 'cook-layout.json').read_text())
            record = fixture(); record['build']['dir'] = str(self.build)
            record['launch']['script'] = str(self.build / 'run.sh')
            unit = self.build / 'connectome-example.service'
            unit.write_text('[Service]\nExecStart=/bin/true\n')
            record['launch']['userUnit'] = str(unit)
            record['layout'] = layout; record['state'] = {'path': layout['state']}
            record['hostDependencies'] = [{'name': adapter.FRAMEWORK,
                'sha256': spec['config']['cook']['framework_archive']['sha256'],
                'originalRange': '^1.0.0', 'version': '1.0.0', 'size': 123, 'snapshot': '/tmp/example.tgz',
                'packageJsonDelta': {'direct': {'section': 'dependencies', 'name': adapter.FRAMEWORK,
                     'before': '^1.0.0', 'after': 'file:/tmp/example.tgz'},
                    'override': {'section': 'overrides', 'name': adapter.FRAMEWORK,
                     'before': None, 'after': 'file:/tmp/example.tgz'}}}]
            record['components'] = [{'kind': 'git', 'key': 'extra', 'role': 'mcp',
                'url': spec['config']['connectors']['extra']['repo'], 'ref': 'e' * 40,
                'commit': 'e' * 40, 'path': str(self.build / 'connectors/extra'), 'install': 'make build'}]
            (self.build / 'run.sh').write_text('#!/bin/sh\nexit 0\n')
            (self.build / 'connectome.lock').write_text(json.dumps(record))
        with patch.object(adapter, 'verify_cook'), patch.object(adapter, 'command', side_effect=materialize), \
                patch.dict(os.environ, {'EXAMPLE_SECRET': 'synthetic-answer', 'COOK_PASS_ENV': 'EXAMPLE_SECRET',
                                       'BUN_OPTIONS': 'untrusted'}):
            result = adapter.adapt(str(spec_path), str(self.build), str(self.work / 'state'),
                                   sys.executable, '/tmp/example/bin/cook', sys.executable, sys.executable)
        self.assertEqual(result['build']['id'], 'b' * 64)
        for flag in ('install', '--out', '--no-prompts', '--yes', '--strict', '--layout', '--host-dependency', '--user-unit'):
            self.assertIn(flag, calls[0])
        self.assertNotIn('--env-file', calls[0])
        with self.assertRaisesRegex(adapter.AdapterError, 'fresh'):
            adapter.adapt(str(spec_path), str(self.build), str(self.work / 'state'),
                          sys.executable, '/tmp/example/bin/cook', sys.executable, sys.executable)


class RealCookTests(unittest.TestCase):
    def setUp(self):
        # Unlike unit scratch, split-layout roots cannot have group-writable
        # worktree ancestors. The runner supplies a private TMPDIR.
        self.tmp = tempfile.TemporaryDirectory(prefix='connectome-real-')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve()
        self.build = self.work / 'build'
        self.build.mkdir(mode=0o700)

    def test_real_cook_offline_local_host_build(self):
        """No fake runtimes/Git/dependency tools, downloads, or network recipes."""
        required = ('CONNECTOME_TEST_COOK_SOURCE', 'CONNECTOME_TEST_BUN', 'CONNECTOME_TEST_COOK_DEPS')
        missing_env = [name for name in required if not os.environ.get(name)]
        if missing_env:
            self.skipTest('real-cook integration: set ' + ', '.join(missing_env))
        source_value = os.environ['CONNECTOME_TEST_COOK_SOURCE']
        deps = Path(os.environ['CONNECTOME_TEST_COOK_DEPS']).resolve()
        source = Path(source_value).resolve()
        try:
            git(source, 'cat-file', '-e', adapter.COOK_COMMIT)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest('real-cook integration: reviewed cook commit unavailable locally')
        missing = [name for name in ('mri', 'picocolors', 'prompts', 'semver')
                   if not (deps / name / 'package.json').is_file()]
        bun = os.environ['CONNECTOME_TEST_BUN']
        node = os.environ.get('CONNECTOME_TEST_NODE') or shutil.which('node')
        if not node and Path('/opt/node/bin/node').is_file():
            node = '/opt/node/bin/node'
        reasons = (['cook dependencies unavailable offline: ' + ', '.join(missing)] if missing else [])
        reasons += ([] if bun else ['Bun unavailable (host install requires Bun)'])
        reasons += ([] if node else ['Node unavailable'])
        if reasons:
            self.skipTest('real-cook integration: ' + '; '.join(reasons))
        # Copy the exact Git tree into a fresh private temporary checkout;
        # leave the supplied clone untouched. No install/download of cook deps.
        checkout = self.work / 'cook'
        subprocess.run(['git', 'clone', '--quiet', '--no-hardlinks', '--no-checkout', str(source), str(checkout)], check=True)
        git(checkout, 'checkout', '--detach', '-q', adapter.COOK_COMMIT)
        # A symlink is NOT ignored by cook's directory-only node_modules rule.
        # Copy transitives too, dereferencing any dependency symlinks.
        shutil.copytree(deps, checkout / 'node_modules', symlinks=False)
        self.assertEqual(git(checkout, 'status', '--porcelain', '--untracked-files=normal'), '')
        framework = self.work / 'framework'
        framework_pin = repository(framework, {'package.json': json.dumps({'name': adapter.FRAMEWORK, 'version': '1.0.0'}),
                                                'index.js': 'export const example = true;\n'})
        artifact = framework_archive(self.work / 'framework.tgz', framework_pin)
        host = self.work / 'host'
        host_pin = repository(host, {'package.json': json.dumps({'name': 'example-host', 'version': '1.0.0',
                     'type': 'module', 'engines': {'bun': '>=1.0.0', 'node': '>=20.0.0'},
                     'dependencies': {adapter.FRAMEWORK: '^1.0.0'}}), 'src/index.ts': 'console.log("example");\n'})
        spec = {'config': {'name': 'example', 'agent': {'systemPrompt': 'Offline example'},
                          'required_secrets': ['EXAMPLE_SECRET'], 'connectors': {}, 'modules': {},
                          'cook': {'host_repo': str(host), 'framework_archive': artifact, 'connector_state': {}}},
                'pins': {'runtime': {'cook': adapter.COOK_COMMIT, 'connectome-host': host_pin,
                                     'agent-framework': framework_pin}, 'connectors': {}}}
        spec_path = self.build / 'deployment.json'; spec_path.write_text(json.dumps(spec))
        # All acquisition is file-local. An empty private HOME prevents reuse
        # of operator credentials; Git's protocol allowlist prohibits remotes.
        home = self.work / 'empty-home'; home.mkdir(mode=0o700)
        environment = {'HOME': str(home), 'GIT_ALLOW_PROTOCOL': 'file',
                       'EXAMPLE_SECRET': 'synthetic-answer'}
        with patch.dict(os.environ, environment), patch.object(adapter, 'BASE_ENV', adapter.BASE_ENV | {'GIT_ALLOW_PROTOCOL'}):
            record = adapter.adapt(str(spec_path), str(self.build), str(self.work / 'state'),
                                   str(Path(bun).absolute()), str(checkout / 'bin/cook'),
                                   str(Path(bun).absolute()), str(Path(node).absolute()))
        self.assertEqual(record['version'], 2)
        self.assertEqual(record['connectomeHost']['commit'], host_pin)
        self.assertEqual(record['build']['dir'], str(self.build))
        self.assertEqual(record['state']['path'], str(self.work / 'state'))
        self.assertEqual(record['requirements'][0]['envName'], 'EXAMPLE_SECRET')
        self.assertTrue((self.build / 'run.sh').is_file())
        self.assertEqual(record['units'][0]['path'], 'connectome-example.service')
        self.assertNotIn('synthetic-answer', (self.build / 'connectome.lock').read_text())
