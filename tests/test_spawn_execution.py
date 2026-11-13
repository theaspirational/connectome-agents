"""Executor-only lifecycle tests. No host commands or outbound traffic."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'roles/connectome_agent/module_utils'))
from connectome_spawn import Lifecycle, SpawnError, FileIdentityProvider
from connectome_spawn.persistence import read, write

IDENTITY = {'name': 'ExampleAgent', 'displayName': 'Example Agent', 'summaryParticipant': 'ExampleAgent'}
HOME = {'transport': 'console', 'channel': 'primary'}


class LostAck:
    def __init__(self):
        self.calls = []
        self.fail = True
        self.observations = []

    def perform(self, step, context, operation_id):
        self.calls.append(step)
        if self.fail:
            raise RuntimeError('lost acknowledgement')

    def observe(self, context, operation_id):
        self.observations.append(operation_id)
        return {'state': 'unknown', 'evidence': None}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.cache').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.cache')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.adapter = LostAck()
        self.life = Lifecycle(self.root / 'agents', adapter=self.adapter,
                              identity_provider=FileIdentityProvider(self.root / 'identities'))
        self.life.create('example-agent', seed={'mode': 'clean'}, identity=IDENTITY, home=HOME, operation_id='create')
        with self.assertRaises(RuntimeError):
            self.life.transition('example-agent', 'initiation', 'init')

    def test_recovery_requires_reason_and_never_replays(self):
        with self.assertRaises(SpawnError):
            self.life.recover('example-agent', operation_id='init', observed='not-applied', reason='')
        before = list(self.adapter.calls)
        observed = self.life.reconcile('example-agent')
        self.assertEqual(observed['observation']['state'], 'unknown')
        self.assertEqual(self.adapter.calls, before)
        for op in ('init', 'different'):
            with self.assertRaises(SpawnError):
                self.life.transition('example-agent', 'initiation', op)
        recovered = self.life.recover('example-agent', operation_id='init', observed='applied',
                                     reason='Inspected the completed offline build')
        self.assertEqual(self.adapter.calls, before)
        audit = recovered['audit'][-1]
        self.assertEqual(audit['who'], {'uid': os.getuid(), 'euid': os.geteuid()})
        self.assertIn('when', audit)
        self.assertEqual(audit['reason'], 'Inspected the completed offline build')
        self.adapter.fail = False
        self.life.transition('example-agent', 'initiation', 'init')
        self.assertEqual(self.adapter.calls.count('build'), 1)

    def test_not_applied_needs_observation_before_explicit_retry(self):
        self.life.recover('example-agent', operation_id='init', observed='not-applied', reason='Checked absence')
        self.assertEqual(self.adapter.calls, ['build'])
        self.assertTrue(self.adapter.observations)
        self.adapter.fail = False
        self.life.transition('example-agent', 'initiation', 'init')
        self.assertEqual(self.adapter.calls.count('build'), 2)

    def test_blind_retry_is_refused_even_when_executor_would_succeed(self):
        self.adapter.fail = False
        before = list(self.adapter.calls)
        with self.assertRaises(SpawnError):
            self.life.transition('example-agent', 'initiation', 'init')
        self.assertEqual(self.adapter.calls, before)

    def test_recovery_cli_requires_reason(self):
        cmd = [sys.executable, str(ROOT / 'scripts/connectome-spawn'), 'recover', 'example-agent',
               '--state-root', str(self.root / 'agents'), '--operation', 'init', '--observed', 'applied']
        self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 2)
        result = subprocess.run(cmd + ['--reason', 'Inspected offline build'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['pending']['ready'], True)

    def test_recover_twice_cannot_flip_resolution(self):
        self.life.recover('example-agent', operation_id='init', observed='applied', reason='Inspected build')
        before = self.life.status('example-agent')
        with self.assertRaisesRegex(SpawnError, 'already resolved'):
            self.life.recover('example-agent', operation_id='init', observed='not-applied', reason='Try reversing grant')
        self.assertEqual(self.life.status('example-agent'), before)

    def test_resolved_operation_still_refuses_other_id(self):
        self.life.recover('example-agent', operation_id='init', observed='not-applied', reason='Confirmed no build')
        self.adapter.fail = False
        before = list(self.adapter.calls)
        with self.assertRaisesRegex(SpawnError, 'replay refused'):
            self.life.transition('example-agent', 'initiation', 'other-id')
        self.assertEqual(self.adapter.calls, before)
        self.assertTrue(self.life.status('example-agent')['pending']['ready'])

    def test_recover_cannot_undo_completed_checkpoint(self):
        # Crash after checkpoint receipt is durable, before the next intent.
        from connectome_spawn import lifecycle
        original = lifecycle.write
        def crash_after_receipt(path, value):
            original(path, value)
            if value.get('pending', {}).get('step') == 'build:completed':
                raise RuntimeError('fixture crash after completed receipt')
        self.life.recover('example-agent', operation_id='init', observed='not-applied', reason='Build was absent')
        self.adapter.fail = False
        with patch.object(lifecycle, 'write', crash_after_receipt):
            with self.assertRaises(RuntimeError):
                self.life.transition('example-agent', 'initiation', 'init')
        before = self.life.status('example-agent')
        with self.assertRaisesRegex(SpawnError, 'cannot be undone'):
            self.life.recover('example-agent', operation_id='init', observed='not-applied', reason='Try undoing receipt')
        self.assertEqual(self.life.status('example-agent'), before)

    def test_cli_errors_are_distinct_and_typos_do_not_create_directories(self):
        def run(agent, *extra):
            cmd = [sys.executable, str(ROOT / 'scripts/connectome-spawn'), 'recover', agent,
                   '--state-root', str(self.root / 'agents'), *extra]
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            return json.loads(result.stderr)
        extra = ('--operation', 'wrong-id', '--observed', 'applied', '--reason', 'Private operator text')
        mismatch = run('example-agent', *extra)
        unknown = run('misspelled-agent', *extra)
        self.assertEqual(mismatch['code'], 'pending-operation-mismatch')
        self.assertEqual(unknown['code'], 'unknown-agent')
        self.assertNotIn('Private operator text', json.dumps(mismatch))
        self.assertFalse((self.root / 'agents/misspelled-agent').exists())
        for command in ('reconcile', 'status'):
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/connectome-spawn'), command,
                'misspelled-agent', '--state-root', str(self.root / 'agents')], capture_output=True, text=True)
            self.assertEqual(json.loads(result.stderr)['code'], 'unknown-agent')
            self.assertFalse((self.root / 'agents/misspelled-agent').exists())


class FakeExecutor:
    """Only local memory/fixture effects; receipts can outlive a lost ack."""
    def __init__(self, root):
        self.root = root
        self.actions, self.observations, self.receipts = [], [], {}
        self.fail = None
        self.health = {'state': 'unknown', 'evidence': None}
        self.auth = {'state': 'unknown', 'evidence': None}
        self.reply = {'state': 'unknown', 'evidence': None}
        self.handshake = {'state': 'unknown', 'evidence': None}
        self.before = None

    def execute(self, action, inputs, operation_id):
        if self.before:
            self.before(action, inputs, operation_id)
        self.actions.append((action, deepcopy(inputs), operation_id))
        result = {'ready': True} if action == 'ai-access' else None
        if action in ('provision-temporary', 'provision-permanent'):
            result = {'id': action, 'agent': inputs['agent']}
        if action == 'rename':
            result = inputs['handle']
        if action == 'ingest':
            Path(inputs['store']).mkdir(parents=True, exist_ok=False)
            (Path(inputs['store']) / 'fixture.bin').write_bytes(inputs['payload'])
        receipt = {'state': 'applied', 'evidence': {'fixture': operation_id}, 'result': result}
        if action == 'mcp-handshake':
            receipt = deepcopy(self.handshake)
        self.receipts[operation_id] = receipt
        if self.fail == action:
            raise RuntimeError('fixture lost ack')
        return receipt

    def observe(self, action, inputs, operation_id):
        self.observations.append((action, deepcopy(inputs), operation_id))
        if action == 'service-health':
            return self.health
        if action == 'provider-authenticated':
            return self.auth
        if action == 'first-real-reply':
            return self.reply
        return deepcopy(self.receipts.get(operation_id, {'state': 'unknown', 'evidence': None}))


def proof(**result):
    return {'state': 'applied', 'evidence': {'fixture': 'offline-observation'}, 'result': result}


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        import test_spawn as fixture
        fixture.SpawnTests.setUp(self)
        self.plan = lambda **kwargs: fixture.SpawnTests.plan(self, **kwargs)
        self.declaration['homeDirectory'] = str(self.work / 'home')
        self.envelope = self.plan()
        self.executor = FakeExecutor(self.work)
        from connectome_spawn import ExecutionBackend
        self.backend = ExecutionBackend(self.work / 'agents', self.envelope,
            private_repo=str(self.work / 'private-repo'), build_root=str(self.work / 'build-root'), executor=self.executor)
        self.agent = self.declaration['instance']
        self.name_file = self.work / 'chosen.json'
        write(self.name_file, {'name': 'ChosenAgent', 'displayName': 'Chosen Agent', 'summaryParticipant': 'ChosenAgent'})

    def promote(self):
        self.executor.health = proof(ready=True)
        self.executor.handshake = proof(initialized=True, toolsListed=True)
        self.backend.apply('init')
        self.backend.apply('name', target='named', name_source=self.name_file)
        return self.backend.apply('promote', target='active')

    def test_promotion_requires_health_and_every_handshake(self):
        self.backend.apply('init')
        self.backend.apply('name', target='named', name_source=self.name_file)
        self.executor.health = proof(ready=True)
        # Test unknown AND explicit failure, including a single bad connector.
        for receipt in ({'state': 'unknown', 'evidence': None},
                        proof(initialized=True, toolsListed=False)):
            self.executor.handshake = receipt
            with self.assertRaisesRegex(SpawnError, 'serviceReady'):
                self.backend.apply('promote', target='active')
            state = self.backend.life.status(self.agent)
            self.assertEqual(state['state'], 'named')
            self.assertIsNotNone(state['temporaryIdentity'])
            self.assertTrue(state['topicOpen'])
            self.assertNotIn('retire-identity', [a for a, _, _ in self.executor.actions])
            self.assertNotIn('close-initiation', [a for a, _, _ in self.executor.actions])
            self.assertIn('serviceReady', ' '.join(state['recovery']))
            # Fixture confirms the probe is closed and discards its unsuccessful receipt.
            pending_step = state['pending']['step']
            self.executor.receipts.pop('promote:' + pending_step, None)
            self.backend.life.recover(self.agent, operation_id='promote', observed='not-applied',
                                      reason='Closed fixture probe; explicit new probe authorized')
        self.executor.handshake = proof(initialized=True, toolsListed=True)
        result = self.backend.apply('promote', target='active')
        self.assertEqual(result['state'], 'active')
        self.assertEqual(result['verification']['serviceReady']['state'], 'success')
        self.assertTrue(any(k.startswith('serviceReady:') for k in result['receipts']['promote']))

    def test_retirement_closes_topic_with_live_identity_before_deactivation(self):
        self.backend.apply('init')
        def before(action, inputs, operation_id):
            if action == 'close-initiation':
                self.assertIsNotNone(inputs['identity'])
                self.assertNotIn('retire-identity', [a for a, _, _ in self.executor.actions])
        self.executor.before = before
        self.backend.apply('retire', target='retired')
        actions = [a for a, _, _ in self.executor.actions]
        self.assertLess(actions.index('close-initiation'), actions.index('retire-identity'))

    def test_verify_retired_refuses_without_handshakes(self):
        self.backend.apply('init')
        self.backend.apply('retire', target='retired')
        count = len(self.executor.actions)
        with self.assertRaisesRegex(SpawnError, 'initiation or active'):
            self.backend.verify('retired-probe')
        self.assertEqual(len(self.executor.actions), count)

    def test_recover_promotion_applied_requires_observer_evidence(self):
        self.backend.apply('init')
        self.backend.apply('name', target='named', name_source=self.name_file)
        self.executor.fail = 'switch-start'
        with self.assertRaises(RuntimeError):
            self.backend.apply('promote', target='active')
        with self.assertRaisesRegex(SpawnError, 'applied observer evidence'):
            Lifecycle(self.work / 'agents').recover(self.agent, operation_id='promote',
                observed='applied', reason='No evidence is not proof')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/connectome-spawn'), 'recover', self.agent,
            '--state-root', str(self.work / 'agents'), '--operation', 'promote', '--observed', 'applied',
            '--reason', 'No evidence is not proof'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        error = json.loads(result.stderr)
        self.assertEqual(error['code'], 'observer-evidence-required')
        self.assertIsNone(error['filesWritten'])
        self.assertFalse(self.backend.life.status(self.agent)['pending']['ready'])

    def seeded_backend(self):
        from connectome_spawn import ExecutionBackend
        self.spec['seed']['mode'] = 'required'
        payload = b'fixture seed'
        backend = ExecutionBackend(self.work / 'seeded-agents', self.plan(seed_payload=payload),
            private_repo=str(self.work / 'private-repo'), build_root=str(self.work / 'seeded-builds'), executor=self.executor)
        return backend, payload

    def test_recover_ingest_applied_requires_observer_evidence(self):
        backend, payload = self.seeded_backend()
        self.executor.fail = 'ingest'
        with self.assertRaises(RuntimeError):
            backend.apply('seed', payload=payload)
        store = Path(backend.life.status(self.agent)['store'])
        marker = store.parent / 'seed.consumed'
        with self.assertRaisesRegex(SpawnError, 'applied observer evidence'):
            Lifecycle(self.work / 'seeded-agents').recover(self.agent, operation_id='seed',
                observed='applied', reason='Partial store is not evidence')
        self.assertFalse(marker.exists())
        self.assertFalse(backend.life.status(self.agent)['pending']['ready'])
        backend.life.recover(self.agent, operation_id='seed', observed='applied', reason='Observed durable import receipt')
        self.executor.fail = None
        self.executor.health = proof(ready=True)
        self.executor.handshake = proof(initialized=True, toolsListed=True)
        self.assertEqual(backend.apply('seed', payload=payload)['state'], 'active')
        self.assertEqual(sum(a == 'ingest' for a, _, _ in self.executor.actions), 1)
        self.assertTrue(marker.exists())

    def test_seeded_activation_requires_health_evidence(self):
        backend, payload = self.seeded_backend()
        self.executor.handshake = proof(initialized=True, toolsListed=True)
        with self.assertRaisesRegex(SpawnError, 'serviceReady'):
            backend.apply('seed', payload=payload)
        state = backend.life.status(self.agent)
        self.assertEqual(state['state'], 'planned')
        self.assertTrue(state['pending']['step'].startswith('serviceReady:'))
        self.assertTrue((Path(state['store']).parent / 'seed.consumed').exists())
        self.assertNotIn('seed', state['operations'])

    def test_gate_checks_every_connector_not_just_first(self):
        self.backend.adapter.plan['recipe']['mcpServers']['another-fixture'] = {}
        # The adapter is explicitly a test fixture; both probes must be journaled.
        self.executor.health = proof(ready=True)
        self.executor.handshake = proof(initialized=True, toolsListed=True)
        original = self.executor.execute
        def execute(action, inputs, operation_id):
            if action == 'mcp-handshake' and inputs['connector'] == 'another-fixture':
                return {'state': 'unknown', 'evidence': None}
            return original(action, inputs, operation_id)
        self.executor.execute = execute
        self.backend.apply('init')
        self.backend.apply('name', target='named', name_source=self.name_file)
        with self.assertRaisesRegex(SpawnError, 'serviceReady'):
            self.backend.apply('promote', target='active')
        state = self.backend.life.status(self.agent)
        self.assertEqual(state['state'], 'named')
        self.assertIsNotNone(state['temporaryIdentity'])
        self.assertEqual(state['pending']['step'], 'serviceReady:mcp-handshake:another-fixture')

    def test_empty_evidence_never_proves_or_acknowledges(self):
        from connectome_spawn.execution import acknowledged
        self.backend.apply('init')
        self.executor.health = dict(proof(ready=True), evidence={})
        self.executor.handshake = proof(initialized=True, toolsListed=True)
        self.assertEqual(self.backend.verify('empty-proof')['verification']['serviceReady']['state'], 'unknown')
        class EmptyReceipt:
            def execute(self, *args):
                return {'state': 'applied', 'evidence': {}, 'result': None}
        with self.assertRaisesRegex(SpawnError, 'did not acknowledge'):
            acknowledged(EmptyReceipt(), 'fixture', {}, 'fixture')

    def test_ai_access_receipt_requires_explicit_ready(self):
        original = self.executor.execute
        def execute(action, inputs, operation_id):
            if action == 'ai-access':
                return proof(ready=False)
            return original(action, inputs, operation_id)
        self.executor.execute = execute
        with self.assertRaisesRegex(SpawnError, 'AI access awaiting'):
            self.backend.apply('init')
        self.assertEqual(self.executor.actions, [])
        self.assertEqual(self.backend.life.status(self.agent)['state'], 'planned')

    def test_changed_seed_binding_on_same_root_refuses_all_effects(self):
        from connectome_spawn import ExecutionBackend
        self.backend.apply('init')
        self.spec['seed']['mode'] = 'optional'
        changed = ExecutionBackend(self.work / 'agents', self.plan(),
            private_repo=str(self.work / 'private-repo'), build_root=str(self.work / 'build-root'), executor=self.executor)
        count = len(self.executor.actions)
        with self.assertRaisesRegex(SpawnError, 'execution binding changed'):
            changed.apply('changed')
        self.assertEqual(len(self.executor.actions), count)

    def test_verify_probe_guard_with_unknown_observer(self):
        self.backend.apply('init')
        self.executor.fail = 'mcp-handshake'
        with self.assertRaises(RuntimeError):
            self.backend.verify('probe')
        self.executor.receipts.clear()  # Observer now cannot mask the replay guard.
        self.executor.fail = None
        count = len(self.executor.actions)
        with self.assertRaisesRegex(SpawnError, 'probe replay refused'):
            self.backend.verify('probe')
        self.assertEqual(len(self.executor.actions), count)

    def test_lost_gate_handshake_recovers_observed_result_without_replay(self):
        self.backend.apply('init')
        self.backend.apply('name', target='named', name_source=self.name_file)
        self.executor.health = proof(ready=True)
        self.executor.handshake = proof(initialized=True, toolsListed=True)
        self.executor.fail = 'mcp-handshake'
        with self.assertRaises(RuntimeError):
            self.backend.apply('promote', target='active')
        state = self.backend.life.status(self.agent)
        self.assertIsNotNone(state['temporaryIdentity'])
        self.assertNotEqual(state['state'], 'active')
        self.assertNotIn('retire-identity', [a for a, _, _ in self.executor.actions])
        count = len(self.executor.actions)
        with self.assertRaises(SpawnError):
            self.backend.apply('promote', target='active')
        self.backend.life.recover(self.agent, operation_id='promote', observed='applied',
                                  reason='Observed original initialized/toolsListed result')
        self.assertEqual(len(self.executor.actions), count)
        self.assertEqual(self.executor.observations[-1][0], 'mcp-handshake')
        self.executor.fail = None
        result = self.backend.apply('promote', target='active')
        self.assertEqual(result['state'], 'active')
        probe_ids = [op for a, _, op in self.executor.actions if a == 'mcp-handshake']
        self.assertEqual(len(probe_ids), len(set(probe_ids)))

    def test_failed_gate_probe_cannot_be_recovered_as_success(self):
        self.backend.apply('init')
        self.backend.apply('name', target='named', name_source=self.name_file)
        self.executor.health = proof(ready=True)
        self.executor.handshake = proof(initialized=True, toolsListed=False)
        with self.assertRaises(SpawnError):
            self.backend.apply('promote', target='active')
        with self.assertRaisesRegex(SpawnError, 'successful observed probe evidence'):
            self.backend.life.recover(self.agent, operation_id='promote', observed='applied',
                                      reason='Failed is not success')
        state = self.backend.life.status(self.agent)
        self.assertFalse(state['pending']['ready'])
        self.assertIsNotNone(state['temporaryIdentity'])
        self.assertNotEqual(state['state'], 'active')

    def test_default_executor_cannot_claim_deployment(self):
        from connectome_spawn import ExecutionBackend, DryRunExecutor
        backend = ExecutionBackend(self.work / 'dry-agents', self.envelope,
            private_repo=str(self.work / 'private-repo'), build_root=str(self.work / 'dry-builds'))
        self.assertIsInstance(backend.executor, DryRunExecutor)
        with self.assertRaisesRegex(SpawnError, 'did not acknowledge'):
            backend.apply('init')
        self.assertEqual(backend.life.status(self.agent)['state'], 'planned')
        self.assertEqual(len(backend.executor.actions), 1)
        self.assertFalse((self.work / 'dry-builds').exists())
        self.assertFalse((self.work / 'home').exists())
        self.assertFalse((self.work / 'private-repo').exists())
        with self.assertRaises(SpawnError):
            backend.apply('init')
        self.assertEqual(len(backend.executor.actions), 1)

    def test_clean_replace_and_retire_keep_memory(self):
        state = self.promote()
        self.assertEqual(state['state'], 'active')
        actions = [a[0] for a in self.executor.actions]
        self.assertLess(actions.index('provision-temporary'), actions.index('join-initiation'))
        self.assertLess(actions.index('join-initiation'), actions.index('switch-start'))
        self.assertLess(max(i for i, a in enumerate(actions) if a == 'switch-start'), actions.index('retire-identity'))
        self.assertLess(actions.index('close-initiation'), actions.index('retire-identity'))
        last_start = max(i for i, action in enumerate(actions) if action == 'switch-start')
        self.assertLess(last_start, actions.index('mcp-handshake'))
        self.assertLess(max(i for i, action in enumerate(actions) if action == 'mcp-handshake'),
                        actions.index('close-initiation'))
        starts = [i for a, i, _ in self.executor.actions if a == 'switch-start']
        builds = [i for a, i, _ in self.executor.actions if a == 'cook-build']
        self.assertEqual(len({b['buildDirectory'] for b in builds}), 2)
        self.assertEqual(starts[0]['buildDirectory'], builds[0]['buildDirectory'])
        self.assertEqual(starts[1]['buildDirectory'], builds[1]['buildDirectory'])
        self.assertTrue(all(i['stateDirectory'] == state['store'] for i in builds + starts))
        self.assertTrue(all(i['noStart'] and i['activation'] == 'install_only' for i in builds))
        for file in builds[-1]['files']:
            import hashlib
            self.assertEqual(file['sha256'], hashlib.sha256(file['content'].encode()).hexdigest())
            self.assertNotIn('__CONNECTOME_BUILD_DIR__', file['content'])
        self.assertNotIn('systemPrompt', builds[-1]['recipe']['agent'])
        count = len(self.executor.actions)
        self.backend.apply('promote', target='active')
        self.assertEqual(len(self.executor.actions), count)
        retired = self.backend.apply('retire', target='retired')
        self.assertEqual(retired['state'], 'retired')
        self.assertEqual([a for a, _, _ in self.executor.actions[count:]], ['stop-unit', 'retire-identity'])
        self.assertTrue(self.executor.actions[count][1]['keepState'])
        self.assertIn('promote', retired['receipts'])

    def test_rename_promotion_never_retires_same_identity(self):
        from connectome_spawn import ExecutionBackend
        self.backend = ExecutionBackend(self.work / 'renamed-agents', self.envelope,
            private_repo=str(self.work / 'private-repo'), build_root=str(self.work / 'renamed-builds'),
            executor=self.executor, supports_rename=True)
        state = self.promote()
        self.assertIsNone(state['temporaryIdentity'])
        self.assertEqual(state['permanentIdentity']['id'], 'provision-temporary')
        self.assertNotIn('retire-identity', [a for a, _, _ in self.executor.actions])

    def test_durable_subeffect_intent_and_no_blind_retry(self):
        path = self.work / 'agents' / self.agent / 'lifecycle.json'
        def before(action, inputs, operation_id):
            state = read(path)
            self.assertEqual(state['pending']['step'], operation_id.split(':', 1)[1])
            self.assertEqual(state['pending']['effect']['action'], action)
        self.executor.before = before
        self.executor.fail = 'cook-build'
        with self.assertRaises(RuntimeError):
            self.backend.apply('init')
        count = len(self.executor.actions)
        with self.assertRaises(SpawnError):
            self.backend.apply('init')
        self.assertEqual(len(self.executor.actions), count)
        status = self.backend.reconcile()
        self.assertEqual(status['observation']['state'], 'applied')
        self.assertEqual(len(self.executor.actions), count)
        self.backend.life.recover(self.agent, operation_id='init', observed='applied', reason='Observed fixture build')
        self.executor.fail = None
        self.executor.before = None
        self.backend.apply('init')
        self.assertEqual(sum(a == 'cook-build' for a, _, _ in self.executor.actions), 1)
        self.assertEqual(sum(a == 'create-unix-user' for a, _, _ in self.executor.actions), 1)

    def test_lost_permanent_redeploy_ack_never_retires_temporary(self):
        self.backend.apply('init')
        self.backend.apply('name', target='named', name_source=self.name_file)
        self.executor.fail = 'switch-start'
        with self.assertRaises(RuntimeError):
            self.backend.apply('promote', target='active')
        self.assertNotIn('retire-identity', [a for a, _, _ in self.executor.actions])
        self.backend.life.recover(self.agent, operation_id='promote', observed='applied', reason='Observed permanent switch')
        self.executor.fail = None
        self.executor.health = proof(ready=True)
        self.executor.handshake = proof(initialized=True, toolsListed=True)
        self.backend.apply('promote', target='active')
        self.assertEqual(sum(a == 'retire-identity' for a, _, _ in self.executor.actions), 1)

    def test_lost_identity_ack_recovers_only_observed_handle(self):
        self.executor.fail = 'provision-temporary'
        with self.assertRaises(RuntimeError):
            self.backend.apply('init')
        # A CLI without an injected observer cannot invent a lost handle.
        with self.assertRaises(SpawnError):
            Lifecycle(self.work / 'agents').recover(self.agent, operation_id='init', observed='applied', reason='No handle evidence')
        self.backend.life.recover(self.agent, operation_id='init', observed='applied', reason='Observed reserved handle')
        self.executor.fail = None
        self.backend.apply('init')
        self.assertEqual(sum(a == 'provision-temporary' for a, _, _ in self.executor.actions), 1)

    def test_verify_stages_are_separate_and_require_mcp_tools(self):
        self.backend.apply('init')
        self.executor.health = proof(ready=True)
        unknown = self.backend.verify('verify-a')['verification']
        self.assertTrue(all(s['state'] == 'unknown' for s in unknown.values()))
        self.executor.handshake = proof(initialized=True, toolsListed=True)
        ready = self.backend.verify('verify-b')['verification']
        self.assertEqual(ready['serviceReady']['state'], 'success')
        self.assertEqual(ready['providerAuthenticated']['state'], 'unknown')
        self.assertEqual(ready['firstRealReply']['state'], 'unknown')
        self.executor.auth = proof(authenticated=True)
        authenticated = self.backend.verify('verify-c')['verification']
        self.assertEqual(authenticated['providerAuthenticated']['state'], 'success')
        self.assertEqual(authenticated['firstRealReply']['state'], 'unknown')
        self.executor.reply = proof(realReply=True)
        replied = self.backend.verify('verify-d')['verification']
        self.assertTrue(all(s['state'] == 'success' for s in replied.values()))
        probes = [i for a, i, _ in self.executor.actions if a == 'mcp-handshake']
        self.assertTrue(all('tools/list' in i['steps'] and 'start' in i['steps'] for i in probes))
        self.executor.health = {'state': 'applied', 'evidence': None, 'result': {'ready': True}}
        self.assertEqual(self.backend.verify('verify-e')['verification']['serviceReady']['state'], 'unknown')

    def test_probe_lost_ack_requires_recovery_not_restart(self):
        self.backend.apply('init')
        self.executor.fail = 'mcp-handshake'
        self.executor.handshake = proof(initialized=True, toolsListed=True)
        with self.assertRaises(RuntimeError):
            self.backend.verify('probe')
        count = len(self.executor.actions)
        with self.assertRaises(SpawnError):
            self.backend.verify('probe')
        self.assertEqual(len(self.executor.actions), count)
        self.backend.life.recover(self.agent, operation_id='probe', observed='applied', reason='Observed MCP result')
        self.executor.fail = None
        self.executor.health = proof(ready=True)
        recovered = self.backend.verify('probe')
        self.assertTrue(next(iter(recovered['verification']['serviceReady']['evidence']['mcp'].values()))['result']['initialized'])
        self.assertEqual(len(self.executor.actions), count)

    def test_reconcile_completed_deployment_only_observes(self):
        self.promote()
        count = len(self.executor.actions)
        result = self.backend.reconcile()
        self.assertEqual(result['state'], 'active')
        self.assertEqual(result['observation']['state'], 'unknown')
        self.assertEqual(self.executor.observations[-1][0], 'deployment-state')
        self.assertEqual(len(self.executor.actions), count)

    def test_not_applied_recovery_retries_only_the_uncertain_subeffect(self):
        self.executor.fail = 'cook-build'
        with self.assertRaises(RuntimeError):
            self.backend.apply('init')
        # An operator cannot contradict the observer's applied receipt.
        with self.assertRaises(SpawnError):
            self.backend.life.recover(self.agent, operation_id='init', observed='not-applied', reason='Contradictory statement')
        self.executor.receipts.pop('init:build:cook-build')
        self.backend.life.recover(self.agent, operation_id='init', observed='not-applied', reason='Confirmed fixture build absent')
        self.executor.fail = None
        self.backend.apply('init')
        self.assertEqual(sum(a == 'cook-build' for a, _, _ in self.executor.actions), 2)
        self.assertEqual(sum(a == 'create-unix-user' for a, _, _ in self.executor.actions), 1)

    def test_retry_reobserves_reality_changed_after_operator_statement(self):
        self.executor.fail = 'cook-build'
        with self.assertRaises(RuntimeError):
            self.backend.apply('init')
        receipt = self.executor.receipts.pop('init:build:cook-build')
        self.backend.life.recover(self.agent, operation_id='init', observed='not-applied', reason='Observed absent fixture build')
        # A delayed effect lands between recovery and explicit continuation.
        self.executor.receipts['init:build:cook-build'] = receipt
        count = len(self.executor.actions)
        with self.assertRaisesRegex(SpawnError, 'reality changed'):
            self.backend.apply('init')
        self.assertEqual(len(self.executor.actions), count)
        self.backend.life.recover(self.agent, operation_id='init', observed='applied', reason='Observed late fixture build')
        self.executor.fail = None
        self.backend.apply('init')
        self.assertEqual(sum(a == 'cook-build' for a, _, _ in self.executor.actions), 1)

    def test_binding_rejects_existing_unbound_state_and_changed_plan(self):
        self.backend.life.create(self.agent, seed={'mode': 'clean'}, identity=IDENTITY, home=HOME, operation_id='old')
        with self.assertRaises(SpawnError):
            self.backend.apply('init')
        self.assertEqual(self.executor.actions, [])
        from connectome_spawn import ExecutionBackend
        changed = deepcopy(self.envelope)
        changed['plan']['home']['channel'] = 'different'
        with self.assertRaises(SpawnError):
            ExecutionBackend(self.work / 'different', changed,
                private_repo=str(self.work / 'private-repo'), build_root=str(self.work / 'builds'))

    def test_memory_inside_deployment_root_refused_before_any_effect(self):
        from connectome_spawn import ExecutionBackend
        with self.assertRaisesRegex(SpawnError, 'overlap persistent memory'):
            ExecutionBackend(self.work / 'agents', self.envelope,
                private_repo=str(self.work / 'private-repo'), build_root=str(self.work / 'agents'),
                executor=self.executor)
        self.assertEqual(self.executor.actions, [])

    def test_seeded_hold_and_receipt_precede_activation(self):
        from connectome_spawn import ExecutionBackend
        payload = b'offline opaque fixture'
        self.spec['seed']['mode'] = 'required'
        envelope = self.plan(seed_payload=payload)
        backend = ExecutionBackend(self.work / 'seeded-agents', envelope,
            private_repo=str(self.work / 'private-repo'), build_root=str(self.work / 'seeded-builds'), executor=self.executor)
        def before(action, inputs, operation_id):
            if action == 'switch-start':
                self.assertTrue((Path(inputs['stateDirectory']).parent / 'seed.consumed').is_file())
        self.executor.before = before
        self.executor.health = proof(ready=True)
        self.executor.handshake = proof(initialized=True, toolsListed=True)
        result = backend.apply('activate-seeded', payload=payload)
        self.assertEqual(result['state'], 'active')
        self.assertEqual((Path(result['store']) / 'fixture.bin').read_bytes(), payload)
        actions = [a for a, _, _ in self.executor.actions]
        self.assertLess(actions.index('install-user-unit'), actions.index('ingest'))
        self.assertLess(actions.index('ingest'), actions.index('switch-start'))
        build = next(i for a, i, _ in self.executor.actions if a == 'cook-build')
        installs = [i for a, i, _ in self.executor.actions if a == 'install-user-unit']
        self.assertTrue(all(i['buildDirectory'] == build['buildDirectory'] for i in installs))
