"""Opaque seed payload contract. Real provider import is a T4 adapter hook."""
import hashlib
from pathlib import Path
from typing import Protocol

from .data import fields, need
from .sources import read_file


class SeedImporter(Protocol):
    def ingest(self, payload: bytes, store: Path, operation_id: str) -> None:
        """Import once into the reserved empty store; return only after durable ingest.

        No activation. Payload format belongs to the adapter, not this package.
        A raised error or process crash requires reconciliation, never re-ingest.
        """
        ...


def seed_payload(file):
    return read_file(file)


def seed_policy(value):
    fields(value, ('mode',))
    need(value['mode'] in ('required', 'clean', 'optional'), 'unknown seed mode')
    return dict(value)


def seed_intent(policy, payload=None):
    mode = seed_policy(policy)['mode']
    need(payload is None or type(payload) is bytes and 0 < len(payload) <= 1024 * 1024,
         'bounded nonempty seed bytes required')
    need(mode != 'required' or payload is not None, 'required seed payload missing')
    need(mode != 'clean' or payload is None, 'clean mode refuses seed payload')
    return {'mode': mode, 'branch': 'seeded' if payload is not None else 'clean',
            'digest': hashlib.sha256(payload).hexdigest() if payload is not None else None}


def lifecycle_steps(branch):
    if branch == 'seeded':
        steps = [('build', True), ('hold', True), ('ingest', True),
                 ('seed.consumed', False), ('activate', True)]
    else:
        steps = [('build', True), ('provision-temporary', True), ('join-initiation', True),
                 ('start-initiation', True), ('home-channel-conversation', True),
                 ('agent-name-record', False), ('rename', True), ('provision-permanent', True),
                 ('redeploy-keep-memory', True), ('retire-temporary', True), ('close-initiation', True)]
    result = [{'step': step, 'needsProvider': provider} for step, provider in steps]
    for entry in result:
        if entry['step'] == 'rename':
            entry['when'] = 'provider.supports_rename'
        if entry['step'] in ('provision-permanent', 'retire-temporary'):
            entry['when'] = 'not provider.supports_rename'
    return result
