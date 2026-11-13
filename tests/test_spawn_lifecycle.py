"""Offline lifecycle fakes: no imports of provider SDKs, no service commands."""
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'roles/connectome_agent/module_utils'))
from connectome_spawn import (FileIdentityProvider, Lifecycle, SpawnError, name_record,
                              read_name_record, template)
from connectome_spawn.lifecycle import STATES
from connectome_spawn.persistence import read, write
from connectome_spawn.seed import lifecycle_steps, seed_intent
from connectome_spawn.identity import identity_policy, temporary_name, validate_provider_name
from datetime import datetime, timezone

IDENTITY = {'name': 'ExampleAgent', 'displayName': 'Example Agent',
            'systemPrompt': 'Help the creator with their work.', 'summaryParticipant': 'ExampleAgent'}
HOME = {'transport': 'console', 'channel': 'primary'}
PAYLOAD = b'opaque offline seed fixture\x00\xff'


class FakeAdapter:
    def __init__(self, fail=None):
        self.events = []
        self.fail = fail

    def perform(self, step, context, operation_id):
        self.events.append((step, deepcopy(context), operation_id))
        if step == 'activate':
            assert (Path(context['store']).parent / 'seed.consumed').is_file()
        if step == self.fail:
            raise RuntimeError('simulated crash after effect')

    def verify(self, context, operation_id, checkpoint):
        health = checkpoint('service-health', lambda: {
            'state': 'applied', 'evidence': {'fixture': operation_id}, 'result': {'ready': True}})
        return {'serviceReady': {'state': 'success', 'evidence': {'health': health, 'mcp': {}}}}


class FakeImporter:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def ingest(self, payload, store, operation_id):
        self.calls += 1
        (store / 'memory.bin').write_bytes(payload)
        if self.fail:
            raise RuntimeError('simulated crash after ingest')


def competing_identity(root, agent):
    provider = FileIdentityProvider(root)
    return provider.provision_temporary(agent, 'ConnectomeNewborn' + agent.replace('-', ''), 'temporary')


