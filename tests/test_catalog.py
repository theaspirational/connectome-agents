"""Catalog closure tests use local synthetic payloads, never upstream releases."""
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

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'roles/connectome_agent/module_utils'))
import connectome_catalog as catalog
import connectome_cook as cook
from connectome_config import merge_layers, public_catalog
from connectome_deploy import Deployment, resolve_pins
from connectome_archive import ArchiveError


def archive(path, files):
    with tarfile.open(path, 'w:gz') as stream:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), 0o755
            stream.addfile(member, io.BytesIO(data))
    return catalog.sha256(path)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        mask = os.umask(0o022)
        self.addCleanup(os.umask, mask)
        (ROOT / '.cache').mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name)
        self.root = self.work / 'agent'
        self.build = self.root / 'builds/request'
        self.build.mkdir(parents=True)
        (self.root / 'inputs').mkdir()
        self.config = {'name': 'example-agent', 'agent': {'systemPrompt': 'Example'},
                       'state_dir': str(self.work / 'state'), 'modules': {},
                       'cook': {'host_repo': str(self.root / 'inputs/host'), 'host_dependencies': [],
                                'connector_state': {}}, 'connectors': {}, 'catalog': {}}
        self.versions = {'runtime': {'cook': cook.COOK_COMMIT, 'connectome-host': 'c' * 40,
                                    'agent-framework': 'd' * 40}, 'artifacts': {}, 'connectors': {},
                         'platform': {'/bin/sh': catalog.sha256('/bin/sh')}}
        # Python executables stand in for Node/Bun. Not a runtime qualification.
        binary = Path(sys.executable).read_bytes()
        for name in ('node', 'bun'):
            self.add(name, {'kind': 'runtime', 'requires': [], 'exports': {name: 'bin/' + name}},
                     {'bin/' + name: binary}, version='24.1.0' if name == 'node' else '1.0.0')
        self.add('extra', {'repo': 'https://example.invalid/connector.git',
                          'build': [['{artifact:node}/bin/node', 'build.py']],
                          'settings': {'command': '{artifact:node}/bin/node',
                                       'args': ['{artifact:extra}/server.py']},
                          'exports': {'server': 'server.py'}},
                 {'build.py': b'from pathlib import Path\nPath("server.py").write_text("print(42)\\n")\n'})
        self.add('readiness', {'kind': 'preflight', 'requires': ['node'],
                              'run': ['{artifact:node}/bin/node', '{artifact:readiness}/check.py']},
                 {'check.py': b'import os,sys\nsys.exit(0 if os.getenv("READY") == "yes" else 1)\n'})

    def add(self, name, descriptor, files, version='1.0.0'):
        connector = descriptor.get('kind', 'connector') == 'connector'
        descriptor['pin_ref'] = ('connectors.' if connector else 'artifacts.') + name
        self.config['connectors' if connector else 'catalog'][name] = descriptor
        path = self.root / 'inputs' / (name + '.tar.gz')
        pin = {'archive': 'inputs/' + path.name, 'sha256': archive(path, files),
               'version': version, 'descriptor_sha256': catalog.descriptor_digest(descriptor)}
        if connector:
            pin['commit'] = 'e' * 40
        self.versions['connectors' if connector else 'artifacts'][name] = pin

    def spec(self):
        return {'config': self.config, 'pins': resolve_pins(self.config, self.versions)}

    def install(self):
        spec = self.spec()
        recipe, _ = cook.recipe_from_spec(spec, build_dir=self.build)
        (self.build / 'cook-recipe.json').write_text(json.dumps(recipe))
        catalog.install(self.config, spec['pins'], self.build)
        return spec, recipe

    def test_compile_build_lock_and_revalidate_without_input_archives(self):
        spec, recipe = self.install()
        server = recipe['mcpServers']['extra']
        self.assertEqual(server['source'], {'external': True})
        self.assertEqual(server['command'], str(self.build / 'catalog/node/bin/node'))
        self.assertEqual((self.build / 'catalog/extra/server.py').read_text(), 'print(42)\n')
        shutil.rmtree(self.root / 'inputs')
        catalog.verify(self.config, spec['pins'], self.build)
        (self.build / 'catalog/extra/server.py').write_text('changed')
        with self.assertRaises(ArchiveError):
            catalog.verify(self.config, spec['pins'], self.build)

    def test_preflight_execution_and_unit_are_selected_only(self):
        spec, _ = self.install()
        unit = catalog.preflight_unit(self.config, self.build)
        self.assertEqual(unit.count('ExecStartPre='), 1)
        argv = catalog.preflights(self.config, self.build)[0]
        self.assertEqual(subprocess.run(argv, env={'READY': 'yes'}).returncode, 0)
        self.assertNotEqual(subprocess.run(argv, env={}).returncode, 0)
        config = deepcopy(self.config)
        del config['catalog']['readiness']
        self.assertEqual(catalog.preflight_unit(config, self.build), '')

    def test_placeholder_and_zero_pins_fail_before_io(self):
        for section, name in (('artifacts', 'node'), ('connectors', 'extra')):
            for field in ('sha256', 'descriptor_sha256', 'version'):
                for bad in ('PLACEHOLDER', '0' * 64):
                    versions = deepcopy(self.versions)
                    versions[section][name][field] = bad
                    with self.subTest(section=section, field=field, bad=bad), self.assertRaises(ArchiveError):
                        resolve_pins(self.config, versions)
        versions = deepcopy(self.versions)
        versions['connectors']['extra']['commit'] = '0' * 40
        with self.assertRaises(ArchiveError):
            resolve_pins(self.config, versions)

    def test_descriptor_changes_require_new_digest(self):
        self.config['connectors']['extra']['build'][0].append('extra')
        with self.assertRaises(ArchiveError):
            self.spec()

    def test_dependencies_cycles_and_node_major_fail_closed(self):
        for change in ('missing', 'cycle', 'major'):
            config, versions = deepcopy(self.config), deepcopy(self.versions)
            if change == 'missing':
                del config['catalog']['node']
            elif change == 'cycle':
                config['catalog']['node']['requires'] = ['bun']
                config['catalog']['bun']['requires'] = ['node']
                for name in ('node', 'bun'):
                    versions['artifacts'][name]['descriptor_sha256'] = catalog.descriptor_digest(config['catalog'][name])
            else:
                versions['artifacts']['node']['version'] = '22.0.0'
            with self.subTest(change=change), self.assertRaises(ArchiveError):
                resolve_pins(config, versions)

    def test_digest_mismatch_and_archive_escape_links_duplicates_refused(self):
        pin = self.versions['artifacts']['node']
        path = self.root / pin['archive']
        path.write_bytes(b'not the pinned archive')
        with self.assertRaises(ArchiveError):
            self.install()
        for label in ('escape', 'symlink', 'hardlink', 'duplicate'):
            target = self.work / (label + '.tgz')
            with tarfile.open(target, 'w:gz') as stream:
                member = tarfile.TarInfo('../outside' if label == 'escape' else 'file')
                if label in ('symlink', 'hardlink'):
                    member.type = tarfile.SYMTYPE if label == 'symlink' else tarfile.LNKTYPE
                    member.linkname = '/outside/file'
                stream.addfile(member)
                if label == 'duplicate':
                    stream.addfile(member)
            with self.subTest(label=label), self.assertRaises(ArchiveError):
                catalog.extract(target, self.work / label, catalog.sha256(target))

    def test_hidden_paths_in_settings_args_env_and_generated_launchers(self):
        for value in ('/opt/other-agent/tool', '--config=/opt/other-agent/file',
                      'PATH=/opt/other-agent/bin:/usr/bin', 'exec /opt/other-agent/node script.js',
                      str(self.root / '../outside/file'), 'file:///opt/other-agent/file', 'file://other-agent/file'):
            with self.subTest(value=value), self.assertRaises(ArchiveError):
                catalog.audit({'nested': [value]}, self.root, self.work / 'state', {})
        spec, _ = self.install()
        (self.root / 'alias').symlink_to(self.work)
        with self.assertRaises(ArchiveError):
            catalog.audit(str(self.root / 'alias/file'), self.root, self.work / 'state', {})
        lock = {'launch': {'script': 'run.sh'}, 'recipe': {'sha256': catalog.sha256(self.build / 'cook-recipe.json')},
                'components': [], 'localExtensions': [],
                'external': [{'name': 'extra', 'command': catalog.runtime(self.config, self.build, 'node')}],
                'engines': {name: {'path': catalog.runtime(self.config, self.build, name),
                                  'realPath': catalog.runtime(self.config, self.build, name),
                                  'version': spec['pins']['artifacts'][name]['version']} for name in ('node', 'bun')}}
        (self.build / 'run.sh').write_text('#!/bin/sh\nexit 0\n')
        catalog.verify(self.config, spec['pins'], self.build, lock)
        (self.build / 'run.sh').write_text('#!/bin/sh\nexec /opt/other-agent/node\n')
        with self.assertRaises(ArchiveError):
            catalog.verify(self.config, spec['pins'], self.build, lock)

    def test_input_archives_and_checkout_must_belong_to_agent(self):
        self.config['cook']['host_repo'] = '/opt/other-agent/host'
        with self.assertRaises(ArchiveError):
            catalog.input_paths(self.config, self.root)

    def test_public_catalog_selection_and_placeholder_supply(self):
        descriptors = {}
        for path in (ROOT / 'connectors').glob('*.yml'):
            descriptors.update(yaml.safe_load(path.read_text()))
        baseline = public_catalog({}, descriptors)
        selected = merge_layers([baseline, {}, {}, {'catalog': {'node': {'enabled': True},
                                  'bun': {'enabled': True}}, 'connectors': None}])
        self.assertEqual(set(selected['catalog']), {'node', 'bun'})
        with self.assertRaises(ArchiveError):
            resolve_pins(selected, yaml.safe_load((ROOT / 'versions.yml').read_text()))
        bridge = merge_layers([baseline, {}, {}, {'connectors': None}])
        self.assertNotIn('catalog', bridge)

    def test_role_prepare_adapter_build_lock_reuse_and_preflight_dropin(self):
        import connectome_deploy as deploy
        secrets = self.work / 'secrets'
        secrets.mkdir(mode=0o700)
        path = secrets / 'example.json'
        path.write_text('{}')
        path.chmod(0o600)
        self.config['secrets_file'] = str(path)
        entrypoint = self.root / 'inputs/cook/bin/cook'
        entrypoint.parent.mkdir(parents=True)
        entrypoint.write_bytes((ROOT / 'tests/fixtures/catalog-cook.py').read_bytes())
        command = [sys.executable, str(ROOT / 'scripts/cook-adapter.py'),
                   '--spec', '{recipe}', '--out', '{build_dir}', '--state', '{state_dir}',
                   '--entrypoint', str(entrypoint), '--runtime', sys.executable,
                   '--bun-path', sys.executable, '--node-path', sys.executable]
        manager = [sys.executable, str(ROOT / 'tests/fixtures/systemctl.py'), str(self.root)]
        dep = Deployment(self.root, self.work / 'units', self.config, self.versions, command, manager)
        original_run = deploy.run
        def invoke(argv, **kwargs):
            if kwargs.get('adapter'):
                options = {argv[i]: argv[i + 1] for i in range(2, len(argv), 2)}
                with patch.object(cook, 'verify_cook'):
                    cook.adapt(options['--spec'], options['--out'], options['--state'],
                               options['--runtime'], options['--entrypoint'], options['--bun-path'], options['--node-path'])
            else:
                return original_run(argv, **kwargs)
        with patch.object(deploy, 'run', side_effect=invoke):
            prepared = dep.prepare()
            self.assertTrue(prepared['changed'])
            lock = dep._lock()
            self.assertEqual(lock['external'][0]['command'], str(dep.build / 'catalog/node/bin/node'))
            artifacts = dep._activation_plan()[-1]
            body = artifacts[dep._dropin('connectome-example-agent.service')][1]
            self.assertIn('ExecStartPre=', body)
            self.assertIn(str(dep.build / 'catalog/readiness/check.py'), body)
            self.assertFalse(dep.prepare()['changed'])
            (dep.build / 'catalog/extra/server.py').write_text('modified')
            with self.assertRaises(ArchiveError):
                dep.prepare()

    def test_recipe_file_bridge_ignores_catalog_and_matches_base_golden(self):
        # Compare against the task's public baseline, not a newly generated golden.
        baseline = self.work / 'baseline'
        baseline.mkdir()
        prefix = 'roles/connectome_agent/module_utils/'
        revision = '0ec19aaeee84c09d3bd3dda06eaf2300ebbde4de'
        paths = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', revision, prefix], cwd=ROOT).decode().splitlines()
        for path in paths:
            (baseline / Path(path).name).write_bytes(subprocess.check_output(['git', 'show', revision + ':' + path], cwd=ROOT))
        content = b'{ "name":"example-agent", "mcpServers":{"extra":{"command":"/bin/true","source":{"external":true}}} }\r\n'
        path = self.work / 'recipe.json'
        path.write_bytes(content)
        self.config['recipe_file'] = {'path': str(path), 'sha256': hashlib.sha256(content).hexdigest()}
        spec = self.spec()
        spec_path = self.work / 'spec.json'
        spec_path.write_text(json.dumps(spec))
        driver = r"""
import json,sys
from pathlib import Path
from unittest.mock import patch
import connectome_cook as c
spec = json.load(open(sys.argv[1]))
build = Path(sys.argv[1]).parent / 'golden/builds/request'
build.mkdir(parents=True)
path = build / 'deployment.json'
path.write_text(json.dumps(spec))
result = {}
class Captured(Exception): pass
def capture(argv, **kwargs):
    result['argv'] = argv
    raise Captured()
with patch.object(c, 'verify_cook'), patch.object(c, 'command', side_effect=capture):
    try:
        c.adapt(str(path), str(build), spec['config']['state_dir'], sys.executable,
                '/example/bin/cook', sys.executable, sys.executable)
    except Captured:
        pass
for name in ('cook-recipe.json', 'cook-layout.json'):
    result[name] = (build / name).read_bytes().hex()
print(json.dumps(result, sort_keys=True))
"""
        outputs = []
        for source in (baseline, ROOT / 'roles/connectome_agent/module_utils'):
            outputs.append(subprocess.check_output([sys.executable, '-c', driver, str(spec_path)],
                           env=dict(os.environ, PYTHONPATH=str(source))))
            shutil.rmtree(self.work / 'golden')
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(json.loads(outputs[1])['cook-recipe.json'], content.hex())



