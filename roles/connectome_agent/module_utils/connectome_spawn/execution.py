"""Executor-only new-agent backend. There is deliberately NO real executor.

Trusted callers supply an already validated plan, explicit local execution
locations and an executor. Hashes bind inputs, not operator authorization.
The default records proposed actions and cannot acknowledge deployment.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Protocol

from .data import BUILD_BINDING, canonical, digest, fields, json_data, need
from .identity import validate_capabilities
from .lifecycle import Lifecycle, _handle
from .persistence import locked, read, safe_path, write
from .planner import require_bound
from .schema import activation_identity, instance
from .seed import seed_intent
from .sources import absolute


class Executor(Protocol):
    def execute(self, action: str, inputs: dict, operation_id: str) -> dict:
        """Return {state: applied, evidence: {...}, result: JSON} ONLY on ack.

        Uncertain effects raise or return unknown, never success. Implementations
        must not log credentials. Real adapters need authorization and their own
        durable sub-effect receipts; no real implementation ships in this slice.
        """
        ...

    def observe(self, action: str, inputs: dict, operation_id: str) -> dict:
        """Observe reality, never create/restart/retry a persistent effect.

        Return state applied|not-applied|unknown, evidence and optional result.
        Evidence is credential-free, scoped to these exact inputs and operation.
        """
        ...


class DryRunExecutor:
    """Memory-only recorder; no subprocess, filesystem or transport effects."""
    def __init__(self):
        self.actions = []
        self.observations = []

    def execute(self, action, inputs, operation_id):
        self.actions.append({'action': action, 'inputs': deepcopy(inputs), 'operation_id': operation_id})
        return {'state': 'unknown', 'evidence': None, 'result': None}

    def observe(self, action, inputs, operation_id):
        self.observations.append({'action': action, 'inputs': deepcopy(inputs), 'operation_id': operation_id})
        return {'state': 'unknown', 'evidence': None}


def acknowledged(executor, action, inputs, operation_id):
    receipt = executor.execute(action, deepcopy(inputs), operation_id)
    json_data(receipt)
    need(type(receipt) is dict and receipt.get('state') == 'applied' and
         type(receipt.get('evidence')) is dict and bool(receipt['evidence']),
         'executor did not acknowledge effect; reconcile before retry')
    return receipt


class ExecutorIdentityProvider:
    """The lifecycle identity contract, with every operation behind executor."""
    max_name_length = 63
    name_pattern = r'[A-Za-z][A-Za-z0-9_-]*'

    def __init__(self, executor, *, supports_rename):
        self.executor, self.supports_rename = executor, supports_rename
        validate_capabilities(self)

    def _call(self, action, inputs, operation_id):
        return acknowledged(self.executor, action, inputs, operation_id).get('result')

    def provision_temporary(self, agent, name, operation_id):
        return _handle(self._call('provision-temporary', {'agent': agent, 'name': name}, operation_id), agent)

    def provision_permanent(self, agent, name_record, operation_id):
        return _handle(self._call('provision-permanent', {'agent': agent, 'record': name_record}, operation_id), agent)

    def rename(self, handle, name_record, operation_id):
        return self._call('rename', {'handle': handle, 'record': name_record}, operation_id)

    def retire(self, handle, operation_id):
        self._call('retire-identity', {'handle': handle, 'keepTombstone': True}, operation_id)

    def list(self):
        observed = self.executor.observe('owned-identities', {}, 'identity-list')
        need(observed.get('state') == 'applied' and bool(observed.get('evidence')) and
             type(observed.get('result')) is list, 'owned identity listing unknown')
        return deepcopy(observed['result'])


class ExecutorSeedImporter:
    reserves_store = True

    def __init__(self, executor):
        self.executor = executor

    def ingest(self, payload, store, operation_id):
        # Opaque bytes are never journaled. The executor needs a role-provided
        # importer; this is not an Anthropic/OpenAI import-format implementation.
        acknowledged(self.executor, 'ingest', {'payload': payload, 'store': str(store),
                     'digest': hashlib.sha256(payload).hexdigest(), 'freshOnly': True}, operation_id)


class ExecutorLifecycleAdapter:
    """Thin role-engine requests, not a second implementation of deployment."""
    requires_permanent_identity = True

    def __init__(self, plan, *, private_repo, build_root, executor=None):
        self.plan = deepcopy(plan)
        self.private_repo = str(safe_path(absolute(private_repo)))
        self.build_root = str(safe_path(absolute(build_root)))
        self.executor = executor if executor is not None else DryRunExecutor()

    def _inputs(self, context, operation_id):
        declaration = self.plan['instance']
        # Each operation/phase gets a new, isolated build directory. A retry is
        # only allowed after explicit recovery, never by deleting/reusing builds.
        build = self.build_root + '/' + context['agent'] + '/builds/' + digest({
            'plan': digest(self.plan), 'operation': operation_id})
        store = safe_path(context['store'])
        build_path = safe_path(build)
        unit_dir = safe_path(declaration['homeDirectory'] + '/.config/systemd/user')
        for path in (build_path.parent.parent, unit_dir, Path(self.private_repo)):
            need(not (path.is_relative_to(store) or store.is_relative_to(path)),
                 'execution paths overlap persistent memory')
        identity = activation_identity(context['identity'], context['home'])
        recipe = deepcopy(self.plan['recipe'])
        recipe['name'] = identity['name']
        recipe['agent']['name'] = identity['name']
        recipe['agent']['strategy']['summaryParticipant'] = identity['summaryParticipant']
        recipe['agent'].pop('systemPrompt', None)
        if 'systemPrompt' in identity:
            recipe['agent']['systemPrompt'] = identity['systemPrompt']
        layout = deepcopy(self.plan['layout'])
        layout.update(runtime=build, state=context['store'],
                      config=build + '/config', credentials=build + '/credentials')
        def bind(value):
            if type(value) is dict:
                return {k: bind(v) for k, v in value.items()}
            if type(value) is list:
                return [bind(v) for v in value]
            if type(value) is str:
                # Connector data paths must use the same persistent state root.
                return value.replace(BUILD_BINDING, build).replace(
                    self.plan['layout']['state'] + '/', context['store'] + '/')
            return value
        bound = require_bound(bind({'recipe': recipe, 'layout': layout}))
        recipe, layout = bound['recipe'], bound['layout']
        files = []
        for filename, content in (('cook-recipe.json', recipe), ('cook-layout.json', layout)):
            raw = canonical(content).decode() + '\n'
            files.append({'path': build + '/' + filename, 'mode': '0600', 'content': raw,
                          'sha256': hashlib.sha256(raw.encode()).hexdigest()})
        return {'agent': context['agent'], 'unixName': declaration['unixName'],
                'homeDirectory': declaration['homeDirectory'], 'buildDirectory': build,
                'stateDirectory': context['store'], 'unit': 'connectome-' + context['agent'] + '.service',
                'unitDirectory': declaration['homeDirectory'] + '/.config/systemd/user',
                'identity': identity, 'transportIdentity': context['permanentIdentity'] or context['temporaryIdentity'],
                'recipe': recipe, 'layout': layout, 'files': files,
                'runtimeSources': deepcopy(self.plan['runtimeSources']),
                'artifacts': deepcopy(self.plan['artifacts']),
                'engine': {'prepare': 'connectome_deploy.Deployment.prepare',
                           'activate': 'connectome_deploy.Deployment.activate', 'cook': 'connectome_cook.adapt'},
                'freshBuildRequired': True, 'keepMemory': True}

    def execute_effect(self, action, inputs, operation_id):
        receipt = acknowledged(self.executor, action, inputs, operation_id)
        if action == 'ai-access':
            need(type(receipt.get('result')) is dict and receipt['result'].get('ready') is True,
                 'AI access awaiting operator provisioning; authentication remains unknown')
        return receipt

    def perform(self, step, context, operation_id):
        # Direct callers must provide their own durable intent; Lifecycle uses
        # perform_journaled to persist EACH action, including a partial build.
        return self.perform_journaled(step, context, operation_id,
            lambda action, inputs: self.execute_effect(action, inputs, operation_id + ':' + action))

    def perform_journaled(self, step, context, operation_id, effect):
        inputs = self._inputs(context, operation_id)
        if step in ('build', 'redeploy-keep-memory'):
            if step == 'build':
                effect('ai-access', {'agent': context['agent'], 'selection': [
                    a for a in self.plan['providerActions'] if a['step'] == 'supply-ai-access']})
                for entry in self.plan['lifecycle']:
                    if entry['step'] == 'validate-optional-secrets':
                        effect('validate-optional-secrets', dict(entry['inputs'], agent=context['agent']))
                effect('create-unix-user', {'unixName': inputs['unixName'],
                    'homeDirectory': inputs['homeDirectory'], 'collisionPolicy': 'refuse-unowned'})
                effect('create-directories', {'unixName': inputs['unixName'],
                    'paths': [self.plan['layout'][k] for k in ('config', 'workspace', 'credentials')],
                    'mode': '0700', 'createMemoryStore': False})
            effect('render-agent-layer', {'repo': self.private_repo,
                'path': 'agents/' + context['agent'] + '.yml', 'requireWriteAccess': True,
                'layer': {'name': context['agent'], 'agent': inputs['recipe']['agent'],
                          'state_dir': context['store']}, 'mode': '0600'})
            effect('cook-build', dict(inputs, activation='install_only', noStart=True))
            if step == 'redeploy-keep-memory':
                effect('switch-start', dict(inputs, activation='start', usePreparedOperation=operation_id))
        elif step == 'hold':
            build_operation = operation_id.rsplit(':', 1)[0] + ':build'
            effect('install-user-unit', dict(self._inputs(context, build_operation), activation='install_only', noStart=True))
        elif step in ('activate', 'start-initiation'):
            # Build and activation MUST refer to the same prepared build.
            build_operation = operation_id.rsplit(':', 1)[0] + ':build'
            prepared = self._inputs(context, build_operation)
            effect('install-user-unit', dict(prepared, activation='install_only', noStart=True))
            effect('switch-start', dict(prepared, activation='start'))
        elif step in ('join-initiation', 'close-initiation'):
            effect(step, {'agent': context['agent'], 'home': context['home'], 'topic': 'initiation',
                'identity': context['permanentIdentity'] or context['temporaryIdentity'],
                'participants': 'home-channel-members'})
        elif step == 'retire':
            effect('stop-unit', {'unixName': inputs['unixName'], 'unit': inputs['unit'],
                                'scope': 'user', 'keepState': True, 'store': context['store']})
        else:
            need(False, 'unsupported executor lifecycle step')

    def observe(self, context, operation_id):
        if context['pending'] is None:
            return self.executor.observe('deployment-state', {'agent': context['agent'],
                'desiredState': context['state'], 'store': context['store'],
                'unit': 'connectome-' + context['agent'] + '.service',
                'unixName': self.plan['instance']['unixName'],
                'identity': context['permanentIdentity'] or context['temporaryIdentity']}, operation_id)
        descriptor = context['pending'].get('effect')
        if descriptor is None:
            # Identity/import operations still have a stable scoped id; the
            # observer must return the original result (handle) after a lost ack.
            descriptor = {'action': context['pending']['step'].removesuffix(':completed'),
                'inputs': {'agent': context['agent'], 'store': context['store'],
                           'temporaryIdentity': context['temporaryIdentity'],
                           'permanentIdentity': context['permanentIdentity']}, 'operation_id': operation_id}
        return self.executor.observe(descriptor['action'], deepcopy(descriptor['inputs']), descriptor['operation_id'])

    def verify(self, context, operation_id, checkpoint):
        inputs = {'agent': context['agent'], 'store': context['store'],
                  'unit': 'connectome-' + context['agent'] + '.service',
                  'unixName': self.plan['instance']['unixName']}
        def observe(action):
            return checkpoint(action, lambda: self.executor.observe(action, inputs, operation_id + ':' + action))
        health = observe('service-health')
        probes = {}
        for connector in self.plan['recipe']['mcpServers']:
            action = 'mcp-handshake:' + connector
            # A probe starts the connector, initializes MCP and lists tools,
            # then closes it. It cannot start/restart the agent service.
            probes[connector] = checkpoint(action, lambda connector=connector: self.executor.execute(
                'mcp-handshake', {'agent': context['agent'], 'connector': connector,
                    'steps': ['start', 'initialize', 'tools/list', 'close'],
                    'useInstalledLaunch': True, 'verifyOwnedRelease': True}, operation_id + ':' + action))
        auth = observe('provider-authenticated')
        reply = observe('first-real-reply')
        def proven(receipt, key):
            return (type(receipt) is dict and receipt.get('state') == 'applied' and
                    type(receipt.get('evidence')) is dict and bool(receipt['evidence']) and
                    type(receipt.get('result')) is dict and receipt['result'].get(key) is True)
        service = proven(health, 'ready') and all(
            proven(p, 'initialized') and proven(p, 'toolsListed') for p in probes.values())
        return {'serviceReady': {'state': 'success' if service else 'unknown',
                                 'evidence': {'health': health, 'mcp': probes}},
                'providerAuthenticated': {'state': 'success' if proven(auth, 'authenticated') else 'unknown', 'evidence': auth},
                'firstRealReply': {'state': 'success' if proven(reply, 'realReply') else 'unknown', 'evidence': reply}}


class ExecutionBackend:
    """Explicit Python frontend; no real apply/start CLI or role auto-wiring."""
    def __init__(self, root, validated_plan, *, private_repo, build_root, executor=None, supports_rename=False):
        fields(validated_plan, ('plan', 'planHash', 'idempotenceKey'))
        plan = validated_plan['plan']
        need(validated_plan['planHash'] == digest(plan) and plan['schema'] == 'connectome.spawn-plan/v1',
             'validated plan hash mismatch')
        declaration = instance(plan['instance'])
        need(plan['home'] == declaration['home'], 'plan home mismatch')
        self.root, self.agent = safe_path(root), declaration['instance']
        self.adapter = ExecutorLifecycleAdapter(plan, private_repo=private_repo, build_root=build_root, executor=executor)
        self.executor = self.adapter.executor
        bootstrap = plan['recipe']['agent']
        identity = {'name': bootstrap['name'], 'displayName': bootstrap['name'],
                    'summaryParticipant': bootstrap['strategy']['summaryParticipant']}
        if 'systemPrompt' in bootstrap:
            identity['systemPrompt'] = bootstrap['systemPrompt']
        self.adapter._inputs({'agent': self.agent, 'identity': identity, 'home': plan['home'],
            'store': str(self.root / self.agent / 'state/store'),
            'permanentIdentity': None, 'temporaryIdentity': None}, 'admission:build')
        self.binding = {'plan': deepcopy(validated_plan), 'privateRepo': self.adapter.private_repo,
                        'buildRoot': self.adapter.build_root, 'supportsRename': supports_rename}
        self.path = self.root / self.agent / 'execution.json'
        self.life = Lifecycle(root, adapter=self.adapter,
            identity_provider=ExecutorIdentityProvider(self.executor, supports_rename=supports_rename),
            importer=ExecutorSeedImporter(self.executor))

    def _bind(self, create=False):
        need(create or self.path.is_file(), 'execution binding missing')
        with locked(self.path.parent):
            if self.path.exists():
                need(read(self.path) == self.binding, 'execution binding changed; reconciliation required')
            else:
                need(create and not (self.path.parent / 'lifecycle.json').exists(),
                     'existing unbound agent cannot be adopted as new')
                write(self.path, self.binding)

    def apply(self, operation_id, *, target=None, name_source=None, payload=None):
        self._bind(create=True)
        plan = self.binding['plan']['plan']
        if not (self.path.parent / 'lifecycle.json').exists():
            need(seed_intent({'mode': plan['seed']['mode']}, payload) == plan['seed'], 'plan seed mismatch')
            agent = plan['recipe']['agent']
            identity = {'name': agent['name'], 'displayName': agent['name'],
                        'summaryParticipant': agent['strategy']['summaryParticipant']}
            if 'systemPrompt' in agent:
                identity['systemPrompt'] = agent['systemPrompt']
            self.life.create(self.agent, seed={'mode': plan['seed']['mode']}, payload=payload,
                identity=identity, home=plan['home'], operation_id='create',
                identity_config={'temporaryNameTemplate': plan['identity']['temporaryNameTemplate']})
        return self.life.transition(self.agent, target or ('initiation' if plan['seed']['branch'] == 'clean' else 'active'),
                                   operation_id, name_source=name_source, payload=payload)

    def verify(self, operation_id):
        self._bind()
        return self.life.verify(self.agent, operation_id)

    def reconcile(self):
        self._bind()
        return self.life.reconcile(self.agent)
