"""Per-agent identity-provider lifecycle contract and offline durable file fake.

No pooled identities: retired identities are retained as tombstones, never reused.
The Zulip implementation is in zulip.py; no provider is selected implicitly.
"""
from copy import deepcopy
from datetime import datetime, timezone
from typing import Protocol
import uuid
import re

from .data import digest, fields, match, need
from .persistence import locked, read, safe_path, write
from .schema import NAME, concrete, name_record

DEFAULT_TEMPORARY_NAME = 'ConnectomeNewborn{timestamp}'


def identity_policy(value=None):
    value = {} if value is None else value
    fields(value, (), ('temporaryNameTemplate',))
    pattern = value.get('temporaryNameTemplate', DEFAULT_TEMPORARY_NAME)
    need(type(pattern) is str and pattern.count('{timestamp}') == 1 and
         match(pattern.replace('{timestamp}', '20000101T000000000000Z'), r'[A-Za-z][A-Za-z0-9_-]{0,255}'),
         'temporary name template requires exactly one timestamp and safe literal text')
    concrete(pattern)
    return {'temporaryNameTemplate': pattern}


def temporary_name(policy, now=None):
    instant = now if now is not None else datetime.now(timezone.utc)
    need(instant.tzinfo is not None, 'timezone-aware temporary identity timestamp required')
    stamp = instant.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    return identity_policy(policy)['temporaryNameTemplate'].replace('{timestamp}', stamp)


def validate_capabilities(provider):
    need(type(provider.supports_rename) is bool and type(provider.max_name_length) is int and
         0 < provider.max_name_length <= 256 and type(provider.name_pattern) is str,
         'invalid identity provider capabilities')
    try:
        re.compile(provider.name_pattern)
    except re.error:
        need(False, 'invalid identity provider charset')


def validate_provider_name(provider, name):
    validate_capabilities(provider)
    need(type(name) is str and len(name) <= provider.max_name_length and match(name, provider.name_pattern),
         'identity name exceeds provider length or charset; never truncated')


class IdentityProvider(Protocol):
    """Per-agent native identity ownership; durable operation-id receipts required.

    Uncertain external effects must be reported for reconciliation, never blindly
    replayed. Rename returns the same handle, now permanent. Replace creates a
    different handle. Retirement never makes an identity reusable by another agent.
    """
    supports_rename: bool
    max_name_length: int
    name_pattern: str

    def provision_temporary(self, agent: str, name: str, operation_id: str) -> dict: ...
    def provision_permanent(self, agent: str, name_record: dict, operation_id: str) -> dict: ...
    def rename(self, handle: dict, name_record: dict, operation_id: str) -> dict: ...
    def retire(self, handle: dict, operation_id: str) -> None: ...
    def list(self) -> list: ...


