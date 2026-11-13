"""Persisted offline lifecycle coordinator. Only injected adapters perform effects.

An intent is durable before each effect. An uncertain effect is recovery, never
replayed. Completed operations are idempotent by id AND exact input digest.
"""
from copy import deepcopy
from datetime import datetime, timezone
from typing import Protocol
import os

from .data import digest, fields, match, need
from .persistence import directory, locked, read, safe_path, write
from .schema import NAME, activation_identity, name_receipt, read_name_record
from .seed import seed_intent
from .identity import identity_policy, temporary_name, validate_provider_name
from .verification import proven

STATES = ('planned', 'initiation', 'named', 'active', 'failed', 'retired')


class LifecycleAdapter(Protocol):
    def perform(self, step: str, context: dict, operation_id: str) -> None:
        """Adapter contract; acknowledge only durable completion, never activate on build/hold.

        join-initiation uses context.home and topic 'initiation'. start-initiation
        runs on the agent's own temporary identity. Normally the initiator commits
        the name record to the config repo; self-naming needs an explicit request
        and repo write access. provision-permanent uses the pinned record; redeploy-keep-memory
        switches identity without resetting context.store. No extra approval.
        """
        ...

    def verify(self, context: dict, operation_id: str, checkpoint) -> dict:
        """Return evidenced stages; serviceReady requires health and every MCP.

        Use checkpoint(name, action, descriptor=None) for every observation or
        probe. The descriptor scopes recovery to the exact original request.
        No adapter may infer readiness from a switch/start acknowledgement.
        """
        ...


def _agent(value):
    need(match(value, NAME), 'invalid agent identifier')
    return value


def _handle(value, agent):
    fields(value, ('id', 'agent'))
    need(value['agent'] == agent and match(value['id'], r'[A-Za-z0-9_-]{1,128}'),
         'invalid identity provider handle')
    return deepcopy(value)


def _state(value, agent):
    fields(value, ('schema', 'agent', 'state', 'seed', 'home', 'identity', 'identityPolicy', 'temporaryName', 'supportsRename',
                   'temporaryIdentity', 'permanentIdentity', 'store', 'operations', 'pending'), ('nameRecord', 'audit', 'topicOpen', 'receipts', 'verification'))
    need(value['schema'] == 'connectome.spawn-state/v1' and value['agent'] == agent and
         value['state'] in STATES, 'invalid lifecycle state')
    fields(value['seed'], ('mode', 'branch', 'digest'))
    need(value['seed']['mode'] in ('required', 'clean', 'optional') and
         value['seed']['branch'] in ('seeded', 'clean') and
         (match(value['seed']['digest'], r'[0-9a-f]{64}') if value['seed']['branch'] == 'seeded'
          else value['seed']['digest'] is None), 'invalid persisted seed intent')
    need(value['seed']['mode'] == 'optional' or
         value['seed']['branch'] == ('seeded' if value['seed']['mode'] == 'required' else 'clean'),
         'inconsistent persisted seed intent')
    activation_identity(value['identity'], value['home'])
    if value.get('nameRecord') is not None:
        name_receipt(value['nameRecord'])
        need(value['nameRecord']['recordHash'] == digest(value['identity']), 'name record hash mismatch')
    need(type(value['store']) is str and type(value['operations']) is dict, 'invalid lifecycle records')
    need(identity_policy(value['identityPolicy']) == value['identityPolicy'] and
         match(value['temporaryName'], r'[A-Za-z][A-Za-z0-9_-]{0,255}') and
         (value['supportsRename'] is None or type(value['supportsRename']) is bool),
         'invalid persisted identity policy')
    for handle in (value['temporaryIdentity'], value['permanentIdentity']):
        if handle is not None:
            _handle(handle, agent)
    for key, row in value['operations'].items():
        fields(row, ('digest', 'target'))
        need(match(key, r'[A-Za-z0-9_-]{1,128}') and match(row['digest'], r'[0-9a-f]{64}') and
             row['target'] in STATES, 'invalid operation record')
    if value['pending'] is not None:
        fields(value['pending'], ('operation', 'digest', 'target', 'step'),
               ('completed', 'ready', 'effect', 'kind', 'resolution'))
        need(type(value['pending'].get('completed', {})) is dict and
             type(value['pending'].get('ready', False)) is bool, 'invalid pending checkpoints')
        need(match(value['pending']['operation'], r'[A-Za-z0-9_-]{1,128}') and
             match(value['pending']['digest'], r'[0-9a-f]{64}') and
             value['pending']['target'] in STATES and type(value['pending']['step']) is str,
             'invalid pending operation')
    need(type(value.get('audit', [])) is list and type(value.get('receipts', {})) is dict and
         type(value.get('topicOpen', False)) is bool, 'invalid lifecycle journal')
    return value


