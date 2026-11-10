"""Catalog closure tests use local synthetic payloads, never upstream releases."""
from copy import deepcopy
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import shlex
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
        self.addCleanup(self.thaw, self.build)
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

    def verify(self, *args, **kwargs):
        # Existing surface-audit tests mutate fixtures between checks. Exercise
        # the sealed verification path, then thaw only the disposable fixture.
        try:
            catalog.seal(self.build)
            return catalog.verify(*args, **kwargs)
        finally:
            self.thaw(self.build)

    @staticmethod
    def thaw(build):
        for path in [build, *build.rglob('*')]:
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o200)

    def test_compile_build_lock_and_revalidate_without_input_archives(self):
        spec, recipe = self.install()
        server = recipe['mcpServers']['extra']
        self.assertEqual(server['source'], {'external': True})
        self.assertEqual(server['command'], str(self.build / 'catalog/node/bin/node'))
        self.assertEqual((self.build / 'catalog/extra/server.py').read_text(), 'print(42)\n')
        shutil.rmtree(self.root / 'inputs')
        self.verify(self.config, spec['pins'], self.build)
        (self.build / 'catalog/extra/server.py').write_text('changed')
        with self.assertRaises(ArchiveError):
            self.verify(self.config, spec['pins'], self.build)

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

    def test_environment_controls_fail_at_install_and_digest_valid_reuse(self):
        spec, original = self.install()
        bad = {'NODE_OPTIONS': '--require=../outside.cjs', 'PYTHONPATH': '../outside',
               'LD_PRELOAD': '../outside.so'}
        for name, value in bad.items():
            self.config['connectors']['extra']['settings']['env'] = {name: value}
            pins = self.repin()
            recipe = deepcopy(original)
            recipe['mcpServers']['extra']['env'] = {name: value}
            self.reseal(pins, recipe)
            for operation in (catalog.install, self.verify):
                with self.subTest(name=name, operation=operation.__name__), \
                        patch.object(catalog.subprocess, 'run') as run:
                    with self.assertRaisesRegex(ArchiveError, 'environment refused: ' + name):
                        operation(self.config, pins, self.build)
                    run.assert_not_called()

    def test_reused_lock_audits_recipe_only_and_unit_environment(self):
        spec, recipe = self.install()
        altered = deepcopy(recipe)
        altered['agent']['provider'] = {'env': {'NODE_OPTIONS': '--require=../outside.cjs'}}
        self.reseal(spec['pins'], altered)
        with self.assertRaisesRegex(ArchiveError, 'environment refused: NODE_OPTIONS'):
            self.verify(self.config, spec['pins'], self.build)
        self.reseal(spec['pins'], recipe)
        (self.build / 'run.sh').write_text('#!/bin/sh\nexit 0\n')
        lock = self.launch_lock(spec['pins'])
        lock['units'] = [{'path': 'agent.service'}]
        body = ('[Service]\nWorkingDirectory=' + str(self.build) + '\n'
                'ExecStart=/bin/sh ' + str(self.build / 'run.sh') + '\n')
        for name in ('NODE_OPTIONS', 'PYTHONPATH', 'LD_PRELOAD'):
            (self.build / 'agent.service').write_text(body + 'Environment=' + name + '=yes\n')
            with self.subTest(name=name), self.assertRaisesRegex(ArchiveError, 'environment refused: ' + name):
                self.verify(self.config, spec['pins'], self.build, lock)
        (self.build / 'agent.service').write_text(body + 'Environment=CONFIG=./file\n')
        self.verify(self.config, spec['pins'], self.build, lock)
        # Relative environment paths must also be safe after launcher chdir.
        (self.build / 'agent.service').write_text(body +
                                                 'Environment=CONFIG=../../outside\n')
        (self.build / 'run.sh').write_text('#!/bin/sh\ncd ' + str(self.root) + '\nexit 0\n')
        with self.assertRaisesRegex(ArchiveError, 'outside build'):
            self.verify(self.config, spec['pins'], self.build, lock)

    def test_environment_path_escape_and_ambiguity_at_install_and_reuse(self):
        _, original = self.install()
        alias = self.build
        (alias / 'link').symlink_to(self.work)
        for value in ('../outside.cjs', './link/outside.cjs', '--config=../outside.cjs',
                      '--config=./link/outside.cjs', '~/.config', './a::./b',
                      'https://example.invalid/config', 'x=../outside', '${UNDECLARED}', '-r../outside',
                      'name%20encoded', 'name;command', 'name,other', 'name:', ':name'):
            self.config['connectors']['extra']['settings']['env'] = {'CONFIG_FILE': value}
            pins = self.repin()
            recipe = deepcopy(original)
            recipe['mcpServers']['extra']['env'] = {'CONFIG_FILE': value}
            self.reseal(pins, recipe)
            for operation in (catalog.install, self.verify):
                with self.subTest(value=value, operation=operation.__name__):
                    with self.assertRaises(ArchiveError):
                        operation(self.config, pins, self.build)

    def test_bare_environment_paths_and_lists_refused_fresh_and_digest_valid_reuse(self):
        state = self.build
        outside = self.work / 'outside'
        outside.mkdir()
        marker = outside / 'ran'
        helper = outside / 'helper'
        helper.write_text('#!/bin/sh\nprintf invoked > ' + shlex.quote(str(marker)) + '\n')
        helper.chmod(0o755)
        (state / 'escape').symlink_to(outside, target_is_directory=True)
        (state / 'file').symlink_to(helper)
        (state / 'two words').symlink_to(helper)
        (state / 'option=local').symlink_to(helper)
        (state / 'development:production').symlink_to(helper)
        (state / 'loop').symlink_to('loop')
        (state / 'local').mkdir()
        cases = [('PATH', 'escape'), ('MANPATH', 'local'), ('CDPATH', 'local'),
                 ('APP_PATH', 'local'), ('CUSTOMPATH', 'local'), ('path', 'local'),
                 ('CONFIG', 'escape'), ('CONFIG', 'file'), ('CONFIG', 'two words'),
                 ('CONFIG', 'option=local'), ('CONFIG', 'development:production'),
                 ('CONFIG', 'loop'), ('CONFIG', 'local:escape'), ('CONFIG', 'escape:local'),
                 ('CONFIG', 'local:file:development'), ('CONFIG', '--config=local:escape')]
        settings = self.config['connectors']['extra']['settings']
        for phase in ('fresh', 'reuse'):
            if phase == 'reuse':
                settings.pop('env')
                self.repin()
                links = {p: p.readlink() for p in state.iterdir() if p.is_symlink()}
                for p in links:
                    p.unlink()
                _, original = self.install()
                for p, target in links.items():
                    p.symlink_to(target)
            for name, value in cases:
                settings['env'] = {name: value}
                pins = self.repin()
                if phase == 'reuse':
                    recipe = deepcopy(original)
                    recipe['mcpServers']['extra']['env'] = {name: value}
                    self.reseal(pins, recipe)
                with self.subTest(phase=phase, name=name, value=value), \
                        patch.object(catalog.subprocess, 'run') as run:
                    with self.assertRaises(ArchiveError):
                        if phase == 'fresh':
                            self.install()
                        else:
                            self.verify(self.config, pins, self.build)
                    run.assert_not_called()
                    self.assertFalse(marker.exists())
                    if phase == 'fresh':
                        self.assertFalse((self.build / 'catalog').exists())

    def test_environment_denials_cover_all_descriptor_unit_and_secret_surfaces(self):
        _, original = self.install()
        names = sorted(catalog.CODE_ENV) + ['DYLD_INSERT_LIBRARIES', 'DYLD_FUTURE',
            'GIT_CONFIG_COUNT', 'BUN_CONFIG_PRELOAD', 'BUN_CONFIG_PATH', 'BUN_CONFIG_FILE',
            'BUN_CONFIG', 'BUN_PRELOAD', 'BUN_OPTIONS', 'PYTHON3_OPTIONS', 'RUBY_VM_OPTIONS',
            'CUSTOM_PRELOAD', 'PATH', 'MANPATH', 'CDPATH', 'APP_PATH', 'CUSTOMPATH', 'path']
        body = '[Service]\nWorkingDirectory=' + str(self.build) + '\n'
        for name in names:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ArchiveError, 'environment refused'):
                    catalog.audit_unit(body + 'Environment="' + name + '=yes"\nEnvironment=\n',
                                       self.build, Path(self.config['state_dir']), {})
                self.config['required_secrets'] = [name]
                with self.assertRaisesRegex(ArchiveError, 'environment refused'):
                    catalog.secret_environment_names(self.config)
        self.config.pop('required_secrets')
        for name in ('node', 'readiness'):
            self.config['catalog'][name]['settings'] = {'env': {'NODE_OPTIONS': 'yes'}}
            pins = self.repin()
            with self.assertRaisesRegex(ArchiveError, 'environment refused'):
                catalog.audit_descriptors(self.config, pins, self.build)
            del self.config['catalog'][name]['settings']
        for directive in ('EnvironmentFile=local.env', 'PassEnvironment=NODE_OPTIONS',
                          'PAMName=login', 'Environment=CONFIG=../outside.cjs'):
            with self.subTest(directive=directive), self.assertRaises(ArchiveError):
                catalog.audit_unit(body + directive, self.build, Path(self.config['state_dir']), {})

    def test_bare_environment_reuse_checks_recipe_and_unit_at_launcher_cwd(self):
        spec, original = self.install()
        cwd = self.root / 'workspace'
        cwd.mkdir()
        (cwd / 'escape').symlink_to(self.work, target_is_directory=True)
        (self.build / 'run.sh').write_text('#!/bin/sh\ncd ' + str(cwd) + '\nexit 0\n')
        body = ('[Service]\nWorkingDirectory=' + str(self.build) + '\n'
                'ExecStart=/bin/sh ' + str(self.build / 'run.sh') + '\n')
        for surface in ('recipe', 'unit'):
            for name, value in (('PATH', 'escape'), ('CONFIG', 'escape'), ('CONFIG', 'local:escape')):
                recipe, unit = deepcopy(original), body
                if surface == 'recipe':
                    recipe['agent']['provider'] = {'env': {name: value}}
                else:
                    unit += 'Environment=' + name + '=' + value + '\n'
                self.reseal(spec['pins'], recipe)
                (self.build / 'agent.service').write_text(unit)
                lock = self.launch_lock(spec['pins'])
                lock['units'] = [{'path': 'agent.service'}]
                with self.subTest(surface=surface, name=name, value=value), self.assertRaises(ArchiveError):
                    self.verify(self.config, spec['pins'], self.build, lock)

    def test_environment_literals_and_local_data_paths_are_accepted(self):
        state = self.build
        (state / 'local').mkdir()
        (state / 'alias').mkdir()
        (state / 'local.json').write_text('{}')
        settings = self.config['connectors']['extra']['settings']
        settings['env'] = {'MODE': 'development', 'COUNT': '4', 'LABEL': 'two words',
                           'DATA_FILE': './local.json', 'CONFIG': '--config=./local.json',
                           'BARE_FILE': 'local.json', 'BARE_DIR': 'alias', 'EMPTY': '',
                           'FILES': 'local:alias:development', 'RELATIVE_FILES': './a:./b',
                           'NODE_EXTRA_CA_CERTS': './certificate.pem',
                           'EXAMPLE_SECRET': '${EXAMPLE_SECRET}'}
        self.config['required_secrets'] = ['EXAMPLE_SECRET']
        self.repin()
        spec, _ = self.install()
        self.verify(self.config, spec['pins'], self.build)
        body = '[Service]\nWorkingDirectory=' + str(self.build) + '\nEnvironment="MODE=development"'
        catalog.audit_unit(body, self.build, Path(self.config['state_dir']), {})

    def test_build_and_cook_environment_never_copy_operator_environment(self):
        entry = deepcopy(self.config['connectors']['extra'])
        self.add('extra', entry, {'build.py': (
            'import os,json\nfrom pathlib import Path\n'
            'Path("observed.json").write_text(json.dumps(dict(os.environ)))\n'
            'Path("server.py").write_text("print(42)\\n")\n').encode()})
        ambient = {'NODE_OPTIONS': '--require=../outside.cjs', 'PYTHONPATH': '../outside',
                   'LD_PRELOAD': '../outside.so', 'OPERATOR_ONLY': 'not-allowed',
                   'GIT_CONFIG_COUNT': '99', 'BASH_ENV': '../outside.sh'}
        with patch.dict(os.environ, ambient):
            spec, _ = self.install()
            observed = json.loads((self.build / 'catalog/extra/observed.json').read_text())
            cooked = catalog.environment(self.config, spec['pins'], self.build)
        self.assertFalse(ambient.keys() & observed.keys())
        self.assertFalse(ambient.keys() & cooked.keys())
        self.assertEqual(observed['NO_UPDATE_NOTIFIER'], '1')
        self.assertNotIn(os.environ['PATH'], cooked['PATH'])
        self.assertEqual(cooked['PATH'].split(os.pathsep),
                         [str(Path(catalog.runtime(self.config, self.build, name)).parent)
                          for name in ('bun', 'node')] + [str(self.build / 'platform-bin')])

    def test_unit_bootstrap_execs_preflight_and_launch_with_closed_environment(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('local Node with process.execve is unavailable')
        supported = subprocess.run([node, '-p', 'typeof process.execve'],
                                   env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
        if supported.stdout.strip() != 'function':
            self.skipTest('local Node lacks process.execve (pinned production runtime is v24)')
        spec, _ = self.install()
        self.config['required_secrets'] = ['EXAMPLE_SECRET']
        state = Path(self.config['state_dir'])
        state.mkdir()
        script = self.build / 'observe.py'
        script.write_text('import os,json\nprint(json.dumps(dict(os.environ)))\n')
        command = [catalog.runtime(self.config, self.build, 'node'), str(script)]
        self.config['catalog']['readiness']['run'] = command
        body = ('[Service]\nWorkingDirectory=' + str(self.build) + '\nEnvironment="MODE=development"\n'
                'ExecStart=' + ' '.join(command) + '\n')
        dropin = catalog.isolated_unit(self.config, spec['pins'], self.build, body)
        analyzer = shutil.which('systemd-analyze')
        if analyzer:
            unit = state / 'closed-env.service'
            unit.write_text(body + dropin)
            checked = subprocess.run([analyzer, '--user', 'verify', str(unit)],
                                     capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
        unset = next(line.split('=', 1)[1].split() for line in dropin.splitlines()
                     if line.startswith('UnsetEnvironment='))
        ambient = {'NODE_OPTIONS': '--require=../outside.cjs', 'PYTHONPATH': '../outside',
                   'LD_PRELOAD': '../outside.so', 'OPERATOR_ONLY': 'not-allowed',
                   'DYLD_FUTURE': 'not-allowed', 'EXAMPLE_SECRET': 'synthetic-answer'}
        # Emulate systemd's documented UnsetEnvironment before the pinned Node
        # bootstrap. Both preflight and launch targets must see only the allowlist.
        manager_env = {key: value for key, value in ambient.items() if key not in unset}
        catalog.seal(self.build)
        self.addCleanup(self.thaw, self.build)
        count = 0
        for line in dropin.splitlines():
            if not line.startswith(('ExecStart=', 'ExecStartPre=')) or line.endswith('='):
                continue
            argv = shlex.split(line.split('=', 1)[1])
            self.assertEqual(argv[0], catalog.runtime(self.config, self.build, 'node'))
            argv[0] = node  # Fixture runtimes are Python; use local Node only for bootstrap.
            self.assertNotIn('synthetic-answer', line)
            result = subprocess.run(argv, cwd=state, env=manager_env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads(result.stdout)
            self.assertFalse((ambient.keys() - {'EXAMPLE_SECRET'}) & observed.keys())
            self.assertEqual(observed['EXAMPLE_SECRET'], 'synthetic-answer')
            self.assertEqual(observed['MODE'], 'development')
            self.assertEqual(observed['DATA_DIR'], str(state))
            count += 1
        self.assertEqual(count, 2)

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
        self.verify(self.config, spec['pins'], self.build, lock)
        (self.build / 'run.sh').write_text('#!/bin/sh\nexec /opt/other-agent/node\n')
        with self.assertRaises(ArchiveError):
            self.verify(self.config, spec['pins'], self.build, lock)

    def escape_args(self, cwd):
        outside = self.work / 'outside.py'
        outside.write_text('from pathlib import Path\nPath(__file__).with_suffix(".ran").touch()\n')
        alias = self.root / 'alias'
        if not alias.is_symlink():
            alias.symlink_to(self.work, target_is_directory=True)
        relative = os.path.relpath(outside, cwd)
        linked = os.path.relpath(alias / 'outside.py', cwd)
        return (relative, linked, '--config=' + relative,
                '--config=' + linked, '--config=' + str(outside))

    def repin(self):
        for name, entry in catalog.entries_for(self.config).items():
            section = 'connectors' if catalog.kind(entry) == 'connector' else 'artifacts'
            self.versions[section][name]['descriptor_sha256'] = catalog.descriptor_digest(entry)
        return self.spec()['pins']

    def reseal(self, pins, recipe):
        # Simulate a digest-valid lock produced by the old, absolute-only audit.
        path = self.build / 'cook-recipe.json'
        path.write_text(json.dumps(recipe))
        record_path = self.build / 'catalog.lock.json'
        record = json.loads(record_path.read_text())
        record['pins'] = {key: pins[key] for key in ('artifacts', 'platform')}
        record['descriptors'] = {n: catalog.descriptor_digest(e)
                                 for n, e in catalog.entries_for(self.config).items()}
        record['recipe_sha256'] = catalog.sha256(path)
        record_path.write_bytes(catalog.canonical(record) + b'\n')

    def launch_lock(self, pins):
        return {'launch': {'script': 'run.sh'},
                'recipe': {'sha256': catalog.sha256(self.build / 'cook-recipe.json')},
                'components': [], 'localExtensions': [],
                'external': [{'name': 'extra', 'command': catalog.runtime(self.config, self.build, 'node')}],
                'engines': {name: {'path': catalog.runtime(self.config, self.build, name),
                                  'realPath': catalog.runtime(self.config, self.build, name),
                                  'version': pins['artifacts'][name]['version']} for name in ('node', 'bun')}}

    def test_build_escapes_refused_before_any_execution(self):
        entry = self.config['connectors']['extra']
        args = self.escape_args(self.build / 'catalog/extra')
        self.assertEqual(args[0], '../../../../../outside.py')
        for arg in args:
            entry['build'] = [['{artifact:node}/bin/node', arg]]
            self.repin()
            with self.subTest(arg=arg), patch.object(catalog.subprocess, 'run') as run:
                with self.assertRaises(ArchiveError):
                    self.install()
                run.assert_not_called()
                self.assertFalse((self.work / 'outside.ran').exists())

    def test_build_created_symlink_is_refused_before_next_command(self):
        outside = self.work / 'outside.py'
        marker = self.work / 'outside.ran'
        outside.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').touch()\n')
        entry = deepcopy(self.config['connectors']['extra'])
        entry['build'].append(['{artifact:node}/bin/node', 'alias.py'])
        payload = ('from pathlib import Path\nPath("alias.py").symlink_to(' +
                   repr(str(outside)) + ')\n').encode()
        self.add('extra', entry, {'build.py': payload})
        with patch.object(catalog.subprocess, 'run', wraps=subprocess.run) as run:
            with self.assertRaises(ArchiveError):
                self.install()
            self.assertEqual(run.call_count, 1)
        self.assertFalse(marker.exists())

    def test_preflight_and_launch_escapes_refused_before_execution_or_unit_render(self):
        original = deepcopy(self.config)
        for surface in ('preflight', 'launch'):
            for arg in self.escape_args(Path(self.config['state_dir'])):
                self.config = deepcopy(original)
                if surface == 'preflight':
                    self.config['catalog']['readiness']['run'][1] = arg
                else:
                    self.config['connectors']['extra']['settings']['args'] = [arg]
                self.repin()
                with self.subTest(surface=surface, arg=arg), patch.object(catalog.subprocess, 'run') as run:
                    with self.assertRaises(ArchiveError):
                        self.install()
                    run.assert_not_called()
                    if surface == 'preflight':
                        with self.assertRaises(ArchiveError):
                            catalog.preflight_unit(self.config, self.build)

    def test_verify_reaudits_digest_valid_build_preflight_and_launch_argv(self):
        _, original_recipe = self.install()
        original = deepcopy(self.config)
        for surface in ('build', 'preflight', 'launch'):
            cwd = self.build / 'catalog/extra' if surface == 'build' else Path(self.config['state_dir'])
            for arg in self.escape_args(cwd):
                self.config = deepcopy(original)
                recipe = deepcopy(original_recipe)
                if surface == 'build':
                    self.config['connectors']['extra']['build'][0][1] = arg
                elif surface == 'preflight':
                    self.config['catalog']['readiness']['run'][1] = arg
                else:
                    self.config['connectors']['extra']['settings']['args'] = [arg]
                    recipe['mcpServers']['extra']['args'] = [arg]
                pins = self.repin()
                self.reseal(pins, recipe)
                with self.subTest(surface=surface, arg=arg), self.assertRaises(ArchiveError):
                    self.verify(self.config, pins, self.build)

    def test_exported_launch_and_execstartpre_use_actual_cwd(self):
        spec, _ = self.install()
        pins = spec['pins']
        script = self.build / 'run.sh'
        unit = self.build / 'example.service'
        node = catalog.runtime(self.config, self.build, 'node')
        cwd = self.root / 'workspace'
        cwd.mkdir()
        lock = self.launch_lock(pins)
        lock['units'] = [{'path': unit.name}]
        for surface in ('launcher', 'ExecStart', 'ExecStartPre'):
            for arg in self.escape_args(cwd):
                script.write_text('#!/bin/sh\nexit 0\n')
                body = '[Service]\nWorkingDirectory=' + str(cwd) + '\n'
                if surface == 'launcher':
                    script.write_text('#!/bin/sh\nexec ' + node + ' ' + arg + '\n')
                    body += 'ExecStart=/bin/sh ' + str(script) + '\n'
                else:
                    body += surface + '=' + node + ' ' + arg + '\n'
                unit.write_text(body)
                with self.subTest(surface=surface, arg=arg), self.assertRaises(ArchiveError):
                    self.verify(self.config, pins, self.build, lock)
        # A relative path safe from the default state cwd escapes from this
        # unit's cwd: the preflight drop-in must use the unit, not the default.
        self.config['catalog']['readiness']['run'][1] = '../outside.py'
        with self.assertRaises(ArchiveError):
            catalog.preflight_unit(self.config, self.build, self.root, pins['platform'])

    def test_argv_executable_exports_symlinks_and_ambiguous_forms(self):
        spec, _ = self.install()
        cwd = self.build / 'catalog/extra'
        node = catalog.runtime(self.config, self.build, 'node')
        for executable in self.escape_args(cwd)[:2]:
            with self.subTest(executable=executable), self.assertRaises(ArchiveError):
                catalog.audit_argv([executable], cwd, self.build, Path(self.config['state_dir']), {})
        for arg in ('--config=../a:../../outside', '-r../../outside.py',
                    '$(pwd)/file', '@../response', 'file:../outside', '--config=~/outside', '--eval=outside'):
            with self.subTest(arg=arg), self.assertRaises(ArchiveError):
                catalog.audit_argv([node, arg], cwd, self.build, Path(self.config['state_dir']), {})
        (cwd / 'server.py').unlink()
        (cwd / 'server.py').symlink_to(self.work / 'outside.py')
        with self.assertRaises(ArchiveError):
            catalog.audit_descriptors(self.config, spec['pins'], self.build)

    def test_launcher_cd_and_unclassified_shell_forms_fail_closed(self):
        spec, _ = self.install()
        node = catalog.runtime(self.config, self.build, 'node')
        lock = self.launch_lock(spec['pins'])
        for body in ('cd ' + str(self.root) + '\nexec ' + node + ' ../outside.py',
                     'exec ' + node + ' $(pwd)/script.py',
                     'exec ' + node + ' build.py; exit 0',
                     'cd relative\nexec ' + node + ' build.py',
                     '/bin/sh -c outside'):
            (self.build / 'run.sh').write_text('#!/bin/sh\n' + body + '\n')
            with self.subTest(body=body), self.assertRaises(ArchiveError):
                self.verify(self.config, spec['pins'], self.build, lock)

    def test_literal_local_argv_and_unit_cwd_are_accepted(self):
        spec, _ = self.install()
        cwd = self.build / 'catalog/extra'
        node = catalog.runtime(self.config, self.build, 'node')
        for arg in ('build.py', './build.py', '../extra/build.py', '--outfile=dist/adapter.js'):
            catalog.audit_argv([node, arg], cwd, self.build, Path(self.config['state_dir']), {})
        (cwd / 'alias.py').symlink_to(cwd / 'build.py')
        catalog.audit_argv([node, 'alias.py'], cwd, self.build, Path(self.config['state_dir']), {})
        body = '[Service]\nWorkingDirectory=' + str(cwd) + '\nExecStart=' + node + ' ./build.py\n'
        self.assertEqual(catalog.audit_unit(body, self.build, Path(self.config['state_dir']), {}), cwd)
        with self.assertRaises(ArchiveError):
            catalog.audit_unit('[Service]\nExecStart=' + node, self.root, Path(self.config['state_dir']), {})
        for command in ('-/outside/node', 'node', '@' + node):
            bad = '[Service]\nWorkingDirectory=' + str(cwd) + '\nExecStart=' + command
            with self.subTest(command=command), self.assertRaises(ArchiveError):
                catalog.audit_unit(bad, self.root, Path(self.config['state_dir']), {})
        with self.assertRaises(ArchiveError):
            catalog.audit_unit(body + 'WorkingDirectory = ' + str(self.root),
                               self.build, Path(self.config['state_dir']), {})

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
        Path(self.config['cook']['host_repo']).mkdir()
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
            commands = [shlex.split(line.split('=', 1)[1]) for line in body.splitlines()
                        if line.startswith('ExecStartPre=') and line != 'ExecStartPre=']
            payload = json.loads(base64.b64decode(commands[-1][-1]))
            self.assertIn(str(dep.build / 'catalog/readiness/check.py'), payload['argv'])
            self.assertFalse(dep.prepare()['changed'])
            self.assertTrue(dep.activate({})['changed'])
            self.assertFalse(dep.activate({})['changed'])
            self.assertTrue(catalog.secret_env_path(dep.build).is_file())
            self.assertFalse((dep.build / 'secrets.env').exists())
            catalog.check_immutable(dep.build)
            self.thaw(dep.build)
            (dep.build / 'catalog/extra/server.py').write_text('modified')
            with self.assertRaises(ArchiveError):
                dep.prepare()

    def test_state_and_workspace_code_refused_at_plan_install_and_verify(self):
        spec, recipe = self.install()
        self.config['secrets_file'] = str(self.work / 'secrets/example.json')
        original = deepcopy(self.config)
        state = Path(self.config['state_dir'])
        state.mkdir(mode=0o700)
        script = state / 'entry.py'
        script.write_text('print(42)')
        for location in (script, self.root / 'workspace/entry.py', self.root / 'inputs/entry.py'):
            for surface in ('command', 'script', 'preload', 'build', 'preflight', 'health'):
                self.config = deepcopy(original)
                settings = self.config['connectors']['extra']['settings']
                if surface == 'command':
                    settings['command'] = str(location)
                elif surface == 'script':
                    settings['args'] = [str(location)]
                elif surface == 'preload':
                    settings['args'] = ['--require=' + str(location)]
                elif surface == 'build':
                    self.config['connectors']['extra']['build'][0][1] = str(location)
                elif surface == 'preflight':
                    self.config['catalog']['readiness']['run'][1] = str(location)
                else:
                    self.config['health'] = {'command': ['/bin/sh', str(location)]}
                pins = self.repin()
                self.reseal(pins, recipe)
                dep = Deployment(self.root, self.work / 'units', self.config, self.versions, [], check=True)
                with self.subTest(surface=surface, location=location):
                    for operation in (dep.plan,
                                      lambda: catalog.install(self.config, pins, self.build),
                                      lambda: self.verify(self.config, pins, self.build)):
                        with patch.object(catalog.subprocess, 'run') as run, self.assertRaises(ArchiveError):
                            operation()
                        run.assert_not_called()
        # The reviewer's state script is never admitted, even before its swap.
        script.unlink()
        script.symlink_to(self.work / 'outside.py')
        self.config = deepcopy(original)
        self.config['connectors']['extra']['settings']['args'] = [str(script)]
        self.repin()
        with self.assertRaises(ArchiveError):
            cook.recipe_from_spec(self.spec(), build_dir=self.build)

    def test_exported_exec_launcher_and_lock_health_refuse_state_code(self):
        spec, _ = self.install()
        state = Path(self.config['state_dir'])
        script = state / 'entry.py'
        node = catalog.runtime(self.config, self.build, 'node')
        for key in ('ExecStart', 'ExecStartPre', 'ExecStop', 'ExecCondition'):
            body = '[Service]\nWorkingDirectory=' + str(self.build) + '\n' + key + '=' + node + ' ' + str(script)
            with self.subTest(key=key), self.assertRaises(ArchiveError):
                catalog.isolated_unit(self.config, spec['pins'], self.build, body)
        with self.assertRaises(ArchiveError):
            catalog.audit_launcher('exec ' + node + ' ' + str(script), self.build,
                                   self.build, state, spec['pins']['platform'])
        (self.build / 'run.sh').write_text('#!/bin/sh\nexit 0\n')
        lock = self.launch_lock(spec['pins'])
        lock['health'] = {'command': [node, str(script)]}
        with self.assertRaises(ArchiveError):
            self.verify(self.config, spec['pins'], self.build, lock)

    def test_interpreter_directive_cannot_load_state_or_workspace_code(self):
        for target in (Path(self.config['state_dir']) / 'interpreter', self.root / 'workspace/interpreter'):
            with self.subTest(target=target):
                path = self.build / 'script'
                path.write_text('#!' + str(target) + '\n')
                with self.assertRaises(ArchiveError):
                    catalog.audit_tree_code(self.build, self.config['state_dir'], self.versions['platform'])
                path.unlink()
        state = Path(self.config['state_dir'])
        state.mkdir()
        (state / 'shell').symlink_to('/bin/sh')
        with self.assertRaises(ArchiveError):
            catalog.audit_argv([str(state / 'shell')], self.build, self.build, state, self.versions['platform'])

    def test_explicit_state_data_argument_is_lowered_and_verified(self):
        state = Path(self.config['state_dir'])
        state.mkdir()
        data = state / 'records.json'
        data.write_text('{}')
        settings = self.config['connectors']['extra']['settings']
        settings['data_args'] = {'--data-file': str(data)}
        self.repin()
        spec, recipe = self.install()
        self.assertNotIn('data_args', recipe['mcpServers']['extra'])
        self.assertEqual(recipe['mcpServers']['extra']['args'][-1], '--data-file=' + str(data))
        self.verify(self.config, spec['pins'], self.build)
        for flag in ('--require', '--import', '--eval', '-r', '0'):
            with self.subTest(flag=flag), self.assertRaises(ArchiveError):
                catalog.audit_data_args({flag: str(data)}, state)
        # No implicit exceptions in ordinary args or environment fields.
        settings.pop('data_args')
        settings['env'] = {'CONFIG': str(data)}
        self.repin()
        with self.assertRaises(ArchiveError):
            cook.recipe_from_spec(self.spec(), build_dir=self.build)

    def test_verify_refuses_writable_directories_files_and_outward_links(self):
        spec, _ = self.install()
        nested = self.build / 'empty'
        nested.mkdir()
        catalog.seal(self.build)
        catalog.verify(self.config, spec['pins'], self.build)
        for path in (self.build, nested, self.build / 'catalog/extra/server.py'):
            for bit in (0o200, 0o020, 0o002):
                original = path.stat().st_mode
                path.chmod(original | bit)
                with self.subTest(path=path, bit=bit), self.assertRaisesRegex(ArchiveError, 'writable'):
                    catalog.verify(self.config, spec['pins'], self.build)
                path.chmod(original)
        self.build.chmod(0o755)
        (self.build / 'outside').symlink_to(self.work)
        self.build.chmod(0o555)
        with self.assertRaisesRegex(ArchiveError, 'symlink'):
            catalog.verify(self.config, spec['pins'], self.build)

    def test_start_wrapper_refuses_post_verify_swap_and_writable_directory(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('local Node unavailable')
        spec, _ = self.install()
        script = self.build / 'run.sh'
        script.write_text('#!/bin/sh\nexit 0\n')
        lock = self.launch_lock(spec['pins'])
        body = '[Service]\nWorkingDirectory=' + str(self.build) + '\nExecStart=/bin/sh ' + str(script) + '\n'
        dropin = catalog.isolated_unit(self.config, spec['pins'], self.build, body)
        argv = shlex.split(next(line.split('=', 1)[1] for line in dropin.splitlines()
                               if line.startswith('ExecStart=') and line != 'ExecStart='))
        argv[0] = node  # synthetic production Node is Python; test real JS bootstrap locally
        catalog.seal(self.build)
        catalog.verify(self.config, spec['pins'], self.build, lock)
        marker = self.work / 'ran'
        outside = self.work / 'outside.sh'
        outside.write_text('touch ' + shlex.quote(str(marker)) + '\n')
        for mutation in ('writable', 'symlink'):
            self.build.chmod(0o755)
            if mutation == 'symlink':
                script.unlink()
                script.symlink_to(outside)
                self.build.chmod(0o555)
            result = subprocess.run(argv, env={}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn('catalog', result.stderr)
            self.assertFalse(marker.exists())

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
