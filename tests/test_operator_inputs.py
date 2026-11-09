"""Operator recipes and file-only secret delivery; all canaries are synthetic."""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

import test_deploy
import connectome_inputs as inputs
from connectome_deploy import Deployment, DeployError
from connectome_lock import read_lock, LockError


class OperatorTests(unittest.TestCase):
    setUp = test_deploy.DeployTests.setUp
    deployment = test_deploy.DeployTests.deployment
    apply = test_deploy.DeployTests.apply
    snapshot = test_deploy.DeployTests.snapshot

    def recipe(self, content=b'{ "name": "example", "agent": {"systemPrompt": "Verbatim"} }\r\n'):
        path = Path(self.tmp.name) / 'operator.json'
        path.write_bytes(content)
        self.config['recipe_file'] = {'path': str(path), 'sha256': hashlib.sha256(content).hexdigest()}
        return path, content

    def file_config(self):
        self.config['secret_files'] = [{'secret': 'EXAMPLE_SECRET', 'env': 'EXAMPLE_FILE'}]
        return self.config

    def test_verbatim_controller_transport_plan_lock_and_retention(self):
        path, content = self.recipe()
        # Neither connector pins nor generator settings are needed in this mode.
        self.config['connectors']['unresolved'] = {'repo': 'not used'}
        self.versions.pop('connectors', None)
        transport = inputs.collect(self.config)
        path.unlink()  # Simulate controller and target having different filesystems.
        dep = Deployment(self.root, self.units, self.config, self.versions, self.cook,
                         self.systemctl, operator_files=transport)
        result = dep.prepare()
        self.assertEqual(result['plan']['recipe_file'], self.config['recipe_file'])
        self.assertEqual(dep.pins['connectors'], {})
        recipe = dep.build / 'cook-recipe.json'
        self.assertEqual(recipe.read_bytes(), content)
        self.assertEqual(stat.S_IMODE(recipe.stat().st_mode), 0o600)
        self.assertEqual(recipe.stat().st_uid, os.geteuid())
        self.assertEqual(dep._lock()['recipe']['sha256'], self.config['recipe_file']['sha256'])
        dep.activate({})
        self.assertFalse(dep.prepare()['changed'])
        self.assertFalse(dep.activate({})['changed'])
        self.assertIn('ExecStart=' + str(dep.build / 'run.sh') + '\n',
                      (dep.build / 'connectome-example.service').read_text())
        # Both the lock's provenance and the installed bytes must still match.
        original = recipe.read_bytes()
        recipe.write_bytes(b'{}')
        with self.assertRaises(DeployError):
            dep.activate({})
        recipe.write_bytes(original)
        recipe.chmod(0o644)
        with self.assertRaises(DeployError):
            dep.activate({})
        recipe.chmod(0o600)
        lockpath = dep.build / 'connectome.lock'
        data = json.loads(lockpath.read_text())
        data['recipe']['sha256'] = 'a' * 64
        lockpath.write_text(json.dumps(data))
        with self.assertRaises(LockError):
            read_lock(lockpath, config=self.config)

    def test_recipe_closed_shape_and_transport_refusals(self):
        path, _ = self.recipe()
        record = self.config['recipe_file']
        cases = [None, [], {}, {'path': str(path)}, dict(record, extra=True),
                 dict(record, path='relative.json'), dict(record, path=str(path) + '/..'),
                 dict(record, path='/input/{state_dir}.json'), dict(record, path='/input/a\n.json'),
                 dict(record, sha256='main'), dict(record, sha256=1)]
        for value in cases:
            config = copy.deepcopy(self.config)
            config['recipe_file'] = value
            with self.subTest(value=value), self.assertRaises(DeployError):
                self.deployment(config)
        for supplied in ({}, {'cook-recipe.json': '!bad!'}, {'cook-recipe.json': 'e30='},
                         dict(inputs.collect(self.config), extra='e30=')):
            dep = Deployment(self.root, self.units, self.config, self.versions, self.cook,
                             operator_files=supplied)
            before = self.snapshot()
            with self.assertRaises(DeployError):
                dep.prepare()
            self.assertEqual(self.snapshot(), before)

    def test_recipe_file_refusals_before_commands_or_writes(self):
        path, _ = self.recipe()
        for content in (b'not JSON', b'[]', b'null', b'{"x":NaN}', b'{"x":1,"x":2}', b'\xff'):
            path.write_bytes(content)
            self.config['recipe_file']['sha256'] = hashlib.sha256(content).hexdigest()
            before = self.snapshot()
            with self.subTest(content=content), patch('connectome_deploy.run') as command:
                with self.assertRaises(DeployError):
                    self.deployment().prepare()
                command.assert_not_called()
                self.assertEqual(self.snapshot(), before)
        path.write_bytes(b'{}')  # mismatched hash
        with self.assertRaises(ValueError):
            inputs.collect(self.config)
        path.unlink()
        target = Path(self.tmp.name) / 'target.json'
        target.write_bytes(b'{}')
        for kind in ('link', 'directory', 'fifo', 'missing'):
            if kind == 'link': path.symlink_to(target)
            if kind == 'directory': path.mkdir()
            if kind == 'fifo': os.mkfifo(path)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                inputs.collect(self.config)
            if kind == 'directory': path.rmdir()
            elif path.exists() or path.is_symlink(): path.unlink()
        parent = Path(self.tmp.name) / 'alias'
        parent.symlink_to(target.parent, target_is_directory=True)
        self.config['recipe_file']['path'] = str(parent / target.name)
        with self.assertRaises(ValueError):
            inputs.collect(self.config)

    def test_secret_files_closed_shapes_names_and_conflicts(self):
        cases = [None, {}, 'bad', [None], [{}], [{'secret': 'EXAMPLE_SECRET'}],
                 [{'secret': 'EXAMPLE_SECRET', 'env': 'FILE', 'mode': '0644'}],
                 [{'secret': 'bad-name', 'env': 'FILE'}], [{'secret': 'EXAMPLE_SECRET', 'env': '../FILE'}],
                 [{'secret': 'EXAMPLE_SECRET', 'env': 'DATA_DIR'}],
                 [{'secret': 'EXAMPLE_SECRET', 'env': 'FILE\nBAD'}],
                 [{'secret': 'EXAMPLE_SECRET', 'env': 'FILE'}] * 2]
        for value in cases:
            config = copy.deepcopy(self.config)
            config['secret_files'] = value
            before = self.snapshot()
            with self.subTest(value=value), self.assertRaises(DeployError):
                self.deployment(config)
            self.assertEqual(self.snapshot(), before)
        self.file_config()
        self.config['required_secrets'] = ['EXAMPLE_FILE']
        with self.assertRaises(DeployError):
            self.deployment()

    def test_file_only_canary_rewrite_rotation_removal_and_no_leaks(self):
        self.file_config()
        canary = 'synthetic-file-canary\nsecond line\n'
        Path(self.config['secrets_file']).write_text(json.dumps({'EXAMPLE_SECRET': canary}))
        dep = self.deployment()
        prepared = dep.prepare()
        self.assertEqual(prepared['secret_names'], ['EXAMPLE_SECRET'])
        result = dep.activate()  # Reads the agent's secrets JSON, not a controller provider.
        path = dep._secret_file_path('EXAMPLE_FILE')
        self.assertEqual(path.read_bytes(), canary.encode())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(path.stat().st_uid, os.geteuid())
        self.assertEqual((dep.build / 'secrets.env').read_text(), 'EXAMPLE_FILE="' + str(path) + '"\n')
        for output in (prepared, result, dep.plan(), json.loads((dep.build / 'connectome.lock').read_text())):
            self.assertNotIn(canary, json.dumps(output))
        for artifact in [*dep.build.rglob('*'), dep.manifest_path, dep.root / 'systemctl-calls']:
            if artifact.is_file():
                self.assertNotIn(canary.encode(), artifact.read_bytes(), str(artifact))
        old = path.stat().st_ino
        self.assertFalse(dep.activate()['changed'])
        self.assertNotEqual(old, path.stat().st_ino)  # Atomic rewrite on every apply.
        calls = (dep.root / 'systemctl-calls').read_bytes()
        Path(self.config['secrets_file']).write_text(json.dumps({'EXAMPLE_SECRET': 'rotated synthetic value'}))
        self.assertTrue(dep.activate()['changed'])
        self.assertNotEqual((dep.root / 'systemctl-calls').read_bytes(), calls)
        self.assertEqual(path.read_text(), 'rotated synthetic value')
        config = copy.deepcopy(self.config)
        config.pop('secret_files')
        removed = self.deployment(config)
        removed.prepare()
        removed.activate({})
        self.assertFalse(path.exists())
        self.assertEqual((removed.build / 'secrets.env').read_bytes(), b'')

    def test_secret_file_health_failure_restores_prior_declarations_and_values(self):
        self.file_config()
        first = self.deployment()
        self.apply(first)
        path = first._secret_file_path('EXAMPLE_FILE')
        original = path.read_bytes()
        config = copy.deepcopy(self.config)
        config['secret_files'] = [{'secret': 'UNUSED_SECRET', 'env': 'OTHER_FILE'}]
        second = self.deployment(config)
        second.prepare()
        (self.root / 'fail-health').write_text(str(second.build))
        with self.assertRaisesRegex(DeployError, 'restored'):
            self.apply(second)
        self.assertEqual(first.current.resolve(), first.build)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(second._secret_file_path('OTHER_FILE').exists())
        self.assertIn(str(path), (first.build / 'secrets.env').read_text())
        # Removal failure must also restore the removed file.
        config.pop('secret_files')
        third = self.deployment(config)
        third.prepare()
        (self.root / 'fail-health').write_text(str(third.build))
        with self.assertRaisesRegex(DeployError, 'restored'):
            third.activate({})
        self.assertEqual(path.read_bytes(), original)
        # Rotation in the same build restores old bytes on failed health too.
        (self.root / 'reject-env').write_text(str(first.build))
        with self.assertRaises(DeployError):
            first.activate({'EXAMPLE_SECRET': 'bad rotation'})
        self.assertEqual(path.read_bytes(), original)

    def test_secret_file_symlinks_modes_and_unmanaged_names_refused(self):
        self.file_config()
        dep = self.deployment()
        dep.prepare()
        directory = dep.state / 'secret-files'
        target = Path(self.tmp.name) / 'outside'
        target.mkdir(mode=0o700)
        directory.symlink_to(target, target_is_directory=True)
        before = self.snapshot()
        with self.assertRaises(DeployError):
            dep.activate({'EXAMPLE_SECRET': 'synthetic'})
        self.assertEqual(self.snapshot(), before)
        directory.unlink()
        self.apply(dep)
        path = dep._secret_file_path('EXAMPLE_FILE')
        content = path.read_bytes()
        path.unlink()
        path.symlink_to(Path(self.config['secrets_file']))
        for check in (False, True):
            before = self.snapshot()
            with self.assertRaises(DeployError):
                self.deployment(check=check).activate({} if check else {'EXAMPLE_SECRET': 'synthetic'})
            self.assertEqual(self.snapshot(), before)
        path.unlink()
        path.write_bytes(content)
        path.chmod(0o600)
        for mode in (0o644, 0o660):
            path.chmod(mode)
            with self.assertRaises(DeployError):
                dep.activate({'EXAMPLE_SECRET': 'synthetic'})
        path.chmod(0o600)
        for mode in (0o750, 0o770):
            directory.chmod(mode)
            with self.assertRaises(DeployError):
                dep.activate({'EXAMPLE_SECRET': 'synthetic'})
        directory.chmod(0o700)
        (directory / 'unknown').write_text('not ours')
        with self.assertRaises(DeployError):
            dep.activate({'EXAMPLE_SECRET': 'synthetic'})

    def test_first_failed_file_activation_removes_created_files(self):
        self.file_config()
        dep = self.deployment()
        dep.prepare()
        (self.root / 'fail-health').write_text(str(dep.build))
        with self.assertRaises(DeployError):
            dep.activate({'EXAMPLE_SECRET': 'synthetic canary'})
        self.assertFalse(dep.current.exists())
        self.assertFalse((dep.state / 'secret-files').exists())
        self.assertFalse((self.units / 'connectome-example.service').exists())

    def test_file_lock_binds_destination_name_and_source_lookup_is_exact(self):
        self.file_config()
        dep = self.deployment()
        dep.prepare()
        data = dep._lock()
        self.assertEqual([record['envName'] for record in data['requirements']], ['EXAMPLE_FILE'])
        self.assertEqual(dep.names, ['EXAMPLE_SECRET'])
        Path(self.config['secrets_file']).write_text('{}')
        before = self.snapshot()
        with self.assertRaises(DeployError):
            dep.activate()
        self.assertEqual(self.snapshot(), before)
        lockpath = dep.build / 'connectome.lock'
        data = json.loads(lockpath.read_text())
        data['requirements'] = []
        data['credentials'] = []
        lockpath.write_text(json.dumps(data))
        with self.assertRaises(DeployError):
            dep.activate({'EXAMPLE_SECRET': 'synthetic canary'})

    def test_check_mode_file_secret_has_no_writes_or_provider_calls(self):
        self.file_config()
        before = self.snapshot()
        dep = self.deployment(check=True)
        plan = dep.prepare()['plan']['secret_files']
        self.assertEqual(plan, [{'secret': 'EXAMPLE_SECRET', 'env': 'EXAMPLE_FILE',
                                 'path': str(dep.state / 'secret-files/EXAMPLE_FILE')}])
        self.assertTrue(dep.activate({})['changed'])
        self.assertEqual(self.snapshot(), before)