class FileIdentityProvider:
    """Atomic local fake. Capabilities and operation receipts survive restarts.

    Handles remain stable across rename. Every new identity belongs exclusively
    to its agent. No deletion, recycling, expiry or automatic crash reclamation.
    """
    def __init__(self, root, *, supports_rename=None, max_name_length=None, name_pattern=None):
        self.root = safe_path(root)
        self.path = self.root / 'identities.json'
        with locked(self.root):
            if self.path.exists():
                value = self._read()
                for key, supplied in (('supports_rename', supports_rename),
                                      ('max_name_length', max_name_length), ('name_pattern', name_pattern)):
                    need(supplied is None or supplied == value['capabilities'][key],
                         'identity provider capability mismatch')
            else:
                value = {'capabilities': {
                    'supports_rename': False if supports_rename is None else supports_rename,
                    'max_name_length': 63 if max_name_length is None else max_name_length,
                    'name_pattern': r'[A-Za-z][A-Za-z0-9_-]*' if name_pattern is None else name_pattern},
                    'identities': {}, 'operations': {}}
                self._capabilities(value)
                validate_capabilities(self)
                write(self.path, value)
            self._capabilities(value)

    def _capabilities(self, value):
        for key, supplied in value['capabilities'].items():
            setattr(self, key, supplied)

    def _read(self):
        value = read(self.path)
        fields(value, ('capabilities', 'identities', 'operations'))
        fields(value['capabilities'], ('supports_rename', 'max_name_length', 'name_pattern'))
        self._capabilities(value)
        validate_capabilities(self)
        need(type(value['identities']) is dict and type(value['operations']) is dict,
             'invalid provider state')
        active_names, active_roles = set(), set()
        for key, row in value['identities'].items():
            fields(row, ('agent', 'name', 'kind', 'retired'))
            need(match(key, r'[0-9a-f]{32}') and match(row['agent'], NAME) and
                 row['kind'] in ('temporary', 'permanent') and type(row['retired']) is bool,
                 'invalid identity record')
            validate_provider_name(self, row['name'])
            if not row['retired']:
                role = (row['agent'], row['kind'])
                need(row['name'] not in active_names and role not in active_roles, 'duplicate active identity')
                active_names.add(row['name'])
                active_roles.add(role)
        for key, row in value['operations'].items():
            fields(row, ('digest', 'result'))
            need(match(key, r'[a-z][a-z0-9-]{0,62}:[A-Za-z0-9_:-]{1,256}') and
                 match(row['digest'], r'[0-9a-f]{64}'), 'invalid provider operation')
            if row['result'] is not None:
                self._row(value, row['result'])
        return value

    @staticmethod
    def _row(value, handle):
        fields(handle, ('id', 'agent'))
        need(match(handle['id'], r'[0-9a-f]{32}') and match(handle['agent'], NAME), 'invalid identity handle')
        row = value['identities'].get(handle['id'])
        need(row is not None and row['agent'] == handle['agent'], 'unknown identity handle')
        return row

    def _operation(self, agent, operation_id, request, action):
        need(match(agent, NAME) and match(operation_id, r'[A-Za-z0-9_:-]{1,256}'),
             'invalid identity operation')
        key, request_hash = agent + ':' + operation_id, digest(request)
        with locked(self.root):
            value = self._read()
            if key in value['operations']:
                prior = value['operations'][key]
                need(prior['digest'] == request_hash, 'identity operation id conflict')
                return deepcopy(prior['result'])
            result = action(value)
            value['operations'][key] = {'digest': request_hash, 'result': result}
            write(self.path, value)
            return deepcopy(result)

    def _provision(self, agent, name, kind, operation_id, record=None):
        validate_provider_name(self, name)
        def action(value):
            need(not any(not row['retired'] and (row['name'] == name or
                     (row['agent'] == agent and row['kind'] == kind))
                     for row in value['identities'].values()), 'identity already exists; reconcile, never reuse')
            handle = {'id': uuid.uuid4().hex, 'agent': agent}
            value['identities'][handle['id']] = {'agent': agent, 'name': name, 'kind': kind, 'retired': False}
            return handle
        return self._operation(agent, operation_id, {'kind': kind, 'name': name, 'record': record}, action)

    def provision_temporary(self, agent, name, operation_id):
        return self._provision(agent, name, 'temporary', operation_id)

    def provision_permanent(self, agent, name_record_value, operation_id):
        record = name_record(name_record_value)
        return self._provision(agent, record['name'], 'permanent', operation_id, record)

    def rename(self, handle, name_record_value, operation_id):
        fields(handle, ('id', 'agent'))
        record = name_record(name_record_value)
        validate_provider_name(self, record['name'])
        need(self.supports_rename, 'provider does not support rename')
        def action(value):
            row = self._row(value, handle)
            need(not row['retired'] and row['kind'] == 'temporary', 'only live temporary identity can be renamed')
            need(not any(key != handle['id'] and not other['retired'] and
                         (other['name'] == record['name'] or
                          (other['agent'] == handle['agent'] and other['kind'] == 'permanent'))
                         for key, other in value['identities'].items()), 'permanent identity collision')
            row.update(name=record['name'], kind='permanent')
            return handle
        return self._operation(handle['agent'], operation_id,
                               {'rename': handle, 'record': record}, action)

    def retire(self, handle, operation_id):
        fields(handle, ('id', 'agent'))
        def action(value):
            self._row(value, handle)['retired'] = True
        return self._operation(handle['agent'], operation_id, {'retire': handle}, action)

    def list(self):
        with locked(self.root):
            value = self._read()
            return [dict(row, id=key, recovery='retired; never reused' if row['retired'] else
                         'owned; liveness unknown; reconcile if agent crashed')
                    for key, row in sorted(value['identities'].items())]
