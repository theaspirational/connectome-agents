"""Host-installed MCP servers (cook source.external): lock schema and ownership."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import test_cook
import test_deploy
from test_cook import fixture, adapter
from connectome_lock import read_lock, LockError
import connectome_inputs as inputs
from connectome_deploy import (DeployError, check_external_command, check_external_data_arg,
                               external_paths)

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / 'roles/connectome_agent/module_utils'
EXTERNAL = [{'name': 'alpha', 'command': '/opt/alpha/bin/server'},
            {'name': 'beta_2', 'command': '/usr/local/bin/beta'}]


def system_command():
    """A root-maintained executable reached through the real filesystem, if any."""
    for candidate in ('/bin/sh', '/usr/bin/env', '/bin/true'):
        try:
            check_external_command('probe', candidate)
            return candidate
        except DeployError:
            continue
    return None


def fakeroot_base():
    """A directory whose ancestors fakeroot can present as root-owned and usable.

    Ancestors must not be group/other-writable and must grant read/traverse to
    other or to group (the fakeroot case then assigns them the agent's group).
    """
    for candidate in (os.environ.get('TMPDIR'), str(Path.home())):
        if candidate and Path(candidate).is_dir():
            path = Path(candidate).resolve()
            if all(not mode & 0o022 and (mode & 0o005 == 0o005 or mode & 0o050 == 0o050)
                   for mode in (p.stat().st_mode for p in (path, *path.parents))):
                return path
    return None


class ExternalLockTests(test_cook.ScratchTest):
    record = test_cook.LockTests.record

    def test_without_external_key_is_unchanged(self):
        lock = read_lock(self.record())
        self.assertNotIn('external', lock)

    def test_external_list_accepted_and_exposed(self):
        record = fixture()
        record['external'] = deepcopy(EXTERNAL)
        self.assertEqual(read_lock(self.record(record))['external'], EXTERNAL)

    def test_malformed_external_entries_refused(self):
        def entry(**change):
            return [dict(EXTERNAL[0], **change)]
        cases = {
            'empty list': [], 'mapping': {'alpha': '/bin/true'}, 'null': None, 'string': 'alpha',
            'commit': entry(commit='e' * 40), 'args': entry(args=['/x.js']), 'env': entry(env={}),
            'source': entry(source={'external': True}), 'no command': [{'name': 'alpha'}],
            'no name': [{'command': '/bin/true'}], 'not a mapping': [['alpha', '/bin/true']],
            'relative command': entry(command='bin/server'), 'bare command': entry(command='node'),
            'variable command': entry(command='${HOME}/bin/server'), 'dotdot': entry(command='/opt/../bin/x'),
            'non-normal': entry(command='/opt//bin/x'), 'trailing slash': entry(command='/opt/bin/'),
            'dot': entry(command='/opt/./x'), 'root': entry(command='/'), 'control': entry(command='/opt/x\n'),
            'numeric command': entry(command=1), 'empty name': entry(name=''), 'space name': entry(name='a b'),
            'path name': entry(name='a/b'), 'option name': entry(name='-a'), 'numeric name': entry(name=7),
            'duplicate': [EXTERNAL[0], dict(EXTERNAL[0], command='/opt/other')],
            'unsorted': list(reversed(EXTERNAL)),
            'credential': entry(command='/opt/ghp_' + 'A' * 36),
        }
        for label, value in cases.items():
            record = fixture()
            record['external'] = value
            with self.subTest(label=label), self.assertRaises(LockError):
                read_lock(self.record(record))

    def test_lock_connector_state_must_not_name_external(self):
        record = fixture()
        record['external'] = deepcopy(EXTERNAL)
        record['layout'] = {'schema': 'connectome.host-layout/v1', 'runtime': '/tmp/runtime',
                            'config': '/tmp/config', 'credentials': '/tmp/credentials', 'state': '/tmp/state',
                            'workspace': '/tmp/workspace', 'connectorState': {'other': {'DATA_DIR': '/tmp/data'}}}
        read_lock(self.record(record))
        record['layout']['connectorState']['alpha'] = {'DATA_DIR': '/tmp/data'}
        with self.assertRaisesRegex(LockError, 'external'):
            read_lock(self.record(record))


class ExternalAdapterTests(test_cook.ScratchTest):
    spec = test_cook.AdapterTests.spec

    def operator_spec(self, servers, connector_state=None):
        spec = self.spec()
        content = json.dumps({'name': 'example', 'agent': {'systemPrompt': 'Verbatim'},
                              'mcpServers': servers}).encode()
        path = self.work / 'operator.json'; path.write_bytes(content)
        config = spec['config']
        config.pop('agent')
        config['connectors'] = {}
        config['recipe_file'] = {'path': str(path), 'sha256': hashlib.sha256(content).hexdigest()}
        config['cook']['connector_state'] = connector_state or {}
        spec['pins']['connectors'] = {}
        return spec

    def adapt(self, spec, external, components=()):
        spec_path = self.build / 'deployment.json'; spec_path.write_text(json.dumps(spec))
        calls = []
        def materialize(argv, cwd, env, flag_indices):
            calls.append(argv)
            layout = json.loads((self.build / 'cook-layout.json').read_text())
            record = fixture(); record['build']['dir'] = str(self.build)
            recipe = self.build / 'cook-recipe.json'
            record['recipe'] = {'path': str(recipe), 'sha256': hashlib.sha256(recipe.read_bytes()).hexdigest()}
            record['recipePath'] = str(recipe)
            record['launch']['script'] = str(self.build / 'run.sh')
            record['layout'] = layout; record['state'] = {'path': layout['state']}
            record['hostDependencies'] = [{'name': adapter.FRAMEWORK,
                'sha256': spec['config']['cook']['framework_archive']['sha256'],
                'originalRange': '^1.0.0', 'version': '1.0.0', 'size': 123, 'snapshot': '/tmp/example.tgz',
                'packageJsonDelta': {'direct': {'section': 'dependencies', 'name': adapter.FRAMEWORK,
                     'before': '^1.0.0', 'after': 'file:/tmp/example.tgz'},
                    'override': {'section': 'overrides', 'name': adapter.FRAMEWORK,
                     'before': None, 'after': 'file:/tmp/example.tgz'}}}]
            record['components'] = list(components)
            if external:
                record['external'] = external
            (self.build / 'run.sh').write_text('#!/bin/sh\nexit 0\n')
            (self.build / 'connectome.lock').write_text(json.dumps(record))
        with patch.object(adapter, 'verify_cook'), patch.object(adapter, 'command', side_effect=materialize):
            try:
                return adapter.adapt(str(spec_path), str(self.build), str(self.work / 'state'),
                                     sys.executable, '/tmp/example/bin/cook', sys.executable, sys.executable)
            finally:
                self.calls = calls

    def servers(self):
        return {'alpha': {'command': '/opt/alpha/bin/server', 'args': ['/opt/alpha/index.js'],
                          'source': {'external': True}},
                'gamma': {'command': 'node', 'args': ['index.js'],
                          'source': {'url': 'https://example.invalid/gamma.git', 'ref': 'main'}}}

    def test_supplied_recipe_binds_recorded_external_servers(self):
        gamma = {'kind': 'git', 'key': 'gamma', 'role': 'mcp', 'url': 'https://example.invalid/gamma.git',
                 'ref': 'main', 'commit': 'e' * 40, 'path': '/tmp/gamma', 'install': ''}
        external = [{'name': 'alpha', 'command': '/opt/alpha/bin/server'}]
        lock = self.adapt(self.operator_spec(self.servers()), external, [gamma])
        self.assertEqual(lock['external'], external)
        self.assertEqual(lock['components'], [gamma])
        for label, recorded in {
                'missing': None, 'command differs': [dict(external[0], command='/opt/other')],
                'non-external server recorded': external + [{'name': 'gamma', 'command': '/opt/gamma'}],
                'unknown (fleet child) server': external + [{'name': 'zeta', 'command': '/opt/zeta'}]}.items():
            for path in self.build.iterdir():
                if path.name != 'deployment.json':
                    path.unlink()
            with self.subTest(label=label), self.assertRaisesRegex(adapter.AdapterError, 'external'):
                self.adapt(self.operator_spec(self.servers()), recorded, [gamma])

    def test_generated_recipe_refuses_recorded_external(self):
        spec = self.spec()
        spec['config']['connectors'] = {}
        spec['pins']['connectors'] = {}
        with self.assertRaisesRegex(adapter.AdapterError, 'component provenance'):
            self.adapt(spec, [{'name': 'alpha', 'command': '/opt/alpha/bin/server'}])

    def test_connector_state_naming_external_refused_before_cook(self):
        spec = self.operator_spec(self.servers(), {'alpha': {'DATA_DIR': '/tmp/alpha'}})
        with self.assertRaisesRegex(adapter.AdapterError, 'connector_state') as error:
            self.adapt(spec, None)
        self.assertEqual(error.exception.code, 'E_ADAPTER_INPUT')
        self.assertEqual(self.calls, [])
        # A non-external server may still own declared state.
        spec = self.operator_spec(self.servers(), {'gamma': {'DATA_DIR': '/tmp/gamma'}})
        lock = self.adapt(spec, [{'name': 'alpha', 'command': '/opt/alpha/bin/server'}])
        self.assertEqual(lock['layout']['connectorState'], {'gamma': {'DATA_DIR': '/tmp/gamma'}})
        self.assertEqual(len(self.calls), 1)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR', Path.home()))
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve()

    def test_real_root_owned_command_and_symlink_chain_accepted(self):
        command = system_command()
        if command is None:
            self.skipTest('no root-owned system executable with safe ancestors')
        self.assertEqual(check_external_command('probe', command), Path(os.path.realpath(command)))

    def test_real_refusals_have_typed_public_code_without_path(self):
        own = self.work / 'server'; own.write_text('#!/bin/sh\n'); own.chmod(0o755)
        for label, command in (('not root-owned', str(own)), ('missing', str(self.work / 'missing')),
                               ('directory', '/usr/bin'), ('root', '/')):
            with self.subTest(label=label), self.assertRaises(DeployError) as error:
                check_external_command('alpha', command)
            public = error.exception.public()
            self.assertEqual(public['error_code'], 'E_EXTERNAL_COMMAND')
            self.assertEqual(public['external_server'], 'alpha')
            if command != '/':
                self.assertNotIn(command, json.dumps(public))

    @unittest.skipUnless(shutil.which('fakeroot'), 'fakeroot unavailable')
    def test_fakeroot_owner_mode_ancestor_and_link_refusals(self):
        """Real lstat on real files; fakeroot only fakes uid 0 and chown."""
        base = fakeroot_base()
        if base is None:
            self.skipTest('TMPDIR/HOME ancestors are writable, or deny the agent read/traverse')
        tmp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name).resolve()
        script = r'''
import json, os, sys
from pathlib import Path
from connectome_deploy import DeployError, check_external_command
base = Path(sys.argv[1])
account = (int(sys.argv[2]), set(json.loads(sys.argv[3])))
OTHER_GROUP = 4321
def tree(label, file_mode=0o755, dir_mode=0o755):
    directory = base / label / 'bin'
    directory.mkdir(parents=True)
    (base / label).chmod(0o755)
    target = directory / 'server'
    target.write_text('#!/bin/sh\n')
    target.chmod(file_mode)
    directory.chmod(dir_mode)
    return directory, target
os.chmod(base, 0o755)
# A private TMPDIR's own ancestors may deny "other" (for example a 0750
# HOME). Within fakeroot only, give them the agent's group; real files keep
# their owner. Every created case below is still checked with real modes.
for parent in base.parents:
    if parent.stat().st_mode & 0o005 != 0o005:
        os.chown(parent, 0, min(account[1]))
cases = {}
_, cases['ok'] = tree('ok')
directory, target = tree('relative-link')
(directory / 'link').symlink_to('../bin/server'); cases['relative-link'] = directory / 'link'
directory, target = tree('absolute-link')
(directory / 'link').symlink_to(target); cases['absolute-link'] = directory / 'link'
_, cases['other-owner'] = tree('other-owner'); os.chown(cases['other-owner'], 1234, 0)
_, cases['group-writable'] = tree('group-writable', 0o775)
_, cases['other-writable'] = tree('other-writable', 0o757)
_, cases['writable-parent'] = tree('writable-parent', dir_mode=0o777)
directory, cases['other-parent'] = tree('other-parent'); os.chown(directory, 1234, 0)
directory, unsafe = tree('unsafe-target', dir_mode=0o777)
safe, _ = tree('link-to-unsafe')
(safe / 'link').symlink_to(unsafe); cases['link-to-unsafe'] = safe / 'link'
safe, target = tree('foreign-link')
(safe / 'link').symlink_to(target); os.chown(safe / 'link', 1234, 0, follow_symlinks=False)
cases['foreign-link'] = safe / 'link'
safe, _ = tree('dangling-link')
(safe / 'link').symlink_to(safe / 'missing'); cases['dangling-link'] = safe / 'link'
safe, _ = tree('directory-link')
(safe / 'link').symlink_to(safe); cases['directory-link'] = safe / 'link'
safe, _ = tree('loop')
(safe / 'link').symlink_to(safe / 'link'); cases['loop'] = safe / 'link'
# root:<other group> 0750 passes ownership/writability but not agent access.
directory, cases['group-only-dir'] = tree('group-only-dir', dir_mode=0o750); os.chown(directory, 0, OTHER_GROUP)
_, cases['group-only-file'] = tree('group-only-file', 0o750); os.chown(cases['group-only-file'], 0, OTHER_GROUP)
_, cases['not-readable'] = tree('not-readable', 0o711)
_, cases['not-executable'] = tree('not-executable', 0o644)
directory, cases['member-group-dir'] = tree('member-group-dir', dir_mode=0o750)
os.chown(directory, 0, min(account[1]))
args = {}
_, args['arg-file'] = tree('arg-file', 0o644)
args['arg-dir'], _ = tree('arg-dir')
_, args['arg-group-only'] = tree('arg-group-only', 0o640); os.chown(args['arg-group-only'], 0, OTHER_GROUP)
_, args['arg-other-owner'] = tree('arg-other-owner', 0o644); os.chown(args['arg-other-owner'], 1234, 0)
directory, args['arg-hidden-dir'] = tree('arg-hidden-dir', 0o644, 0o750); os.chown(directory, 0, OTHER_GROUP)
result = {}
for label, path, arg in [*((k, v, False) for k, v in cases.items()), *((k, v, True) for k, v in args.items())]:
    try:
        result[label] = ['ok', str(check_external_command(label, str(path), arg, account))]
    except DeployError as error:
        result[label] = [error.public()['error_code'], str(error)]
print(json.dumps(result))
'''
        # fakeroot also fakes the process uid; pass the real agent account.
        groups = sorted({os.getegid(), *os.getgroups()})
        self.assertNotIn(4321, groups)
        output = subprocess.run(['fakeroot', sys.executable, '-c', script, str(self.work),
                                 str(os.geteuid()), json.dumps(groups)],
                                env=dict(os.environ, PYTHONPATH=str(MODULES)), cwd=self.work,
                                stdout=subprocess.PIPE, check=True).stdout
        result = json.loads(output)
        self.assertEqual(result.pop('ok'), ['ok', str(self.work / 'ok/bin/server')])
        self.assertEqual(result.pop('relative-link'), ['ok', str(self.work / 'relative-link/bin/server')])
        self.assertEqual(result.pop('absolute-link'), ['ok', str(self.work / 'absolute-link/bin/server')])
        self.assertEqual(result.pop('member-group-dir'), ['ok', str(self.work / 'member-group-dir/bin/server')])
        self.assertEqual(result.pop('arg-file'), ['ok', str(self.work / 'arg-file/bin/server')])
        self.assertEqual(result.pop('arg-dir'), ['ok', str(self.work / 'arg-dir/bin')])
        access = {label: ('E_EXTERNAL_ACCESS', 'agent account') for label in (
            'group-only-dir', 'group-only-file', 'not-readable', 'not-executable',
            'arg-group-only', 'arg-hidden-dir')}
        reasons = {'other-owner': 'not root-owned', 'group-writable': 'group/other-writable',
                   'other-writable': 'group/other-writable', 'writable-parent': 'group/other-writable',
                   'other-parent': 'not root-owned', 'link-to-unsafe': 'group/other-writable',
                   'foreign-link': 'not root-owned', 'dangling-link': 'missing',
                   'directory-link': 'not a regular file', 'loop': 'too many symbolic links',
                   'arg-other-owner': 'not root-owned'}
        expected = {label: ('E_EXTERNAL_COMMAND', reason) for label, reason in reasons.items()} | access
        self.assertEqual(set(result), set(expected))
        for label, (code, reason) in expected.items():
            with self.subTest(label=label):
                self.assertEqual(result[label][0], code)
                self.assertIn(reason, result[label][1])


class ExternalDeploymentTests(unittest.TestCase):
    setUp = test_deploy.DeployTests.setUp
    deployment = test_deploy.DeployTests.deployment

    def recipe(self, command, args=()):
        server = {'command': command, 'args': list(args), 'source': {'external': True}}
        content = json.dumps({'name': 'example', 'agent': {'systemPrompt': 'Verbatim'},
                              'mcpServers': {'alpha': server}}).encode()
        path = Path(self.tmp.name) / 'operator.json'
        path.write_bytes(content)
        self.config['recipe_file'] = {'path': str(path), 'sha256': hashlib.sha256(content).hexdigest()}
        self.versions.pop('connectors', None)

    def test_unsafe_external_command_refused_before_activation(self):
        own = Path(self.tmp.name) / 'server'; own.write_text('#!/bin/sh\n'); own.chmod(0o755)
        self.recipe(str(own))
        dep = self.deployment()
        with self.assertRaises(DeployError) as error:
            dep.prepare()
        self.assertEqual(error.exception.code, 'E_EXTERNAL_COMMAND')
        self.assertEqual(error.exception.public()['external_server'], 'alpha')
        self.assertFalse((dep.build / '.prepared').exists())
        self.assertFalse(dep.current.is_symlink())
        self.assertFalse((self.units / 'connectome-example.service').exists())

    def test_unsafe_or_relative_path_argument_refused_before_activation(self):
        command = system_command()
        if command is None:
            self.skipTest('no root-owned system executable with safe ancestors')
        own = Path(self.tmp.name) / 'index.js'; own.write_text('\n')
        for args, code in ((['--script=' + str(own)], 'E_EXTERNAL_COMMAND'), ([str(own)], 'E_EXTERNAL_COMMAND'),
                           (['index.js'], 'E_EXTERNAL_COMMAND'), (['./server'], 'E_EXTERNAL_COMMAND')):
            self.recipe(command, args)
            dep = self.deployment()
            with self.subTest(args=args), self.assertRaises(DeployError) as error:
                dep.prepare()
            self.assertEqual(error.exception.code, code)
            self.assertFalse((dep.build / '.prepared').exists())
            self.assertFalse(dep.current.is_symlink())

    def test_root_owned_external_command_activates_and_is_rechecked(self):
        command = system_command()
        if command is None:
            self.skipTest('no root-owned system executable with safe ancestors')
        directory = str(Path(os.path.realpath(command)).parent)
        # Flags without a value, words and URLs are not paths; directories may be.
        self.recipe(command, ['--verbose', 'serve', 'https://example.invalid/a/b', '--root=' + directory])
        dep = self.deployment()
        dep.prepare()
        self.assertEqual(dep._lock()['external'], [{'name': 'alpha', 'command': command}])
        with patch('connectome_deploy.check_external_command') as check:
            dep.activate({})
        self.assertEqual(check.call_args_list, [((('alpha', command, False)),), ((('alpha', directory, True)),)])
        self.assertEqual(dep.current.resolve(), dep.build)
        # The recorded command stays bound to the installed recipe.
        lock = dep.build / 'connectome.lock'
        data = json.loads(lock.read_text())
        data['external'][0]['command'] = str(Path(self.tmp.name) / 'missing-server')
        lock.write_text(json.dumps(data))
        with self.assertRaises(DeployError) as error:
            dep.activate({})
        self.assertEqual(error.exception.code, 'E_LOCK_INVALID')
        self.assertEqual(dep.current.resolve(), dep.build)


class ExternalDataArgTests(unittest.TestCase):
    """Real private files, with targeted metadata changes for foreign uid cases."""
    def setUp(self):
        self.home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
        self.tmp = tempfile.TemporaryDirectory(dir=self.home)
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.path = self.directory / 'registry.json'
        self.path.write_text('synthetic-private-data-canary')
        self.path.chmod(0o600)
        self.entry = {'server': 'alpha', 'path': str(self.path), 'kind': 'file'}

    def check(self, path=None, kind='file'):
        return check_external_data_arg('alpha', str(path or self.path), kind=kind)

    def assert_data_error(self, call):
        with self.assertRaises(DeployError) as error:
            call()
        self.assertEqual(error.exception.code, 'E_EXTERNAL_DATA_ARG')
        public = json.dumps(error.exception.public())
        self.assertNotIn(str(self.path), public)
        self.assertNotIn('synthetic-private-data-canary', public)

    def metadata(self, target, **changes):
        original = Path.lstat
        def changed(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == target:
                return SimpleNamespace(**dict({'st_uid': info.st_uid, 'st_gid': info.st_gid,
                                               'st_mode': info.st_mode}, **changes))
            return info
        return patch.object(Path, 'lstat', changed)

    def test_real_private_file_accepted_without_reading_content(self):
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('data was read')), \
                patch.object(Path, 'read_text', side_effect=AssertionError('data was read')):
            self.assertEqual(self.check(), self.path)

    def test_file_kind_mode_and_missing_refusals(self):
        for mode in (0o644, 0o700, 0o660, 0o1600, 0o4600):
            self.path.chmod(mode)
            with self.subTest(mode=oct(mode)):
                self.assert_data_error(self.check)
        self.path.unlink()
        self.assert_data_error(self.check)
        self.path.mkdir(mode=0o700)
        self.assert_data_error(self.check)
        self.path.rmdir()
        self.path.symlink_to('/etc/passwd')
        self.assert_data_error(self.check)
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        self.assert_data_error(self.check)

    def test_private_directory_kind_mode_owner_and_symlink_checks(self):
        self.assert_data_error(lambda: self.check(kind='dir'))
        self.path.unlink()
        self.path.mkdir(mode=0o700)
        self.assertEqual(self.check(kind='dir'), self.path)
        self.assert_data_error(self.check)
        for mode in (0o600, 0o750, 0o770, 0o1700, 0o4700):
            self.path.chmod(mode)
            with self.subTest(mode=oct(mode)):
                self.assert_data_error(lambda: self.check(kind='dir'))
        self.path.chmod(0o700)
        with self.metadata(self.path, st_uid=54321):
            self.assert_data_error(lambda: self.check(kind='dir'))
        self.path.rmdir()
        self.path.symlink_to(self.directory, target_is_directory=True)
        self.assert_data_error(lambda: self.check(kind='dir'))

    def test_foreign_uid_and_root_owned_fallback(self):
        with self.metadata(self.path, st_uid=54321):
            self.assert_data_error(self.check)
        with self.metadata(self.path, st_uid=0), \
                patch('connectome_deploy.check_external_command', return_value=self.path) as normal:
            self.assertEqual(self.check(), self.path)
            normal.assert_called_once_with('alpha', str(self.path), True, None)
        # In reality the agent-owned home prevents a root-owned leaf from
        # passing the unchanged normal rule; declaring it cannot rescue it.
        with self.metadata(self.path, st_uid=0), self.assertRaises(DeployError) as error:
            self.check()
        self.assertEqual(error.exception.code, 'E_EXTERNAL_COMMAND')

    def test_ancestor_owner_mode_symlink_and_access_refusals(self):
        for mode in (0o770, 0o707):
            self.directory.chmod(mode)
            self.assert_data_error(self.check)
        self.directory.chmod(0o700)
        with self.metadata(self.directory, st_uid=54321):
            self.assert_data_error(self.check)
        with self.metadata(self.home.parent, st_uid=os.geteuid()):
            self.assert_data_error(self.check)
        for parent in (self.directory, self.home.parent):
            with self.metadata(parent, st_mode=stat.S_IFDIR | 0o777):
                self.assert_data_error(self.check)
        alias = self.directory / 'alias'
        alias.symlink_to(self.directory, target_is_directory=True)
        self.assert_data_error(lambda: self.check(alias / self.path.name))
        for parent, mode in ((self.directory, 0o300), (self.home.parent, 0o711)):
            with self.metadata(parent, st_mode=stat.S_IFDIR | mode), \
                    self.assertRaises(DeployError) as error:
                self.check()
            self.assertEqual(error.exception.code, 'E_EXTERNAL_ACCESS')
        with self.metadata(self.directory, st_uid=0, st_mode=stat.S_IFDIR | 0o755):
            self.assertEqual(self.check(), self.path)

    def test_outside_home_and_lexical_alias_refusals(self):
        self.assert_data_error(lambda: check_external_data_arg(
            'alpha', str(self.path), home=self.directory / 'different-home'))
        for path in ('relative.json', str(self.directory) + '/../registry.json',
                     str(self.directory) + '//registry.json', str(self.path) + '/',
                     '/' + str(self.path), str(self.directory) + '/./registry.json'):
            with self.subTest(path=path):
                self.assert_data_error(lambda: self.check(path))

    def test_closed_input_schema(self):
        inputs.validate({'external_data_args': [self.entry]})
        for records in (None, {}, '', [None], [{}], [dict(self.entry, extra=True)],
                        [dict(self.entry, server='bad/name')], [dict(self.entry, server=1)],
                        [dict(self.entry, path=1)], [dict(self.entry, path='relative')],
                        [dict(self.entry, path='/a/../b')], [dict(self.entry, path='/a//b')],
                        [dict(self.entry, path='/a/')], [dict(self.entry, path='//a/b')],
                        [dict(self.entry, path='/a\n')], [self.entry] * 2,
                        [{'server': 'alpha', 'path': str(self.path)}],
                        [dict(self.entry, kind=None)], [dict(self.entry, kind='directory')],
                        [dict(self.entry, kind=[])], [dict(self.entry, flag=None)],
                        [dict(self.entry, flag='')], [dict(self.entry, flag='--')],
                        [dict(self.entry, flag='--state=')], [dict(self.entry, flag='state')],
                        [dict(self.entry, flag='--state other')], [dict(self.entry, flag=1)]):
            with self.subTest(records=records), self.assertRaises(inputs.ExternalDataArgError):
                inputs.validate({'external_data_args': records})

    def test_binding_is_raw_argv_exact_server_and_path(self):
        command = '/usr/bin/node'
        script = system_command()
        if script is None:
            self.skipTest('no safe root-owned script path')
        recipe = {'mcpServers': {
            'alpha': {'command': command, 'args': [script, str(self.path)], 'source': {'external': True}},
            'beta': {'command': command, 'args': [script, str(self.path)], 'source': {'external': True}},
            'gamma': {'command': command, 'args': [str(self.path)], 'source': {'npm': 'example'}}}}
        lock = {'external': [{'name': name, 'command': command} for name in ('alpha', 'beta')]}
        paths = external_paths(lock, recipe, [self.entry])
        self.assertEqual(paths, [('alpha', command, False, False),
                                ('alpha', script, True, False),
                                ('alpha', str(self.path), True, True),
                                ('beta', command, False, False),
                                ('beta', script, True, False),
                                ('beta', str(self.path), True, False)])
        for entry in (dict(self.entry, server='unknown'), dict(self.entry, server='gamma'),
                      dict(self.entry, path=command), dict(self.entry, path=str(self.path)[:-1]),
                      dict(self.entry, path=str(self.path) + '*'),
                      dict(self.entry, path=str(self.path) + '.stale')):
            with self.subTest(entry=entry):
                self.assert_data_error(lambda: external_paths(lock, recipe, [entry]))
        recipe['mcpServers']['alpha']['args'] = [script, '--config=' + str(self.path)]
        self.assert_data_error(lambda: external_paths(lock, recipe, [self.entry]))
        # Even when the same path also occurs as bare argv, a flag value is
        # still a different arg and must use the root-owned check.
        recipe['mcpServers']['alpha']['args'].extend(['word', str(self.path)])
        paths = external_paths(lock, recipe, [self.entry])
        self.assertEqual([row[3] for row in paths[:4]], [False, False, False, True])

    def test_declared_data_flag_exact_binding_and_code_flag_deny_set(self):
        command = system_command()
        if command is None:
            self.skipTest('no safe root-owned script path')
        server = {'command': command, 'args': [], 'source': {'external': True}}
        recipe = {'mcpServers': {'alpha': server}}
        lock = {'external': [{'name': 'alpha', 'command': command}]}
        for kind, path in (('file', self.path), ('dir', self.directory)):
            entry = dict(self.entry, path=str(path), kind=kind, flag='--state')
            inputs.validate_external_data_args([entry])
            for args in ([command, '--state', str(path)], [command, '--state=' + str(path)]):
                server['args'] = args
                with self.subTest(kind=kind, args=args):
                    self.assertEqual(external_paths(lock, recipe, [entry])[-1],
                                     ('alpha', str(path), True, True))
                    self.assertEqual(self.check(path, kind), path)
            for args in ([command, str(path)], [command, '--other', str(path)],
                         [command, '--State', str(path)], [command, '--state', 'word', str(path)],
                         ['--state', str(path)], ['--state=' + str(path)],
                         ['word', '--state', str(path)], [command, '--other=' + str(path)]):
                server['args'] = args
                with self.subTest(kind=kind, args=args):
                    self.assert_data_error(lambda: external_paths(lock, recipe, [entry]))
        # Deny comparisons are case-sensitive, but binding remains exact.
        for flag in ('--State', '--Require', '--IMPORT', '--Loader'):
            entry = dict(self.entry, flag=flag)
            server['args'] = [command, flag, str(self.path)]
            self.assertTrue(external_paths(lock, recipe, [entry])[-1][3])
        denied = ('-e', '--eval', '-p', '--print', '-c', '-r', '--require', '--import',
                  '--loader', '--experimental-loader', '-x', '-m', '--require-extra',
                  '--requireAnything', '--import-extra', '--importAnything',
                  '--loader-extra', '--loaderAnything', '--experimental-loader-extra')
        for flag in denied:
            entry = dict(self.entry, flag=flag)
            with self.subTest(flag=flag), self.assertRaises(inputs.ExternalDataArgError):
                inputs.validate_external_data_args([entry])
            for args in ([command, flag, str(self.path)], [command, flag + '=' + str(self.path)]):
                server['args'] = args
                with self.subTest(args=args):
                    self.assert_data_error(lambda: external_paths(lock, recipe, [entry]))

    def test_node_data_requires_root_owned_positional_script(self):
        command = '/usr/bin/node'
        script = '/opt/example/rootscript.js'
        server = {'command': command, 'args': [script, str(self.path)],
                  'source': {'external': True}}
        recipe = {'mcpServers': {'alpha': server}}
        lock = {'external': [{'name': 'alpha', 'command': command}]}
        # Only the script's metadata check is doubled; the private file is real.
        with patch('connectome_deploy.check_external_command', return_value=Path(script)) as check:
            paths = external_paths(lock, recipe, [self.entry])
        check.assert_called_once_with('alpha', script, True)
        self.assertEqual(paths[-1], ('alpha', str(self.path), True, True))
        self.assertEqual(self.check(), self.path)
        cases = [[str(self.path)], ['word', str(self.path)],
                 ['--script=' + script, 'word', str(self.path)],
                 [str(self.path), script]]
        for flag in ('-e', '-c', '--require', '-r', '--import', '--loader', '-x', '--config'):
            cases.extend(([flag, str(self.path)], [script, flag, str(self.path)]))
        with patch('connectome_deploy.check_external_command', return_value=Path(script)):
            for args in cases:
                server['args'] = args
                with self.subTest(args=args):
                    self.assert_data_error(lambda: external_paths(lock, recipe, [self.entry]))
        server['args'] = [script, str(self.path)]
        with patch('connectome_deploy.check_external_command',
                   side_effect=DeployError('not root-owned', 'E_EXTERNAL_COMMAND')):
            with self.assertRaises(DeployError) as error:
                external_paths(lock, recipe, [self.entry])
        self.assertEqual(error.exception.code, 'E_EXTERNAL_COMMAND')


class ExternalDataDeploymentTests(unittest.TestCase):
    setUp = test_deploy.DeployTests.setUp
    deployment = test_deploy.DeployTests.deployment
    recipe = ExternalDeploymentTests.recipe

    def data_config(self):
        self.command = system_command()
        if self.command is None:
            self.skipTest('no safe root-owned executable')
        self.data = Path(self.tmp.name) / 'registry.json'
        self.data.write_text('synthetic-private-data-canary')
        self.data.chmod(0o600)
        self.config['external_data_args'] = [{'server': 'alpha', 'path': str(self.data), 'kind': 'file'}]
        self.recipe(self.command, [self.command, str(self.data)])

    def test_positive_lock_plan_status_and_recheck(self):
        self.data_config()
        dep = self.deployment()
        result = dep.prepare()
        self.assertEqual(result['plan']['external_data_args'], self.config['external_data_args'])
        self.assertEqual(dep._lock()['external_data_args'], self.config['external_data_args'])
        recorded = json.loads((dep.build / 'deployment.json').read_text())
        self.assertEqual(recorded['config']['external_data_args'], self.config['external_data_args'])
        status = dep.activate({})
        self.assertEqual(status['external_data_args'], self.config['external_data_args'])
        self.assertNotIn('synthetic-private-data-canary', json.dumps([result, status, dep._lock()]))
        for mode in (0o644, 0o700):
            self.data.chmod(mode)
            for operation in (dep.prepare, lambda: dep.activate({})):
                with self.subTest(mode=oct(mode)):
                    with self.assertRaises(DeployError) as error:
                        operation()
                    self.assertEqual(error.exception.code, 'E_EXTERNAL_DATA_ARG')
        self.assertEqual(dep.current.resolve(), dep.build)

    def test_opt_in_does_not_exempt_command_or_other_args(self):
        self.data_config()
        self.recipe(str(self.data), [str(self.data)])
        with self.assertRaises(DeployError) as error:
            self.deployment().prepare()
        self.assertEqual(error.exception.code, 'E_EXTERNAL_DATA_ARG')
        other = self.data.with_name(self.data.name + '.js'); other.write_text('synthetic')
        other.chmod(0o600)
        self.recipe(self.command, [self.command, str(self.data), str(other)])
        with self.assertRaises(DeployError) as error:
            self.deployment().prepare()
        self.assertEqual(error.exception.code, 'E_EXTERNAL_COMMAND')
        self.recipe(self.command, [self.command, str(self.data), '--config=' + str(self.data)])
        with self.assertRaises(DeployError) as error:
            self.deployment().prepare()
        self.assertEqual(error.exception.code, 'E_EXTERNAL_COMMAND')

    def test_python_state_directory_flag_prepare_activate_and_recheck(self):
        self.data_config()
        command, script = '/usr/bin/python3', os.__file__
        try:
            check_external_command('alpha', command)
            check_external_command('alpha', script, True)
        except DeployError:
            self.skipTest('no safe root-owned Python and script')
        self.data.unlink()
        self.data.mkdir(mode=0o700)
        self.config['external_data_args'][0].update(kind='dir', flag='--state')
        for args in ([script, '--state', str(self.data)], [script, '--state=' + str(self.data)]):
            self.recipe(command, args)
            dep = self.deployment()
            result = dep.prepare()
            status = dep.activate({})
            self.assertEqual(result['plan']['external_data_args'], self.config['external_data_args'])
            self.assertEqual(status['external_data_args'], self.config['external_data_args'])
            self.assertEqual(dep.current.resolve(), dep.build)
            self.data.chmod(0o750)
            for operation in (dep.prepare, lambda: dep.activate({})):
                with self.assertRaises(DeployError) as error:
                    operation()
                self.assertEqual(error.exception.code, 'E_EXTERNAL_DATA_ARG')
            self.data.chmod(0o700)
        self.config['external_data_args'][0]['kind'] = 'file'
        with self.assertRaises(DeployError) as error:
            self.deployment().prepare()
        self.assertEqual(error.exception.code, 'E_EXTERNAL_DATA_ARG')
        self.config['external_data_args'][0].update(kind='dir', flag='--require')
        self.recipe(command, [script, '--require', str(self.data)])
        with self.assertRaises(DeployError) as error:
            self.deployment()
        self.assertEqual(error.exception.code, 'E_EXTERNAL_DATA_ARG')
        self.config['external_data_args'][0]['flag'] = '--state'
        self.recipe(command, [script, str(self.data)])
        with self.assertRaises(DeployError) as error:
            self.deployment().prepare()
        self.assertEqual(error.exception.code, 'E_EXTERNAL_DATA_ARG')

    def test_dash_private_script_probe_refused(self):
        self.data_config()
        command = '/usr/bin/dash'
        if not Path(command).is_file():
            self.skipTest('dash unavailable')
        check_external_command('alpha', command)
        marker = self.data.with_name('executed')
        self.data.write_text('printf executed > "' + str(marker) + '"\n')
        # 0600 is readable by the interpreter and does not stop code execution.
        subprocess.run([command, str(self.data)], check=True)
        self.assertEqual(marker.read_text(), 'executed')
        marker.unlink()
        for args in ([str(self.data)], ['-s', str(self.data)], ['-e', str(self.data)]):
            self.recipe(command, args)
            dep = self.deployment()
            with self.subTest(args=args), self.assertRaises(DeployError) as error:
                dep.prepare()
            self.assertEqual(error.exception.code, 'E_EXTERNAL_DATA_ARG')
            self.assertFalse((dep.build / '.prepared').exists())
            self.assertFalse(dep.current.is_symlink())
            self.assertFalse((self.units / 'connectome-example.service').exists())
            self.assertFalse(marker.exists())

    def test_unknown_nonexternal_and_stale_before_activation(self):
        self.data_config()
        for server in ('unknown', 'alpha'):
            self.config['external_data_args'][0]['server'] = server
            if server == 'alpha':
                self.recipe(self.command, [])
            dep = self.deployment()
            with self.subTest(server=server), self.assertRaises(DeployError) as error:
                dep.prepare()
            self.assertEqual(error.exception.code, 'E_EXTERNAL_DATA_ARG')
            self.assertFalse(dep.current.is_symlink())
            self.assertFalse((dep.build / '.prepared').exists())
        # No external inventory at all must not silently discard the opt-in.
        path = Path(self.tmp.name) / 'operator.json'
        path.write_text('{"name":"example","agent":{"systemPrompt":"Verbatim"}}')
        self.config['recipe_file']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(DeployError) as error:
            self.deployment().prepare()
        self.assertEqual(error.exception.code, 'E_EXTERNAL_DATA_ARG')

    def test_invalid_input_has_typed_error_before_writes(self):
        self.config['external_data_args'] = [{'server': 'alpha', 'path': '/a/../b', 'kind': 'file'}]
        with self.assertRaises(DeployError) as error:
            self.deployment()
        self.assertEqual(error.exception.public()['error_code'], 'E_EXTERNAL_DATA_ARG')
        self.assertFalse(self.root.exists())


if __name__ == '__main__':
    unittest.main()