def crash_during_import(root):
    class CrashingImporter:
        def ingest(self, payload, store, operation_id):
            (store / 'partial.bin').write_bytes(payload)
            os._exit(17)
    Lifecycle(root, adapter=FakeAdapter(), importer=CrashingImporter()).transition(
        'example-agent', 'active', 'crashed-process', payload=PAYLOAD)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.cache').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.cache')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.root = self.work / 'agents'
        self.provider = FileIdentityProvider(self.work / 'identities')
        self.adapter = FakeAdapter()
        self.importer = FakeImporter()
        self.life = self.restart()
        self.name_file = self.work / 'name-record.json'
        write(self.name_file, IDENTITY)

    def restart(self):
        return Lifecycle(self.root, adapter=self.adapter, identity_provider=self.provider, importer=self.importer)

    def create(self, mode='clean', agent='example-agent'):
        return self.life.create(agent, seed={'mode': mode}, identity=IDENTITY, home=HOME,
                                operation_id='create', payload=PAYLOAD if mode == 'required' else None)

    def move(self, target, op=None, **kwargs):
        return self.life.transition('example-agent', target, op or target, **kwargs)

    def repo_name_source(self, record=IDENTITY):
        repo = self.work / 'config-repo'
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
        git('init', '-q')
        (repo / 'names').mkdir()
        (repo / 'names/agent.json').write_text(json.dumps(record))
        (repo / 'link.json').symlink_to('names/agent.json')
        git('add', '.')
        git('-c', 'user.name=Fixture Committer', '-c', 'user.email=noreply@users.noreply.github.com',
            'commit', '-qm', 'Name record', '--author=Fixture Initiator <contributors@users.noreply.github.com>')
        commit = git('rev-parse', 'HEAD')
        git('update-index', '--add', '--cacheinfo', '160000,' + commit + ',submodule')
        git('-c', 'user.name=Fixture Committer', '-c', 'user.email=noreply@users.noreply.github.com',
            'commit', '-qm', 'Gitlink refusal fixture')
        return {'kind': 'repo', 'repo': str(repo), 'commit': git('rev-parse', 'HEAD'), 'path': 'names/agent.json'}

    def test_repo_name_pinned_provenance_and_identical_file_hash(self):
        source = self.repo_name_source()
        repo = Path(source['repo'])
        expected_blob = subprocess.check_output([
            'git', '-C', str(repo), 'rev-parse', source['commit'] + ':' + source['path']]).decode().strip()
        self.name_file.write_bytes((repo / source['path']).read_bytes())
        named = read_name_record(source, with_provenance=True)
        file_named = read_name_record(self.name_file, with_provenance=True)
        self.assertEqual(named['record'], IDENTITY)
        self.assertEqual(named['recordHash'], file_named['recordHash'])
        self.assertEqual(named['source'], dict(source, blob=expected_blob,
            author=named['source']['author'], committer=named['source']['committer']))
        self.assertTrue(named['source']['author'].startswith('Fixture Committer <noreply@users.noreply.github.com>'))
        self.assertTrue(named['source']['committer'].startswith('Fixture Committer <noreply@users.noreply.github.com>'))
        # Author is informational, not an identity/permission check. Use the
        # first commit to exercise a distinct initiator author and committer.
        first = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD^']).decode().strip()
        authored = read_name_record(dict(source, commit=first), with_provenance=True)['source']
        self.assertTrue(authored['author'].startswith('Fixture Initiator <contributors@users.noreply.github.com>'))
        self.assertNotEqual(authored['author'], authored['committer'])
        (repo / source['path']).write_text('uncommitted invalid input')
        subprocess.run(['git', '-C', str(repo), 'update-ref', 'HEAD', first], check=True)
        self.assertEqual(read_name_record(source), IDENTITY)
        self.create()
        self.move('initiation')
        state = self.move('named', name_source=source)
        self.assertEqual(state['nameRecord'], {k: named[k] for k in ('recordHash', 'source')})
        self.assertEqual(state['identity'], IDENTITY)
        self.assertEqual(self.move('named', name_source=source), state)
        # Even another commit containing the same record is different pinned intent.
        with self.assertRaises(SpawnError):
            self.move('named', name_source=dict(source, commit=first))
        with self.assertRaises(SpawnError):
            self.move('named', name_source=source, name_file=self.name_file)
        # Status reads only persisted provenance, not the now-unavailable repo.
        repo.rename(self.work / 'moved-repo')
        self.assertEqual(self.restart().status('example-agent'), state)
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/connectome-spawn'), 'status',
            'example-agent', '--state-root', str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['nameRecord'], state['nameRecord'])

    def test_repo_name_requires_real_repository_top(self):
        source = self.repo_name_source()
        repo = Path(source['repo'])
        alias = self.work / 'repo-alias'
        alias.symlink_to(repo, target_is_directory=True)
        parent_alias = self.work / 'parent-alias'
        parent_alias.symlink_to(self.work, target_is_directory=True)
        # This used to read names/agent.json relative to the discovered parent,
        # despite recording names/ as the repository in provenance.
        bad = [(repo / 'names', 'agent.json'), (alias, source['path']),
               (parent_alias / repo.name, source['path']), (repo / '.git', source['path'])]
        gitfile_repo = self.work / 'gitfile-repo'
        gitfile_repo.mkdir()
        (gitfile_repo / '.git').write_text('gitdir: ' + str(repo / '.git') + '\n')
        bad.append((gitfile_repo, source['path']))
        worktree = self.work / 'linked-worktree'
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-q', '--detach',
                        str(worktree), source['commit']], check=True)
        bad.append((worktree, source['path']))
        for directory, path in bad:
            with self.subTest(repo=directory), self.assertRaises(SpawnError):
                read_name_record(dict(source, repo=str(directory), path=path))
        # A .git symlink is refused even when its target is the original store.
        store = self.work / 'saved-git'
        (repo / '.git').rename(store)
        (repo / '.git').symlink_to(store, target_is_directory=True)
        with self.assertRaises(SpawnError):
            read_name_record(source)

    def test_repo_name_bare_store_and_explicit_git_directory(self):
        source = self.repo_name_source()
        bare = self.work / 'bare.git'
        subprocess.run(['git', 'clone', '-q', '--bare', '--no-hardlinks',
                        source['repo'], str(bare)], check=True)
        from connectome_spawn import sources
        original = subprocess.Popen
        commands = []

        def capture(argv, **kwargs):
            commands.append(argv)
            return original(argv, **kwargs)

        with patch.object(sources.subprocess, 'Popen', side_effect=capture):
            self.assertEqual(read_name_record(source), IDENTITY)
            self.assertEqual(read_name_record(dict(source, repo=str(bare))), IDENTITY)
        self.assertTrue(commands)
        for argv in commands:
            self.assertTrue(any(arg.startswith('--git-dir=') for arg in argv))
            self.assertNotIn('-C', argv)
            self.assertFalse(any(arg.startswith('--work-tree') for arg in argv))

    def test_repo_name_full_sha_ref_collisions_refused(self):
        source = self.repo_name_source()
        repo, commit = source['repo'], source['commit']
        other = subprocess.check_output(['git', '-C', repo, 'rev-parse', 'HEAD^']).decode().strip()
        for prefix in ('refs/heads/', 'refs/tags/', 'refs/custom/nested/', 'refs/'):
            for target in (commit, other):
                ref = prefix + commit
                subprocess.run(['git', '-C', repo, 'update-ref', ref, target], check=True)
                try:
                    with self.subTest(ref=ref, target=target), self.assertRaises(SpawnError):
                        read_name_record(source)
                finally:
                    subprocess.run(['git', '-C', repo, 'update-ref', '-d', ref], check=True)
        self.assertEqual(read_name_record(source), IDENTITY)

    def test_repo_name_without_prompt_remains_absent_through_activation(self):
        record = {k: v for k, v in IDENTITY.items() if k != 'systemPrompt'}
        source = self.repo_name_source(record)
        self.assertEqual(read_name_record(source), record)
        write(self.name_file, record)
        self.assertEqual(read_name_record(self.name_file), record)
        self.create()
        self.move('initiation')
        self.assertEqual(self.move('named', name_source=source)['identity'], record)
        active = self.move('active')
        self.assertNotIn('systemPrompt', active['identity'])
        self.assertNotIn('systemPrompt', self.adapter.events[-1][1]['identity'])
        self.assertEqual(self.restart().status('example-agent')['identity'], record)
        for bad in (None, '', 'TODO', 'none'):
            with self.subTest(prompt=bad), self.assertRaises(SpawnError):
                name_record(dict(record, systemPrompt=bad))

    def test_repo_name_refusals_leave_lifecycle_unchanged(self):
        source = self.repo_name_source()
        repo = source['repo']
        tree = subprocess.check_output(['git', '-C', repo, 'rev-parse', 'HEAD^{tree}']).decode().strip()
        blob = subprocess.check_output(['git', '-C', repo, 'rev-parse', 'HEAD:names/agent.json']).decode().strip()
        bad_sources = [dict(source, commit=v) for v in (
            'HEAD', source['commit'][:12], '0' * 40, 'f' * 40, source['commit'] + '^', tree, blob)]
        bad_sources += [dict(source, path=v) for v in (
            'missing.json', 'names', 'link.json', 'link.json/agent.json', 'submodule',
            '../names/agent.json', 'names/../names/agent.json', '/names/agent.json')]
        bad_sources += [dict(source, repo='https://example.invalid/config.git'),
                        dict(source, repo=str(self.work / 'absent')), dict(source, extra=True),
                        {'kind': 'repo', 'repo': repo, 'path': source['path']}, dict(source, kind='network')]
        self.create()
        self.move('initiation')
        before = self.life.status('example-agent')
        for bad in bad_sources:
            with self.subTest(source=bad), self.assertRaises(SpawnError):
                self.move('named', name_source=bad)
            self.assertEqual(self.life.status('example-agent'), before)
        with self.assertRaises(SpawnError):
            read_name_record(source, ingress_root=self.work)

    def test_repo_name_reuses_strict_record_decoder(self):
        source = self.repo_name_source()
        from connectome_spawn import sources
        for raw in (b'{"name":"A","name":"B"}', b'{bad', b'unknown: field', b'x' * (1024 * 1024 + 1)):
            with self.subTest(raw=raw[:32]), patch.object(sources, 'pinned_blob', return_value=(raw, source)), self.assertRaises(SpawnError):
                read_name_record(source)

    def test_clean_lifecycle_restart_and_idempotence(self):
        self.assertEqual(self.create()['state'], 'planned')
        self.assertEqual(self.create()['state'], 'planned')
        self.assertEqual(self.move('initiation')['state'], 'initiation')
        temporary = self.provider.list()[0]
        self.assertFalse(temporary['retired'])
        self.assertEqual(self.adapter.events[-1][1]['topic'], 'initiation')
        self.assertEqual(self.adapter.events[-1][1]['home'], HOME)
        self.assertEqual(self.adapter.events[-1][1]['initiationParticipants'], 'home-channel-members')
        self.assertEqual(self.adapter.events[-1][1]['temporaryIdentity']['agent'], 'example-agent')
        memory = self.root / 'example-agent/state/store/memory.bin'
        memory.parent.mkdir(parents=True)
        memory.write_bytes(b'initiation conversation')
        self.life = self.restart()
        self.move('initiation')
        self.assertEqual(len(self.adapter.events), 3)
        chosen = dict(IDENTITY, name='ChosenAgent', summaryParticipant='ChosenAgent',
                      systemPrompt='Remember the conversation with the creator.')
        write(self.name_file, chosen)
        self.assertEqual(self.move('named', name_file=self.name_file)['identity'], chosen)
        self.assertEqual(self.move('named', name_file=self.name_file)['state'], 'named')
        self.life = self.restart()
        self.assertEqual(self.move('active')['state'], 'active')
        self.assertEqual(self.move('active')['state'], 'active')
        self.assertEqual(sum(row['retired'] for row in self.provider.list()), 1)
        self.assertEqual(memory.read_bytes(), b'initiation conversation')
        self.assertEqual([e[0] for e in self.adapter.events],
                         ['build', 'join-initiation', 'start-initiation',
                          'redeploy-keep-memory', 'close-initiation'])
        self.assertEqual(self.adapter.events[-2][1]['identity'], chosen)
        context = self.adapter.events[-2][1]
        self.assertNotEqual(context['temporaryIdentity'], context['permanentIdentity'])
        self.assertEqual(self.provider.list()[0]['agent'], 'example-agent')
        self.assertEqual(self.move('retired')['state'], 'retired')
        self.assertEqual(self.move('retired')['state'], 'retired')
        with self.assertRaises(SpawnError):
            self.move('active', 'another-active')

    def test_required_seed_order_receipt_and_no_reingest(self):
        self.create('required')
        self.assertEqual(self.move('active', payload=PAYLOAD)['state'], 'active')
        self.assertEqual([e[0] for e in self.adapter.events], ['build', 'hold', 'activate'])
        marker = read(self.root / 'example-agent/state/seed.consumed')
        self.assertEqual(marker['digest'], hashlib.sha256(PAYLOAD).hexdigest())
        self.assertIn('+00:00', marker['timestamp'])
        self.assertEqual(self.importer.calls, 1)
        self.life = self.restart()
        self.move('active', payload=PAYLOAD)
        self.assertEqual(self.importer.calls, 1)
        with self.assertRaises(SpawnError):
            self.move('active', 'reimport', payload=PAYLOAD)
        with self.assertRaises(SpawnError):
            self.move('active', payload=b'changed')
        self.assertEqual(self.importer.calls, 1)

    def test_seed_refusals_and_explicit_optional_branches(self):
        for mode, payload in [('required', None), ('clean', PAYLOAD), ('unknown', None),
                              ('required', b''), ('optional', 'not bytes')]:
            with self.subTest(mode=mode, payload=payload), self.assertRaises(SpawnError):
                seed_intent({'mode': mode}, payload)
        for policy in ({}, {'mode': 'clean', 'extra': True}):
            with self.assertRaises(SpawnError):
                seed_intent(policy)
        self.assertEqual(seed_intent({'mode': 'optional'})['branch'], 'clean')
        self.assertEqual(seed_intent({'mode': 'optional'}, PAYLOAD)['branch'], 'seeded')
        for branch in ('clean', 'seeded'):
            self.assertTrue(any(s['needsProvider'] for s in lifecycle_steps(branch)))
        self.create('required')
        for payload in (None, b'different'):
            with self.assertRaises(SpawnError):
                self.move('active', payload=payload)
        self.assertEqual(self.adapter.events, [])

    def test_existing_store_is_recovery_not_import(self):
        self.create('required')
        (self.root / 'example-agent/state/store').mkdir(parents=True)
        self.assertIn('existing store', self.life.status('example-agent')['recovery'][0])
        with self.assertRaisesRegex(SpawnError, 're-ingest'):
            self.move('active', payload=PAYLOAD)
        self.assertEqual(self.importer.calls, 0)
        orphan = self.root / 'orphan-agent/state/store'
        orphan.mkdir(parents=True)
        with self.assertRaisesRegex(SpawnError, 'recovery'):
            self.create(agent='orphan-agent')

    def test_seed_ingest_crash_is_not_retried(self):
        self.create('required')
        self.importer.fail = True
        with self.assertRaises(RuntimeError):
            self.move('active', payload=PAYLOAD)
        self.life = self.restart()
        status = self.life.status('example-agent')
        self.assertEqual(status['state'], 'planned')
        self.assertEqual(status['pending']['step'], 'ingest')
        self.assertEqual(len(status['recovery']), 2)
        with self.assertRaisesRegex(SpawnError, 'replay refused'):
            self.move('active', payload=PAYLOAD)
        self.assertEqual(self.importer.calls, 1)
        self.assertNotIn('activate', [e[0] for e in self.adapter.events])

    def test_all_external_crash_points_refuse_replay(self):
        for failure in ('build', 'hold', 'activate', 'join-initiation', 'start-initiation',
                        'redeploy-keep-memory', 'close-initiation', 'retire'):
            with self.subTest(step=failure):
                self.root = self.work / failure
                self.provider = FileIdentityProvider(self.work / ('identities-' + failure))
                self.adapter = FakeAdapter()
                self.life = self.restart()
                seeded = failure in ('hold', 'activate')
                self.create('required' if seeded else 'clean')
                if failure in ('provision-permanent', 'redeploy-keep-memory', 'close-initiation'):
                    self.move('initiation')
                    self.move('named', name_file=self.name_file)
                self.adapter.fail = failure
                target = 'retired' if failure == 'retire' else 'active' if seeded or failure in (
                    'provision-permanent', 'redeploy-keep-memory', 'close-initiation') else 'initiation'
                args = {'payload': PAYLOAD} if seeded else {}
                with self.assertRaises(RuntimeError):
                    self.move(target, **args)
                self.life = self.restart()
                self.assertTrue(self.life.status('example-agent')['recovery'])
                before = len(self.adapter.events)
                with self.assertRaises(SpawnError):
                    self.move(target, **args)
                self.assertEqual(len(self.adapter.events), before)

    def test_identity_effect_crashes_report_recovery_without_replay(self):
        for failure in ('provision_temporary', 'provision_permanent', 'rename', 'retire'):
            with self.subTest(step=failure):
                self.root = self.work / failure
                provider_root = self.work / ('identities-' + failure)
                self.provider = FileIdentityProvider(provider_root, supports_rename=failure == 'rename')
                self.life = self.restart()
                self.create()
                if failure != 'provision_temporary':
                    self.move('initiation')
                    self.move('named', name_file=self.name_file)
                original = getattr(self.provider, failure)
                def crash(*args):
                    original(*args)
                    raise RuntimeError('crashed after identity effect')
                target = 'initiation' if failure == 'provision_temporary' else 'active'
                with patch.object(self.provider, failure, side_effect=crash), self.assertRaises(RuntimeError):
                    self.move(target)
                status = self.restart().status('example-agent')
                self.assertTrue(status['recovery'])
                provider = FileIdentityProvider(provider_root)
                before = provider.list()
                self.assertTrue(before)
                with self.assertRaises(SpawnError):
                    self.restart().transition('example-agent', target, target)
                self.assertEqual(provider.list(), before)
                self.assertTrue(all('liveness unknown' in row['recovery'] for row in before if not row['retired']))

    def test_illegal_transition_matrix_and_failure_retirement(self):
        self.create()
        path = self.root / 'example-agent/lifecycle.json'
        base = read(path)
        legal = {('planned', 'initiation'), ('initiation', 'named'), ('named', 'active')}
        legal |= {(s, 'failed') for s in ('planned', 'initiation', 'named', 'active')}
        legal |= {(s, 'retired') for s in STATES if s != 'retired'}
        for source in STATES:
            for target in STATES:
                if (source, target) in legal:
                    continue
                write(path, dict(base, state=source))
                kwargs = {'name_file': self.name_file} if target == 'named' else {}
                with self.subTest(source=source, target=target), self.assertRaises(SpawnError):
                    self.move(target, 'matrix', **kwargs)
        for source in ('planned', 'initiation', 'named', 'active'):
            write(path, dict(base, state=source))
            self.assertEqual(self.move('failed')['state'], 'failed')
            self.assertEqual(self.move('retired')['state'], 'retired')
        for source in ('planned', 'initiation', 'named', 'active'):
            write(path, dict(base, state=source))
            self.assertEqual(self.move('retired')['state'], 'retired')

    def test_refuse_placeholders_unsafe_records_and_operation_conflicts(self):
        for key in IDENTITY:
            for bad in ('TODO', '{{ value }}', '<name>', 'name', 'bad\x00value', 'bad\u202evalue'):
                with self.subTest(key=key, bad=bad), self.assertRaises(SpawnError):
                    name_record(dict(IDENTITY, **{key: bad}))
        for key in ('name', 'summaryParticipant', 'displayName'):
            with self.assertRaises(SpawnError):
                name_record(dict(IDENTITY, **{key: '../unsafe;command'}))
        for record in ({}, dict(IDENTITY, extra=True), dict(IDENTITY, systemPrompt='x' * 65537)):
            with self.assertRaises(SpawnError):
                name_record(record)
        self.assertEqual(read_name_record(self.name_file), IDENTITY)
        self.assertEqual(read_name_record(path=self.name_file), IDENTITY)
        for channel in ('TODO', '../escape', '${CHANNEL}'):
            with self.assertRaises(SpawnError):
                self.life.create('example-agent', seed={'mode': 'clean'}, identity=IDENTITY,
                                 home=dict(HOME, channel=channel), operation_id='create')
        self.create()
        with self.assertRaises(SpawnError):
            self.move('initiation', 'create')
        with self.assertRaises(SpawnError):
            self.move('initiation', '../bad')
        with self.assertRaises(SpawnError):
            self.move('initiation', payload=PAYLOAD)
        with self.assertRaises(SpawnError):
            self.move('initiation', name_file=self.name_file)
        self.move('initiation')
        with self.assertRaises(SpawnError):
            self.move('named')
        self.move('named', name_file=self.name_file)
        write(self.name_file, dict(IDENTITY, name='DifferentAgent'))
        with self.assertRaises(SpawnError):
            self.move('named', name_file=self.name_file)
        path = self.root / 'example-agent/lifecycle.json'
        valid = read(path)
        for key in IDENTITY:
            altered = deepcopy(valid)
            altered['identity'][key] = 'CHANGEME'
            write(path, altered)
            with self.assertRaises(SpawnError):
                self.move('active')
        altered = deepcopy(valid)
        altered['home']['channel'] = 'CHANGEME'
        write(path, altered)
        with self.assertRaises(SpawnError):
            self.move('active')

    def test_status_cli_read_only_and_missing_state(self):
        self.create()
        before = {p: p.read_bytes() for p in self.work.rglob('*') if p.is_file()}
        command = [str(ROOT / 'scripts/connectome-spawn'), 'status', 'example-agent',
                   '--state-root', str(self.root)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['state'], 'planned')
        self.assertEqual(before, {p: p.read_bytes() for p in self.work.rglob('*') if p.is_file()})
        for args in (command[:-2], command + ['--inline', '{}'],
                     [command[0], 'status', '../escape', '--state-root', str(self.root)]):
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 2)
        absent = self.work / 'missing'
        with self.assertRaises(SpawnError):
            Lifecycle(absent).status('example-agent')
        self.assertFalse(absent.exists())

    def test_persistence_crash_before_and_after_replace(self):
        path = self.work / 'atomic.json'
        write(path, {'version': 1})
        with patch('connectome_spawn.persistence.os.replace', side_effect=OSError('crash')):
            with self.assertRaises(OSError):
                write(path, {'version': 2})
        self.assertEqual(read(path), {'version': 1})
        with patch('connectome_spawn.persistence.sync_dir', side_effect=OSError('crash')):
            with self.assertRaises(OSError):
                write(path, {'version': 2})
        self.assertEqual(read(path), {'version': 2})
        self.assertEqual(list(self.work.glob('.spawn-*')), [])

    def test_marker_write_crash_and_missing_adapters(self):
        self.create('required')
        for attr in ('adapter', 'importer'):
            current = getattr(self.life, attr)
            setattr(self.life, attr, None)
            with self.assertRaises(SpawnError):
                self.move('active', payload=PAYLOAD)
            setattr(self.life, attr, current)
        actual_write = write
        def crash(path, value):
            if Path(path).name == 'seed.consumed':
                raise OSError('crash before marker commit')
            actual_write(path, value)
        with patch('connectome_spawn.lifecycle.write', side_effect=crash), self.assertRaises(OSError):
            self.move('active', payload=PAYLOAD)
        with self.assertRaises(SpawnError):
            self.restart().transition('example-agent', 'active', 'active', payload=PAYLOAD)
        self.assertEqual(self.importer.calls, 1)

    def test_provider_restart_idempotence_unique_ownership_and_concurrency(self):
        root = self.work / 'identities'
        with ProcessPoolExecutor(max_workers=2) as executor:
            handles = list(executor.map(competing_identity, [root, root], ['first-agent', 'second-agent']))
        self.assertNotEqual(handles[0]['id'], handles[1]['id'])
        provider = FileIdentityProvider(root)
        handle = handles[0]
        self.assertEqual(provider.provision_temporary('first-agent', 'ConnectomeNewbornfirstagent', 'temporary'), handle)
        provider.retire(handle, 'retire')
        provider.retire(handle, 'retire')
        provider.retire(handle, 'retire-again')
        later = provider.provision_temporary('third-agent', 'ConnectomeNewbornthirdagent', 'temporary')
        self.assertNotIn(later['id'], {h['id'] for h in handles})
        provider.retire(handle, 'retire-again')
        self.assertFalse(next(r for r in provider.list() if r['id'] == later['id'])['retired'])
        with self.assertRaises(SpawnError):
            provider.retire(dict(handle, id='a' * 32), 'unknown')
        with self.assertRaises(SpawnError):
            provider.provision_temporary('../escape', 'SafeName', 'temporary')
        with self.assertRaises(SpawnError):
            provider.provision_temporary('first-agent', 'ChangedName', 'temporary')
        with self.assertRaises(SpawnError):
            provider.provision_temporary('another-agent', 'ConnectomeNewbornthirdagent', 'temporary')
        with self.assertRaises(SpawnError):
            FileIdentityProvider(root, supports_rename=True)
        with self.assertRaises(SpawnError):
            provider.rename(later, IDENTITY, 'rename')
        permanent = provider.provision_permanent('third-agent', IDENTITY, 'permanent')
        self.assertEqual(provider.provision_permanent('third-agent', IDENTITY, 'permanent'), permanent)
        with self.assertRaises(SpawnError):
            provider.provision_permanent('third-agent', dict(IDENTITY, name='ChangedAgent'), 'permanent')

    def test_process_death_releases_lock_but_never_replays_import(self):
        self.create('required')
        process = multiprocessing.get_context('fork').Process(target=crash_during_import, args=(self.root,))
        process.start()
        process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join()
            self.fail('crash fixture did not exit')
        self.assertEqual(process.exitcode, 17)
        status = self.restart().status('example-agent')
        self.assertEqual(status['pending']['step'], 'ingest')
        self.assertEqual(len(status['recovery']), 2)
        with self.assertRaises(SpawnError):
            self.restart().transition('example-agent', 'active', 'crashed-process', payload=PAYLOAD)
        self.assertEqual(self.importer.calls, 0)

    def test_optional_seeded_lifecycle_and_seeded_transition_refusals(self):
        self.life.create('example-agent', seed={'mode': 'optional'}, identity=IDENTITY,
                         home=HOME, payload=PAYLOAD, operation_id='create')
        for target in ('initiation', 'named'):
            args = {'name_file': self.name_file} if target == 'named' else {}
            with self.assertRaises(SpawnError):
                self.move(target, **args)
        self.move('active', payload=PAYLOAD)
        self.assertEqual(self.life.status('example-agent')['seed']['mode'], 'optional')
        self.move('failed')
        self.move('retired')

    def test_missing_provider_and_retire_never_reuses_temporary(self):
        self.create()
        self.life.identity_provider = None
        with self.assertRaises(SpawnError):
            self.move('initiation')
        self.assertIsNone(self.life.status('example-agent')['pending'])
        self.life.identity_provider = self.provider
        self.move('initiation')
        self.move('failed')
        self.assertFalse(self.provider.list()[0]['retired'])
        self.move('retired')
        self.assertTrue(self.provider.list()[0]['retired'])
        self.assertEqual(self.adapter.events[-1][0], 'close-initiation')

    def test_rename_preserves_handle_without_retiring_it(self):
        self.provider = FileIdentityProvider(self.work / 'rename-identities', supports_rename=True)
        self.life = self.restart()
        self.create()
        temporary = self.move('initiation')['temporaryIdentity']
        self.move('named', name_file=self.name_file)
        active = self.move('active')
        self.assertEqual(active['permanentIdentity'], temporary)
        self.assertIsNone(active['temporaryIdentity'])
        self.assertEqual(len(self.provider.list()), 1)
        row = self.provider.list()[0]
        self.assertFalse(row['retired'])
        self.assertEqual(row['name'], IDENTITY['name'])
        self.assertEqual(row['kind'], 'permanent')
        self.assertEqual(self.move('active')['permanentIdentity'], temporary)
        self.assertEqual(self.provider.rename(temporary, IDENTITY, 'active:rename'), temporary)
        with self.assertRaises(SpawnError):
            self.provider.rename(temporary, IDENTITY, 'new-rename')

    def test_name_convention_capability_validation_and_no_truncation(self):
        instant = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
        self.assertEqual(temporary_name(None, instant), 'ConnectomeNewborn20260102T030405123456Z')
        self.assertEqual(temporary_name({'temporaryNameTemplate': 'Agent_{timestamp}'}, instant),
                         'Agent_20260102T030405123456Z')
        for pattern in ('NoTimestamp', '{timestamp}{timestamp}', 'bad/{timestamp}',
                        'Bad {timestamp}', 'X{unknown}{timestamp}', 1):
            with self.assertRaises(SpawnError):
                identity_policy({'temporaryNameTemplate': pattern})
        with self.assertRaises(SpawnError):
            identity_policy({'unknown': True})
        self.create()
        first = self.life.status('example-agent')['temporaryName']
        self.assertEqual(self.create()['temporaryName'], first)
        self.provider = FileIdentityProvider(self.work / 'small-identities', max_name_length=10)
        self.life = self.restart()
        with self.assertRaisesRegex(SpawnError, 'never truncated'):
            self.move('initiation')
        self.assertIsNone(self.life.status('example-agent')['pending'])
        self.assertEqual(self.provider.list(), [])
        restricted = FileIdentityProvider(self.work / 'restricted-identities', name_pattern=r'[A-Z]+')
        with self.assertRaises(SpawnError):
            validate_provider_name(restricted, first)
        for settings in ({'max_name_length': 0}, {'supports_rename': 'yes'}, {'name_pattern': '['}):
            with self.assertRaises(SpawnError):
                FileIdentityProvider(self.work / ('invalid-' + str(len(settings)) + str(settings.keys())), **settings)

    def test_changed_capability_and_invalid_provider_handles_refused(self):
        self.create()
        self.move('initiation')
        self.move('named', name_file=self.name_file)
        self.provider.supports_rename = True
        with self.assertRaises(SpawnError):
            self.move('active')
        self.assertIsNone(self.life.status('example-agent')['pending'])
        self.provider.supports_rename = False
        temporary = self.life.status('example-agent')['temporaryIdentity']
        with patch.object(self.provider, 'provision_permanent', return_value=temporary), self.assertRaises(SpawnError):
            self.move('active')
        self.assertTrue(self.life.status('example-agent')['recovery'])
        self.assertNotIn('redeploy-keep-memory', [e[0] for e in self.adapter.events])

    def test_provider_constraints_refuse_name_before_named_state(self):
        self.create()
        self.move('initiation')
        self.provider.max_name_length = 5
        with self.assertRaises(SpawnError):
            self.move('named', name_file=self.name_file)
        self.assertEqual(self.life.status('example-agent')['state'], 'initiation')
        self.assertIsNone(self.life.status('example-agent')['pending'])

    def test_store_created_during_build_refuses_ingest_and_bad_receipt(self):
        self.create('required')
        original = self.adapter.perform
        def build(step, context, operation_id):
            original(step, context, operation_id)
            if step == 'build':
                Path(context['store']).mkdir(parents=True)
        with patch.object(self.adapter, 'perform', side_effect=build), self.assertRaises(SpawnError):
            self.move('active', payload=PAYLOAD)
        self.assertEqual(self.importer.calls, 0)
        marker = self.root / 'example-agent/state/seed.consumed'
        write(marker, {'digest': 'f' * 64, 'timestamp': '2026-01-01T00:00:00+00:00'})
        with self.assertRaises(SpawnError):
            self.life.status('example-agent')

    def test_crash_after_marker_and_before_final_state_commit(self):
        for point in ('marker', 'commit'):
            self.root = self.work / point
            self.life = self.restart()
            self.create('required')
            def crash(path, value):
                if point == 'marker' and Path(path).name == 'seed.consumed':
                    write(path, value)
                    raise OSError('crash after marker commit')
                if point == 'commit' and value.get('state') == 'active':
                    raise OSError('crash before state commit')
                write(path, value)
            with patch('connectome_spawn.lifecycle.write', side_effect=crash), self.assertRaises(OSError):
                self.move('active', payload=PAYLOAD)
            self.assertTrue((self.root / 'example-agent/state/seed.consumed').is_file())
            self.assertTrue(self.restart().status('example-agent')['recovery'])
            count = self.importer.calls
            with self.assertRaises(SpawnError):
                self.restart().transition('example-agent', 'active', 'new-op', payload=PAYLOAD)
            self.assertEqual(self.importer.calls, count)

    def test_name_ingress_component_boundary(self):
        ingress = self.work / 'ingress'
        nested = ingress / 'nested' / 'deeper'
        nested.mkdir(parents=True)
        outside = self.work / 'outside'
        outside.mkdir()
        write(outside / 'name.json', dict(IDENTITY, name='OutsideAgent'))
        write(nested / 'name.json', IDENTITY)
        (ingress / 'alias').symlink_to(outside, target_is_directory=True)
        (ingress / 'nested' / 'alias').symlink_to(outside, target_is_directory=True)
        alias_root = self.work / 'ingress-alias'
        alias_root.symlink_to(ingress, target_is_directory=True)
        self.life = Lifecycle(self.root, adapter=self.adapter, identity_provider=self.provider,
                              ingress_root=ingress)
        self.create()
        self.move('initiation')
        probes = [str(ingress) + '/../outside/name.json', ingress / 'alias/name.json',
                  ingress / 'nested/alias/name.json', outside / 'name.json',
                  alias_root / 'nested/deeper/name.json']
        for path in probes:
            with self.subTest(path=path), self.assertRaises(SpawnError):
                self.move('named', name_file=path)
            self.assertEqual(self.life.status('example-agent')['state'], 'initiation')
            self.assertIsNone(self.life.status('example-agent')['pending'])
        with self.assertRaises(SpawnError):
            Lifecycle(self.root, ingress_root=alias_root)
        with self.assertRaises(SpawnError):
            read_name_record(alias_root / 'nested/deeper/name.json', ingress_root=alias_root)
        for path in probes[:3]:
            with self.assertRaises(SpawnError):
                read_name_record(path)  # Compatibility API must be safe too.
        self.assertEqual(self.move('named', name_file=nested / 'name.json')['identity'], IDENTITY)

    def test_hard_linked_name_record_refused_before_named_transition(self):
        ingress = self.work / 'ingress'
        ingress.mkdir()
        linked = ingress / 'name.json'
        os.link(self.name_file, linked)
        self.life = Lifecycle(self.root, adapter=self.adapter, identity_provider=self.provider,
                              ingress_root=ingress)
        self.create()
        self.move('initiation')
        with self.assertRaisesRegex(SpawnError, 'single-link'):
            read_name_record(linked, ingress_root=ingress)
        with self.assertRaisesRegex(SpawnError, 'single-link'):
            self.move('named', name_file=linked)
        self.assertEqual(self.life.status('example-agent')['state'], 'initiation')
        self.assertIsNone(self.life.status('example-agent')['pending'])
        linked.unlink()
        write(linked, IDENTITY)
        self.assertEqual(read_name_record(linked, ingress_root=ingress), IDENTITY)
        self.assertEqual(self.move('named', name_file=linked)['identity'], IDENTITY)

    def test_hard_linked_seed_and_lifecycle_refused(self):
        from connectome_spawn.seed import seed_payload
        outside = self.work / 'outside'
        outside.mkdir()
        seed = outside / 'seed.bin'
        seed.write_bytes(PAYLOAD)
        linked = self.work / 'seed.bin'
        os.link(seed, linked)
        with self.assertRaisesRegex(SpawnError, 'single-link'):
            seed_payload(linked)
        linked.unlink()
        linked.write_bytes(PAYLOAD)
        self.assertEqual(seed_payload(linked), PAYLOAD)
        self.create('required')
        path = self.root / 'example-agent/lifecycle.json'
        saved = outside / 'lifecycle.json'
        path.rename(saved)
        os.link(saved, path)
        with self.assertRaisesRegex(SpawnError, 'single-link'):
            self.life.status('example-agent')
        saved.unlink()
        self.assertEqual(self.life.status('example-agent')['state'], 'planned')

    def test_all_local_reads_refuse_traversal_symlinks_and_nonregular(self):
        from connectome_spawn.sources import read_file
        from connectome_spawn.seed import seed_payload
        real = self.work / 'real'
        real.mkdir()
        write(real / 'data.json', IDENTITY)
        alias = self.work / 'read-alias'
        alias.symlink_to(real, target_is_directory=True)
        final = real / 'link.json'
        final.symlink_to(real / 'data.json')
        fifo = real / 'fifo'
        os.mkfifo(fifo)
        for reader in (read_file, seed_payload, read, read_name_record):
            for path in (alias / 'data.json', final, fifo, real,
                         str(real) + '/../real/data.json'):
                with self.subTest(reader=reader.__name__, path=path), self.assertRaises(SpawnError):
                    reader(path)
            self.assertTrue(reader(real / 'data.json'))
        self.create('required')
        self.move('active', payload=PAYLOAD)
        agent_root = self.root / 'example-agent'
        for relative in ('lifecycle.json', 'state/seed.consumed'):
            path = agent_root / relative
            raw = path.read_bytes()
            saved = self.work / ('saved-' + path.name)
            saved.write_bytes(raw)
            path.unlink()
            path.symlink_to(saved)
            with self.assertRaises(SpawnError):
                self.life.status('example-agent')
            path.unlink()
            path.write_bytes(raw)
        state = agent_root / 'state'
        state.rename(agent_root / 'original-state')
        state.symlink_to(agent_root / 'original-state', target_is_directory=True)
        with self.assertRaises(SpawnError):
            self.life.status('example-agent')
        with self.assertRaises(SpawnError):
            Lifecycle(str(self.root) + '/../agents').status('example-agent')

    def test_symlinks_corrupt_state_and_migration_refused(self):
        self.create()
        alias = self.work / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(SpawnError):
            Lifecycle(alias)
        self.name_file.unlink()
        self.name_file.symlink_to(self.root / 'example-agent/lifecycle.json')
        with self.assertRaises(SpawnError):
            read_name_record(self.name_file)
        path = self.root / 'example-agent/lifecycle.json'
        path.write_text('{"state":"active"}')
        with self.assertRaises(SpawnError):
            self.life.status('example-agent')
        with self.assertRaisesRegex(SpawnError, 'migrate template v1'):
            template({'schema': 'connectome.spawn-template/v1', 'seedPolicy': 'clean'})


if __name__ == '__main__':
    unittest.main()