class Lifecycle:
    def __init__(self, root, *, adapter=None, identity_provider=None, importer=None, ingress_root=None):
        self.root = safe_path(root)
        self.ingress_root = safe_path(ingress_root) if ingress_root is not None else None
        self.adapter, self.identity_provider, self.importer = adapter, identity_provider, importer

    def _paths(self, agent):
        root = safe_path(self.root / _agent(agent))
        return (root, safe_path(root / 'lifecycle.json'), safe_path(root / 'state' / 'store'),
                safe_path(root / 'state' / 'seed.consumed'))

    def _read(self, agent):
        _, path, store, _ = self._paths(agent)
        need(path.is_file(), 'unknown agent; lifecycle journal missing')
        value = _state(read(path), agent)
        need(value['store'] == str(store), 'store binding mismatch')
        return value

    def status(self, agent):
        value = self._read(agent)  # Atomic read; never creates files or lock directories.
        _, _, store, marker = self._paths(agent)
        reasons = []
        if value['pending'] is not None:
            if value['pending']['step'].startswith('serviceReady:'):
                reasons.append('serviceReady pending: health and every connector handshake must succeed; temporary identity kept')
            reasons.append('checkpoint resolved; explicit continuation required' if value['pending'].get('ready') else
                           'uncertain operation; manual reconciliation required; no replay')
        if value['seed']['branch'] == 'seeded' and store.exists() and not marker.exists():
            reasons.append('existing store without seed.consumed; never a fresh import')
        if marker.exists():
            self._marker(marker, value)
            if not store.is_dir():
                reasons.append('seed receipt without memory store; manual reconciliation required')
        elif value['seed']['branch'] == 'seeded' and value['state'] == 'active' and not store.exists():
            reasons.append('active seeded agent missing store and seed receipt; recovery required')
        return dict(value, recovery=reasons,
                    identityNotice='owned; liveness unknown; reconcile with provider'
                    if value['temporaryIdentity'] or value['permanentIdentity'] else None)

    def _observe(self, value):
        pending = value['pending']
        if self.adapter is None or not hasattr(self.adapter, 'observe'):
            return {'state': 'unknown', 'evidence': None}
        observation = self.adapter.observe(deepcopy(value),
            pending['operation'] + ':' + pending['step'] if pending is not None else 'reconcile')
        fields(observation, ('state', 'evidence'), ('result',))
        need(observation['state'] in ('applied', 'not-applied', 'unknown'), 'invalid observation')
        need(observation['state'] == 'unknown' or bool(observation['evidence']), 'observation evidence required')
        return observation

    def reconcile(self, agent):
        """Observe a pending intent, never replay an effect or clear uncertainty."""
        root, path, _, _ = self._paths(agent)
        self._read(agent)
        with locked(root):
            value = self._read(agent)
            observation = self._observe(value)
            value.setdefault('audit', []).append({'event': 'reconcile',
                'when': datetime.now(timezone.utc).isoformat(), 'observation': observation,
                'operation': value['pending']['operation'] if value['pending'] else None,
                'step': value['pending']['step'] if value['pending'] else None})
            write(path, value)
            return dict(self.status(agent), observation=observation)

    def _resume(self, value, path):
        # Consume the operator grant durably, then observe again immediately
        # before continuation. Reality may have changed since recover returned.
        declared = value['pending'].get('resolution')
        value['pending']['ready'] = False
        write(path, value)
        observation = self._observe(value)
        value.setdefault('audit', []).append({'event': 'resume-observation',
            'operation': value['pending']['operation'], 'step': value['pending']['step'],
            'when': datetime.now(timezone.utc).isoformat(), 'observation': observation})
        write(path, value)
        need(observation['state'] in ('unknown', declared),
             'reality changed after recovery; reconcile before continuation')

    def verify(self, agent, operation_id):
        """Journal probes independently of deployment; never infer authentication/reply."""
        need(match(operation_id, r'[A-Za-z0-9_-]{1,128}') and self.adapter is not None and
             hasattr(self.adapter, 'verify'), 'verification adapter and operation required')
        root, path, _, _ = self._paths(agent)
        self._read(agent)
        with locked(root):
            value = self._read(agent)
            need(value['state'] in ('initiation', 'active'), 'verify requires initiation or active agent')
            request = digest({'verify': agent})
            prior = value['operations'].get(operation_id)
            if prior is not None:
                need(prior['digest'] == request, 'operation id conflict')
                return self.status(agent)
            pending = value['pending']
            if pending is not None:
                need(pending.get('ready', False) and pending.get('kind') == 'verify' and
                     pending['operation'] == operation_id and pending['digest'] == request,
                     'uncertain operation requires recovery; probe replay refused')
                self._resume(value, path)
            else:
                value['pending'] = {'operation': operation_id, 'digest': request, 'target': value['state'],
                    'step': 'prepared', 'completed': {}, 'ready': False, 'kind': 'verify'}
            value['pending']['ready'] = False
            write(path, value)
            def checkpoint(name, action, descriptor=None):
                completed = value['pending']['completed']
                if name in completed:
                    return deepcopy(completed[name])
                value['pending']['step'] = name
                value['pending']['effect'] = descriptor
                write(path, value)
                result = action()
                completed[name] = deepcopy(result)
                value['pending']['step'] = name + ':completed'
                write(path, value)
                return result
            value['verification'] = self.adapter.verify(deepcopy(value), operation_id, checkpoint)
            value['operations'][operation_id] = {'digest': request, 'target': value['state']}
            value.setdefault('receipts', {})[operation_id] = value['pending']['completed']
            value['pending'] = None
            write(path, value)
            return self.status(agent)

    def recover(self, agent, *, operation_id, observed, reason):
        """Resolve ONE checkpoint by explicit operator statement; do not resume it.

        A handle-producing effect needs an observed handle; an operator cannot
        invent/adopt an identity by clearing the pending operation. Promotion,
        ingest and serviceReady checkpoints also require actual observer evidence.
        """
        need(observed in ('applied', 'not-applied') and type(reason) is str and
             0 < len(reason.strip()) <= 2048 and not any(ord(c) < 32 for c in reason),
             'explicit bounded recovery reason and observation required')
        root, path, _, _ = self._paths(agent)
        self._read(agent)
        with locked(root):
            value = self._read(agent)
            pending = value['pending']
            need(pending is None or 'completed' in pending, 'legacy pending operation needs external migration; replay refused')
            need(pending is not None and pending['operation'] == operation_id and
                 not pending.get('ready', False), 'pending operation mismatch or already resolved')
            observation = self._observe(value)  # observe BEFORE granting an explicit retry
            need(observation['state'] in ('unknown', observed), 'operator statement contradicts observation')
            step = pending['step']
            completed = pending.setdefault('completed', {})
            if step.endswith(':completed'):
                need(observed == 'applied', 'durable completed checkpoint cannot be undone')
            elif step != 'prepared' and observed == 'applied':
                if (step == 'ingest' or step in ('activate', 'redeploy-keep-memory') or
                        step.endswith(':switch-start') or step.startswith('serviceReady:')):
                    need(observation['state'] == 'applied' and bool(observation['evidence']),
                         'applied observer evidence required for promotion, ingest or serviceReady recovery')
                if step == 'serviceReady:service-health':
                    need(proven(observation, 'ready'), 'serviceReady checkpoint requires successful observed probe evidence')
                elif step.startswith('serviceReady:mcp-handshake:'):
                    need(proven(observation, 'initialized') and proven(observation, 'toolsListed'),
                         'serviceReady checkpoint requires successful observed probe evidence')
                result = (observation if pending.get('kind') == 'verify' or step.startswith('serviceReady:')
                          else observation.get('result'))
                if step in ('provision-temporary', 'provision-permanent', 'rename'):
                    result = _handle(result, agent)
                completed[step] = result
            pending['ready'] = True
            pending['resolution'] = observed
            value.setdefault('audit', []).append({'event': 'recover', 'operation': operation_id,
                'step': step, 'observed': observed, 'reason': reason,
                'who': {'uid': os.getuid(), 'euid': os.geteuid()},
                'when': datetime.now(timezone.utc).isoformat(), 'observation': observation})
            write(path, value)
            return self.status(agent)

    @staticmethod
    def _marker(path, value):
        marker = read(path)
        fields(marker, ('digest', 'timestamp'))
        need(value['seed']['branch'] == 'seeded' and
             marker['digest'] == value['seed']['digest'] and
             match(marker['timestamp'], r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?\+00:00'),
             'seed receipt mismatch')

    def create(self, agent, *, seed, identity, home, operation_id, payload=None, identity_config=None):
        root, path, store, marker = self._paths(agent)
        intent = seed_intent(seed, payload)
        policy = identity_policy(identity_config)
        identity = activation_identity(identity, home)
        need(match(operation_id, r'[A-Za-z0-9_-]{1,128}'), 'invalid operation id')
        request = digest({'seed': intent, 'identity': identity, 'home': home, 'identityPolicy': policy})
        with locked(root):
            if path.exists():
                value = self._read(agent)
                need(value['operations'].get(operation_id) == {'digest': request, 'target': 'planned'},
                     'agent exists or operation id conflicts')
                return self.status(agent)
            need(not store.exists() and not marker.exists(),
                 'existing store or seed receipt requires recovery; never a fresh import')
            value = {'schema': 'connectome.spawn-state/v1', 'agent': agent, 'state': 'planned',
                     'seed': intent, 'home': deepcopy(home), 'identity': identity,
                     'identityPolicy': policy, 'temporaryName': temporary_name(policy), 'supportsRename': None,
                     'temporaryIdentity': None, 'permanentIdentity': None,
                     'store': str(store), 'operations': {operation_id: {'digest': request, 'target': 'planned'}},
                     'pending': None, 'audit': [], 'topicOpen': False, 'receipts': {}}
            write(path, value)
            return self.status(agent)

    def transition(self, agent, target, operation_id, *, name_file=None, name_source=None, payload=None):
        need(target in STATES and target != 'planned', 'illegal lifecycle target')
        need(match(operation_id, r'[A-Za-z0-9_-]{1,128}'), 'invalid operation id')
        need(name_file is None or name_source is None, 'exactly one name record source required')
        selected = name_source if name_source is not None else name_file
        named = read_name_record(selected, ingress_root=self.ingress_root, with_provenance=True) if selected is not None else None
        record = named['record'] if named is not None else None
        receipt = {key: named[key] for key in ('recordHash', 'source')} if named is not None else None
        root, path, store, marker = self._paths(agent)
        with locked(root):
            value = self._read(agent)
            need((target == 'named') == (record is not None), 'name record required only for named transition')
            need(payload is None or target == 'active' and value['seed']['branch'] == 'seeded',
                 'seed payload not accepted for this transition')
            intent = {'target': target, 'record': record,
                      'seed': seed_intent({'mode': 'required'}, payload) if payload is not None else None}
            if receipt is not None:
                intent['nameRecord'] = receipt
            request = digest(intent)
            if operation_id in value['operations']:
                need(value['operations'][operation_id] == {'digest': request, 'target': target},
                     'operation id reused with different input')
                return self.status(agent)
            resuming = value['pending'] is not None
            if resuming:
                need(value['pending'].get('ready', False) and
                     value['pending']['operation'] == operation_id and value['pending']['digest'] == request,
                     'uncertain operation requires recovery; replay refused')
                self._resume(value, path)
            source, branch = value['state'], value['seed']['branch']
            legal = ((branch == 'clean' and (source, target) in
                      (('planned', 'initiation'), ('initiation', 'named'), ('named', 'active'))) or
                     (branch == 'seeded' and (source, target) == ('planned', 'active')) or
                     (target == 'failed' and source in ('planned', 'initiation', 'named', 'active')) or
                     (target == 'retired' and source in ('planned', 'initiation', 'named', 'active', 'failed')))
            need(legal, 'illegal lifecycle transition')
            if record is not None:
                activation_identity(record, value['home'])
            if target == 'active':
                activation_identity(value['identity'], value['home'])
            if branch == 'seeded' and target == 'active':
                need(seed_intent({'mode': value['seed']['mode']}, payload) == value['seed'],
                     'seed payload does not match planned digest')
                need(resuming or not store.exists() and not marker.exists(),
                     'existing store or consumed seed requires recovery; re-ingest refused')
                need(self.importer is not None, 'seed importer adapter required')
            if branch == 'clean' and target == 'active':
                need(value['temporaryIdentity'] is not None or resuming, 'temporary identity missing')
            seed_identity = branch == 'seeded' and target == 'active' and getattr(self.adapter, 'requires_permanent_identity', False)
            effects = target in ('initiation', 'active', 'retired')
            need(not effects or self.adapter is not None, 'lifecycle adapter required')
            identity_effects = seed_identity or target in ('initiation', 'named') or branch == 'clean' and target == 'active' or (
                target == 'retired' and (value['temporaryIdentity'] or value['permanentIdentity']))
            if identity_effects:
                need(self.identity_provider is not None, 'identity provider required')
                validate_provider_name(self.identity_provider,
                                       value['temporaryName'] if target == 'initiation' else
                                       (record or value['identity'])['name'])
                need(value['supportsRename'] is None or
                     value['supportsRename'] == self.identity_provider.supports_rename,
                     'identity provider capability changed; reconciliation required')
                value['supportsRename'] = self.identity_provider.supports_rename
            if not resuming:
                value['pending'] = {'operation': operation_id, 'digest': request, 'target': target,
                                    'step': 'prepared', 'completed': {}, 'ready': False}
            value['pending']['ready'] = False
            write(path, value)

            def effect(name, action, descriptor=None):
                completed = value['pending'].setdefault('completed', {})
                if name in completed:
                    return deepcopy(completed[name])
                value['pending']['step'] = name
                value['pending']['effect'] = descriptor
                write(path, value)
                result = action()
                completed[name] = deepcopy(result)
                value['pending']['step'] = name + ':completed'
                write(path, value)
                return result

            def step(name, action=None):
                if action is not None:
                    action_name, inputs = name, None
                    if name == 'provision-temporary':
                        inputs = {'agent': agent, 'name': value['temporaryName']}
                    elif name == 'provision-permanent':
                        inputs = {'agent': agent, 'record': value['identity']}
                    elif name == 'rename':
                        inputs = {'handle': value['temporaryIdentity'] or value['permanentIdentity'],
                                  'record': value['identity']}
                    elif name.startswith('retire-'):
                        field = 'temporaryIdentity' if name == 'retire-temporary' else name[len('retire-'):]
                        action_name = 'retire-identity'
                        inputs = {'handle': value[field], 'keepTombstone': True}
                    elif name == 'ingest':
                        inputs = {'store': str(store), 'digest': value['seed']['digest'], 'freshOnly': True}
                    descriptor = None if inputs is None else {
                        'action': action_name, 'inputs': deepcopy(inputs), 'operation_id': operation_id + ':' + name}
                    return effect(name, action, descriptor)
                context = deepcopy(value)
                context['topic'] = 'initiation'
                context['initiationParticipants'] = 'home-channel-members'
                if hasattr(self.adapter, 'perform_journaled'):
                    def run(action_name, inputs):
                        checkpoint = name + ':' + action_name
                        action_id = operation_id + ':' + checkpoint
                        return effect(checkpoint, lambda: self.adapter.execute_effect(
                            action_name, deepcopy(inputs), action_id),
                            {'action': action_name, 'inputs': inputs, 'operation_id': action_id})
                    return self.adapter.perform_journaled(name, context, operation_id + ':' + name, run)
                return effect(name, lambda: self.adapter.perform(name, context, operation_id + ':' + name))

            def service_ready():
                need(hasattr(self.adapter, 'verify'), 'serviceReady verification adapter required')
                value['verification'] = {'serviceReady': {'state': 'unknown', 'evidence': None}}
                write(path, value)
                def checkpoint(name, action, descriptor=None):
                    def probe():
                        result = action()
                        # Do not complete an unknown/failed gate probe. Recovery
                        # may observe its result or explicitly authorize a fresh probe.
                        value['verification']['serviceReady'] = {'state': 'unknown',
                            'evidence': {'checkpoint': name, 'receipt': result}}
                        write(path, value)
                        if name == 'service-health':
                            need(proven(result, 'ready'), 'serviceReady pending: service health unknown or failed')
                        elif name.startswith('mcp-handshake:'):
                            need(proven(result, 'initialized') and proven(result, 'toolsListed'),
                                 'serviceReady pending: MCP handshake unknown or failed')
                        return result
                    return effect('serviceReady:' + name, probe, descriptor)
                value['verification'] = self.adapter.verify(deepcopy(value), operation_id + ':serviceReady', checkpoint)
                write(path, value)
                need(value['verification']['serviceReady']['state'] == 'success',
                     'serviceReady pending: health and every connector handshake required')

            if target == 'initiation':
                step('build')
                value['temporaryIdentity'] = _handle(step('provision-temporary', lambda:
                    self.identity_provider.provision_temporary(
                        agent, value['temporaryName'], operation_id + ':provision-temporary')), agent)
                step('join-initiation')
                value['topicOpen'] = True
                step('start-initiation')
            elif target == 'named':
                value['identity'] = record
                value['nameRecord'] = receipt
            elif target == 'active' and branch == 'seeded':
                if seed_identity:
                    value['permanentIdentity'] = _handle(step('provision-permanent', lambda:
                        self.identity_provider.provision_permanent(
                            agent, value['identity'], operation_id + ':provision-permanent')), agent)
                step('build')
                step('hold')
                def ingest():
                    need(not store.exists() and not marker.exists(),
                         'existing store requires recovery; never a fresh import')
                    if not getattr(self.importer, 'reserves_store', False):
                        directory(store)
                    self.importer.ingest(payload, store, operation_id + ':ingest')
                step('ingest', ingest)
                need(store.is_dir(), 'seed importer must acknowledge a durable memory store')
                step('seed.consumed', lambda: write(marker, {
                    'digest': value['seed']['digest'], 'timestamp': datetime.now(timezone.utc).isoformat()}))
                self._marker(marker, value)
                step('activate')
                service_ready()
            elif target == 'active':
                if value['supportsRename']:
                    original = value['temporaryIdentity'] or value['permanentIdentity']
                    value['permanentIdentity'] = _handle(step('rename', lambda:
                        self.identity_provider.rename(original, value['identity'], operation_id + ':rename')), agent)
                    need(value['permanentIdentity'] == original, 'rename must preserve identity handle')
                    value['temporaryIdentity'] = None
                else:
                    value['permanentIdentity'] = _handle(step('provision-permanent', lambda:
                        self.identity_provider.provision_permanent(
                            agent, value['identity'], operation_id + ':provision-permanent')), agent)
                    need(value['permanentIdentity'] != value['temporaryIdentity'],
                         'replace must provision a distinct permanent identity')
                step('redeploy-keep-memory')
                service_ready()
                step('close-initiation')
                value['topicOpen'] = False
                if value['temporaryIdentity'] is not None:
                    step('retire-temporary', lambda: self.identity_provider.retire(
                        value['temporaryIdentity'], operation_id + ':retire-temporary'))
                    value['temporaryIdentity'] = None
            elif target == 'retired':
                step('retire')
                had_temporary = value.get('topicOpen', value['temporaryIdentity'] is not None)
                if had_temporary:
                    step('close-initiation')
                    value['topicOpen'] = False
                for field in ('temporaryIdentity', 'permanentIdentity'):
                    if value[field] is not None:
                        step('retire-' + field, lambda: self.identity_provider.retire(
                            value[field], operation_id + ':retire-' + field))
                        value[field] = None
            if target == 'active':
                need(value.get('verification', {}).get('serviceReady', {}).get('state') == 'success',
                     'serviceReady required before active')
            value['state'] = target
            value['operations'][operation_id] = {'digest': request, 'target': target}
            value.setdefault('receipts', {})[operation_id] = value['pending']['completed']
            value['pending'] = None
            write(path, value)
            return self.status(agent)
