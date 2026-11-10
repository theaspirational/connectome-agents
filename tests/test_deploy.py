import copy
import hashlib
import os
import json
import importlib.util
from pathlib import Path
import stat
import sys
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'roles/connectome_agent/module_utils'))
from connectome_config import merge_layers, public_catalog
from connectome_deploy import Deployment, DeployError, make_directory
from deploy_fixtures import private_scratch


def read(path):
    return yaml.safe_load((ROOT / path).read_text())


def example():
    catalog = {}
    for path in (ROOT / 'connectors').glob('*.yml'):
        catalog.update(yaml.safe_load(path.read_text()))
    public = public_catalog(read('defaults/agent.yml'), catalog)
    return merge_layers([public, read('examples/private-skeleton/overrides/agent.yml'), {},
                         read('examples/private-skeleton/agents/example/agent.yml')])


class MockProvider:
    def __init__(self, path):
        self.path, self.requested = path, []

    def get(self, names):
        self.requested.extend(names)
        values = json.loads(self.path.read_text())
        return {name: values[name] for name in names}


class DeployTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest('deployment fixtures model an unprivileged agent account')
        old_mask = os.umask(0o022)
        self.addCleanup(os.umask, old_mask)
        self.tmp = private_scratch(self)
        self.root = Path(self.tmp.name) / 'agent'
        self.units = Path(self.tmp.name) / 'units'
        self.config = example()
        self.config['state_dir'] = str(Path(self.tmp.name) / 'runtime-state')
        secrets_dir = Path(self.tmp.name) / 'connectome-secrets'
        secrets_dir.mkdir(mode=0o700)
        self.config['secrets_file'] = str(secrets_dir / 'example.json')
        Path(self.config['secrets_file']).write_text('{}')
        Path(self.config['secrets_file']).chmod(0o600)
        self.versions = read('versions.yml')
        self.cook = [sys.executable, str(ROOT / 'tests/fixtures/cook.py'),
                     '{recipe}', '{build_dir}', '{state_dir}']
        self.systemctl = [sys.executable, str(ROOT / 'tests/fixtures/systemctl.py'), str(self.root)]
        self.provider_path = Path(self.tmp.name) / 'provider.json'
        self.provider_path.write_text(json.dumps({'EXAMPLE_SECRET': 'synthetic-value',
                                                 'UNUSED_SECRET': 'never-requested'}))
        self.provider = MockProvider(self.provider_path)
        self.analyze = [sys.executable, str(ROOT / 'tests/fixtures/analyze.py')]

    def deployment(self, config=None, check=False, versions=None):
        return Deployment(self.root, self.units, config or self.config,
                          versions or self.versions, self.cook, self.systemctl, check,
                          analyze_command=self.analyze)

    def apply(self, deployment):
        plan = deployment.prepare()
        return deployment.activate(self.provider.get(plan['secret_names']))

    def state_snapshot(self, path):
        # State ownership/modes, bytes, inode and mtimes must not change.
        return {str(p.relative_to(path)): (p.lstat().st_ino, p.lstat().st_uid,
                p.lstat().st_mode, p.lstat().st_mtime_ns,
                p.read_bytes() if p.is_file() else None)
                for p in [path, *path.rglob('*')]}

    def lifecycle_calls(self):
        paths = [self.root / 'systemctl-calls', Path(self.tmp.name) / 'preflight-systemctl-calls']
        return [json.loads(line) for path in paths if path.exists()
                for line in path.read_text().splitlines()]

    def track_units(self, states=None):
        (Path(self.tmp.name) / 'track-unit-state').touch()
        (Path(self.tmp.name) / 'unit-states.json').write_text(json.dumps(states or {}))

    def assert_no_activation(self):
        self.assertFalse(any(call[0] in ('enable', 'start', 'restart', 'stop', 'disable')
                             for call in self.lifecycle_calls()), self.lifecycle_calls())
        self.assertFalse((self.units / 'default.target.wants').exists())

    def test_install_only_static_health_and_status(self):
        self.track_units()
        self.config.update(activation='install_only', required_secrets=['EXAMPLE_SECRET'],
                           health={'command': ['/not/a/runtime-health-command']},
                           secret_files=[{'secret': 'EXAMPLE_SECRET', 'env': 'TOKEN_FILE'}])
        # Direct and file delivery deliberately use distinct environment names.
        dep = self.deployment()
        result = self.apply(dep)
        self.assertEqual(result['activation'], 'install_only')
        self.assertEqual(result['unit_state'], 'inactive')
        self.assertEqual(dep.current.resolve(), dep.build)
        self.assertTrue((self.units / 'connectome-example.service').is_file())
        self.assertIn('EXAMPLE_SECRET="synthetic-value"', (dep.build / 'secrets.env').read_text())
        self.assertEqual((dep.state / 'secret-files/TOKEN_FILE').read_text(), 'synthetic-value')
        self.assert_no_activation()
        self.assertTrue(any(call[:4] == ['show', '-p', 'EnvironmentFiles', '--value']
                            for call in self.lifecycle_calls()))
        self.assertFalse(self.apply(self.deployment())['changed'])
        self.assert_no_activation()
        self.assertEqual(dep.plan()['activation'], 'install_only')

    def test_install_only_then_start_reuses_release_and_lock(self):
        self.track_units()
        self.config['activation'] = 'install_only'
        staged = self.deployment()
        self.apply(staged)
        lock = (staged.build / 'connectome.lock').read_bytes()
        cook_calls = (self.root / 'cook-calls').read_bytes()
        config = dict(self.config, activation='start')
        start = self.deployment(config)
        self.assertEqual(start.build, staged.build)
        self.assertFalse(start.prepare()['changed'])
        self.assertTrue(start.activate({})['changed'])
        self.assertEqual((start.build / 'connectome.lock').read_bytes(), lock)
        self.assertEqual((self.root / 'cook-calls').read_bytes(), cook_calls)
        self.assertTrue((self.units / 'default.target.wants/connectome-example.service').is_symlink())
        self.assertIn(['restart', 'connectome-example.service'], self.lifecycle_calls())
        self.assertFalse(self.apply(self.deployment(config))['changed'])

    def test_install_only_then_start_without_install_section(self):
        self.track_units()
        self.config['activation'] = 'install_only'
        dep = self.deployment()
        dep.prepare()
        source = dep.build / 'other.service'
        source.write_text('[Service]\nExecStart=/bin/true\n')
        lock = dep.build / 'connectome.lock'
        data = json.loads(lock.read_text())
        data['units'] = [{'path': source.name, 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}]
        lock.write_text(json.dumps(data))
        dep.activate({})
        started = self.deployment(dict(self.config, activation='start'))
        self.assertFalse(started.prepare()['changed'])
        self.assertTrue(started.activate({})['changed'])
        self.assertIn(['restart', 'other.service'], self.lifecycle_calls())
        self.assertEqual(len((self.root / 'cook-calls').read_text().splitlines()), 1)

    def test_install_only_active_refused_before_any_changes(self):
        self.config['activation'] = 'install_only'
        dep = self.deployment()
        for state in (b'active', b'activating', b'reloading', b'deactivating'):
            before = self.snapshot()
            with patch('connectome_deploy.subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout=state)):
                for operation in (dep.preflight_activation, dep.prepare, lambda: dep.activate({})):
                    with self.assertRaises(DeployError) as error:
                        operation()
                    self.assertEqual(error.exception.public()['error_code'], 'E_UNIT_ACTIVE')
                    self.assertEqual(error.exception.public()['unit'], 'connectome-example.service')
            self.assertEqual(self.snapshot(), before)
        # Also protect a previous differently named unit before building its replacement.
        self.apply(self.deployment(dict(self.config, activation='start')))
        self.track_units({'user:connectome-example.service': 'active'})
        renamed = self.deployment(dict(self.config, name='renamed'))
        with self.assertRaises(DeployError) as error:
            renamed.prepare()
        self.assertEqual(error.exception.code, 'E_UNIT_ACTIVE')
        self.assertFalse(renamed.build.exists())

    def test_install_only_refuses_late_activation_before_env_or_pointer_changes(self):
        self.config['activation'] = 'install_only'
        dep = self.deployment()
        dep.prepare()
        self.track_units({'user:connectome-example.service': 'active'})
        with self.assertRaises(DeployError) as error:
            dep.activate({})
        self.assertEqual(error.exception.code, 'E_UNIT_ACTIVE')
        self.assertFalse(dep.current.exists())
        self.assertFalse((dep.build / 'secrets.env').exists())
        self.assert_no_activation()

    def test_install_only_rollback_and_operator_rollback_never_start(self):
        self.track_units()
        (Path(self.tmp.name) / 'forbid-unit-activation').touch()
        self.config['activation'] = 'install_only'
        first = self.deployment()
        self.apply(first)
        second = self.deployment(dict(self.config, name='renamed'))
        second.prepare()
        (self.root / 'reject-env').write_text(str(second.build))
        with self.assertRaises(DeployError) as error:
            second.activate({})
        self.assertEqual(error.exception.code, 'E_ENVIRONMENT_FILE')
        self.assertEqual(first.current.resolve(), first.build)
        self.assert_no_activation()
        (self.root / 'reject-env').unlink()
        self.apply(second)
        before = (self.root / 'cook-calls').read_bytes()
        self.apply(self.deployment())  # Re-select exact first request.
        self.assertEqual(first.current.resolve(), first.build)
        self.assertEqual((self.root / 'cook-calls').read_bytes(), before)
        self.assert_no_activation()

    def test_install_only_first_failure_never_starts(self):
        self.track_units()
        (Path(self.tmp.name) / 'forbid-unit-activation').touch()
        self.config['activation'] = 'install_only'
        dep = self.deployment()
        dep.prepare()
        (self.root / 'reject-env').write_text(str(dep.build))
        with self.assertRaises(DeployError):
            dep.activate({})
        self.assertFalse(dep.current.exists())
        self.assertFalse((self.units / 'connectome-example.service').exists())
        self.assert_no_activation()

    def test_install_only_runs_static_verify_when_available(self):
        self.analyze = None  # Exercise production discovery with a fake tool.
        self.track_units()
        self.config['activation'] = 'install_only'
        import connectome_deploy
        original = connectome_deploy.run
        def verify(argv, **kwargs):
            if argv[0] == '/test/systemd-analyze':
                self.assertEqual(argv[1:3], ['--user', 'verify'])
                self.assertTrue(kwargs['unit_verify'])
                self.assertTrue(Path(argv[3]).is_file())
                return
            return original(argv, **kwargs)
        with patch('connectome_deploy.shutil.which', return_value='/test/systemd-analyze'), \
                patch('connectome_deploy.run', side_effect=verify) as calls:
            self.apply(self.deployment())
        self.assertTrue(any(call.args[0][0] == '/test/systemd-analyze' for call in calls.call_args_list))
        # Verification failure restores the previous pointer without runtime health.
        bad = self.deployment(dict(self.config, description='bad-verify'))
        bad.prepare()
        first = bad.current.resolve()
        def reject(argv, **kwargs):
            if argv[0] == '/test/systemd-analyze' and bad.current.resolve() == bad.build:
                raise DeployError('static verify failed')
            return verify(argv, **kwargs)
        with patch('connectome_deploy.shutil.which', return_value='/test/systemd-analyze'), \
                patch('connectome_deploy.run', side_effect=reject), self.assertRaises(DeployError):
            bad.activate({})
        self.assertEqual(bad.current.resolve(), first)
        self.assert_no_activation()
        with patch('connectome_deploy.shutil.which', return_value=None):
            self.assertFalse(self.apply(self.deployment())['changed'])

    def test_verifier_failure_reports_step_tool_status_without_output(self):
        self.track_units()
        self.config['activation'] = 'install_only'
        # Exercise real subprocess failure, not just a mocked exception.
        self.analyze = [sys.executable, '-c',
                        'import sys; print("synthetic-private-canary", file=sys.stderr); sys.exit(7)']
        dep = self.deployment()
        dep.prepare()
        with patch('connectome_deploy.shutil.which', side_effect=AssertionError('host discovery')), \
                self.assertRaises(DeployError) as failure:
            dep.activate({})
        error = failure.exception
        self.assertEqual(error.code, 'E_UNIT_VERIFY')
        self.assertIn('activation failed at health: systemd-analyze exited 7', str(error))
        self.assertIn('systemd-analyze exited 7', error.public()['msg'])
        self.assertEqual(error.__cause__.returncode, 7)
        self.assertFalse(dep.current.exists())
        self.assert_no_activation()
        import traceback
        diagnostic = ''.join(traceback.format_exception(error)) + json.dumps(error.public())
        self.assertNotIn('synthetic-private-canary', diagnostic)

    def test_activation_cause_survives_failed_recovery_without_private_context(self):
        import connectome_deploy
        import traceback
        for recovery_fails in (False, True):
            with self.subTest(recovery_fails=recovery_fails):
                first = self.deployment()
                self.apply(first)
                second = self.deployment(dict(self.config, description='changed'))
                second.prepare()
                original = DeployError('synthetic-private-canary', 'E_ENVIRONMENT_FILE')
                recovery = RuntimeError('synthetic-private-canary') if recovery_fails else None
                with patch.object(connectome_deploy.Deployment, '_health',
                                  side_effect=[original, recovery]), self.assertRaises(DeployError) as failure:
                    second.activate({})
                error = failure.exception
                self.assertEqual(error.__cause__.code, 'E_ENVIRONMENT_FILE')
                self.assertIn('activation failed at health', str(error))
                self.assertIn('rollback recovery also failed' if recovery_fails else
                              'previous deployment restored', str(error))
                self.assertEqual(first.current.resolve(), first.build)
                diagnostic = ''.join(traceback.format_exception(error)) + json.dumps(error.public())
                self.assertNotIn('synthetic-private-canary', diagnostic)

    @unittest.skipUnless(os.environ.get('CONNECTOME_TEST_SYSTEMD_ANALYZE') == '1',
                         'real systemd-analyze integration is opt-in')
    def test_real_static_verifier_with_user_runtime(self):
        import shutil
        import subprocess
        analyzer, manager = shutil.which('systemd-analyze'), shutil.which('systemctl')
        runtime = os.environ.get('XDG_RUNTIME_DIR')
        if not analyzer or not manager or not runtime or not Path(runtime).is_dir():
            self.skipTest('real verifier requires systemd tools and a user runtime directory')
        try:
            status = subprocess.run([manager, '--user', 'show-environment'], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=5).returncode
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest('real verifier requires an available user manager')
        if status:
            self.skipTest('real verifier requires a reachable user manager')
        self.analyze = [analyzer]
        self.track_units()
        self.config['activation'] = 'install_only'
        self.apply(self.deployment())
        self.assert_no_activation()

    def test_exclusive_units_refuse_start_in_both_scopes(self):
        for scope in ('system', 'user'):
            for state in ('active', 'activating'):
                with self.subTest(scope=scope, state=state):
                    self.track_units({scope + ':old.service': state})
                    config = dict(self.config, exclusive_with=[{'scope': scope, 'unit': 'old.service'}])
                    dep = self.deployment(config)
                    with self.assertRaises(DeployError) as error:
                        self.apply(dep)
                    public = error.exception.public()
                    self.assertEqual(public['error_code'], 'E_UNIT_EXCLUSIVE')
                    self.assertEqual(public['unit'], 'old.service')
                    self.assertEqual(public['scope'], scope)
                    self.assertIn('old.service', public['msg'])
                    self.assertIn(['--' + scope, 'is-active', 'old.service'], self.lifecycle_calls())
                    self.assertFalse(dep.build.exists())
                    self.assert_no_activation()
        self.track_units({'system:old.service': 'inactive'})
        self.assertTrue(self.apply(self.deployment(config))['changed'])

    def test_exclusive_units_report_only_during_staging(self):
        self.track_units({'system:old.service': 'active', 'user:old.service': 'activating'})
        entries = [{'scope': scope, 'unit': 'old.service'} for scope in ('system', 'user')]
        self.config.update(activation='install_only', exclusive_with=entries)
        result = self.apply(self.deployment())
        self.assertEqual(result['exclusive_with'], [dict(entries[0], state='active'),
                                                  dict(entries[1], state='activating')])
        self.assertEqual(result['unit_state'], 'inactive')
        self.assert_no_activation()
        self.track_units({'system:old.service': 'inactive', 'user:old.service': 'inactive'})
        result = self.apply(self.deployment())
        self.assertEqual([entry['state'] for entry in result['exclusive_with']], ['inactive', 'inactive'])
        start = self.deployment(dict(self.config, activation='start'))
        self.assertTrue(self.apply(start)['changed'])
        self.assertEqual(len((self.root / 'cook-calls').read_text().splitlines()), 1)

    def test_closed_activation_and_exclusive_schema(self):
        for field, values in (
                ('activation', [None, False, [], {}, 'restart', 'install-only']),
                ('exclusive_with', [None, {}, ['old.service'], [{'scope': 'global', 'unit': 'old.service'}],
                    [{'scope': 'system', 'unit': '../old.service'}], [{'scope': 'user', 'unit': '-old.service'}],
                    [{'scope': 'user', 'unit': 'old.timer'}], [{'scope': 'user', 'unit': 'old.service', 'extra': True}],
                    [{'scope': 'user', 'unit': 'old.service'}] * 2])):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(DeployError):
                    self.deployment(dict(self.config, **{field: value}))
        self.assertFalse(self.root.exists())

    def test_install_only_check_never_queries_or_writes(self):
        self.config.update(activation='install_only', exclusive_with=[{'scope': 'system', 'unit': 'old.service'}])
        dep = self.deployment(check=True)
        before = self.snapshot()
        with patch('connectome_deploy.subprocess.run') as run:
            self.assertTrue(dep.prepare()['changed'])
            self.assertTrue(dep.activate({})['changed'])
        run.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_module_active_guard_precedes_root_and_serialization_lock(self):
        self.config['activation'] = 'install_only'
        module = Mock(params=dict(operation='prepare', root=str(self.root), unit_dir=str(self.units),
                                  config=self.config, versions=self.versions, cook_command=self.cook,
                                  systemctl=self.systemctl, secrets={}), check_mode=False)
        module.fail_json.side_effect = DeployError('boundary rejected')
        modules = {
            'ansible.module_utils.basic': SimpleNamespace(AnsibleModule=Mock(return_value=module)),
            'ansible.module_utils.connectome_deploy': sys.modules['connectome_deploy'],
            'ansible.module_utils.connectome_errors': sys.modules['connectome_errors'],
        }
        spec = importlib.util.spec_from_file_location(
            'active_boundary', ROOT / 'roles/connectome_agent/library/connectome_release.py')
        boundary = importlib.util.module_from_spec(spec)
        before = self.snapshot()
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(boundary)
            with patch('connectome_deploy.subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout=b'active')), self.assertRaisesRegex(DeployError, 'boundary rejected'):
                boundary.main()
        self.assertEqual(module.fail_json.call_args.kwargs['error_code'], 'E_UNIT_ACTIVE')
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.root.exists())

    def test_exclusive_refusal_rechecked_before_activation(self):
        self.track_units()
        self.config['exclusive_with'] = [{'scope': 'system', 'unit': 'old.service'}]
        dep = self.deployment()
        dep.prepare()
        self.track_units({'system:old.service': 'activating'})
        with self.assertRaises(DeployError) as error:
            dep.activate({})
        self.assertEqual(error.exception.public()['unit'], 'old.service')
        self.assertEqual(error.exception.code, 'E_UNIT_EXCLUSIVE')
        self.assertFalse(dep.current.exists())
        self.assertFalse((dep.build / 'secrets.env').exists())
        self.assert_no_activation()

    def test_exclusive_inspection_failure_is_informational_only_when_staging(self):
        self.config.update(activation='install_only', exclusive_with=[{'scope': 'system', 'unit': 'old.service'}])
        dep = self.deployment()
        for result in (SimpleNamespace(returncode=1, stdout=b''),
                       SimpleNamespace(returncode=0, stdout=b'not-a-state')):
            with patch('connectome_deploy.subprocess.run', return_value=result):
                self.assertEqual(dep._exclusive_status()[0]['state'], 'unavailable')
                with self.assertRaises(DeployError):
                    dep._exclusive_status(refuse=True)
        with patch('connectome_deploy.subprocess.run', side_effect=OSError):
            self.assertEqual(dep._exclusive_status()[0]['state'], 'unavailable')
            with self.assertRaises(DeployError):
                dep._exclusive_status(refuse=True)

    def test_rendered_systemd_paths_are_bare_literals(self):
        dep = self.deployment()
        self.apply(dep)
        dropin = self.units / 'connectome-example.service.d/90-connectome-env.conf'
        self.assertEqual(dropin.read_text(), '[Service]\nEnvironment=DATA_DIR=' +
                         str(dep.state_dir) + '\nEnvironmentFile=' + str(dep.build / 'secrets.env') + '\n')
        unit = (dep.build / 'connectome-example.service').read_text()
        self.assertIn('WorkingDirectory=' + str(dep.state_dir) + '\n', unit)
        self.assertIn('ExecStart=' + str(dep.build / 'run.sh') + '\n', unit)

    def test_ambiguous_systemd_paths_fail_before_writes(self):
        for bad in ('a b', 'a\tb', 'a\nb', 'a%b', 'a*b', 'a?b', 'a[b]', 'a"b', "a'b", 'a\\b', 'a$b', 'a;b'):
            for field in ('root', 'unit_dir', 'state_dir', 'secrets_file'):
                with self.subTest(bad=bad, field=field):
                    config = copy.deepcopy(self.config)
                    root, units = self.root, self.units
                    path = str(Path(self.tmp.name) / bad)
                    if field == 'root':
                        root = Path(path)
                    elif field == 'unit_dir':
                        units = Path(path)
                    else:
                        config[field] = path
                    before = self.snapshot()
                    with self.assertRaises(DeployError):
                        Deployment(root, units, config, self.versions, self.cook)
                    self.assertEqual(self.snapshot(), before)

    def test_directory_modes_ignore_umask_without_repairing_existing_dirs(self):
        for mask in (0o002, 0o077):
            with self.subTest(umask=mask):
                base = Path(self.tmp.name) / ('mask-' + str(mask))
                self.root = base / '.local/share/connectome/example'
                self.units = base / '.config/systemd/user'
                self.config['state_dir'] = str(base / '.local/state/connectome/example')
                self.systemctl[-1] = str(self.root)
                old = os.umask(mask)
                try:
                    dep = self.deployment()
                    self.apply(dep)
                finally:
                    os.umask(old)
                for path in (base, base / '.local', base / '.local/share',
                             base / '.local/share/connectome', base / '.local/state',
                             base / '.local/state/connectome', base / '.config', base / '.config/systemd'):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755, str(path))
                for path in (dep.root, dep.builds, dep.build, dep.state, dep.state_dir, dep.unit_dir,
                             dep.unit_dir / 'connectome-example.service.d', dep.unit_dir / 'default.target.wants'):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, str(path))
        existing = Path(self.tmp.name) / 'existing'
        existing.mkdir(mode=0o750)
        make_directory(existing)
        self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o750)

    def test_directory_helper_sets_exact_modes_under_fully_restrictive_umask(self):
        path = Path(self.tmp.name) / 'ancestor/private'
        old = os.umask(0o777)
        try:
            make_directory(path)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_writable_ancestors_are_named_and_never_modified(self):
        for field in ('root', 'state_dir', 'secrets_file'):
            for mode in (0o775, 0o757):
                with self.subTest(field=field, mode=mode):
                    parent = Path(self.tmp.name) / 'unsafe'
                    parent.mkdir()
                    parent.chmod(mode)
                    config = copy.deepcopy(self.config)
                    root = self.root
                    if field == 'root':
                        root = parent / 'agent'
                    else:
                        config[field] = str(parent / 'nested/leaf')
                    before = self.snapshot()
                    for check in (False, True):
                        with self.assertRaises(DeployError) as error:
                            Deployment(root, self.units, config, self.versions, self.cook, check=check)
                        self.assertEqual(error.exception.public()['error_code'], 'E_ANCESTOR_WRITABLE')
                        self.assertEqual(error.exception.public()['ancestor_path'], str(parent))
                        self.assertEqual(self.snapshot(), before)
                    parent.rmdir()
        dep = self.deployment()
        self.root.parent.chmod(0o775)
        try:
            before = self.snapshot()
            with self.assertRaises(DeployError) as error:
                dep.prepare()
            self.assertEqual(error.exception.code, 'E_ANCESTOR_WRITABLE')
            self.assertEqual(self.snapshot(), before)
        finally:
            self.root.parent.chmod(0o700)

    def test_required_env_file_manager_rejection_rolls_back(self):
        self.config['required_secrets'] = ['EXAMPLE_SECRET']
        first = self.deployment()
        self.apply(first)
        config = copy.deepcopy(self.config)
        config['description'] = 'Second request'
        bad = self.deployment(config)
        bad.prepare()
        (self.root / 'reject-env').write_text(str(bad.build))
        before = (self.root / 'systemctl-calls').read_text().splitlines()
        with self.assertRaisesRegex(DeployError, 'restored') as error:
            bad.activate(self.provider.get(bad.names))
        self.assertEqual(error.exception.code, 'E_ENVIRONMENT_FILE')
        self.assertEqual(first.current.resolve(), first.build)
        calls = [json.loads(line) for line in (self.root / 'systemctl-calls').read_text().splitlines()[len(before):]]
        self.assertEqual(sum(call[0] == 'show' for call in calls), 2)  # candidate and rollback
        self.assertIn(str(first.build / 'secrets.env'),
                      (self.units / 'connectome-example.service.d/90-connectome-env.conf').read_text())

    def test_first_activation_env_file_rejection_removes_units(self):
        self.config['required_secrets'] = ['EXAMPLE_SECRET']
        dep = self.deployment()
        dep.prepare()
        (self.root / 'reject-env').write_text(str(dep.build))
        with self.assertRaises(DeployError) as error:
            dep.activate(self.provider.get(dep.names))
        self.assertEqual(error.exception.code, 'E_ENVIRONMENT_FILE')
        self.assertFalse(dep.current.exists())
        self.assertFalse((self.units / 'connectome-example.service').exists())

    def test_environment_file_health_requires_exact_nonoptional_manager_path(self):
        dep = self.deployment()
        expected = str(dep.build / 'secrets.env').encode()
        for output, code in ((b'', 0), (expected + b' (ignore_errors=yes)\n', 0),
                             (expected + b'.other (ignore_errors=no)\n', 0),
                             (expected + b' (ignore_errors=no)\n', 1)):
            with self.subTest(output=output), patch('connectome_deploy.subprocess.run',
                    return_value=SimpleNamespace(returncode=code, stdout=output)):
                with self.assertRaises(DeployError) as error:
                    dep._check_environment_file('example.service', dep.build)
                self.assertEqual(error.exception.code, 'E_ENVIRONMENT_FILE')
        with patch('connectome_deploy.subprocess.run', return_value=SimpleNamespace(
                returncode=0, stdout=b'/other (ignore_errors=yes) ' + expected + b' (ignore_errors=no)\n')):
            dep._check_environment_file('example.service', dep.build)

    def test_apply_recreates_disabled_unit_links_without_rebuild(self):
        first = self.deployment()
        self.apply(first)
        unit = self.units / 'connectome-example.service'
        enabled = self.units / 'default.target.wants/connectome-example.service'
        unit.unlink()
        enabled.unlink()
        enabled.parent.rmdir()
        before = (self.root / 'cook-calls').read_text()
        snapshot = self.snapshot()
        check = self.deployment(check=True)
        self.assertFalse(check.prepare()['changed'])
        self.assertTrue(check.activate({})['changed'])
        self.assertEqual(self.snapshot(), snapshot)
        self.assertTrue(self.apply(self.deployment())['changed'])
        self.assertEqual(unit.resolve(), first.build / unit.name)
        self.assertTrue(enabled.is_symlink())
        self.assertEqual((self.root / 'cook-calls').read_text(), before)
        self.assertFalse(self.apply(self.deployment())['changed'])
        # Update and rollback tolerate links removed by systemctl disable too.
        unit.unlink()
        enabled.unlink()
        config = copy.deepcopy(self.config)
        config['name'] = 'renamed'
        bad = self.deployment(config)
        bad.prepare()
        (self.root / 'fail-health').write_text(str(bad.build))
        with self.assertRaisesRegex(DeployError, 'restored'):
            bad.activate({})
        self.assertTrue(unit.is_symlink())
        self.assertTrue(enabled.is_symlink())
        self.assertEqual(first.current.resolve(), first.build)

    def test_registry_archive_plan_and_retained_sidecar(self):
        from test_cook import framework_archive
        from connectome_archive import FRAMEWORK
        artifact = framework_archive(Path(self.tmp.name) / 'release.tgz', None, version='0.32.0')
        artifact.update(registry_release=True, name=FRAMEWORK, version='0.32.0')
        config = copy.deepcopy(self.config)
        config['cook'] = {'framework_archive': artifact}
        deployment = self.deployment(config, check=True)
        before = deployment.prepare()['plan']['framework_archive']
        self.assertFalse(before['commit_verifiable'])
        self.assertEqual(before['name'], FRAMEWORK)
        self.assertEqual(before['version'], '0.32.0')
        self.assertEqual(before['requested_commit'], self.versions['runtime']['agent-framework'])
        self.assertFalse(self.root.exists())  # check-mode does not write the sidecar
        # Retained build plans use the validated sidecar, not an input archive
        # that may no longer be provisioned at its original location.
        deployment.build.mkdir(parents=True)
        (deployment.build / 'framework.lock.json').write_text(json.dumps(before))
        (deployment.build / '.prepared').write_text(deployment.request_id)
        Path(artifact['path']).unlink()
        with patch.object(deployment, '_lock', return_value={'hostDependencies': [{'version': before['version']}]}):
            plan = deployment.plan()
        self.assertEqual(plan['framework_archive'], before)
        self.assertEqual(plan['host_dependencies'][0]['kind'], 'artifact')
        self.assertEqual(plan['host_dependencies'][0]['payload'],
                         'operator-supplied prebuilt payload, verified out of band')

    def test_generic_archive_check_plan_is_complete_and_mutation_free(self):
        from test_cook import framework_archive
        artifact = framework_archive(Path(self.tmp.name) / 'context.tgz', None,
                                     name="@animalabs/context-manager")
        config = copy.deepcopy(self.config)
        config['cook'] = {'host_dependencies': [{'name': "@animalabs/context-manager",
            'archive': artifact['path'], 'sha256': artifact['sha256'], 'kind': 'artifact', 'version': '1.0.0'}]}
        deployment = self.deployment(config, check=True)
        plan = deployment.prepare()['plan']['host_dependencies']
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0], dict(config['cook']['host_dependencies'][0],
            commit_verifiable=False, payload='operator-supplied prebuilt payload, verified out of band'))
        self.assertFalse(self.root.exists())

    def test_secrets_permissions_report_typed_code(self):
        Path(self.config['secrets_file']).chmod(0o644)
        with self.assertRaises(DeployError) as error:
            self.deployment().prepare()
        self.assertEqual(error.exception.code, 'E_SECRETS_MODE')
        self.assertFalse(self.root.exists())

    def test_rebuild_and_rollback_leave_external_state_untouched(self):
        state = Path(self.config['state_dir'])
        state.mkdir(mode=0o700)
        (state / 'sessions').mkdir(mode=0o700)
        (state / 'sessions/live.json').write_text('existing live state')
        before = self.state_snapshot(state)
        first = self.deployment()
        self.apply(first)
        self.assertEqual(self.state_snapshot(state), before)
        config = copy.deepcopy(self.config)
        config['budgets']['max_steps'] = 7
        second = self.deployment(config)
        (self.root / 'cook-fail-once').touch()
        with self.assertRaises(DeployError):
            second.prepare()
        second.prepare()  # recursively discard/rebuild only the incomplete build
        self.assertEqual(self.state_snapshot(state), before)
        (self.root / 'fail-health').write_text(str(second.build))
        with self.assertRaisesRegex(DeployError, 'restored'):
            second.activate({})
        self.assertEqual(self.state_snapshot(state), before)
        self.assertEqual(first.current.resolve(), first.build)
        self.assertEqual(json.loads((second.build / 'connectome.lock').read_text())['state'],
                         {'path': str(state)})
        dropin = self.units / 'connectome-example.service.d/90-connectome-env.conf'
        self.assertIn('DATA_DIR=' + str(state), dropin.read_text())
        self.assertIn(str(state), (first.build / 'connectome-example.service').read_text())

    def test_state_is_created_private_and_not_owned_as_an_artifact(self):
        dep = self.deployment()
        self.apply(dep)
        self.assertEqual(stat.S_IMODE(dep.state_dir.stat().st_mode), 0o700)
        self.assertEqual(dep.state_dir.stat().st_uid, os.geteuid())
        self.assertNotIn(str(dep.state_dir), dep.manifest_path.read_text())
        self.assertFalse(dep.state_dir.is_relative_to(dep.root))

    def test_real_shaped_split_layout_lock_accepts_only_exact_external_state(self):
        dep = self.deployment()
        dep.prepare()
        # Match cook's emitted v2 shape: absolute launcher, exported userUnit,
        # explicit split layout and external state.path (no planned units field).
        record = json.loads((ROOT / 'tests/fixtures/locks/v2.json').read_text())
        record['build']['dir'] = str(dep.build)
        record['connectomeHost']['commit'] = self.versions['runtime']['connectome-host']
        record['connectomeHost']['path'] = str(dep.build / 'app')
        record['recipe']['path'] = record['recipePath'] = str(dep.build / 'deployment.json')
        unit = dep.build / 'exported.service'
        unit.write_text('[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=default.target\n')
        record['launch'] = {'kind': 'script', 'script': str(dep.build / 'run.sh'),
                            'userUnit': str(unit)}
        record['state'] = {'path': str(dep.state_dir)}
        record['layout'] = {'schema': 'connectome.host-layout/v1',
                            'runtime': str(dep.build), 'state': str(dep.state_dir),
                            'config': str(dep.root / 'cook-config' / dep.request_id),
                            'credentials': str(dep.root / 'cook-credentials' / dep.request_id),
                            'workspace': str(dep.root / 'workspace'), 'connectorState': {}}
        lockfile = dep.build / 'connectome.lock'
        for value in (str(dep.state_dir) + '/.', str(dep.state_dir.parent / 'other'),
                      str(dep.build / 'data')):
            record['state']['path'] = value
            lockfile.write_text(json.dumps(record))
            before = self.snapshot()
            with self.assertRaisesRegex(DeployError, 'configured state_dir'):
                dep.activate({})
            self.assertEqual(self.snapshot(), before)
        record['state']['path'] = str(dep.state_dir)
        record['launch']['script'] = str(dep.state_dir / 'outside.sh')
        lockfile.write_text(json.dumps(record))
        with self.assertRaisesRegex(DeployError, 'escapes'):
            dep.activate({})
        record['launch']['script'] = str(dep.build / 'run.sh')
        lockfile.write_text(json.dumps(record))
        self.assertTrue(dep.activate({})['changed'])
        self.assertEqual(dep.current.resolve(), dep.build)
        self.assertEqual((self.units / 'exported.service').resolve(),
                         dep.build / 'managed-units/exported.service')

    def test_operator_reapply_previous_recorded_request_without_rebuild_or_state_changes(self):
        first = self.deployment()
        self.apply(first)
        (first.state_dir / 'live.json').write_text('live data')
        before_state = self.state_snapshot(first.state_dir)
        config = copy.deepcopy(self.config)
        config['budgets']['max_steps'] = 7
        second = self.deployment(config)
        self.apply(second)
        before_cook = (self.root / 'cook-calls').read_text()
        before_calls = (self.root / 'systemctl-calls').read_text().splitlines()
        # Operator restores the exact prior private inputs, not a guessed cook ID.
        restored = self.deployment(copy.deepcopy(self.config))
        self.assertFalse(restored.prepare()['changed'])
        self.assertTrue(restored.activate({})['changed'])
        self.assertEqual(restored.current.resolve(), first.build)
        self.assertTrue(second.build.exists())
        self.assertEqual((self.root / 'cook-calls').read_text(), before_cook)
        self.assertEqual(self.state_snapshot(first.state_dir), before_state)
        calls = [json.loads(line) for line in (self.root / 'systemctl-calls').read_text().splitlines()[len(before_calls):]]
        self.assertIn(['restart', 'connectome-example.service'], calls)
        self.assertIn(['is-active', '--quiet', 'connectome-example.service'], calls)

    def test_reused_build_recreates_missing_state_directory_only(self):
        dep = self.deployment()
        self.apply(dep)
        dep.state_dir.rmdir()  # simulate externally missing empty state
        self.assertFalse(dep.prepare()['changed'])
        self.assertEqual(stat.S_IMODE(dep.state_dir.stat().st_mode), 0o700)
        self.assertEqual(len((self.root / 'cook-calls').read_text().splitlines()), 1)

    def test_late_state_symlink_and_wrong_lock_state_fail_closed(self):
        dep = self.deployment()
        dep.prepare()
        dep.state_dir.rmdir()
        dep.state_dir.symlink_to(dep.build)
        before = self.snapshot()
        for operation in (dep.prepare, lambda: dep.activate({})):
            with self.assertRaisesRegex(DeployError, 'state_dir'):
                operation()
            self.assertEqual(self.snapshot(), before)
        dep.state_dir.unlink()
        lockfile = dep.build / 'connectome.lock'
        lock = json.loads(lockfile.read_text())
        lock['state']['path'] = str(dep.build / 'data')
        lockfile.write_text(json.dumps(lock))
        before = self.snapshot()
        with self.assertRaisesRegex(DeployError, 'configured state_dir'):
            dep.activate({})
        self.assertEqual(self.snapshot(), before)

    def test_invalid_state_paths_and_permissions_fail_without_repair(self):
        for value in (None, 'relative', str(self.root), str(self.root / 'builds/live'),
                      str(self.root / 'current/data'), str(self.root.parent)):
            config = copy.deepcopy(self.config)
            config['state_dir'] = value
            with self.subTest(value=value), self.assertRaisesRegex(DeployError, 'state_dir'):
                self.deployment(config)
        target = self.root / 'builds/live'
        target.mkdir(parents=True)
        state = Path(self.config['state_dir'])
        state.symlink_to(target)
        before = self.snapshot()
        with self.assertRaisesRegex(DeployError, 'state_dir'):
            self.deployment()
        self.assertEqual(self.snapshot(), before)
        state.unlink()
        state.mkdir(mode=0o755)
        before = self.snapshot()
        with self.assertRaisesRegex(DeployError, '0700'):
            self.deployment()
        self.assertEqual(self.snapshot(), before)
        state.chmod(0o700)
        with patch('connectome_deploy.os.geteuid', return_value=os.geteuid() + 1), \
                patch('connectome_deploy.pwd.getpwuid', return_value=SimpleNamespace(pw_dir='/example')):
            with self.assertRaisesRegex(DeployError, 'owned by the agent'):
                self.deployment()

    def test_secrets_default_uses_agent_account_home_not_environment(self):
        config = copy.deepcopy(self.config)
        del config['secrets_file']
        with patch('connectome_deploy.pwd.getpwuid', return_value=SimpleNamespace(pw_dir='/example-home')), \
                patch.dict(os.environ, {'HOME': '/unrelated'}):
            dep = self.deployment(config, check=True)
        self.assertEqual(str(dep.secrets_file), '/example-home/.config/connectome-secrets/example.json')

    def test_target_file_lookup_exact_names_and_check_plan(self):
        self.config['required_secrets'] = ['EXAMPLE_SECRET']
        path = Path(self.config['secrets_file'])
        path.write_text(json.dumps({'EXAMPLE_SECRET': 'synthetic-value', 'UNUSED': 'ignored'}))
        dep = self.deployment(check=True)
        before = self.snapshot()
        with patch.object(dep, '_read_secrets', side_effect=AssertionError('no lookup in check')):
            plan = dep.prepare()['plan']
            dep.activate()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(plan, {'state_dir': self.config['state_dir'],
                               'secrets_file': str(path), 'budgets': {'max_steps': 50, 'max_cost': 2.0}})
        dep = self.deployment()
        dep.prepare()
        dep.activate()
        self.assertEqual((dep.build / 'secrets.env').read_text(), 'EXAMPLE_SECRET="synthetic-value"\n')
        path.write_text('{}')
        with self.assertRaisesRegex(DeployError, 'lookup failed'):
            dep.activate()

    def test_secrets_permissions_owner_and_symlinks_fail_closed_before_start(self):
        dep = self.deployment()
        dep.prepare()
        path = dep.secrets_file
        for kind in ('directory-mode', 'file-mode', 'file-link', 'directory-link', 'missing', 'owner'):
            with self.subTest(kind=kind):
                if kind == 'directory-mode': path.parent.chmod(0o755)
                if kind == 'file-mode': path.chmod(0o644)
                if kind == 'file-link':
                    path.rename(path.with_suffix('.saved'))
                    path.symlink_to(path.with_suffix('.saved'))
                if kind == 'directory-link':
                    path.parent.rename(path.parent.with_suffix('.saved'))
                    path.parent.symlink_to(path.parent.with_suffix('.saved'))
                if kind == 'missing': path.rename(path.with_suffix('.saved'))
                before = self.snapshot()
                uid = os.geteuid() + 1 if kind == 'owner' else os.geteuid()
                with patch('connectome_deploy.os.geteuid', return_value=uid), \
                        patch.object(dep, '_validate_state'):
                    for operation in (dep.prepare, lambda: dep.activate({})):
                        with self.assertRaisesRegex(DeployError, 'secrets file check failed'):
                            operation()
                self.assertEqual(self.snapshot(), before)
                if kind == 'directory-mode': path.parent.chmod(0o700)
                if kind == 'file-mode': path.chmod(0o600)
                if kind == 'file-link': path.unlink()
                if kind == 'directory-link':
                    path.parent.unlink()
                    path.parent.with_suffix('.saved').rename(path.parent)
                if kind in ('file-link', 'missing'): path.with_suffix('.saved').rename(path)
        self.assertFalse(dep.current.exists())
        self.assertFalse((self.root / 'systemctl-calls').exists())

    def test_switch_rollback_idempotency_and_exact_secrets(self):
        self.config['required_secrets'] = ['EXAMPLE_SECRET']
        first = self.deployment()
        self.assertTrue(self.apply(first)['changed'])
        self.assertEqual((self.root / 'current').resolve(), first.build)
        env = first.build / 'secrets.env'
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
        self.assertEqual(env.read_text(), 'EXAMPLE_SECRET="synthetic-value"\n')
        before = {p: p.stat().st_mtime_ns for p in (env, first.build / 'connectome.lock')}
        calls = (self.root / 'systemctl-calls').read_text().splitlines()
        self.assertFalse(self.apply(first)['changed'])
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in before})
        after = (self.root / 'systemctl-calls').read_text().splitlines()[len(calls):]
        self.assertEqual([json.loads(line)[0] for line in after], ['is-active', 'show'])
        self.assertEqual(len((self.root / 'cook-calls').read_text().splitlines()), 1)
        second_config = copy.deepcopy(self.config)
        second_config['budgets']['max_cost'] = 3
        second = self.deployment(second_config)
        self.assertTrue(self.apply(second)['changed'])
        self.assertEqual((self.root / 'current').resolve(), second.build)
        self.assertNotEqual(first.build, second.build)
        bad_config = copy.deepcopy(second_config)
        bad_config['budgets']['max_cost'] = 4
        bad = self.deployment(bad_config)
        bad.prepare()
        (self.root / 'fail-health').write_text(str(bad.build))
        with self.assertRaisesRegex(DeployError, 'previous deployment restored'):
            bad.activate(self.provider.get(bad.names))
        self.assertEqual((self.root / 'current').resolve(), second.build)
        self.assertEqual((self.units / 'connectome-example.service').resolve().parent, second.build)
        self.assertEqual(self.provider.requested, ['EXAMPLE_SECRET'] * 4)
        self.assertTrue(first.build.exists())
        self.assertTrue((self.root / 'state').is_dir())

    def test_secret_rotation_restarts_without_rebuilding(self):
        self.config['required_secrets'] = ['EXAMPLE_SECRET']
        dep = self.deployment()
        self.apply(dep)
        self.provider_path.write_text(json.dumps({'EXAMPLE_SECRET': 'rotated-value'}))
        self.assertTrue(self.apply(dep)['changed'])
        self.assertFalse(self.apply(dep)['changed'])
        self.assertIn('rotated-value', (dep.build / 'secrets.env').read_text())
        self.assertEqual(len((self.root / 'cook-calls').read_text().splitlines()), 1)

    def test_failed_cook_preserves_active_and_retry_is_fresh(self):
        first = self.deployment()
        self.apply(first)
        config = copy.deepcopy(self.config)
        config['budgets']['max_steps'] = 7
        second = self.deployment(config)
        (self.root / 'cook-fail-once').touch()
        with self.assertRaises(DeployError):
            second.prepare()
        self.assertTrue((second.build / 'partial').exists())
        entries = json.loads(second.manifest_path.read_text())['entries']
        self.assertIn({'path': str(second.build), 'kind': 'directory'}, entries)
        self.assertFalse((second.build / '.prepared').exists())
        self.assertEqual((self.root / 'current').resolve(), first.build)
        self.assertTrue(self.apply(self.deployment(config))['changed'])
        self.assertFalse((second.build / 'partial').exists())

    def test_unknown_build_directory_is_refused_unchanged(self):
        dep = self.deployment()
        dep.build.mkdir(parents=True)
        (dep.build / 'never-installed.service').write_text('not owned by deployment')
        # Neither a missing marker nor a plausible marker grants ownership.
        for prepared in (False, True):
            if prepared:
                (dep.build / '.prepared').write_text(dep.request_id)
            for check in (False, True):
                before = self.snapshot()
                candidate = self.deployment(check=check)
                for operation in (candidate.prepare, lambda: candidate.activate({})):
                    with self.assertRaisesRegex(DeployError, 'unmanaged build directory'):
                        operation()
                    self.assertEqual(self.snapshot(), before)

    def test_contained_builds_alias_refused_before_cleanup(self):
        dep = self.deployment()
        dep.build.mkdir(parents=True)
        (dep.build / 'never-installed.service').write_text('not owned by deployment')
        original = self.root / 'retained'
        dep.builds.rename(original)
        dep.builds.symlink_to(original, target_is_directory=True)
        before = self.snapshot()
        # Check both an already-constructed deployment and a new instance.
        for candidate in (dep, self.deployment(), self.deployment(check=True)):
            with self.assertRaisesRegex(DeployError, 'symlinked base'):
                candidate.prepare()
            self.assertEqual(self.snapshot(), before)

    def test_owned_builds_alias_refused_before_cleanup(self):
        dep = self.deployment()
        self.root.mkdir()
        (self.root / 'cook-fail-once').touch()
        with self.assertRaises(DeployError):
            dep.prepare()
        original = self.root / 'retained'
        dep.builds.rename(original)
        dep.builds.symlink_to(original, target_is_directory=True)
        before = self.snapshot()
        with self.assertRaisesRegex(DeployError, 'symlinked base'):
            dep.prepare()
        self.assertEqual(self.snapshot(), before)

    def test_unmanaged_unit_is_not_overwritten(self):
        self.units.mkdir()
        unit = self.units / 'connectome-example.service'
        dep = self.deployment()
        dep.prepare()
        unit.write_text('unmanaged')
        with self.assertRaisesRegex(DeployError, 'unmanaged'):
            dep.activate({})
        self.assertEqual(unit.read_text(), 'unmanaged')
        self.assertFalse((self.root / 'current').exists())

    def test_invalid_names_fail_before_any_filesystem_mutation(self):
        bad_names = ['../escaped', '/absolute', '.', '..', '', 'Upper', 'x' * 64,
                     'has_space', 'with/slash', 'with\\backslash', 'line\n']
        for section in ('agent', 'connector'):
            for name in bad_names:
                with self.subTest(section=section, name=name):
                    config = copy.deepcopy(self.config)
                    if section == 'agent':
                        config['name'] = name
                    else:
                        config['connectors'][name] = {'pin_ref': 'connectors.slack'}
                    with self.assertRaisesRegex(DeployError, 'identifier'):
                        self.deployment(config)
                    self.assertFalse(self.root.exists())
                    self.assertFalse(self.units.exists())

    def test_identifier_boundaries_allowed(self):
        for name in ('a', '0', 'a' + '-' * 62):
            config = copy.deepcopy(self.config)
            config['name'] = name
            self.deployment(config, check=True)
        self.assertFalse(self.root.exists())

    def test_module_rejects_before_root_mkdir_or_lock(self):
        # Exercise the actual Ansible boundary, not just the shared constructor.
        for connector in (False, True):
            config = copy.deepcopy(self.config)
            if connector:
                config['connectors']['../escaped'] = {}
            else:
                config['name'] = '../escaped'
            base = Path(self.tmp.name) / 'base'
            escaped = base / config['name']
            module = Mock(params=dict(operation='prepare', root=str(escaped), base=str(base),
                                      unit_dir=str(self.units), config=config, versions=self.versions,
                                      cook_command=self.cook, secrets={}), check_mode=False)
            module.fail_json.side_effect = DeployError('boundary rejected')
            modules = {
                'ansible.module_utils.basic': SimpleNamespace(AnsibleModule=Mock(return_value=module)),
                'ansible.module_utils.connectome_deploy': sys.modules['connectome_deploy'],
                'ansible.module_utils.connectome_errors': sys.modules['connectome_errors'],
            }
            spec = importlib.util.spec_from_file_location(
                'release_boundary', ROOT / 'roles/connectome_agent/library/connectome_release.py')
            boundary = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, modules):
                spec.loader.exec_module(boundary)
                with self.assertRaisesRegex(DeployError, 'boundary rejected'):
                    boundary.main()
            self.assertFalse(base.exists())
            self.assertFalse((Path(self.tmp.name) / 'escaped').exists())
            module.exit_json.assert_not_called()

    def test_explicit_base_contains_root(self):
        base = Path(self.tmp.name) / 'base'
        with self.assertRaisesRegex(DeployError, 'escapes'):
            Deployment(base / '../escaped', self.units, self.config, self.versions,
                       self.cook, base=base)
        self.assertFalse(base.exists())

    def test_symlinked_bases_and_derived_escapes_refused(self):
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        self.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(DeployError, 'symlinked base'):
            self.deployment()
        # A symlinked ancestor is refused too, even if the agent root is absent.
        with self.assertRaisesRegex(DeployError, 'symlinked base'):
            Deployment(self.root / 'child', self.units, self.config, self.versions, self.cook)
        self.root.unlink()
        self.root.mkdir()
        for name in ('builds', 'state', 'current', '.deploy-lock'):
            with self.subTest(path=name):
                path = self.root / name
                path.symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(DeployError, 'escapes'):
                    self.deployment()
                path.unlink()
        self.assertEqual(list(outside.iterdir()), [])

    def test_late_symlink_escape_refused_before_prepare(self):
        dep = self.deployment()
        self.root.mkdir()
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        (self.root / 'builds').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(DeployError, 'escapes'):
            dep.prepare()
        self.assertEqual(list(outside.iterdir()), [])

    def test_retired_unmanaged_unit_is_not_disabled_or_deleted_on_rename(self):
        first = self.deployment()
        self.apply(first)
        old_unit = self.units / 'connectome-example.service'
        config = copy.deepcopy(self.config)
        config['name'] = 'renamed'
        second = self.deployment(config)
        second.prepare()
        old_unit.unlink()
        old_unit.write_text('unmanaged replacement')
        calls = (self.root / 'systemctl-calls').read_text()
        dropin = self.units / 'connectome-example.service.d/90-connectome-env.conf'
        before = dropin.read_text()
        with self.assertRaisesRegex(DeployError, 'unmanaged'):
            second.activate({})
        self.assertEqual(old_unit.read_text(), 'unmanaged replacement')
        self.assertEqual(dropin.read_text(), before)
        self.assertEqual((self.root / 'systemctl-calls').read_text(), calls)
        self.assertEqual((self.root / 'current').resolve(), first.build)
        self.assertFalse((second.build / 'secrets.env').exists())
        self.assertFalse((self.units / 'connectome-renamed.service').exists())

    def test_managed_retired_unit_is_removed_on_rename(self):
        self.apply(self.deployment())
        config = copy.deepcopy(self.config)
        config['name'] = 'renamed'
        self.apply(self.deployment(config))
        self.assertFalse((self.units / 'connectome-example.service').exists())
        self.assertFalse((self.units / 'connectome-example.service.d').exists())
        self.assertTrue((self.units / 'connectome-renamed.service').is_symlink())

    def test_symlinked_dropin_escape_refused(self):
        dep = self.deployment()
        dep.prepare()
        self.units.mkdir()
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        (self.units / 'connectome-example.service.d').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(DeployError, 'symlinked drop-in directory'):
            dep.activate({})
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.root / 'current').exists())

    def snapshot(self):
        # Include links verbatim, file bytes and directory modes; no mutations.
        return {str(path.relative_to(self.tmp.name)): (
            ('link', os.readlink(path)) if path.is_symlink() else
            ('directory', stat.S_IMODE(path.stat().st_mode)) if path.is_dir() else
            ('file', path.read_bytes(), stat.S_IMODE(path.stat().st_mode)))
            for path in Path(self.tmp.name).rglob('*')}

    def test_retired_link_retargeted_to_never_installed_build_file_refused(self):
        first = self.deployment()
        self.apply(first)
        never_installed = first.build / 'never-installed.service'
        never_installed.write_text('[Service]\nExecStart=/bin/true\n')
        unit = self.units / 'connectome-example.service'
        config = copy.deepcopy(self.config)
        config['name'] = 'renamed'
        second = self.deployment(config)
        second.prepare()
        unit.unlink()
        unit.symlink_to(never_installed)
        before = self.snapshot()
        with self.assertRaisesRegex(DeployError, 'ownership mismatch'):
            second.activate({})
        self.assertEqual(self.snapshot(), before)

    def test_contained_dropin_directory_alias_over_other_unit_refused(self):
        dep = self.deployment()
        dep.prepare()
        self.units.mkdir()
        other = self.units / 'other.service.d'
        other.mkdir()
        (other / '90-connectome-env.conf').write_text('unmanaged env drop-in')
        (self.units / 'connectome-example.service.d').symlink_to(other, target_is_directory=True)
        before = self.snapshot()
        with self.assertRaisesRegex(DeployError, 'symlinked drop-in directory'):
            dep.activate({})
        self.assertEqual(self.snapshot(), before)

    def test_manifest_records_exact_installed_paths_and_survives_rollback(self):
        dep = self.deployment()
        self.apply(dep)
        manifest = dep.state / 'ownership.json'
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
        data = json.loads(manifest.read_text())
        entries = {Path(entry['path']): entry for entry in data['entries']}
        unit = 'connectome-example.service'
        self.assertEqual(set(entries), {
            dep.build, dep.current, dep.build / unit, self.units / unit,
            self.units / (unit + '.d') / '90-connectome-env.conf',
            self.units / 'default.target.wants' / unit})
        for path, entry in entries.items():
            if entry['kind'] == 'directory':
                self.assertTrue(path.is_dir())
                self.assertFalse(path.is_symlink())
            elif entry['kind'] == 'link':
                self.assertEqual(os.readlink(path), entry['target'])
            else:
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry['sha256'])
        before = manifest.read_bytes(), manifest.stat().st_mtime_ns
        self.assertFalse(self.apply(self.deployment())['changed'])
        self.assertEqual((manifest.read_bytes(), manifest.stat().st_mtime_ns), before)
        bad_config = copy.deepcopy(self.config)
        bad_config['name'] = 'renamed'
        bad = self.deployment(bad_config)
        bad.prepare()
        (self.root / 'fail-health').write_text(str(bad.build))
        with self.assertRaisesRegex(DeployError, 'restored'):
            bad.activate({})
        # A new instance loads and validates the persisted post-rollback state.
        self.assertFalse(self.apply(self.deployment())['changed'])
        self.assertEqual(dep.current.resolve(), dep.build)

    def test_preexisting_units_links_dropins_and_enable_links_are_not_adopted(self):
        dep = self.deployment()
        dep.prepare()
        self.units.mkdir()
        paths = [self.units / 'connectome-example.service',
                 self.units / 'connectome-example.service.d/90-connectome-env.conf',
                 self.units / 'default.target.wants/connectome-example.service',
                 dep.build / 'connectome-example.service']
        for path in paths:
            for kind in ('file', 'link'):
                with self.subTest(path=path.name, kind=kind):
                    path.parent.mkdir(exist_ok=True)
                    if kind == 'file':
                        path.write_text('not installed by this role')
                    else:
                        path.symlink_to(dep.build / 'launch.sh')
                    before = self.snapshot()
                    with self.assertRaisesRegex(DeployError, 'unmanaged'):
                        dep.activate({})
                    self.assertEqual(self.snapshot(), before)
                    path.unlink()

    def test_manifest_drift_never_repaired_or_disabled_even_in_check_mode(self):
        dep = self.deployment()
        self.apply(dep)
        paths = [self.units / 'connectome-example.service.d/90-connectome-env.conf',
                 self.units / 'default.target.wants/connectome-example.service',
                 dep.build / 'connectome-example.service', dep.current]
        for path in paths:
            old_link = os.readlink(path) if path.is_symlink() else None
            old_content = None if old_link else path.read_bytes()
            for kind in ('modified', 'missing'):
                if kind == 'missing' and path.is_relative_to(self.units) and path.is_symlink():
                    continue
                with self.subTest(path=path.name, kind=kind):
                    path.unlink()
                    if kind == 'modified':
                        path.write_text('modified')
                    before = self.snapshot()
                    for check in (False, True):
                        with self.assertRaisesRegex(DeployError, 'ownership mismatch'):
                            self.deployment(check=check).activate({})
                        self.assertEqual(self.snapshot(), before)
                    if path.exists():
                        path.unlink()
                    if old_link:
                        path.symlink_to(old_link)
                    else:
                        path.write_bytes(old_content)

    def test_symlinked_unit_and_enable_directories_refused(self):
        dep = self.deployment()
        dep.prepare()
        other = Path(self.tmp.name) / 'other'
        other.mkdir()
        self.units.symlink_to(other, target_is_directory=True)
        before = self.snapshot()
        with self.assertRaisesRegex(DeployError, 'symlinked base'):
            dep.activate({})
        self.assertEqual(self.snapshot(), before)
        self.units.unlink()
        self.units.mkdir()
        (self.units / 'default.target.wants').symlink_to(other, target_is_directory=True)
        before = self.snapshot()
        with self.assertRaisesRegex(DeployError, 'symlinked artifact parent'):
            dep.activate({})
        self.assertEqual(self.snapshot(), before)

    def test_unowned_alias_and_fixed_temporary_name_are_untouched(self):
        dep = self.deployment()
        self.apply(dep)
        alias = self.units / 'external-alias.service'
        alias.symlink_to(dep.build / 'connectome-example.service')
        temporary = self.units / 'connectome-example.service.next'
        temporary.write_text('not our temporary file')
        config = copy.deepcopy(self.config)
        config['name'] = 'renamed'
        self.apply(self.deployment(config))
        self.assertEqual(os.readlink(alias), str(dep.build / 'connectome-example.service'))
        self.assertEqual(temporary.read_text(), 'not our temporary file')

    def test_planned_units_digest_and_health_contract(self):
        dep = self.deployment()
        dep.prepare()
        import hashlib
        source = dep.build / 'example.service'
        source.write_text('[Service]\nExecStart=/bin/true\n')
        lockfile = dep.build / 'connectome.lock'
        data = json.loads(lockfile.read_text())
        data['units'] = [{'path': 'example.service',
                          'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}]
        data['health'] = {'command': [sys.executable, '-c', 'pass']}
        lockfile.write_text(json.dumps(data))
        self.assertTrue(dep.activate({})['changed'])
        installed = dep.build / 'managed-units/example.service'
        self.assertEqual((self.units / 'example.service').resolve(), installed)
        self.assertEqual(installed.read_bytes(), source.read_bytes())
        entries = json.loads(dep.manifest_path.read_text())['entries']
        self.assertIn({'path': str(installed), 'kind': 'file',
                       'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}, entries)
        source.write_text('drift')
        with self.assertRaisesRegex(DeployError, 'digest'):
            dep.activate({})

    def test_invalid_parent_files_and_corrupt_manifest_refused_before_mutation(self):
        dep = self.deployment()
        dep.prepare()
        self.units.mkdir()
        for parent in (self.units / 'connectome-example.service.d',
                       self.units / 'default.target.wants'):
            parent.write_text('not a directory')
            before = self.snapshot()
            with self.assertRaisesRegex(DeployError, 'non-directory artifact parent'):
                dep.activate({})
            self.assertEqual(self.snapshot(), before)
            parent.unlink()
        self.apply(dep)
        original = dep.manifest_path.read_bytes()
        for content in ('not-json', '[]', '{"version":1,"entries":[{}]}'):
            dep.manifest_path.write_text(content)
            before = self.snapshot()
            for operation in (lambda: dep.activate({}), dep.prepare):
                with self.assertRaisesRegex(DeployError, 'invalid ownership manifest'):
                    operation()
                self.assertEqual(self.snapshot(), before)
        dep.manifest_path.write_bytes(original)

    def test_prepare_cannot_delete_retained_manifest_owned_unit_files(self):
        first = self.deployment()
        self.apply(first)
        config = copy.deepcopy(self.config)
        config['name'] = 'renamed'
        self.apply(self.deployment(config))
        (first.build / '.prepared').unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(DeployError, 'manifest-owned unit files'):
            first.prepare()
        self.assertEqual(self.snapshot(), before)

    def test_planned_install_links_and_unsupported_directives(self):
        dep = self.deployment()
        dep.prepare()
        source = dep.build / 'example.service'
        body = ('[Service]\nExecStart=/bin/true\n[Install]\n'
                'WantedBy=unused.target\nWantedBy=\nWantedBy=default.target\n'
                'RequiredBy=other.target\nAlias=example-alias.service\n')
        source.write_text(body)
        lockfile = dep.build / 'connectome.lock'
        data = json.loads(lockfile.read_text())
        data['units'] = [{'path': source.name, 'sha256': hashlib.sha256(body.encode()).hexdigest()}]
        lockfile.write_text(json.dumps(data))
        dep.activate({})
        installed = dep.build / 'managed-units/example.service'
        for relative in ('default.target.wants/example.service',
                         'other.target.requires/example.service', 'example-alias.service'):
            self.assertEqual((self.units / relative).resolve(), installed)
        self.assertFalse((self.units / 'unused.target.wants').exists())
        source.write_text(body + 'Also=other.service\n')
        data['units'][0]['sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        lockfile.write_text(json.dumps(data))
        before = self.snapshot()
        with self.assertRaisesRegex(DeployError, 'unsupported unit Install directive'):
            dep.activate({})
        self.assertEqual(self.snapshot(), before)

    def test_optional_health_command_rolls_back(self):
        first = self.deployment()
        self.apply(first)
        bad = copy.deepcopy(self.config)
        bad['health'] = {'command': [sys.executable, '-c', 'raise SystemExit(1)']}
        with self.assertRaisesRegex(DeployError, 'restored'):
            self.apply(self.deployment(bad))
        self.assertEqual((self.root / 'current').resolve(), first.build)

    def test_failed_first_activation_leaves_no_current_or_units(self):
        dep = self.deployment()
        dep.prepare()
        (self.root / 'fail-health').write_text(str(dep.build))
        with self.assertRaises(DeployError):
            dep.activate({})
        self.assertFalse((self.root / 'current').exists())
        self.assertFalse((self.units / 'connectome-example.service').exists())

    def test_check_mode_has_no_writes_or_provider_calls(self):
        dep = self.deployment(check=True)
        self.assertTrue(dep.prepare()['changed'])
        self.assertTrue(dep.activate({})['changed'])
        self.assertFalse(self.root.exists())
        self.assertFalse(self.units.exists())
        self.assertEqual(self.provider.requested, [])

    def test_check_mode_existing_is_unchanged(self):
        self.apply(self.deployment())
        dep = self.deployment(check=True)
        self.assertFalse(dep.prepare()['changed'])
        self.assertFalse(dep.activate({})['changed'])

    def test_enabled_unpinned_connector_fails_closed(self):
        config = copy.deepcopy(self.config)
        config['connectors']['terminal-sessions-mcp'] = {
            'pin_ref': 'connectors.terminal-sessions-mcp'}
        with self.assertRaisesRegex(DeployError, 'unresolved'):
            self.deployment(config)
        self.assertFalse(self.root.exists())

    def test_no_secret_values_in_spec_or_lock(self):
        self.config['required_secrets'] = ['EXAMPLE_SECRET']
        dep = self.deployment()
        self.apply(dep)
        for name in ('deployment.json', 'connectome.lock', '.prepared'):
            self.assertNotIn('synthetic-value', (dep.build / name).read_text())
        with self.assertRaises(DeployError):
            dep.activate({'EXAMPLE_SECRET': 'test', 'EXTRA': 'test'})
        with self.assertRaises(DeployError):
            dep.activate({'EXAMPLE_SECRET': 'line\nbreak'})

    def test_unknown_lock_and_secret_value_records_refused(self):
        dep = self.deployment()
        dep.prepare()
        lockfile = dep.build / 'connectome.lock'
        data = json.loads(lockfile.read_text())
        data['version'] = 1
        lockfile.write_text(json.dumps(data))
        with self.assertRaises(DeployError):
            dep.activate({})
        data['version'] = 2
        data['requirements'] = [{'name': 'x', 'envName': 'X', 'origin': 'test', 'value': 'bad'}]
        lockfile.write_text(json.dumps(data))
        with self.assertRaises(DeployError):
            dep.activate({})

    def test_artifact_escape_refused(self):
        dep = self.deployment()
        dep.prepare()
        lockfile = dep.build / 'connectome.lock'
        data = json.loads(lockfile.read_text())
        data['launch']['script'] = '../escape.sh'
        lockfile.write_text(json.dumps(data))
        with self.assertRaisesRegex(DeployError, 'escapes'):
            dep.activate({})

    def test_rollback_across_runtime_pin_change(self):
        first = self.deployment()
        self.apply(first)
        pins = copy.deepcopy(self.versions)
        pins['runtime']['connectome-host'] = '1' * 40
        bad = self.deployment(versions=pins)
        bad.prepare()
        (self.root / 'fail-health').write_text(str(bad.build))
        with self.assertRaisesRegex(DeployError, 'restored'):
            bad.activate({})
        self.assertEqual((self.root / 'current').resolve(), first.build)


if __name__ == '__main__':
    unittest.main()
