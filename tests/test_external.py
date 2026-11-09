"""Host-installed MCP servers (cook source.external): lock schema and ownership."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_cook
import test_deploy
from test_cook import fixture, adapter
from connectome_lock import read_lock, LockError
from connectome_deploy import DeployError, check_external_command

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


if __name__ == '__main__':
    unittest.main()
