"""Executor-only lifecycle tests. No host commands or outbound traffic."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
        self.backend.apply('init')
        self.backend.apply('name', target='named', name_source=self.name_file)
        return self.backend.apply('promote', target='active')

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
        self.assertLess(actions.index('retire-identity'), actions.index('close-initiation'))
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
        result = backend.apply('activate-seeded', payload=payload)
        self.assertEqual(result['state'], 'active')
        self.assertEqual((Path(result['store']) / 'fixture.bin').read_bytes(), payload)
        actions = [a for a, _, _ in self.executor.actions]
        self.assertLess(actions.index('install-user-unit'), actions.index('ingest'))
        self.assertLess(actions.index('ingest'), actions.index('switch-start'))
        build = next(i for a, i, _ in self.executor.actions if a == 'cook-build')
        installs = [i for a, i, _ in self.executor.actions if a == 'install-user-unit']
        self.assertTrue(all(i['buildDirectory'] == build['buildDirectory'] for i in installs))
