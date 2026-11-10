"""Offline spawn contract tests; fixture object stores contain no runtime code."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import io
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / 'roles/connectome_agent/module_utils'
sys.path.insert(0, str(PACKAGE_ROOT))
from connectome_spawn import SpawnError, canonical, decode, digest, instance, load_source, load_catalog, make_plan, require_bound, template
from connectome_spawn.catalog import RoleCatalog, descriptor_digest
from connectome_spawn.planner import BUILD_BINDING, symbolic_fields


def example_template():
    return {'schema': 'connectome.spawn-template/v1', 'runtime': ['node'],
            'agent': {'provider': 'mock', 'model': 'example-model',
                      'systemPrompt': 'Help the creator with their work.', 'contextBudgetTokens': 240000,
                      'proseRouting': 'disabled', 'strategy': {'type': 'autobiographical',
                      'compressionModel': 'example-compression', 'recentWindowTokens': 80000}},
            'connectors': ['example-connector'], 'modules': ['identity'], 'seedPolicy': 'clean'}


def example_instance():
    return {'schema': 'connectome.spawn-instance/v1', 'instance': 'example-instance',
            'unixName': 'example-agent', 'agent': {'logicalName': 'ExampleAgent'},
            'homeDirectory': '/absolute/agent-home', 'home': {'transport': 'console', 'channel': 'primary'},
            'rights': {'administration': False}, 'target': {'os': 'linux', 'architecture': 'x86_64'}}


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


class SpawnTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.cache').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.cache')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.repo = self.work / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        self.spec = example_template()
        self.declaration = example_instance()
        (self.repo / 'template.yml').write_text(yaml.safe_dump(self.spec))
        (self.repo / 'payload.bin').write_bytes(b'local fixture payload\x00\xff')
        (self.repo / 'link.yml').symlink_to('template.yml')
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=Example', '-c', 'user.email=' + 'noreply' + '@' + 'example.invalid',
            'commit', '-qm', 'Offline fixture')
        self.commit = git(self.repo, 'rev-parse', 'HEAD')
        # A symlink blob can never be a template. Runtime tree uses a separate
        # commit without the symlink, because links are forbidden in artifacts.
        (self.repo / 'link.yml').unlink()
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=Example', '-c', 'user.email=' + 'noreply' + '@' + 'example.invalid',
            'commit', '-qm', 'Regular files only')
        self.runtime_commit = git(self.repo, 'rev-parse', 'HEAD')
        self.catalog_root = self.work / 'catalog'
        (self.catalog_root / 'connectors').mkdir(parents=True)
        self.payload_root = self.work / 'payloads'
        (self.payload_root / 'inputs').mkdir(parents=True)
        archive = self.payload_root / 'inputs/node.tar.gz'
        with tarfile.open(archive, 'w:gz') as stream:
            content = b'offline fixture, not an executable'
            member = tarfile.TarInfo('bin/node')
            member.size = len(content)
            stream.addfile(member, io.BytesIO(content))
        connector = {'repo': 'https://example.invalid/connector.git',
                     'pin_ref': 'connectors.example-connector', 'enabled': True,
                     'build': [['npm', 'run', 'build']], 'required_secrets': ['EXAMPLE_TOKEN'],
                     'settings': {'command': 'node', 'args': ['index.js'],
                                  'env': {'EXAMPLE_TOKEN': '${EXAMPLE_TOKEN}'}}}
        node = {'kind': 'runtime', 'pin_ref': 'artifacts.node', 'enabled': False,
                'exports': {'node': 'bin/node'}, 'requires': []}
        versions = {'runtime': {n: self.runtime_commit for n in ('cook', 'connectome-host', 'agent-framework')},
                    'connectors': {'example-connector': self.runtime_commit},
                    'artifacts': {'node': {'archive': 'inputs/node.tar.gz', 'version': '1.0.0',
                        'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                        'descriptor_sha256': descriptor_digest(node)}}}
        for name, descriptor in [('example-connector', connector), ('node', node)]:
            (self.catalog_root / 'connectors' / (name + '.yml')).write_text(yaml.safe_dump({name: descriptor}))
        (self.catalog_root / 'versions.yml').write_text(yaml.safe_dump(versions))
        self.catalog = load_catalog(self.catalog_root)
        self.stores = {'runtime.' + n: str(self.repo) for n in versions['runtime']}
        self.stores.update({'connectors.example-connector': str(self.repo), 'artifacts.node': str(self.payload_root)})
        for name, value in [('instance', self.declaration), ('stores', self.stores), ('template', self.spec)]:
            (self.work / (name + '.json')).write_text(json.dumps(value))

    def plan(self, spec=None, declaration=None, catalog_value=None, **kwargs):
        return make_plan(spec if spec is not None else self.spec,
                         declaration if declaration is not None else self.declaration,
                         catalog_value if catalog_value is not None else self.catalog, self.stores, **kwargs)

    def test_inline_file_repo_have_identical_hashes_and_plans(self):
        sources = [load_source(inline=json.dumps(self.spec)),
                   load_source(inline=yaml.safe_dump(self.spec)),
                   load_source(file=self.repo / 'template.yml'),
                   load_source(repo=str(self.repo) + '+template.yml@' + self.commit)]
        self.assertEqual(len({digest(template(s)) for s in sources}), 1)
        plans = [self.plan(s) for s in sources]
        self.assertEqual(len({p['planHash'] for p in plans}), 1)
        self.assertEqual(len({p['idempotenceKey'] for p in plans}), 1)
        self.assertTrue(all(p == plans[0] for p in plans))
        (self.repo / 'template.yml').write_text('uncommitted: ignored')
        self.assertEqual(load_source(repo=str(self.repo) + '+template.yml@' + self.commit), self.spec)

    def test_bare_and_git_directory_object_stores(self):
        expected = load_source(repo=str(self.repo) + '+template.yml@' + self.commit)
        self.assertEqual(load_source(repo=str(self.repo / '.git') + '+template.yml@' + self.commit), expected)
        bare = self.work / 'bare.git'
        subprocess.run(['git', 'clone', '--bare', '--no-hardlinks', str(self.repo), str(bare)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(load_source(repo=str(bare) + '+template.yml@' + self.commit), expected)

    def test_identity_and_computed_digests(self):
        result = self.plan()
        plan = result['plan']
        self.assertEqual(result['planHash'], digest(plan))
        recipe = plan['recipe']
        self.assertEqual(recipe['name'], 'ExampleAgent')
        self.assertEqual(recipe['agent']['name'], 'ExampleAgent')
        self.assertEqual(recipe['agent']['strategy']['summaryParticipant'], 'ExampleAgent')
        self.assertEqual(plan['identity']['participantNamespace'], 'agents/ExampleAgent')
        self.assertEqual(recipe['agent']['strategy']['recentWindowTokens'], 80000)
        for record in plan['files']:
            self.assertEqual(record['sha256'], hashlib.sha256(record['content'].encode()).hexdigest())
        self.assertEqual(plan['artifacts']['node']['sha256'],
                         hashlib.sha256((self.payload_root / 'inputs/node.tar.gz').read_bytes()).hexdigest())
        self.assertEqual(plan['artifacts']['example-connector']['sha256'], digest(plan['artifacts']['example-connector']['files']))
        self.assertFalse(plan['activationAllowed'])
        self.assertNotIn('logicalName', recipe['agent'])

    def test_hashes_bind_intent_catalog_target_and_observation(self):
        old = self.plan()
        for mutate in (
            lambda s, i, c: s['agent'].update(systemPrompt='A different approved instruction.'),
            lambda s, i, c: s.update(seedPolicy='required'),
            lambda s, i, c: i['agent'].update(logicalName='DifferentAgent'),
            lambda s, i, c: i['home'].update(channel='secondary'),
            lambda s, i, c: i['rights'].update(administration=True),
            lambda s, i, c: i['target'].update(architecture='aarch64'),
            lambda s, i, c: c.descriptors['example-connector']['settings'].update(args=['other.js']),
        ):
            s, i, c = deepcopy((self.spec, self.declaration, self.catalog))
            mutate(s, i, c)
            new = self.plan(s, i, c)
            self.assertNotEqual(old['planHash'], new['planHash'])
            self.assertNotEqual(old['idempotenceKey'], new['idempotenceKey'])
        self.assertNotEqual(old['idempotenceKey'], self.plan(observed={'deploymentHash': 'a' * 64})['idempotenceKey'])
        copy = deepcopy(old)
        old['plan']['recipe']['agent']['name'] = 'Changed'
        self.assertEqual(self.plan(), copy)

    def test_strict_decode_refusals(self):
        bad = ['{"a":1,"a":2}', 'a: 1\na: 2', 'a: {b: 1, b: 2}',
               'a: &anchor [1]\nb: *anchor', 'a: !!str hello', 'a: 2026-01-01',
               'a: .nan', '{"a":NaN}', '{"a":1e10000}', '1: a', 'true: a',
               'a: {<<: {b: 1}}', 'a: 1\n---\nb: 2', '{"a":1,}', '[]',
               'a: ' + '[' * 40 + '0' + ']' * 40, '{"a":"\\ud800"}']
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(SpawnError):
                decode(raw)
        with self.assertRaises(SpawnError):
            decode('a: ' + 'x' * (1024 * 1024))

    def test_unsafe_template_refusals(self):
        for key in ('command', 'hooks', 'shell', 'build', 'unknown', 'sha256', 'name'):
            spec = deepcopy(self.spec)
            spec[key] = 'rejected'
            with self.subTest(key=key), self.assertRaises(SpawnError):
                template(spec)
        for mutate in (
            lambda s: s['agent'].update(name='CHANGEME'),
            lambda s: s['agent']['strategy'].update(summaryParticipant='CHANGEME'),
            lambda s: s['agent']['strategy'].update(command='rejected'),
            lambda s: s['agent']['strategy'].update(unknown=True),
            lambda s: s.update(runtime=['../escape']),
            lambda s: s.update(connectors=['/bin/sh']),
            lambda s: s.update(connectors=['example-connector', 'example-connector']),
            lambda s: s.update(modules=['unknown']),
            lambda s: s['agent'].update(contextBudgetTokens=True),
            lambda s: s['agent'].update(proseRouting=False),
        ):
            spec = deepcopy(self.spec)
            mutate(spec)
            with self.assertRaises(SpawnError):
                template(spec)
        for value in ('', ' ', 'TODO', 'CHANGEME', '${PROMPT}', '{{ prompt }}', '<systemPrompt>', 'systemPrompt'):
            spec = deepcopy(self.spec)
            spec['agent']['systemPrompt'] = value
            with self.subTest(value=value), self.assertRaises(SpawnError):
                template(spec)

    def test_instance_refusals_and_no_identity_fallback(self):
        for mutate in (
            lambda i: i['agent'].clear(),
            lambda i: i['agent'].update(logicalName='name'),
            lambda i: i['agent'].update(logicalName='TODO'),
            lambda i: i.update(instance='../escape'),
            lambda i: i.update(unixName='root/escape'),
            lambda i: i.update(homeDirectory='/absolute/../escape'),
            lambda i: i.update(homeDirectory='/absolute//escape'),
            lambda i: i.update(homeDirectory='/absolute/${HOME}'),
            lambda i: i['home'].update(channel='CHANGEME'),
            lambda i: i['home'].update(channel='<home channel>'),
            lambda i: i['home'].update(channel='channel'),
            lambda i: i['rights'].update(administration='yes'),
        ):
            declaration = deepcopy(self.declaration)
            mutate(declaration)
            with self.assertRaises(SpawnError):
                instance(declaration)

    def test_unpinned_and_escaping_sources_refused(self):
        for ref in ('HEAD', 'main', 'v1.0.0', self.commit[:12], '0' * 40, self.commit + '^'):
            with self.subTest(ref=ref), self.assertRaises(SpawnError):
                load_source(repo=str(self.repo) + '+template.yml@' + ref)
        for path in ('../template.yml', '/template.yml', './template.yml', 'a//b', 'a/../../b', 'link.yml'):
            with self.subTest(path=path), self.assertRaises(SpawnError):
                load_source(repo=str(self.repo) + '+' + path + '@' + self.commit)
        with self.assertRaises(SpawnError):
            load_source(repo='https://example.invalid/repo+template.yml@' + self.commit)
        with self.assertRaises(SpawnError):
            load_source(inline='a: b', file=self.repo / 'template.yml')
        alias = self.work / 'alias'
        alias.symlink_to(self.repo / 'template.yml')
        with self.assertRaises(SpawnError):
            load_source(file=alias)

    def test_catalog_fail_closed_and_template_cannot_supply_executables(self):
        for mutate in (
            lambda c: c.versions['connectors'].update({'example-connector': 'main'}),
            lambda c: c.versions['connectors'].update({'example-connector': '0' * 40}),
            lambda c: c.versions['runtime'].update(cook='HEAD'),
            lambda c: c.versions['artifacts']['node'].update(sha256='0' * 64),
            lambda c: c.versions['artifacts']['node'].update(sha256='a' * 64),
            lambda c: c.versions['artifacts']['node'].update(descriptor_sha256='a' * 64),
            lambda c: c.versions['artifacts']['node'].update(archive='../escape'),
            lambda c: c.versions['connectors'].update({'example-connector': self.commit}),
            lambda c: c.descriptors['example-connector'].update(unknown=True),
            lambda c: c.descriptors['node'].update(kind='unrecognized'),
            lambda c: c.descriptors['node'].update(requires=['node']),
        ):
            value = deepcopy(self.catalog)
            mutate(value)
            with self.assertRaises(SpawnError):
                self.plan(catalog_value=value)
        spec = deepcopy(self.spec)
        spec['connectors'] = [{'name': 'example-connector', 'command': 'sh'}]
        with self.assertRaises(SpawnError):
            self.plan(spec)
        spec['connectors'] = ['unknown']
        with self.assertRaises(SpawnError):
            self.plan(spec)

    def test_role_catalog_single_source_and_future_kind(self):
        current = load_catalog(ROOT)
        self.assertIn('telegram-bot-mcpl', current.descriptors)
        self.assertNotIn('kind', current.descriptors['telegram-bot-mcpl'])
        result = self.plan()['plan']
        self.assertEqual(result['artifacts']['node']['catalogKind'], 'runtime')
        self.assertEqual(result['exports']['node']['node'], BUILD_BINDING + '/catalog/node/bin/node')
        # Enable is role selection metadata, not part of the descriptor pin.
        c = deepcopy(self.catalog)
        c.descriptors['node']['enabled'] = True
        self.plan(catalog_value=c)
        c.descriptors['unused'] = {'kind': 'runtime', 'pin_ref': 'artifacts.unused'}
        c.versions['artifacts']['unused'] = '0' * 40
        without_unused = deepcopy(c)
        without_unused.descriptors.pop('unused')
        self.assertEqual(self.plan(catalog_value=c), self.plan(catalog_value=without_unused))
        s = deepcopy(self.spec)
        s['runtime'].append('unused')
        with self.assertRaises(SpawnError):
            self.plan(s, catalog_value=c)

    def test_archive_connector_symbols_are_explicit_and_refused_as_apply_input(self):
        c = deepcopy(self.catalog)
        entry = c.descriptors['example-connector']
        entry['requires'] = ['node']
        entry['settings'] = {'command': '{artifact:node}/bin/node',
                             'args': ['{artifact:example-connector}/index.js']}
        c.versions['connectors']['example-connector'] = dict(c.versions['artifacts']['node'],
            descriptor_sha256=descriptor_digest(entry), commit=self.runtime_commit)
        stores = dict(self.stores, **{'connectors.example-connector': str(self.payload_root)})
        result = make_plan(self.spec, self.declaration, c, stores)['plan']
        self.assertEqual(result['recipe']['mcpServers']['example-connector']['source'], {'external': True})
        self.assertIn('/recipe/mcpServers/example-connector/command', result['symbolicFields'])
        self.assertIn('/files/0/content', result['symbolicFields'])
        self.assertEqual(result['symbolicFields'], symbolic_fields({k: v for k, v in result.items() if k != 'symbolicFields'}))
        for value in (result['recipe'], result['files'], {'nested': [BUILD_BINDING]}):
            with self.assertRaises(SpawnError):
                require_bound(value)
        # This is only a guard test, not an implementation of apply/binding.
        concrete = json.loads(json.dumps(result['recipe']).replace(BUILD_BINDING, '/absolute/build'))
        self.assertEqual(require_bound(concrete), concrete)
        entry['settings']['args'] = ['{artifact:example-connector}/../escape']
        c.versions['connectors']['example-connector']['descriptor_sha256'] = descriptor_digest(entry)
        with self.assertRaises(SpawnError):
            make_plan(self.spec, self.declaration, c, stores)

    def test_archive_symlinks_and_digest_mismatch_refused(self):
        path = self.payload_root / 'inputs/node.tar.gz'
        original = path.read_bytes()
        path.write_bytes(original + b'changed')
        with self.assertRaises(SpawnError):
            self.plan()
        path.unlink()
        target = self.payload_root / 'saved.tar.gz'
        target.write_bytes(original)
        path.symlink_to(target)
        with self.assertRaises(SpawnError):
            self.plan()

    def test_git_ignores_ambient_config_and_replace_objects(self):
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'alias.cat-file',
                                    'GIT_CONFIG_VALUE_0': '!false', 'GIT_DIR': '/nonexistent',
                                    'GIT_WORK_TREE': '/nonexistent', 'GIT_CONFIG_SYSTEM': '/nonexistent'}):
            self.assertEqual(load_source(repo=str(self.repo) + '+template.yml@' + self.commit), self.spec)
        git(self.repo, 'replace', self.commit, self.runtime_commit)
        # A replaced commit must not expose the second commit's tree.
        from connectome_spawn.sources import commit_tree
        self.assertNotEqual(commit_tree(str(self.repo), self.commit), commit_tree(str(self.repo), self.runtime_commit))

    def test_missing_partial_clone_objects_never_invoke_remote_helper(self):
        marker = self.work / 'remote-was-called'
        helper = self.work / 'remote-helper'
        helper.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 1\n')
        helper.chmod(0o700)
        git(self.repo, 'config', 'extensions.partialClone', 'origin')
        git(self.repo, 'config', 'remote.origin.promisor', 'true')
        git(self.repo, 'config', 'remote.origin.url', 'ext::' + str(helper))
        git(self.repo, 'config', 'protocol.ext.allow', 'always')
        from connectome_spawn.sources import commit_tree
        with self.assertRaises(SpawnError):
            commit_tree(str(self.repo), 'f' * 40)
        self.assertFalse(marker.exists())

    def test_preflight_dependency_closure_is_recorded_not_executed(self):
        c = deepcopy(self.catalog)
        check = {'kind': 'preflight', 'pin_ref': 'artifacts.check', 'requires': ['node'],
                 'run': ['{artifact:node}/bin/node', '{artifact:check}/check.js'],
                 'required_secrets': ['CHECK_CREDENTIAL']}
        c.descriptors['check'] = check
        c.versions['artifacts']['check'] = dict(c.versions['artifacts']['node'],
                                              descriptor_sha256=descriptor_digest(check))
        stores = dict(self.stores, **{'artifacts.check': str(self.payload_root)})
        spec = deepcopy(self.spec)
        spec['runtime'] = []
        spec['preflights'] = ['check']
        plan = make_plan(spec, self.declaration, c, stores)['plan']
        self.assertIn('node', plan['artifacts'])
        self.assertEqual(plan['preflights']['check'][0], BUILD_BINDING + '/catalog/node/bin/node')
        self.assertIn('CHECK_CREDENTIAL', plan['recipe']['requirements'])
        self.assertFalse(plan['activationAllowed'])

    def cli(self, command='plan', extra=(), env=None):
        args = [str(ROOT / 'scripts/connectome-spawn'), command, '--file', str(self.work / 'template.json'),
                '--instance', str(self.work / 'instance.json')]
        if command != 'validate':
            args += ['--catalog', str(self.catalog_root), '--stores', str(self.work / 'stores.json')]
        return subprocess.run(args + list(extra), cwd=self.work, env=env,
                              capture_output=True, text=True)

    def test_cli_read_only_independent_of_ansible_and_working_directory(self):
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.work.rglob('*') if p.is_file()}
        clean = self.cli()
        self.assertEqual(clean.returncode, 0, clean.stderr)
        env = dict(os.environ, ANSIBLE_CONFIG='/nonexistent', ANSIBLE_BECOME='true',
                   ANSIBLE_REMOTE_USER='other-agent', HOME='/nonexistent', GIT_DIR='/nonexistent')
        altered = self.cli(env=env)
        self.assertEqual(clean.stdout, altered.stdout)
        dry = self.cli('dry-run')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        result = json.loads(dry.stdout)
        self.assertEqual(result['wouldWrite'], result['plan']['files'])
        self.assertEqual(result['planHash'], json.loads(clean.stdout)['planHash'])
        self.assertEqual(self.cli('validate').returncode, 0)
        after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.work.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        for command in ('apply', 'seed'):
            bad = self.cli(command)
            self.assertEqual(bad.returncode, 2)
            self.assertEqual(bad.stdout, '')
        bad = self.cli(extra=['--unknown', 'protected-value'])
        self.assertEqual(bad.returncode, 2)
        self.assertNotIn('protected-value', bad.stderr)

    def test_cli_inline_file_repo_equivalence(self):
        outputs = []
        for source in (['--inline', json.dumps(self.spec)], ['--inline', yaml.safe_dump(self.spec)],
                       ['--file', str(self.repo / 'template.yml')],
                       ['--repo', str(self.repo) + '+template.yml@' + self.commit]):
            args = [str(ROOT / 'scripts/connectome-spawn'), 'plan', *source,
                    '--instance', str(self.work / 'instance.json'), '--catalog', str(self.catalog_root),
                    '--stores', str(self.work / 'stores.json')]
            result = subprocess.run(args, cwd=self.work, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(json.loads(result.stdout))
        self.assertTrue(all(p == outputs[0] for p in outputs))

    def test_same_package_under_ansible_namespace(self):
        # Load precisely as module_utils does, not a copied implementation.
        spec = importlib.util.spec_from_file_location('ansible.module_utils.connectome_spawn',
                    PACKAGE_ROOT / 'connectome_spawn/__init__.py',
                    submodule_search_locations=[str(PACKAGE_ROOT / 'connectome_spawn')])
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: module}):
            spec.loader.exec_module(module)
            self.assertEqual(module.make_plan(self.spec, self.declaration, module.load_catalog(self.catalog_root), self.stores), self.plan())


if __name__ == '__main__':
    unittest.main()
