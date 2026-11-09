"""Schema/adapter unit tests and a separately identified, genuine offline build."""
from copy import deepcopy
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
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
from connectome_archive import validate_provenance, dependency_provenance, dependency_inputs
from connectome_deploy import run, DeployError, Deployment
from connectome_errors import diagnostic


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


def framework_archive(path, pin, **metadata):
    manifest = {'name': adapter.FRAMEWORK, 'version': '1.0.0', 'main': 'index.js'}
    if pin is not None:
        manifest['gitHead'] = pin
    manifest.update(metadata)
    with tarfile.open(path, 'w:gz', format=tarfile.USTAR_FORMAT) as archive:
        for name, value in {'package.json': json.dumps(manifest),
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
        self.build = self.work / 'deployment/builds/request'
        self.build.parent.parent.mkdir(mode=0o700)
        self.build.parent.mkdir(mode=0o700)
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
        return {'config': {'name': 'example', 'state_dir': str(self.work / 'state'), 'agent': {'systemPrompt': 'Example'},
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

    def test_nonempty_budgets_fail_closed_at_host_schema_boundary(self):
        for budgets in ({'max_steps': 50}, {'max_cost': 2.0}, {'max_cost': 0}):
            spec = self.spec()
            spec['config']['budgets'] = budgets
            with self.subTest(budgets=budgets), patch.object(adapter, 'command') as run:
                with self.assertRaisesRegex(adapter.AdapterError,
                        'not supported by host recipe schema at ' + 'c' * 40) as error:
                    self.invoke(spec)
                self.assertEqual(error.exception.code, 'E_BUDGETS_UNSUPPORTED')
                run.assert_not_called()
        spec = self.spec()
        spec['config']['budgets'] = {}
        recipe, _ = adapter.recipe_from_spec(spec)
        self.assertNotIn('budgets', recipe)

    def test_operator_recipe_skips_connector_module_and_agent_generation(self):
        spec = self.spec()
        content = b'{ "name":"example", "agent":{"systemPrompt":"Verbatim"} }\r\n'
        path = self.work / 'operator.json'; path.write_bytes(content)
        config = spec['config']
        config['recipe_file'] = {'path': str(path), 'sha256': hashlib.sha256(content).hexdigest()}
        config.pop('agent')
        config['connectors']['extra'].pop('pin_ref')
        config['connectors']['extra']['settings'] = {'not': 'generator metadata'}
        config['modules']['lessons']['repo'] = 'not generated'
        spec['pins']['connectors'] = {}
        recipe, names = adapter.recipe_from_spec(spec)
        self.assertEqual(recipe, json.loads(content))
        self.assertEqual(names, ['EXAMPLE_SECRET'])
        for invalid in (b'not JSON', b'[]', b'{"x":NaN}', b'{"x":1,"x":2}'):
            path.write_bytes(invalid)
            config['recipe_file']['sha256'] = hashlib.sha256(invalid).hexdigest()
            with self.subTest(invalid=invalid), patch.object(adapter, 'command') as command:
                with self.assertRaises(adapter.AdapterError):
                    self.invoke(spec)
                command.assert_not_called()

    def test_no_secret_recipe_omits_cook_only_requirements(self):
        spec = self.spec()
        spec['config']['connectors'] = {}
        spec['pins']['connectors'] = {}
        recipe, names = adapter.recipe_from_spec(spec)
        self.assertEqual(names, [])
        self.assertNotIn('requirements', recipe)

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
                      'git-user' + '@' + 'host-name:repo.git',
                      'ssh://git-user' + '@' + 'host-name/repo.git',
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

    def test_unsafe_ssh_and_scp_authorities_rejected_before_any_subprocess(self):
        unsafe = ['-git', '-oProxyCommand=x', '%2dgit']
        for char in (' ', '\t', '\n', '\r', '\x00', '\x7f', '\x85', '\u00a0', '%20', '%09', '%0a', '%7f'):
            unsafe.append('git' + char + 'extra')
        for authority in unsafe:
            for repo in (authority + '@' + 'host:repo.git', 'git' + '@' + authority + ':repo.git',
                         authority + ':repo.git', 'ssh://' + authority + '@' + 'host/repo.git',
                         'ssh://git' + '@' + authority + '/repo.git', 'ssh://' + authority + '/repo.git'):
                for field in ('host', 'connector'):
                    spec = self.spec()
                    if field == 'host':
                        spec['config']['cook']['host_repo'] = repo
                    else:
                        spec['config']['connectors']['extra']['repo'] = repo
                    with self.subTest(field=field, repo=repo), \
                            patch.object(adapter, 'verify_cook', side_effect=AssertionError('verification reached')) as verify, \
                            patch.object(adapter.subprocess, 'run') as run:
                        self.assertFalse(adapter.repository(repo))
                        with self.assertRaises(adapter.AdapterError):
                            self.invoke(spec)
                        verify.assert_not_called()
                        run.assert_not_called()

    def test_option_like_argv_values_rejected_before_spawn(self):
        with patch.object(adapter.subprocess, 'run') as run:
            for argv, flags in ((['-tool'], ()), (['tool', '-value'], ()), (['tool', '-'], ()),
                                (['tool', '--flag', '--flag'], (1,)),
                                (['tool', '--flag', '-oProxyCommand=x'], (1,)),
                                (['tool', '--flag', '--'], (1,))):
                with self.subTest(argv=argv), self.assertRaises(adapter.AdapterError):
                    adapter.command(argv, flag_indices=flags)
            run.assert_not_called()
            run.return_value = subprocess.CompletedProcess([], 0)
            adapter.command(['tool', '--flag', 'value'], flag_indices=(1,))
            run.assert_called_once()

    def test_option_like_cook_value_rejected_before_verification(self):
        spec = self.spec()
        spec['config']['cook']['host_repo'] = '--yes'
        with patch.object(adapter, 'repository', return_value=True), \
                patch.object(adapter, 'verify_cook') as verify, patch.object(adapter, 'command') as run:
            with self.assertRaises(adapter.AdapterError):
                self.invoke(spec)
            verify.assert_not_called()
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

    def test_registry_release_requires_explicit_mark_identity_and_hash(self):
        spec = self.spec()
        artifact = framework_archive(self.work / 'release.tgz', None, version='0.32.0')
        spec['config']['cook']['framework_archive'] = artifact
        with self.assertRaises(adapter.AdapterError) as error:
            adapter.framework_input(spec['config'], 'd' * 40)
        self.assertEqual(error.exception.code, 'E_ARCHIVE_GITHEAD')
        artifact.update(registry_release=True, name=adapter.FRAMEWORK, version='0.32.0')
        with patch.object(adapter.subprocess, 'run') as run:
            provenance = adapter.framework_provenance(spec['config'], 'd' * 40)
            self.assertFalse(provenance['commit_verifiable'])
            self.assertEqual(provenance['requested_commit'], 'd' * 40)
            self.assertEqual(provenance['sha256'], artifact['sha256'])
            self.assertTrue(adapter.framework_input(spec['config'], 'd' * 40))
            run.assert_not_called()
        for key, value in (('name', 'different'), ('version', '0.33.0'), ('version', None),
                           ('registry_release', False), ('registry_release', 'true'),
                           ('registry_release', 1), ('sha256', '0' * 64)):
            modified = deepcopy(spec)
            modified['config']['cook']['framework_archive'][key] = value
            with self.subTest(field=key, value=value), self.assertRaises(adapter.AdapterError) as error:
                adapter.framework_input(modified['config'], 'd' * 40)
            self.assertEqual(error.exception.code, 'E_ARCHIVE_HASH' if key == 'sha256' else 'E_ARCHIVE_IDENTITY')
        for key in ('name', 'version', 'sha256'):
            modified = deepcopy(spec)
            del modified['config']['cook']['framework_archive'][key]
            with self.subTest(missing=key), self.assertRaises(adapter.AdapterError):
                adapter.framework_input(modified['config'], 'd' * 40)

    def test_registry_mark_never_bypasses_present_githead(self):
        for head in ('c' * 40, '', None, False):
            spec = self.spec()
            artifact = framework_archive(self.work / 'release.tgz', None, gitHead=head)
            artifact.update(registry_release=True, name=adapter.FRAMEWORK, version='1.0.0')
            spec['config']['cook']['framework_archive'] = artifact
            with self.subTest(head=head), self.assertRaises(adapter.AdapterError) as error:
                adapter.framework_input(spec['config'], 'd' * 40)
            self.assertEqual(error.exception.code, 'E_ARCHIVE_GITHEAD')
        spec['config']['cook']['framework_archive'] = framework_archive(self.work / 'valid.tgz', 'd' * 40)
        self.assertTrue(adapter.framework_provenance(spec['config'], 'd' * 40)['commit_verifiable'])
        for marked in (False, True):
            if marked:
                spec['config']['cook']['framework_archive'].update(
                    registry_release=True, name=adapter.FRAMEWORK, version='1.0.0')
            self.assertTrue(adapter.framework_provenance(spec['config'], 'd' * 40)['commit_verifiable'])
        # Even a hash-pinned, correctly versioned release must match the expected package name.
        artifact = framework_archive(self.work / 'wrong.tgz', None, name='different')
        artifact.update(registry_release=True, name=adapter.FRAMEWORK, version='1.0.0')
        spec['config']['cook']['framework_archive'] = artifact
        with self.assertRaises(adapter.AdapterError) as error:
            adapter.framework_input(spec['config'], 'd' * 40)
        self.assertEqual(error.exception.code, 'E_ARCHIVE_IDENTITY')

    def test_real_adapter_cli_failure_is_forwarded_without_paths_or_values(self):
        for code in ('E_ARCHIVE_GITHEAD', 'E_ARCHIVE_HASH', 'E_ARCHIVE_IDENTITY', 'E_BUDGETS_UNSUPPORTED'):
            spec = self.spec()
            artifact = framework_archive(self.work / 'release.tgz', None)
            spec['config']['cook']['framework_archive'] = artifact
            if code == 'E_ARCHIVE_HASH':
                artifact['sha256'] = '0' * 64
            if code == 'E_ARCHIVE_IDENTITY':
                artifact.update(registry_release=True, name=adapter.FRAMEWORK, version='0.32.0')
            if code == 'E_BUDGETS_UNSUPPORTED':
                spec['config']['budgets'] = {'max_steps': 1}
            path = self.build / 'deployment.json'
            path.write_text(json.dumps(spec))
            argv = [sys.executable, str(ROOT / 'scripts/cook-adapter.py'),
                    '--spec', str(path), '--out', str(self.build), '--state', str(self.work / 'state'),
                    '--runtime', sys.executable, '--entrypoint', '/synthetic-private-argument/bin/cook',
                    '--bun-path', sys.executable, '--node-path', sys.executable]
            with self.subTest(code=code), self.assertRaises(DeployError) as error:
                run(argv, adapter=True)
            self.assertEqual(error.exception.public(), diagnostic(code))
            self.assertNotIn('synthetic-private-argument', str(error.exception))
            self.assertNotIn(str(self.work), str(error.exception))

    def test_retained_framework_provenance_is_bound_to_request(self):
        spec = self.spec()
        artifact = framework_archive(self.work / 'release.tgz', None)
        artifact.update(registry_release=True, name=adapter.FRAMEWORK, version='1.0.0')
        spec['config']['cook']['framework_archive'] = artifact
        record = adapter.framework_provenance(spec['config'], 'd' * 40)
        Path(artifact['path']).unlink()
        self.assertEqual(validate_provenance(record, spec['config'], 'd' * 40), record)
        for key, value in (('schema', 'other'), ('sha256', '0' * 64), ('name', 'different'),
                           ('version', '0.32.0'), ('requested_commit', 'c' * 40),
                           ('registry_release', False), ('commit_verifiable', None),
                           ('commit_verifiable', 0), ('extra', 'synthetic-answer')):
            modified = dict(record, **{key: value})
            with self.subTest(field=key), self.assertRaises(adapter.AdapterError) as error:
                validate_provenance(modified, spec['config'], 'd' * 40)
            self.assertEqual(error.exception.code, 'E_LOCK_INVALID')

    def dependency(self, kind='artifact', **changes):
        artifact = framework_archive(self.work / 'context.tgz', None,
                                     name="@animalabs/context-manager")
        result = {'name': "@animalabs/context-manager", 'archive': artifact['path'],
                  'sha256': artifact['sha256'], 'version': '1.0.0', 'kind': kind}
        if kind == 'registry_release':
            result.update(registry_url='https://registry.npmjs.org', registry_integrity=
                'sha512-' + base64.b64encode(hashlib.sha512(Path(artifact['path']).read_bytes()).digest()).decode())
        result.update(changes)
        return result

    def test_generic_closed_schema_names_duplicates_and_no_subprocess(self):
        valid = self.dependency()
        cases = [None, {}, 'package', [None], [dict(valid, extra=True)],
                 [dict(valid, kind='unknown')], [dict(valid, kind=[])],
                 [dict(valid, kind='git_commit')], [dict(valid, commit='d' * 40)],
                 [dict(valid, kind='git_commit', commit='main')],
                 [dict(valid, kind='git_commit', commit='0' * 40)],
                 [dict(valid, archive='relative.tgz')], [dict(valid, sha256='bad')],
                 [dict(valid, version='^1.0.0')], [valid, valid],
                 [dict(valid, name=adapter.FRAMEWORK)]]
        for name in ('', 'Upper', '-option', '.hidden', '_hidden', 'with space', 'a/b', "@scope",
                     "@scope/../bad", 'a%2fb', 'node_modules', 'favicon.ico', 'a' * 215):
            cases.append([dict(valid, name=name)])
        for key in valid:
            cases.append([{k: v for k, v in valid.items() if k != key}])
        for entries in cases:
            spec = self.spec()
            spec['config']['cook']['host_dependencies'] = entries
            with self.subTest(entries=entries), patch.object(adapter, 'command') as run:
                with self.assertRaises((adapter.AdapterError, DeployError)) as error:
                    self.invoke(spec)
                self.assertIn(error.exception.code, ('E_ADAPTER_INPUT', 'E_ARCHIVE_IDENTITY', 'E_ARCHIVE_HASH'))
                run.assert_not_called()
        for name in ('example', "@scope/example", 'a.b_c-1', "@a/b"):
            dependency_inputs({'cook': {'host_dependencies': [dict(valid, name=name)]}}, 'd' * 40)

    def test_registry_claim_requires_origin_and_sha512_without_network(self):
        spec = self.spec()
        entry = self.dependency('registry_release')
        spec['config']['cook']['host_dependencies'] = [entry]
        with patch.object(adapter.subprocess, 'run') as run:
            record = dependency_provenance(spec['config'], 'd' * 40)[1]
            self.assertEqual(record['registry_comparison'], 'bytes were not compared against the registry')
            self.assertEqual(record['registry_url'], entry['registry_url'])
            self.assertEqual(record['registry_integrity'], entry['registry_integrity'])
            self.assertFalse(record['commit_verifiable'])
            run.assert_not_called()
        cases = [{'registry_url': None}, {'registry_integrity': None},
                 {'registry_url': 'http://registry.npmjs.org'},
                 {'registry_url': 'https://user:pass' + '@' + 'example.invalid'},
                 {'registry_url': 'https://registry.npmjs.org/path'},
                 {'registry_url': 'https://registry.npmjs.org?token=x'},
                 {'registry_integrity': 'sha256-' + 'A' * 44},
                 {'registry_integrity': 'sha512-invalid'},
                 {'registry_integrity': 'sha512-' + base64.b64encode(b'x' * 64).decode()}]
        for change in cases:
            spec['config']['cook']['host_dependencies'] = [dict(entry, **change)]
            with self.subTest(change=change), self.assertRaises(adapter.AdapterError):
                dependency_provenance(spec['config'], 'd' * 40)
        for key in ('registry_url', 'registry_integrity'):
            changed = dict(entry); changed.pop(key)
            spec['config']['cook']['host_dependencies'] = [changed]
            with self.assertRaises(adapter.AdapterError):
                dependency_provenance(spec['config'], 'd' * 40)
        for key in ('allow_scripts', 'ignore_scripts', 'trustedDependencies', 'scripts'):
            spec['config']['cook']['host_dependencies'] = [dict(self.dependency(), **{key: True})]
            with self.subTest(key=key), patch.object(adapter, 'command') as run:
                with self.assertRaises(adapter.AdapterError):
                    self.invoke(spec)
                run.assert_not_called()
            spec['config']['cook']['host_dependencies'] = [self.dependency()]
            spec['config']['cook'][key] = True
            with self.subTest(cook_option=key), patch.object(adapter, 'command') as run:
                with self.assertRaises(adapter.AdapterError):
                    self.invoke(spec)
                run.assert_not_called()
            del spec['config']['cook'][key]
        legacy = framework_archive(self.work / 'release.tgz', None)
        legacy.update(registry_release=True, name=adapter.FRAMEWORK, version='1.0.0')
        spec['config']['cook'] = {'framework_archive': legacy}
        record = dependency_provenance(spec['config'], 'd' * 40)[0]
        self.assertEqual(record['kind'], 'artifact')
        self.assertEqual(record['payload'], 'operator-supplied prebuilt payload, verified out of band')
        self.assertNotIn('registry_url', record)
        self.assertNotIn('registry_integrity', record)

    def test_generic_archive_identity_hash_file_and_githead_rules(self):
        spec = self.spec()
        settings = spec['config']['cook']
        for kind in ('artifact', 'registry_release', 'git_commit'):
            for head in (None, 'd' * 40, 'c' * 40, '', False):
                entry = self.dependency(kind)
                artifact = framework_archive(Path(entry['archive']), head,
                                             name=entry['name'])
                entry['sha256'] = artifact['sha256']
                if kind == 'registry_release':
                    entry['registry_integrity'] = 'sha512-' + base64.b64encode(
                        hashlib.sha512(Path(entry['archive']).read_bytes()).digest()).decode()
                if kind == 'git_commit':
                    entry['commit'] = 'd' * 40
                settings['host_dependencies'] = [entry]
                good = head == 'd' * 40 if kind == 'git_commit' else head in (None, 'd' * 40, 'c' * 40)
                with self.subTest(kind=kind, head=head):
                    if good:
                        records = dependency_provenance(spec['config'], 'd' * 40)
                        self.assertEqual(records[1]['commit_verifiable'], kind == 'git_commit')
                        if kind == 'artifact':
                            self.assertEqual(records[1]['payload'], 'operator-supplied prebuilt payload, verified out of band')
                    else:
                        with self.assertRaises(adapter.AdapterError) as error:
                            dependency_provenance(spec['config'], 'd' * 40)
                        self.assertEqual(error.exception.code, 'E_ARCHIVE_GITHEAD')
        for change, code in (({'name': 'other'}, 'E_ARCHIVE_IDENTITY'),
                             ({'version': '2.0.0'}, 'E_ARCHIVE_IDENTITY'),
                             ({'sha256': '0' * 64}, 'E_ARCHIVE_HASH'),
                             ({'archive': str(self.work)}, 'E_ADAPTER_INPUT')):
            settings['host_dependencies'] = [dict(self.dependency(), **change)]
            with self.subTest(change=change), self.assertRaises(adapter.AdapterError) as error:
                dependency_provenance(spec['config'], 'd' * 40)
            self.assertEqual(error.exception.code, code)
        entry = self.dependency()
        link = self.work / 'link.tgz'; link.symlink_to(entry['archive'])
        settings['host_dependencies'] = [dict(entry, archive=str(link))]
        with self.assertRaises(adapter.AdapterError):
            dependency_provenance(spec['config'], 'd' * 40)
        Path(entry['archive']).write_bytes(b'not an archive')
        entry['sha256'] = hashlib.sha256(Path(entry['archive']).read_bytes()).hexdigest()
        settings['host_dependencies'] = [entry]
        with self.assertRaises(adapter.AdapterError) as error:
            dependency_provenance(spec['config'], 'd' * 40)
        self.assertEqual(error.exception.code, 'E_ADAPTER_INPUT')

    def test_generic_plan_lock_exact_binding_and_retention(self):
        spec = self.spec()
        spec['config']['connectors'] = {}; spec['pins']['connectors'] = {}
        spec['config']['cook']['host_dependencies'] = [self.dependency()]
        dependencies = dependency_provenance(spec['config'], 'd' * 40)
        calls = []
        def materialize(argv, **kwargs):
            calls.append(argv)
            record = fixture(); record['build']['dir'] = str(self.build)
            record['components'] = []
            record['layout'] = json.loads((self.build / 'cook-layout.json').read_text())
            record['state'] = {'path': spec['config']['state_dir']}
            record['launch']['script'] = str(self.build / 'run.sh')
            (self.build / 'run.sh').write_text('exit 0')
            record['hostDependencies'] = []
            for entry in dependencies:
                snapshot = str(self.build / (entry['name'].split('/')[-1] + '.tgz'))
                record['hostDependencies'].append({'name': entry['name'], 'version': entry['version'],
                    'sha256': entry['sha256'], 'originalRange': '^1.0.0', 'size': 123, 'snapshot': snapshot,
                    'packageJsonDelta': {'direct': {'section': 'dependencies', 'name': entry['name'],
                        'before': '^1.0.0', 'after': 'file:' + snapshot},
                        'override': {'section': 'overrides', 'name': entry['name'],
                        'before': None, 'after': 'file:' + snapshot}}})
            (self.build / 'connectome.lock').write_text(json.dumps(record))
        with patch.object(adapter, 'verify_cook'), patch.object(adapter, 'command', side_effect=materialize):
            record = self.invoke(spec)
        self.assertEqual([calls[0][i + 1] for i, arg in enumerate(calls[0]) if arg == '--host-dependency'],
            [d['name'] + '=' + d['archive'] + '#sha256=' + d['sha256'] for d in dependencies])
        self.assertEqual([d['provenance'] for d in record['hostDependencies']], dependencies)
        # Reader cross-checks config even after the operator removes inputs.
        for entry in dependencies:
            Path(entry['archive']).unlink()
        path = self.build / 'connectome.lock'
        original = path.read_text()
        kwargs = dict(config=spec['config'], framework_pin='d' * 40)
        read_lock(path, **kwargs)
        for mutate in ('missing', 'extra', 'duplicate', 'order', 'provenance', 'hash', 'version', 'kind', 'archive', 'commit', 'verifiable', 'payload'):
            data = json.loads(original)
            dep = data['hostDependencies'][1]
            if mutate == 'missing': data['hostDependencies'].pop()
            if mutate == 'order': data['hostDependencies'].reverse()
            if mutate == 'provenance': dep.pop('provenance')
            if mutate in ('extra', 'duplicate'): data['hostDependencies'].append(deepcopy(dep))
            if mutate == 'extra': data['hostDependencies'][-1]['name'] = 'extra'
            if mutate == 'hash': dep['sha256'] = '0' * 64
            if mutate == 'version': dep['version'] = '2.0.0'
            if mutate == 'kind': dep['provenance']['kind'] = 'registry_release'; dep['provenance'].pop('payload')
            if mutate == 'archive': dep['provenance']['archive'] += '.other'
            if mutate == 'commit': dep['provenance']['commit'] = 'd' * 40
            if mutate == 'verifiable': dep['provenance']['commit_verifiable'] = True
            if mutate == 'payload': dep['provenance']['payload'] = 'source'
            path.write_text(json.dumps(data))
            with self.subTest(mutate=mutate), self.assertRaises(LockError):
                read_lock(path, **kwargs)
        path.write_text(original)
        # Config drift is refused too, not merely self-consistency of the lock.
        for key, value in (('archive', '/elsewhere.tgz'), ('version', '2.0.0'), ('sha256', '0' * 64),
                           ('name', 'other'), ('kind', 'registry_release')):
            changed = deepcopy(spec['config']); changed['cook']['host_dependencies'][0][key] = value
            with self.subTest(config_field=key), self.assertRaises(LockError):
                read_lock(path, config=changed, framework_pin='d' * 40)

    def test_lock_state_mismatch_has_typed_code(self):
        with patch.object(adapter, 'verify_cook'), patch.object(adapter, 'command'), \
                patch.object(adapter, 'read_lock', return_value={'state': {'path': '/wrong'}, 'layout': {}}):
            with self.assertRaises(adapter.AdapterError) as error:
                self.invoke(self.spec())
        self.assertEqual(error.exception.code, 'E_LOCK_STATE_PATH')

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

    def test_source_checkout_git_argv_trusts_exact_resolved_path(self):
        source = self.work / 'source'
        commit = repository(source, {'bin/cook': 'example'})
        link = self.work / 'link'; link.symlink_to(source)
        checkout = str(source.resolve())
        prefix = ['git', '-c', 'safe.directory=' + checkout, '-c', 'core.fsmonitor=false',
                  '-c', 'core.hooksPath=/dev/null', '--no-optional-locks', '-C', checkout]
        calls = []
        def capture(argv, env, capture, flag_indices):
            adapter.validate_arguments(argv, flag_indices)
            calls.append((argv, tuple(flag_indices)))
            return ('' if 'status' in argv else commit).encode()
        with patch.object(adapter, 'COOK_COMMIT', commit), patch.object(adapter, 'command', side_effect=capture):
            adapter.verify_cook(str(link / 'bin/cook'), commit, {})
        self.assertEqual([argv[len(prefix):] for argv, _ in calls], [
            ['rev-parse', 'HEAD'], ['rev-parse', 'HEAD^{tree}'], ['rev-parse', commit + '^{tree}'],
            ['status', '--porcelain', '--untracked-files=normal']])
        for argv, flags in calls:
            self.assertEqual(argv[:len(prefix)], prefix)
            self.assertNotIn('safe.directory=*', argv)
            self.assertEqual(flags, (1, 3, 5, 7, 8) + ((len(prefix) + 1, len(prefix) + 2) if 'status' in argv else ()))

    @unittest.skipUnless(shutil.which('fakeroot'), 'fakeroot unavailable')
    def test_source_checkout_owned_by_another_uid_is_verified(self):
        # Under fakeroot the euid is 0 and the chowned checkout reports uid 4242,
        # which trips Git's real ownership check without touching any gitconfig.
        source = self.work / 'source'
        commit = repository(source, {'bin/cook': 'example'})
        # Only fakeroot's own preload state is added to the stripped Git env, so
        # Git still sees the faked euid and ownership.
        program = ('import sys; sys.path.insert(0, sys.argv[1]); import connectome_cook as a; '
                   'a.COOK_COMMIT = sys.argv[3]; a.verify_cook(sys.argv[2] + "/bin/cook", sys.argv[3], '
                   'dict({k: v for k, v in a.os.environ.items() if k == "PATH" or k == "FAKED_MODE" '
                   'or k == "FAKEROOTKEY" or k.startswith("LD_")}, HOME=sys.argv[4]))')
        home = self.work / 'home'; home.mkdir()
        env = {'PATH': os.environ['PATH'], 'HOME': str(home)}
        script = ('chown -R 4242:4242 "$1" && ! git -C "$1" rev-parse HEAD 2>/dev/null && '
                  'exec "$2" -I -c "$3" "$4" "$1" "$5" "$6"')
        subprocess.run(['fakeroot', 'sh', '-c', script, 'sh', str(source), sys.executable, program,
                        str(ROOT / 'roles/connectome_agent/module_utils'), commit, str(home)],
                       env=env, check=True, stdout=subprocess.DEVNULL)
        self.assertFalse((home / '.gitconfig').exists())

    def test_invocation_names_only_fresh_dir_and_post_build_validation(self):
        spec = self.spec()
        spec_path = self.build / 'deployment.json'; spec_path.write_text(json.dumps(spec))
        calls = []
        raw_locks = []
        def materialize(argv, cwd, env, flag_indices):
            # UNIT double only. RealCookTests below never replaces a tool.
            adapter.validate_arguments(argv, flag_indices)
            calls.append(argv)
            self.assertEqual(cwd, self.build)
            self.assertNotIn('EXAMPLE_SECRET', env)
            self.assertNotIn('COOK_PASS_ENV', env)
            self.assertNotIn('BUN_OPTIONS', env)
            self.assertNotIn('synthetic-answer', json.dumps(argv))
            layout = json.loads((self.build / 'cook-layout.json').read_text())
            self.assertEqual(layout['state'], spec['config']['state_dir'])
            self.assertEqual(layout['config'], str(self.work / 'deployment/cook-config/request'))
            self.assertEqual(layout['workspace'], str(self.work / 'deployment/workspace'))
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
            raw_locks.append(json.dumps(record).encode())
            (self.build / 'connectome.lock').write_bytes(raw_locks[-1])
        with patch.object(adapter, 'verify_cook'), patch.object(adapter, 'command', side_effect=materialize), \
                patch.dict(os.environ, {'EXAMPLE_SECRET': 'synthetic-answer', 'COOK_PASS_ENV': 'EXAMPLE_SECRET',
                                       'BUN_OPTIONS': 'untrusted'}):
            result = adapter.adapt(str(spec_path), str(self.build), str(self.work / 'state'),
                                   sys.executable, '/tmp/example/bin/cook', sys.executable, sys.executable)
        # Exact pre-generic invocation contract from baseline 0be0db3.
        expected_argv = [sys.executable, '/tmp/example/bin/cook', 'install', str(self.build / 'cook-recipe.json'),
            '--out', str(self.build), '--no-prompts', '--yes', '--strict',
            '--ch-repo', spec['config']['cook']['host_repo'], '--ch-ref', 'c' * 40,
            '--bun-path', sys.executable, '--node-path', sys.executable, '--layout', str(self.build / 'cook-layout.json'),
            '--host-dependency', adapter.FRAMEWORK + '=' + spec['config']['cook']['framework_archive']['path'] +
            '#sha256=' + spec['config']['cook']['framework_archive']['sha256'], '--user-unit', 'connectome-example.service']
        self.assertEqual(json.dumps(calls[0]).encode(), json.dumps(expected_argv).encode())
        self.assertEqual((self.build / 'connectome.lock').read_bytes(), raw_locks[0])
        self.assertEqual(result['build']['id'], 'b' * 64)
        provenance = json.loads((self.build / 'framework.lock.json').read_text())
        self.assertTrue(provenance['commit_verifiable'])
        self.assertEqual(provenance['requested_commit'], 'd' * 40)
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
        self.build = self.work / 'deployment/builds/request'
        self.build.parent.parent.mkdir(mode=0o700)
        self.build.parent.mkdir(mode=0o700)
        self.build.mkdir(mode=0o700)

    def test_real_cook_offline_operator_recipe_and_secret_files(self):
        self.real_build(registry=False, operator_inputs=True)

    def test_real_cook_offline_external_mcp_server(self):
        self.real_build(registry=False, operator_inputs=True, external=True)

    def test_real_cook_offline_install_only_then_start_reuses_release(self):
        self.real_build(registry=False, operator_inputs=True, staging=True)

    def test_real_cook_offline_private_home_pass_through_and_host_cwd(self):
        self.real_build(registry=False, with_secrets=False, home_passthrough=True)

    def test_real_cook_offline_local_host_build(self):
        self.real_build(registry=False)

    def test_real_cook_offline_registry_release_build(self):
        self.real_build(registry=True)

    def test_real_cook_offline_no_secrets_build(self):
        self.real_build(registry=False, with_secrets=False)

    def test_real_cook_offline_two_dependencies(self):
        self.real_build(registry=False, two_dependencies=True)

    def test_real_cook_offline_generic_only_two_dependencies(self):
        self.real_build(registry=False, two_dependencies=True, generic_only=True)

    def test_real_cook_offline_registry_origin_and_integrity(self):
        self.real_build(registry=False, two_dependencies=True, generic_only=True, registry_input=True)

    def test_real_cook_refuses_trusted_lifecycle_dependency(self):
        self.real_build(registry=False, two_dependencies=True, trusted=True)

    def real_build(self, registry, with_secrets=True, two_dependencies=False, generic_only=False,
                   trusted=False, registry_input=False, operator_inputs=False, staging=False,
                   home_passthrough=False, external=False):
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
        artifact = framework_archive(self.work / 'framework.tgz', None if registry else framework_pin)
        if registry:
            artifact.update(registry_release=True, name=adapter.FRAMEWORK, version='1.0.0')
        dependencies = {adapter.FRAMEWORK: '^1.0.0'}
        extra = None
        if two_dependencies:
            name = "@animalabs/context-manager"
            extra = framework_archive(self.work / 'context.tgz', None, name=name,
                scripts={'postinstall': 'touch ' + str(self.work / 'lifecycle-ran')})
            dependencies[name] = '^1.0.0'
        if home_passthrough:
            host_source = ('import {writeFileSync} from "node:fs";\n'
                           'writeFileSync("environment-observed.json", JSON.stringify({\n'
                           '  home: process.env.HOME, cwd: process.cwd(), data_dir: process.env.DATA_DIR\n'
                           '}));\n')
        elif operator_inputs:
            host_source = ('import {readFileSync, writeFileSync} from "node:fs";\n'
                           'const value = readFileSync(process.env.EXAMPLE_FILE!, "utf8");\n'
                           'if (!value || process.env.EXAMPLE_SECRET) process.exit(8);\n'
                           'writeFileSync(process.env.DATA_DIR + "/file-seen", "ok");\n')
        else:
            host_source = 'console.log("example");\n'
        host = self.work / 'host'
        host_pin = repository(host, {'package.json': json.dumps({'name': 'example-host', 'version': '1.0.0',
                     'type': 'module', 'engines': {'bun': '>=1.0.0', 'node': '>=20.0.0'},
                     'dependencies': dependencies,
                     **({'trustedDependencies': ["@animalabs/context-manager"]} if trusted else {})}),
                     'src/index.ts': host_source})
        spec = {'config': {'name': 'example', 'state_dir': str(self.work / 'state'),
                          'secrets_file': str(self.work / 'secrets/example.json'),
                          'agent': {'systemPrompt': 'Offline example'},
                          'required_secrets': ['EXAMPLE_SECRET'] if with_secrets else [], 'connectors': {}, 'modules': {},
                          'cook': {'host_repo': str(host), 'framework_archive': artifact, 'connector_state': {}}},
                'pins': {'runtime': {'cook': adapter.COOK_COMMIT, 'connectome-host': host_pin,
                                     'agent-framework': framework_pin}, 'connectors': {}}}
        if two_dependencies:
            spec['config']['cook']['host_dependencies'] = [{'name': "@animalabs/context-manager",
                'archive': extra['path'], 'sha256': extra['sha256'], 'kind': 'artifact', 'version': '1.0.0'}]
        if registry_input:
            spec['config']['cook']['host_dependencies'][0].update(kind='registry_release',
                registry_url='https://registry.npmjs.org', registry_integrity='sha512-' +
                base64.b64encode(hashlib.sha512(Path(extra['path']).read_bytes()).digest()).decode())
        if generic_only:
            spec['config']['cook'].pop('framework_archive')
            spec['config']['cook']['host_dependencies'].insert(0, {'name': adapter.FRAMEWORK,
                'archive': artifact['path'], 'sha256': artifact['sha256'], 'version': '1.0.0',
                'kind': 'git_commit', 'commit': framework_pin})
        if home_passthrough:
            self.home_environment_lifecycle(spec, checkout, bun, node)
            return
        if operator_inputs:
            self.operator_lifecycle(spec, checkout, bun, node, staging=staging, external=external)
            return
        spec_path = self.build / 'deployment.json'; spec_path.write_text(json.dumps(spec))
        # All acquisition is file-local. An empty private HOME prevents reuse
        # of operator credentials; Git's protocol allowlist prohibits remotes.
        home = self.work / 'empty-home'; home.mkdir(mode=0o700)
        environment = {'HOME': str(home), 'GIT_ALLOW_PROTOCOL': 'file',
                       'EXAMPLE_SECRET': 'synthetic-answer'}
        with patch.dict(os.environ, environment), patch.object(adapter, 'BASE_ENV', adapter.BASE_ENV | {'GIT_ALLOW_PROTOCOL'}):
            if trusted:
                with self.assertRaises(adapter.AdapterError) as error:
                    adapter.adapt(str(spec_path), str(self.build), str(self.work / 'state'),
                        str(Path(bun).absolute()), str(checkout / 'bin/cook'),
                        str(Path(bun).absolute()), str(Path(node).absolute()))
                self.assertEqual(error.exception.code, 'E_COOK_FAILED')
                self.assertEqual(error.exception.cook_error_code, 'E_HOST_DEPENDENCY_TRUSTED')
                self.assertFalse((self.work / 'lifecycle-ran').exists())
                return
            record = adapter.adapt(str(spec_path), str(self.build), str(self.work / 'state'),
                                   str(Path(bun).absolute()), str(checkout / 'bin/cook'),
                                   str(Path(bun).absolute()), str(Path(node).absolute()))
        self.assertEqual(record['version'], 2)
        if not generic_only:
            provenance = json.loads((self.build / 'framework.lock.json').read_text())
            self.assertEqual(provenance['commit_verifiable'], not registry)
            self.assertEqual(provenance['registry_release'], registry)
            self.assertEqual(provenance['sha256'], artifact['sha256'])
        if registry:
            self.assertEqual(record['hostDependencies'][0]['provenance']['kind'], 'artifact')
            self.assertEqual(record['hostDependencies'][0]['provenance']['payload'],
                             'operator-supplied prebuilt payload, verified out of band')
        if two_dependencies:
            self.assertEqual(len(record['hostDependencies']), 2)
            self.assertFalse((self.work / 'lifecycle-ran').exists())
            read_lock(self.build / 'connectome.lock', config=spec['config'], framework_pin=framework_pin)
            for dep in record['hostDependencies']:
                self.assertEqual(dep['sha256'], dep['provenance']['sha256'])
            payload = next(d for d in record['hostDependencies'] if d['name'] == "@animalabs/context-manager")
            self.assertFalse(payload['provenance']['commit_verifiable'])
            if registry_input:
                self.assertEqual(payload['provenance']['registry_comparison'],
                                 'bytes were not compared against the registry')
                lock_path = self.build / 'connectome.lock'
                original = lock_path.read_text()
                for key, value in (('registry_url', 'https://example.invalid'),
                    ('registry_integrity', 'sha512-' + base64.b64encode(b'x' * 64).decode()),
                    ('registry_comparison', 'verified online')):
                    data = json.loads(original)
                    data['hostDependencies'][1]['provenance'][key] = value
                    lock_path.write_text(json.dumps(data))
                    with self.subTest(field=key), self.assertRaises(LockError):
                        read_lock(lock_path, config=spec['config'], framework_pin=framework_pin)
                lock_path.write_text(original)
            else:
                self.assertEqual(payload['provenance']['payload'],
                                 'operator-supplied prebuilt payload, verified out of band')
        self.assertEqual(record['connectomeHost']['commit'], host_pin)
        self.assertEqual(record['build']['dir'], str(self.build))
        self.assertEqual(record['state']['path'], str(self.work / 'state'))
        if with_secrets:
            self.assertEqual(record['requirements'][0]['envName'], 'EXAMPLE_SECRET')
        else:
            self.assertEqual(record['requirements'], [])
            rendered = Path(record['layout']['config']) / 'recipes/cook-recipe.json'
            self.assertNotIn('requirements', json.loads(rendered.read_text()))
        self.assertTrue((self.build / 'run.sh').is_file())
        self.assertIn("export DATA_DIR='" + str(self.work / 'state') + "'",
                      (self.build / 'run.sh').read_text())
        self.assertIn(str(self.work / 'state'), (self.build / 'connectome-example.service').read_text())
        self.assertEqual(record['units'][0]['path'], 'connectome-example.service')
        self.assertNotIn('synthetic-answer', (self.build / 'connectome.lock').read_text())
        # The role also consumes the real lock and its sidecar for retained
        # builds after the original archive is no longer provisioned.
        Path(artifact['path']).unlink()
        deployment = Deployment(self.build.parent.parent, self.work / 'units', spec['config'],
                                {'runtime': spec['pins']['runtime']}, ['unused'])
        self.assertEqual(deployment._lock(self.build)['build'], record['build'])
        if two_dependencies:
            # Adapter fixture uses a literal request path rather than role hashing.
            deployment.build = self.build
            Path(extra['path']).unlink()
            (self.build / '.prepared').write_text(deployment.request_id)
            self.assertEqual(len(deployment.plan()['host_dependencies']), 2)
        if generic_only:
            return
        (self.build / 'framework.lock.json').unlink()
        if registry:
            with self.assertRaises(DeployError) as error:
                deployment._lock(self.build)
            self.assertEqual(error.exception.code, 'E_LOCK_INVALID')
        else:
            # Old gitHead-only builds did not have this role-owned sidecar.
            self.assertEqual(deployment._lock(self.build)['build'], record['build'])

    def home_environment_lifecycle(self, spec, checkout, bun, node):
        """Simulate manager EnvironmentFile delivery, then execute real run.sh.

        This is not a live-systemd qualification. HOME is operator infrastructure
        in a separate private file, not a cook requirement or a secret value.
        """
        config = spec['config']
        secrets = Path(config['secrets_file'])
        secrets.parent.mkdir(mode=0o700)
        secrets.write_text('{}')
        secrets.chmod(0o600)
        build_home = self.work / 'build-home'; build_home.mkdir(mode=0o700)
        runtime_home = self.work / 'runtime home'; runtime_home.mkdir(mode=0o700)
        private_env = self.work / 'private-home.env'
        private_env.write_text('HOME="' + str(runtime_home) + '"\n')
        private_env.chmod(0o600)
        cook = [sys.executable, str(ROOT / 'scripts/cook-adapter.py'),
                '--runtime', str(Path(bun).absolute()), '--entrypoint', str(checkout / 'bin/cook'),
                '--bun-path', str(Path(bun).absolute()), '--node-path', str(Path(node).absolute()),
                '--spec', '{recipe}', '--out', '{build_dir}', '--state', '{state_dir}']
        root = self.build.parent.parent
        systemctl = [sys.executable, str(ROOT / 'tests/fixtures/systemctl.py'), str(root)]
        with patch.dict(os.environ, {'HOME': str(build_home), 'GIT_ALLOW_PROTOCOL': 'file'}):
            dep = Deployment(root, self.work / 'units', config, {'runtime': spec['pins']['runtime']},
                             cook, systemctl)
            dep.prepare()
            dep.activate({})
        unit = dep.unit_dir / 'connectome-example.service'
        private_dropin = unit.parent / (unit.name + '.d') / '95-private-home.conf'
        private_dropin.write_text('[Service]\nEnvironmentFile=' + str(private_env) + '\n')
        private_dropin.chmod(0o600)
        bodies = [unit.read_text(), *(path.read_text() for path in sorted(private_dropin.parent.glob('*.conf')))]
        # Fail on any explicit HOME assignment, not only the common unquoted
        # spelling. Check cook's export and every installed role/private drop-in.
        for body in bodies:
            for line in body.splitlines():
                if line.startswith('Environment='):
                    self.assertFalse(any(word.partition('=')[0] == 'HOME'
                                         for word in shlex.split(line.partition('=')[2])))
        self.assertIn('WorkingDirectory=' + str(dep.state_dir) + '\n', bodies[0])
        self.assertIn('EnvironmentFile=' + str(private_env) + '\n', bodies[-1])
        environment = {key: os.environ[key] for key in ('PATH',) if key in os.environ}
        # Parse only the single quoted assignment used by this synthetic file;
        # this deliberately is not a general systemd EnvironmentFile parser.
        name, value = shlex.split(private_env.read_text())[0].split('=', 1)
        environment[name] = value
        launcher = next(line.partition('=')[2] for line in bodies[0].splitlines()
                        if line.startswith('ExecStart='))
        observed = dep.state_dir / 'environment-observed.json'
        # Even a different initial cwd cannot override run.sh's cd layout.state.
        for initial_cwd in (dep.state_dir, self.work):
            observed.unlink(missing_ok=True)
            subprocess.run(shlex.split(launcher), cwd=initial_cwd, env=environment,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
            self.assertEqual(json.loads(observed.read_text()), {
                'home': str(runtime_home), 'cwd': str(dep.state_dir), 'data_dir': str(dep.state_dir)})
        self.assertFalse((self.work / observed.name).exists())

    def operator_lifecycle(self, spec, checkout, bun, node, staging=False, external=False):
        from connectome_inputs import collect
        config = spec['config']
        config.pop('agent')
        config['required_secrets'] = []
        # These descriptors must never be translated or need source pins.
        config['connectors'] = {'unresolved': {'settings': {}}}
        config['modules'] = {'unused': {'repo': 'not translated'}}
        config['secret_files'] = [{'secret': 'EXAMPLE_SECRET', 'env': 'EXAMPLE_FILE'}]
        config['health'] = {'command': [sys.executable, str(ROOT / 'tests/fixtures/host-health.py')]}
        recipe = self.work / 'operator.json'
        content = (b'{ "name":"example", "agent":{"systemPrompt":"Operator recipe"}, '
                   b'"requirements":{"EXAMPLE_FILE":{"exposeAs":"EXAMPLE_FILE","required":false}} }\r\n')
        if external:
            from test_external import system_command
            command = system_command()
            if command is None:
                self.skipTest('real-cook integration: no root-owned system executable with safe ancestors')
            unsafe = self.work / 'unsafe-server'; unsafe.write_text('#!/bin/sh\n'); unsafe.chmod(0o755)
            servers = {'alpha': {'command': command, 'source': {'external': True},
                                 'args': ['--root=' + str(Path(os.path.realpath(command)).parent)]}}
            content = content.replace(b'"requirements"', b'"mcpServers":' + json.dumps(servers).encode() +
                                      b', "requirements"')
        recipe.write_bytes(content)
        config['recipe_file'] = {'path': str(recipe), 'sha256': hashlib.sha256(content).hexdigest()}
        home = self.work / 'empty-home'; home.mkdir(mode=0o700)
        secrets_dir = self.work / 'secrets'; secrets_dir.mkdir(mode=0o700)
        canary = 'synthetic-offline-file-canary\nsecond line\n'
        secrets = Path(config['secrets_file'])
        secrets.write_text(json.dumps({'EXAMPLE_SECRET': canary}))
        secrets.chmod(0o600)
        cook = [sys.executable, str(ROOT / 'scripts/cook-adapter.py'),
                '--runtime', str(Path(bun).absolute()), '--entrypoint', str(checkout / 'bin/cook'),
                '--bun-path', str(Path(bun).absolute()), '--node-path', str(Path(node).absolute()),
                '--spec', '{recipe}', '--out', '{build_dir}', '--state', '{state_dir}']
        root = self.build.parent.parent
        systemctl = [sys.executable, str(ROOT / 'tests/fixtures/systemctl.py'), str(root)]
        if staging:
            config['activation'] = 'install_only'
            (root.parent / 'track-unit-state').touch()
            (root.parent / 'forbid-unit-activation').touch()
        with patch.dict(os.environ, {'HOME': str(home), 'GIT_ALLOW_PROTOCOL': 'file'}):
            first = Deployment(root, self.work / 'units', config, {'runtime': spec['pins']['runtime']},
                               cook, systemctl, operator_files=collect(config))
            prepared = first.prepare()
            if staging:
                result = first.activate()
                self.assertEqual(result['activation'], 'install_only')
                self.assertEqual(result['unit_state'], 'inactive')
                self.assertFalse((first.state_dir / 'file-seen').exists())
                self.assertFalse((first.unit_dir / 'default.target.wants').exists())
                calls = [json.loads(line) for line in (root / 'systemctl-calls').read_text().splitlines()]
                self.assertFalse(any(call[0] in ('enable', 'start', 'restart', 'stop') for call in calls))
                recorded = {name: ((first.build / name).read_bytes(), (first.build / name).stat().st_mtime_ns)
                            for name in ('.prepared', 'deployment.json', 'connectome.lock')}
                config['activation'] = 'start'
                promoted = Deployment(root, self.work / 'units', config, {'runtime': spec['pins']['runtime']},
                                      cook, systemctl, operator_files=collect(config))
                self.assertEqual(promoted.build, first.build)
                self.assertFalse(promoted.prepare()['changed'])
                (root.parent / 'forbid-unit-activation').unlink()
                self.assertTrue(promoted.activate()['changed'])
                self.assertTrue((promoted.unit_dir / 'default.target.wants/connectome-example.service').is_symlink())
                self.assertEqual((promoted.state_dir / 'file-seen').read_text(), 'ok')
                for name, before in recorded.items():
                    self.assertEqual(((promoted.build / name).read_bytes(),
                                      (promoted.build / name).stat().st_mtime_ns), before)
                return
            first.activate()
            self.assertEqual((first.state_dir / 'file-seen').read_text(), 'ok')
            if external:
                self.assertEqual(first._lock()['external'], [{'name': 'alpha', 'command': command}])
                self.assertEqual(first._lock()['components'], [])
                # Cook records any absolute command; the role refuses an
                # account-owned one after the build, before activation.
                unsafe_config = deepcopy(config)
                unsafe_content = content.replace(b'"command": ' + json.dumps(command).encode(),
                                                 b'"command": ' + json.dumps(str(unsafe)).encode())
                recipe.write_bytes(unsafe_content)
                unsafe_config['recipe_file']['sha256'] = hashlib.sha256(unsafe_content).hexdigest()
                second = Deployment(root, self.work / 'units', unsafe_config, {'runtime': spec['pins']['runtime']},
                                    cook, systemctl, operator_files=collect(unsafe_config))
                with self.assertRaises(DeployError) as error:
                    second.prepare()
                self.assertEqual(error.exception.code, 'E_EXTERNAL_COMMAND')
                self.assertEqual(json.loads((second.build / 'connectome.lock').read_text())['external'],
                                 [{'name': 'alpha', 'command': str(unsafe)}])
                self.assertFalse((second.build / '.prepared').exists())
                self.assertEqual(first.current.resolve(), first.build)
                # connector_state naming an external server never reaches cook.
                state_config = deepcopy(config)
                state_config['cook']['connector_state'] = {'alpha': {'DATA_DIR': str(self.work / 'alpha')}}
                recipe.write_bytes(content)
                third = Deployment(root, self.work / 'units', state_config, {'runtime': spec['pins']['runtime']},
                                   cook, systemctl, operator_files=collect(state_config))
                with self.assertRaises(DeployError) as error:
                    third.prepare()
                self.assertEqual(error.exception.code, 'E_ADAPTER_INPUT')
                self.assertFalse((third.build / 'connectome.lock').exists())
                return
            self.assertEqual((first.build / 'cook-recipe.json').read_bytes(), content)
            self.assertEqual(first._lock()['recipe']['sha256'], config['recipe_file']['sha256'])
            source_unit = (first.build / 'connectome-example.service').read_text()
            self.assertIn('ExecStart=/bin/bash "' + str(first.build / 'run.sh') + '" --headless', source_unit)
            self.assertFalse(first.activate()['changed'])
            for output in (prepared, first.plan()):
                self.assertNotIn(canary, json.dumps(output))
            for path in [*first.build.rglob('*'), first.manifest_path, root / 'systemctl-calls']:
                if path.is_file():
                    self.assertNotIn(canary.encode(), path.read_bytes(), str(path))
            self.assertEqual(first._secret_file_path('EXAMPLE_FILE').read_text(), canary)
            second_config = deepcopy(config)
            second_config['secret_files'][0]['env'] = 'OTHER_FILE'
            content2 = content.replace(b'EXAMPLE_FILE', b'OTHER_FILE')
            recipe.write_bytes(content2)
            second_config['recipe_file']['sha256'] = hashlib.sha256(content2).hexdigest()
            second = Deployment(root, self.work / 'units', second_config, {'runtime': spec['pins']['runtime']},
                                cook, systemctl, operator_files=collect(second_config))
            second.prepare()
            recipe.unlink()  # Rollback must not read the old controller recipe.
            with self.assertRaisesRegex(DeployError, 'restored') as error:
                second.activate()
            self.assertNotIn(canary, str(error.exception))
            self.assertEqual(first.current.resolve(), first.build)
            self.assertEqual(first._secret_file_path('EXAMPLE_FILE').read_text(), canary)
            self.assertFalse(second._secret_file_path('OTHER_FILE').exists())
            self.assertEqual((first.state_dir / 'file-seen').read_text(), 'ok')