class PreflightPayloadTests(unittest.TestCase):
    def test_loopback_http_checks_and_token_failure(self):
        import http.server
        import threading
        node = shutil.which('node')
        if not node:
            self.skipTest('local Node unavailable; no acquisition permitted')
        observed = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append(self.headers.get('Authorization'))
                self.send_response(200 if self.path == '/ready' or
                                   self.headers.get('Authorization') == 'Bearer fixture-value' else 401)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(('localhost', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = 'http://localhost:' + str(server.server_port)
            for name, env, expected in (
                ('inference-gate-check', {'INFERENCE_GATE_URL': endpoint + '/auth',
                                         'INFERENCE_GATE_TOKEN': 'fixture-value'}, 0),
                ('inference-gate-check', {'INFERENCE_GATE_URL': endpoint + '/auth',
                                         'INFERENCE_GATE_TOKEN': 'invalid-value'}, 1),
                ('inference-gate-check', {'INFERENCE_GATE_URL': endpoint + '/auth'}, 1),
                ('transport-readiness', {'TRANSPORT_READINESS_URL': endpoint + '/ready'}, 0),
                ('transport-readiness', {'TRANSPORT_READINESS_URL': endpoint + '/unready'}, 1)):
                result = subprocess.run([node, str(ROOT / 'catalog_payloads' / name / 'check.mjs')],
                                        env=env, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn(b'fixture-value', result.stdout + result.stderr)
            self.assertIn('Bearer fixture-value', observed)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
