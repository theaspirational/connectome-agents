"""Byte comparison with the public baseline, not a regenerated golden file."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import test_deploy
import test_cook
from deploy_fixtures import scratch

ROOT = Path(__file__).resolve().parents[1]
BASELINE = '64d0e3a'

# Both processes use identical paths, inputs, fixtures and environment. Only the
# module source changes. Capture the real adapter argv before its child runs.
DRIVER = r'''
import json
from pathlib import Path
import sys
from unittest.mock import patch
import connectome_cook as adapter
import connectome_deploy as deploy
work = Path(sys.argv[1])
config = json.loads((work / 'config.json').read_text())
if len(sys.argv) > 2:
    config.update(activation='start', exclusive_with=[])
versions = json.loads((work / 'versions.json').read_text())
cook = json.loads((work / 'cook.json').read_text())
manager = json.loads((work / 'manager.json').read_text())
calls = []
original = deploy.run
def record(argv, **kwargs):
    calls.append(argv)
    return original(argv, **kwargs)
from deploy_fixtures import safe_ancestors
with safe_ancestors(work), patch.object(deploy, 'run', side_effect=record):
    dep = deploy.Deployment(work / 'agent', work / 'units', config, versions, cook, manager)
    prepared = dep.prepare()
    dep.activate({})
result = {'calls': calls, 'plan': prepared, 'files': {}}
for path in [dep.build / 'deployment.json', dep.build / 'connectome.lock', dep.build / '.prepared',
             dep.build / 'connectome-example.service', dep.build / 'secrets.env',
             work / 'units/connectome-example.service.d/90-connectome-env.conf']:
    result['files'][str(path)] = path.read_bytes().hex()
spec = json.loads((work / 'adapter-spec.json').read_text())
build = work / 'adapter/builds/request'
build.mkdir(parents=True)
path = build / 'deployment.json'
path.write_text(json.dumps(spec))
class Captured(Exception): pass
def capture(argv, **kwargs):
    result['adapter_argv'] = argv
    raise Captured()
with patch.object(adapter, 'verify_cook'), patch.object(adapter, 'command', side_effect=capture):
    try:
        adapter.adapt(str(path), str(build), spec['config']['state_dir'], sys.executable,
                      '/example/bin/cook', sys.executable, sys.executable)
    except Captured:
        pass
result['recipe'] = (build / 'cook-recipe.json').read_bytes().hex()
result['layout'] = (build / 'cook-layout.json').read_bytes().hex()
print(json.dumps(result, sort_keys=True))
'''


@unittest.skipIf(os.geteuid() == 0, 'compatibility fixtures require an unprivileged account')
class CompatibilityTests(unittest.TestCase):
    def test_default_off_byte_identical_render_argv_and_lock(self):
        if subprocess.run(['git', 'cat-file', '-e', BASELINE + '^{commit}'], cwd=ROOT,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            self.skipTest('historical compatibility baseline unavailable locally')
        with scratch() as tmp:
            work = Path(tmp).resolve()
            baseline = work / 'baseline'; baseline.mkdir()
            prefix = 'roles/connectome_agent/module_utils/'
            files = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', BASELINE, prefix], cwd=ROOT).decode().splitlines()
            self.assertTrue(files)
            for file in files:
                (baseline / Path(file).name).write_bytes(subprocess.check_output(
                    ['git', 'show', BASELINE + ':' + file], cwd=ROOT))
            config = test_deploy.example()
            config['state_dir'] = str(work / 'runtime-state')
            directory = work / 'secrets'; directory.mkdir(mode=0o700)
            secrets = directory / 'example.json'; secrets.write_text('{}'); secrets.chmod(0o600)
            config['secrets_file'] = str(secrets)
            pins = test_deploy.read('versions.yml')
            cook = [sys.executable, str(ROOT / 'tests/fixtures/cook.py'), '{recipe}', '{build_dir}', '{state_dir}']
            manager = [sys.executable, str(ROOT / 'tests/fixtures/systemctl.py'), str(work / 'agent')]
            spec = {'config': {'name': 'example', 'state_dir': str(work / 'adapter-state'),
                'agent': {'systemPrompt': 'Example'}, 'modules': {'lessons': {'settings': {}}},
                'connectors': {'extra': {'pin_ref': 'connectors.extra', 'repo': 'https://example.invalid/extra.git',
                    'settings': {'command': 'node', 'args': ['index.js']}}},
                'cook': {'host_repo': 'https://example.invalid/host.git', 'connector_state': {},
                    'framework_archive': test_cook.framework_archive(work / 'framework.tgz', 'd' * 40)}},
                'pins': {'runtime': {'cook': test_cook.adapter.COOK_COMMIT, 'connectome-host': 'c' * 40,
                                    'agent-framework': 'd' * 40}, 'connectors': {'extra': 'e' * 40}}}
            for name, value in [('config', config), ('versions', pins), ('cook', cook),
                                ('manager', manager), ('adapter-spec', spec)]:
                (work / (name + '.json')).write_text(json.dumps(value))
            snapshots = []
            for modules, explicit in ((baseline, False), (ROOT / 'roles/connectome_agent/module_utils', False),
                                      (ROOT / 'roles/connectome_agent/module_utils', True)):
                env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(modules), str(ROOT / 'tests'))))
                snapshots.append(subprocess.check_output([sys.executable, '-c', DRIVER, str(work)] +
                                 (['explicit'] if explicit else []), env=env, cwd=ROOT))
                for name in ('agent', 'units', 'runtime-state', 'adapter'):
                    shutil.rmtree(work / name)
            self.assertEqual(snapshots[0], snapshots[1])
            self.assertEqual(snapshots[0], snapshots[2])
            self.assertEqual((ROOT / 'roles/connectome_agent/tasks/main.yml').read_bytes(),
                             subprocess.check_output(['git', 'show', BASELINE +
                                 ':roles/connectome_agent/tasks/main.yml'], cwd=ROOT))
