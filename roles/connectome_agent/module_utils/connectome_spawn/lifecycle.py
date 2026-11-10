"""Persisted offline lifecycle coordinator. Only injected adapters perform effects.

An intent is durable before each effect. An uncertain effect is recovery, never
replayed. Completed operations are idempotent by id AND exact input digest.
"""
from copy import deepcopy
from datetime import datetime, timezone
from typing import Protocol

from .data import digest, fields, match, need
from .persistence import directory, locked, read, safe_path, write
from .schema import NAME, activation_identity, read_name_record
from .seed import seed_intent
from .identity import identity_policy, temporary_name, validate_provider_name

STATES = ('planned', 'initiation', 'named', 'active', 'failed', 'retired')


class LifecycleAdapter(Protocol):
    def perform(self, step: str, context: dict, operation_id: str) -> None:
        """T4 hook; acknowledge only durable completion, never activate on build/hold.

        join-initiation uses context.home and topic 'initiation'. start-initiation
        runs on the agent's own temporary identity. Home-channel dialogue produces the agent's
        name-record file. provision-permanent uses that record; redeploy-keep-memory
        switches identity without resetting context.store. No extra approval.
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
                   'temporaryIdentity', 'permanentIdentity', 'store', 'operations', 'pending'))
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
        fields(value['pending'], ('operation', 'digest', 'target', 'step'))
        need(match(value['pending']['operation'], r'[A-Za-z0-9_-]{1,128}') and
             match(value['pending']['digest'], r'[0-9a-f]{64}') and
             value['pending']['target'] in STATES and type(value['pending']['step']) is str,
             'invalid pending operation')
    return value


class Lifecycle:
    def __init__(self, root, *, adapter=None, identity_provider=None, importer=None):
        self.root = safe_path(root)
        self.adapter, self.identity_provider, self.importer = adapter, identity_provider, importer

    def _paths(self, agent):
        root = safe_path(self.root / _agent(agent))
        return (root, safe_path(root / 'lifecycle.json'), safe_path(root / 'state' / 'store'),
                safe_path(root / 'state' / 'seed.consumed'))

    def _read(self, agent):
        _, path, store, _ = self._paths(agent)
        value = _state(read(path), agent)
        need(value['store'] == str(store), 'store binding mismatch')
        return value

    def status(self, agent):
        value = self._read(agent)  # Atomic read; never creates files or lock directories.
        _, _, store, marker = self._paths(agent)
        reasons = []
        if value['pending'] is not None:
            reasons.append('uncertain operation; manual reconciliation required; no replay')
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
                     'pending': None}
            write(path, value)
            return self.status(agent)

    def transition(self, agent, target, operation_id, *, name_file=None, payload=None):
        need(target in STATES and target != 'planned', 'illegal lifecycle target')
        need(match(operation_id, r'[A-Za-z0-9_-]{1,128}'), 'invalid operation id')
        record = read_name_record(name_file) if name_file is not None else None
        root, path, store, marker = self._paths(agent)
        with locked(root):
            value = self._read(agent)
            need((target == 'named') == (record is not None), 'name record required only for named transition')
            need(payload is None or target == 'active' and value['seed']['branch'] == 'seeded',
                 'seed payload not accepted for this transition')
            request = digest({'target': target, 'record': record,
                              'seed': seed_intent({'mode': 'required'}, payload) if payload is not None else None})
            if operation_id in value['operations']:
                need(value['operations'][operation_id] == {'digest': request, 'target': target},
                     'operation id reused with different input')
                return self.status(agent)
            need(value['pending'] is None, 'uncertain operation requires recovery; replay refused')
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
                need(not store.exists() and not marker.exists(),
                     'existing store or consumed seed requires recovery; re-ingest refused')
                need(self.importer is not None, 'seed importer adapter required')
            if branch == 'clean' and target == 'active':
                need(value['temporaryIdentity'] is not None, 'temporary identity missing')
            effects = target in ('initiation', 'active', 'retired')
            need(not effects or self.adapter is not None, 'lifecycle adapter required')
            identity_effects = target in ('initiation', 'named') or branch == 'clean' and target == 'active' or (
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
            value['pending'] = {'operation': operation_id, 'digest': request, 'target': target, 'step': 'prepared'}
            write(path, value)

            def step(name, action=None):
                value['pending']['step'] = name
                write(path, value)
                if action is not None:
                    action()
                else:
                    context = deepcopy(value)
                    context['topic'] = 'initiation'
                    context['initiationParticipants'] = 'home-channel-members'
                    self.adapter.perform(name, context, operation_id + ':' + name)
                # An interruption here intentionally leaves an uncertain intent.
                value['pending']['step'] = name + ':completed'
                write(path, value)

            if target == 'initiation':
                step('build')
                def provision_temporary():
                    value['temporaryIdentity'] = _handle(self.identity_provider.provision_temporary(
                        agent, value['temporaryName'], operation_id + ':provision-temporary'), agent)
                step('provision-temporary', provision_temporary)
                step('join-initiation')
                step('start-initiation')
            elif target == 'named':
                value['identity'] = record
            elif target == 'active' and branch == 'seeded':
                step('build')
                step('hold')
                def ingest():
                    need(not store.exists() and not marker.exists(),
                         'existing store requires recovery; never a fresh import')
                    directory(store)
                    self.importer.ingest(payload, store, operation_id + ':ingest')
                step('ingest', ingest)
                step('seed.consumed', lambda: write(marker, {
                    'digest': value['seed']['digest'], 'timestamp': datetime.now(timezone.utc).isoformat()}))
                self._marker(marker, value)
                step('activate')
            elif target == 'active':
                if value['supportsRename']:
                    def rename():
                        value['permanentIdentity'] = _handle(self.identity_provider.rename(
                            value['temporaryIdentity'], value['identity'], operation_id + ':rename'), agent)
                        need(value['permanentIdentity'] == value['temporaryIdentity'],
                             'rename must preserve identity handle')
                        value['temporaryIdentity'] = None
                    step('rename', rename)
                else:
                    def provision_permanent():
                        value['permanentIdentity'] = _handle(self.identity_provider.provision_permanent(
                            agent, value['identity'], operation_id + ':provision-permanent'), agent)
                        need(value['permanentIdentity'] != value['temporaryIdentity'],
                             'replace must provision a distinct permanent identity')
                    step('provision-permanent', provision_permanent)
                step('redeploy-keep-memory')
                if value['temporaryIdentity'] is not None:
                    step('retire-temporary', lambda: self.identity_provider.retire(
                        value['temporaryIdentity'], operation_id + ':retire-temporary'))
                    value['temporaryIdentity'] = None
                step('close-initiation')
            elif target == 'retired':
                step('retire')
                had_temporary = value['temporaryIdentity'] is not None
                for field in ('temporaryIdentity', 'permanentIdentity'):
                    if value[field] is not None:
                        step('retire-' + field, lambda: self.identity_provider.retire(
                            value[field], operation_id + ':retire-' + field))
                        value[field] = None
                if had_temporary:
                    step('close-initiation')
            value['state'] = target
            value['operations'][operation_id] = {'digest': request, 'target': target}
            value['pending'] = None
            write(path, value)
            return self.status(agent)
