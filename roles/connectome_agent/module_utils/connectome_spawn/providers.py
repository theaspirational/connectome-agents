"""File-backed access and optional secrets. No provider commands or network calls."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
from typing import Protocol

from .data import digest, fields, match, need
from .persistence import locked, read, safe_path, write
from .schema import NAME
from .sources import read_file


def file_metadata(path, *, root=None):
    raw = read_file(path, root=root, private=True)
    need(bool(raw) and bool(raw.strip()), 'nonempty private key file required')
    return {'path': str(Path(path).absolute()), 'digest': hashlib.sha256(raw).hexdigest()}


class FileSecretProvider:
    """Optional declared names map literally to existing private per-agent files.

    No discovery, copying, fallback, environment lookup or automatic provisioning.
    A caller explicitly requesting an absent/unsafe secret gets a refusal; this
    does not make that optional integration mandatory for creating an agent.
    """
    kind = 'file'

    def __init__(self, secrets_root):
        self.root = safe_path(secrets_root)

    def resolve(self, agent, names):
        need(match(agent, NAME), 'invalid secret owner')
        need(type(names) is list and all(match(n, r'[A-Z_][A-Z0-9_]{0,127}') for n in names)
             and len(set(names)) == len(names), 'invalid declared secret names')
        root = safe_path(self.root / agent)
        return {name: file_metadata(root / name, root=root) for name in names}


class AIAccessProvider(Protocol):
    kind: str

    def plan(self, agent: str) -> dict: ...
    def provision(self, agent: str, operation_id: str) -> dict: ...


class DirectKeyProvider:
    """Verify a role-selected key file, never validate it against a real service.

    Receipts contain a path and computed digest only. Ready means safe local
    input, not an assertion of provider acceptance, quota or model availability.
    """
    kind = 'direct-key'

    def __init__(self, root, *, key_file):
        self.root = safe_path(root)
        self.path = self.root / 'access.json'
        self.key_file = safe_path(key_file)

    def plan(self, agent):
        need(match(agent, NAME), 'invalid access owner')
        return {'kind': self.kind, 'execution': 'operator-required', 'action': 'supply-key-file',
                'inputs': {'agent': agent}}

    def provision(self, agent, operation_id):
        need(match(operation_id, r'[A-Za-z0-9_:-]{1,256}'), 'invalid access operation')
        action = self.plan(agent)
        request = digest({'action': action, 'path': str(self.key_file)})
        key = agent + ':' + operation_id
        with locked(self.root):
            value = read(self.path) if self.path.exists() else {'operations': {}}
            fields(value, ('operations',))
            need(type(value['operations']) is dict, 'invalid access receipts')
            prior = value['operations'].get(key)
            if prior is not None:
                fields(prior, ('digest', 'result'))
                need(prior['digest'] == request, 'access operation id conflict')
            result = {'kind': self.kind, 'state': 'awaiting-provisioning', 'action': action}
            # lexists also catches dangling symlinks: they are unsafe, not absent.
            if os.path.lexists(self.key_file):
                result.update(state='ready', file=file_metadata(self.key_file))
            if prior and prior['result']['state'] == 'ready':
                need(result == prior['result'], 'access file changed; reconciliation required')
                return deepcopy(result)
            value['operations'][key] = {'digest': request, 'result': result}
            write(self.path, value)
            return deepcopy(result)


class InferenceGateProvider(DirectKeyProvider):
    """Plan a privileged operator action; only consume an externally placed token.

    Deliberately no gate client, gatectl invocation or token minting. A token file
    cannot attest that the operator actually applied the planned pool/limit.
    """
    kind = 'inference-gate'

    def __init__(self, root, *, token_file, pool, limit):
        super().__init__(root, key_file=token_file)
        need(match(pool, r'[A-Za-z][A-Za-z0-9_-]{0,62}') and
             type(limit) is int and 0 < limit <= 10000000, 'invalid gate pool or limit')
        self.pool, self.limit = pool, limit

    def plan(self, agent):
        need(match(agent, NAME), 'invalid access owner')
        return {'kind': self.kind, 'execution': 'operator-required', 'action': 'provision-agent-token',
                'privileged': True, 'inputs': {'agent': agent, 'pool': self.pool, 'limit': self.limit}}


def provider_policy(value=None):
    """Public intent only. Role-only credential paths never enter template data."""
    value = {} if value is None else value
    fields(value, (), ('aiAccess',))
    if 'aiAccess' not in value:
        return {}
    access = value['aiAccess']
    fields(access, ('kind',), ('pool', 'limit'))
    need(access['kind'] in ('direct-key', 'inference-gate'), 'unsupported AI access kind')
    if access['kind'] == 'direct-key':
        fields(access, ('kind',))
    else:
        fields(access, ('kind', 'pool', 'limit'))
        need(match(access['pool'], r'[A-Za-z][A-Za-z0-9_-]{0,62}') and
             type(access['limit']) is int and 0 < access['limit'] <= 10000000, 'invalid gate pool or limit')
    return deepcopy(value)


def plan_providers(steps, policy, agent, secret_names):
    """Describe automatic provider effects versus work only an operator can do.

    Never instantiate providers or read credentials while compiling public intent.
    Runtime/seed/chat integrations remain optional, unimplemented adapter hooks.
    """
    policy = provider_policy(policy)
    access = policy.get('aiAccess')
    kinds = [access['kind']] if access else ['direct-key', 'inference-gate']
    prefix = [{'step': 'ai-access', 'needsProvider': kinds, 'providerSelection': 'one-of',
               'execution': 'automatic', 'required': True}]
    if not any(s['step'] == 'provision-temporary' for s in steps):
        prefix.insert(0, {'step': 'provision-permanent', 'needsProvider': ['zulip'],
                          'execution': 'automatic', 'required': True})
    if secret_names:
        prefix.append({'step': 'validate-optional-secrets', 'needsProvider': ['file'],
                       'execution': 'automatic', 'required': False,
                       'inputs': {'names': sorted(secret_names)}})
    actions = [{'step': 'supply-zulip-operator-credential', 'providerKinds': ['zulip'],
                'execution': 'operator-required', 'required': True,
                'inputs': {'format': 'role-only-zuliprc', 'mode': '0600'}}]
    supply = {'step': 'supply-ai-access', 'providerKinds': kinds, 'execution': 'operator-required',
              'required': True, 'inputs': {'agent': agent}, 'providerSelection': 'one-of',
              'selectionRequired': access is None}
    if access and access['kind'] == 'inference-gate':
        supply.update(action='provision-agent-token', privileged=True)
        supply['inputs'].update(pool=access['pool'], limit=access['limit'])
    else:
        supply['action'] = 'supply-key-file' if access else 'select-kind-and-supply-key-file'
    actions.append(supply)
    identity_steps = {'provision-temporary', 'provision-permanent', 'rename', 'retire-temporary'}
    result = prefix
    for original in steps:
        entry = deepcopy(original)
        step = entry['step']
        if step in identity_steps:
            entry.update(needsProvider=['zulip'], execution='automatic', required=True)
        elif step == 'agent-name-record':
            entry.update(needsProvider=['lifecycle-adapter'], execution='operator-required', required=False)
        elif step == 'ingest':
            entry.update(needsProvider=['seed-import-adapter'], execution='operator-required', required=False)
        elif step in ('build', 'hold', 'join-initiation', 'start-initiation',
                       'redeploy-keep-memory', 'activate', 'close-initiation'):
            entry.update(needsProvider=['executor'], execution='automatic', required=True,
                         requiresExecutor=True, defaultExecutor='dry-run-recorder')
        elif entry['needsProvider']:
            entry.update(needsProvider=['lifecycle-adapter'], execution='operator-required', required=False)
        else:
            entry.update(needsProvider=[], execution='local', required=False)
        result.append(entry)
    for entry in result:
        if entry['needsProvider']:
            action = {k: deepcopy(v) for k, v in entry.items() if k != 'needsProvider'}
            action['providerKinds'] = deepcopy(entry['needsProvider'])
            if action['execution'] == 'operator-required':
                action['available'] = False
            actions.append(action)
    if secret_names:
        actions.append({'step': 'supply-optional-secrets', 'providerKinds': ['file'],
                        'execution': 'operator-required', 'required': False,
                        'inputs': {'names': sorted(secret_names), 'mode': '0600'}})
    return result, actions
