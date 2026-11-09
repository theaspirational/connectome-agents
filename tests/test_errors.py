"""Closed diagnostics at the actual CLI, subprocess, and Ansible boundaries."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'roles/connectome_agent/module_utils'))
import connectome_cook as adapter
import connectome_deploy as deploy
import connectome_errors as errors
from connectome_lock import LockError

SENTINEL = 'synthetic-private-argument'


class ErrorTests(unittest.TestCase):
    def cli(self, failure):
        stderr, stdout = io.StringIO(), io.StringIO()
        argv = ['adapter'] + [item for flag in ('spec', 'out', 'state', 'runtime', 'entrypoint', 'bun-path', 'node-path')
                              for item in ('--' + flag, SENTINEL)]
        with patch.object(sys, 'argv', argv), patch.object(adapter, 'adapt', side_effect=failure), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as status:
                adapter.main()
        self.assertEqual(status.exception.code, 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertNotIn(SENTINEL, stderr.getvalue())
        return json.loads(stderr.getvalue())

    def test_each_closed_code_survives_cli_and_deployment_command(self):
        # Every public code is checked through the real subprocess boundary;
        # private internal context, stderr and argv must never reach Ansible.
        for code in errors.MESSAGES:
            with self.subTest(code=code):
                expected = errors.diagnostic(code)
                envelope = self.cli(errors.SafeError(SENTINEL, code))
                self.assertEqual(envelope, expected)
                program = 'import sys; print(sys.argv[1], file=sys.stderr); print(sys.argv[2]); sys.exit(1)'
                with self.assertRaises(deploy.DeployError) as failure:
                    deploy.run([sys.executable, '-c', program, json.dumps(envelope), SENTINEL], adapter=True)
                self.assertEqual(failure.exception.public(), expected)
                self.assertNotIn(SENTINEL, str(failure.exception))

    def test_cli_lock_and_unexpected_exception_fallbacks(self):
        self.assertEqual(self.cli(LockError(SENTINEL)), errors.diagnostic('E_LOCK_INVALID'))
        self.assertEqual(self.cli(RuntimeError(SENTINEL)), errors.diagnostic('E_ADAPTER_INPUT'))
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/cook-adapter.py'), '--unknown', SENTINEL],
                                 capture_output=True, check=False)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(process.stderr), errors.diagnostic('E_ADAPTER_INPUT'))
        self.assertNotIn(SENTINEL.encode(), process.stderr + process.stdout)

    def test_every_reviewed_cook_code_is_preserved_without_raw_output(self):
        for code in sorted(errors.COOK_CODES):
            with self.subTest(code=code):
                program = 'import sys; print(sys.argv[1], file=sys.stderr); print(sys.argv[2]); sys.exit(1)'
                with self.assertRaises(adapter.AdapterError) as failure:
                    adapter.command([sys.executable, '-c', program, code + ': ' + SENTINEL, SENTINEL],
                                    flag_indices=(1,))
                expected = errors.diagnostic('E_COOK_FAILED', code)
                self.assertEqual(failure.exception.public(), expected)
                self.assertEqual(self.cli(failure.exception), expected)
                envelope = json.dumps(expected)
                with self.assertRaises(deploy.DeployError) as forwarded:
                    deploy.run([sys.executable, '-c', program, envelope, SENTINEL], adapter=True)
                self.assertEqual(forwarded.exception.public(), expected)
                self.assertNotIn(SENTINEL, str(forwarded.exception))

    def test_cook_code_after_large_output_or_chunk_boundary(self):
        for prefix in (b'x' * 65530 + b'\n', b'x' * 200000 + b'\n'):
            output = prefix + b'E_HOST_LAYOUT: ' + SENTINEL.encode()
            self.assertEqual(errors.cook_code_from_stream(io.BytesIO(output)), 'E_HOST_LAYOUT')

    def test_untyped_unknown_or_forged_output_is_redacted(self):
        safe = errors.diagnostic('E_COOK_FAILED')
        cases = [SENTINEL, 'E_UNKNOWN_' + SENTINEL + ': private', '{}', '[]',
                 json.dumps(dict(errors.diagnostic('E_ARCHIVE_GITHEAD'), msg=SENTINEL)),
                 json.dumps(dict(safe, extra=SENTINEL)),
                 json.dumps(dict(safe, cook_error_code=SENTINEL)),
                 json.dumps(safe) + '\n' + SENTINEL,
                 SENTINEL + '\n' + json.dumps(safe)]
        for output in cases:
            with self.subTest(output=output):
                self.assertEqual(errors.adapter_diagnostic(output.encode()), safe)
        self.assertIsNone(errors.cook_code(('E_UNKNOWN_' + SENTINEL + ': private').encode()))
        with self.assertRaises(deploy.DeployError) as failure:
            deploy.run([sys.executable, '-c', 'import sys; print(sys.argv[1], file=sys.stderr); sys.exit(1)', SENTINEL],
                       adapter=True)
        self.assertEqual(failure.exception.public(), safe)
        self.assertNotIn(SENTINEL, str(failure.exception))
        with patch.object(adapter.subprocess, 'run', side_effect=OSError(SENTINEL)):
            with self.assertRaises(adapter.AdapterError) as failure:
                adapter.command(['/missing/' + SENTINEL])
        self.assertEqual(failure.exception.public(), safe)
        self.assertNotIn(SENTINEL, str(failure.exception))

    def test_ansible_boundary_displays_each_code_and_fixed_message(self):
        class Failed(Exception):
            pass
        failures = [(errors.SafeError(SENTINEL, code), errors.diagnostic(code)) for code in errors.MESSAGES]
        ancestor = deploy.AncestorWritable('/example/ancestor')
        failures.append((ancestor, dict(errors.diagnostic('E_ANCESTOR_WRITABLE'), ancestor_path='/example/ancestor')))
        for failure, expected in failures:
            with self.subTest(code=failure.code):
                module = Mock(params={'operation': 'prepare', 'secrets': None}, check_mode=True)
                module.fail_json.side_effect = Failed()
                engine = Mock()
                engine.prepare.side_effect = failure
                modules = {
                    'ansible.module_utils.basic': SimpleNamespace(AnsibleModule=Mock(return_value=module)),
                    'ansible.module_utils.connectome_deploy': deploy,
                    'ansible.module_utils.connectome_errors': errors,
                }
                spec = importlib.util.spec_from_file_location(
                    'release_errors', ROOT / 'roles/connectome_agent/library/connectome_release.py')
                boundary = importlib.util.module_from_spec(spec)
                with patch.dict(sys.modules, modules):
                    spec.loader.exec_module(boundary)
                    with patch.object(boundary, 'Deployment', return_value=engine), self.assertRaises(Failed):
                        boundary.main()
                module.fail_json.assert_called_once_with(invocation={}, exception=None, **expected)
                module.exit_json.assert_not_called()
