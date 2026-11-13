"""Provider tests use only a loopback HTTP server and disposable private files."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'roles/connectome_agent/module_utils'))
from connectome_spawn import (SpawnError, ZulipIdentityProvider, DirectKeyProvider,
                              InferenceGateProvider, FileSecretProvider)
from connectome_spawn.persistence import read, write_secret_bytes

RECORD = {'name': 'ChosenAgent', 'displayName': 'Chosen Agent', 'summaryParticipant': 'ChosenAgent'}


class ZulipFixture:
    """Loopback Zulip wire model, never internal /register realm_bots records.

    GET /bots uses get_bots_backend's exact field set and caller/active filter.
    User endpoints project only documented fields consumed by this provider;
    POST/PATCH/DELETE return only the common success envelope (payload ignored).
    """
    def __init__(self):
        self.bots, self.calls = {}, []
        self.failure = None
        self.lost = None
        self.committed_error = None
        self.response_mode = None
        self.user_overrides = {}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.handle_call('GET')

            def do_POST(self):
                self.handle_call('POST')

            def do_PATCH(self):
                self.handle_call('PATCH')

            def do_DELETE(self):
                self.handle_call('DELETE')

            def handle_call(self, method):
                path = unquote(self.path.removeprefix('/api/v1'))
                data = parse_qs(self.rfile.read(int(self.headers.get('Content-Length', '0'))).decode())
                fixture.calls.append((method, path, data))
                auth = base64.b64encode(b'noreply@users.noreply.github.com:operator-fixture').decode()
                if self.headers.get('Authorization') != 'Basic ' + auth:
                    self.reply(401, {'result': 'error', 'msg': 'private response must not escape'})
                    return
                if fixture.response_mode:
                    raw = {'malformed': b'{bad json private response', 'yaml': b'result: success',
                           'oversized': b'{' + b' ' * (1024 * 1024 + 1)}[fixture.response_mode]
                    self.reply_raw(200, raw)
                    return
                if fixture.failure and fixture.failure[0] == method:
                    code = fixture.failure[1]
                    if fixture.failure[2:] == ('truncated',):
                        self.send_response(code)
                        self.send_header('Content-Length', '999')
                        self.end_headers()
                        self.wfile.write(b'{"result":"error"')
                        self.close_connection = True
                    elif len(fixture.failure) > 2:
                        self.reply_raw(code, fixture.failure[2])
                    else:
                        self.reply(code, {'result': 'error', 'msg': 'private response must not escape'})
                    return
                if path == '/users/me':
                    result = {'user_id': 1, 'is_bot': False, 'role': 100}
                elif path.split('?')[0] == '/users':
                    self.reply(500, {'result': 'error', 'msg': 'whole-realm listing forbidden'})
                    return
                elif path == '/bots' and method == 'GET':
                    result = {'bots': [fixture.listed(b) for b in fixture.bots.values()
                                       if b['owner_id'] == 1 and b['is_active']]}
                elif path == '/bots' and method == 'POST':
                    if any(b['is_active'] and b['full_name'] == data['full_name'][0]
                           for b in fixture.bots.values()):
                        self.reply(400, {'result': 'error', 'msg': 'Name is already in use.'})
                        return
                    short = data['short_name'][0]
                    if any(b['username'].split('-bot@')[0].lower() == short.lower() for b in fixture.bots.values()):
                        self.reply(400, {'result': 'error', 'msg': 'duplicate short name'})
                        return
                    bot = fixture.add(short, data['full_name'][0])
                    assert data['bot_type'] == ['1']
                    result = {}  # Provider must ignore the create payload.
                elif path.startswith('/users/'):
                    key = path.split('/')[-1]
                    bot = next((b for b in fixture.bots.values() if str(b['user_id']) == key or b['username'] == key), None)
                    if bot is None:
                        self.reply(404, {'result': 'error'})
                        return
                    result = {'user': {k: bot[k] for k in ('user_id', 'full_name', 'is_active')}}
                    result['user'].update(email=bot['username'], bot_owner_id=bot['owner_id'], is_bot=True)
                    result['user'].update(fixture.user_overrides)
                elif path.startswith('/bots/') and method in ('PATCH', 'DELETE'):
                    bot = fixture.bots.get(int(path.split('/')[-1]))
                    if not bot or bot['owner_id'] != 1:
                        self.reply(403, {'result': 'error'})
                        return
                    if method == 'PATCH':
                        if any(b['user_id'] != bot['user_id'] and b['is_active'] and
                               b['full_name'] == data['full_name'][0] for b in fixture.bots.values()):
                            self.reply(400, {'result': 'error', 'msg': 'Name is already in use.'})
                            return
                        bot['full_name'] = data['full_name'][0]
                    else:
                        bot['is_active'] = False
                    result = {}
                else:
                    self.reply(404, {'result': 'error'})
                    return
                if fixture.committed_error == method or fixture.committed_error == (method, 400):
                    self.reply(400 if type(fixture.committed_error) is tuple else 500, {'result': 'error', 'msg': 'private response'})
                    return
                if fixture.lost == method:
                    fixture.lost = None
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.reply(200, dict(result, result='success', msg=''))

            def reply(self, status, value):
                self.reply_raw(status, json.dumps(value).encode())

            def reply_raw(self, status, raw):
                self.send_response(status)
                if status == 302:
                    self.send_header('Location', fixture.site + '/redirected')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass

        address = socket.inet_ntoa(bytes([127, 0, 0, 1]))
        self.server = ThreadingHTTPServer((address, 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.site = 'http://' + address + ':' + str(self.server.server_port)

    def add(self, short, full, owner=1, active=True):
        uid = len(self.bots) + 10
        bot = {'user_id': uid, 'username': short + '-bot@users.noreply.github.com', 'full_name': full,
               'api_key': 'bot-fixture-' + str(uid), 'owner_id': owner, 'is_active': active}
        self.bots[uid] = bot
        return bot

    @staticmethod
    def listed(bot):
        return {**{k: bot[k] for k in ('username', 'full_name', 'api_key')},
                'avatar_url': '', 'default_sending_stream': None,
                'default_events_register_stream': None, 'default_all_public_streams': False}

    def count(self, method):
        return sum(c[0] == method for c in self.calls)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ProviderTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.cache').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.cache')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.http = ZulipFixture()
        self.addCleanup(self.http.close)
        self.credential = self.work / 'operator.ini'
        self.private(self.credential, '[api]\nemail=noreply@users.noreply.github.com\nkey=operator-fixture\nsite=' + self.http.site + '\n')
        self.root, self.secrets = self.work / 'provider', self.work / 'credentials'

    def private(self, path, data='fixture-key'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        path.chmod(0o600)
        return path

    def provider(self, **kwargs):
        return ZulipIdentityProvider(self.root, zuliprc=self.credential, secrets_root=self.secrets,
                                     allow_loopback_http=True, **kwargs)

    def create(self, provider=None, agent='example-agent', name='NewbornAgent', op='create'):
        return (provider or self.provider()).provision_temporary(agent, name, op)

    def test_provision_rename_retire_restart_and_no_credentials_in_state(self):
        provider = self.provider()
        self.assertTrue(provider.supports_rename)
        self.assertEqual(provider.max_name_length, 63)
        handle = self.create(provider)
        self.assertEqual(set(handle), {'id', 'agent'})
        self.assertEqual(self.create(self.provider()), handle)
        self.assertEqual(self.http.count('POST'), 1)
        files = list((self.secrets / 'example-agent').iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(files[0].read_text(), next(iter(self.http.bots.values()))['api_key'])
        self.assertEqual(provider.rename(handle=handle, name_record=RECORD, operation_id='rename'), handle)
        self.assertEqual(self.provider().rename(handle, RECORD, 'rename'), handle)
        self.assertEqual(self.http.count('PATCH'), 1)
        self.assertEqual(next(iter(self.http.bots.values()))['full_name'], RECORD['name'])
        provider.retire(handle, 'retire')
        self.provider().retire(handle, 'retire')
        self.assertEqual(self.http.count('DELETE'), 1)
        self.assertFalse(next(iter(self.http.bots.values()))['is_active'])
        self.assertFalse(files[0].exists())
        with self.assertRaisesRegex(SpawnError, 'retired'):
            self.create(provider)
        rows = self.provider().list()
        self.assertTrue(rows[0]['retired'])
        for path in self.root.rglob('*'):
            if path.is_file():
                raw = path.read_text()
                for private in ('operator-fixture', 'bot-fixture-', self.http.site, str(self.credential)):
                    self.assertNotIn(private, raw)
        self.assertNotIn('api_key', json.dumps(rows))

    def test_completed_create_retry_requires_safe_delivered_key(self):
        provider = self.provider()
        self.create(provider)
        key = next((self.secrets / 'example-agent').iterdir())
        key.chmod(0o644)
        with self.assertRaises(SpawnError):
            self.create(provider)
        key.chmod(0o600)
        key.unlink()
        with self.assertRaises(SpawnError):
            self.create(provider)
        self.assertEqual(self.http.count('POST'), 1)

    def test_permanent_identity_is_distinct_and_receipted(self):
        provider = self.provider()
        temporary = self.create(provider)
        permanent = provider.provision_permanent(agent='example-agent', name_record=RECORD, operation_id='permanent')
        self.assertNotEqual(temporary['id'], permanent['id'])
        self.assertEqual(self.provider().provision_permanent('example-agent', RECORD, 'permanent'), permanent)
        with self.assertRaises(SpawnError):
            provider.provision_permanent('example-agent', dict(RECORD, displayName='Different Display'), 'permanent')
        provider.retire(temporary, 'retire-temporary')
        rows = provider.list()
        self.assertTrue(next(r for r in rows if r['id'] == temporary['id'])['retired'])
        self.assertTrue(next(r for r in rows if r['id'] == permanent['id'])['active'])

    def test_process_death_after_post_is_visible_in_listing(self):
        code = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from connectome_spawn import ZulipIdentityProvider, zulip
original = zulip.write
def crash(path, value):
    if any(o['state'] == 'complete' for o in value.get('operations', {}).values()):
        os._exit(17)
    original(path, value)
zulip.write = crash
provider = ZulipIdentityProvider(sys.argv[2], zuliprc=sys.argv[3], secrets_root=sys.argv[4], allow_loopback_http=True)
provider.provision_temporary('example-agent', 'NewbornAgent', 'create')
"""
        child = subprocess.run([sys.executable, '-c', code, str(ROOT / 'roles/connectome_agent/module_utils'),
                                str(self.root), str(self.credential), str(self.secrets)],
                               capture_output=True, timeout=15)
        self.assertEqual(child.returncode, 17, child.stderr)
        rows = self.provider().list()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['active'])
        self.assertIn('pending', rows[0]['recovery'])
        handle = self.create()
        self.assertEqual(handle['id'], rows[0]['id'])
        self.assertEqual(self.http.count('POST'), 1)

    def test_process_death_during_key_delivery_never_leaves_second_key_copy(self):
        code = """
import os, sys
sys.path.insert(0, sys.argv[1])
from connectome_spawn import ZulipIdentityProvider, persistence
original = persistence.os.replace
def crash(source, destination):
    if str(destination).endswith('.key'):
        os._exit(17)
    original(source, destination)
persistence.os.replace = crash
ZulipIdentityProvider(sys.argv[2], zuliprc=sys.argv[3], secrets_root=sys.argv[4], allow_loopback_http=True).provision_temporary('example-agent', 'NewbornAgent', 'create')
"""
        child = subprocess.run([sys.executable, '-c', code, str(ROOT / 'roles/connectome_agent/module_utils'),
                                str(self.root), str(self.credential), str(self.secrets)],
                               capture_output=True, timeout=15)
        self.assertEqual(child.returncode, 17, child.stderr)
        directory = self.secrets / 'example-agent'
        staged = list(directory.iterdir())
        self.assertEqual(len(staged), 1)
        inode = staged[0].stat().st_ino
        self.create()
        delivered = list(directory.iterdir())
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].stat().st_ino, inode)
        self.assertTrue(delivered[0].name.endswith('.key'))
        self.assertEqual(self.http.count('POST'), 1)

    def test_failed_rename_and_retire_are_reconciled_before_safe_retry(self):
        for method, action in (('PATCH', 'rename'), ('DELETE', 'retire')):
            self.http.bots.clear()
            self.root = self.work / action
            provider = self.provider()
            handle = self.create(provider)
            perform = (lambda: provider.rename(handle, RECORD, 'change')) if action == 'rename' else (lambda: provider.retire(handle, 'change'))
            self.http.failure = (method, 503)
            with self.assertRaises(SpawnError):
                perform()
            # Previously completed operations stay idempotent even with a later pending action.
            self.assertEqual(self.create(provider), handle)
            with self.assertRaises(SpawnError):
                provider.retire(handle, 'different')
            self.http.failure = None
            perform()
            self.assertEqual(provider.list()[0]['retired'], action == 'retire')

    def test_atomic_key_delivery_failure_preserves_pending_and_no_extra_copy(self):
        with patch('connectome_spawn.persistence.os.replace', side_effect=OSError('atomic-write failure')):
            with self.assertRaises(OSError):
                self.create()
        self.assertEqual(self.http.count('POST'), 0)
        # Fail only replacement of key bytes, after server creation has committed.
        original = os.replace
        def fail_key(source, destination):
            if str(destination).endswith('.key'):
                raise OSError('key-write interruption')
            original(source, destination)
        with patch('connectome_spawn.persistence.os.replace', side_effect=fail_key), self.assertRaises(OSError):
            self.create()
        self.assertEqual(list(self.secrets.rglob('.spawn-*')), [])
        self.assertEqual(list(self.secrets.rglob('*.key')), [])
        staged = list((self.secrets / 'example-agent').iterdir())
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0].stat().st_mode & 0o777, 0o600)
        self.create()
        self.assertEqual(self.http.count('POST'), 1)
        self.assertEqual(len(list(self.secrets.rglob('*.key'))), 1)
        self.assertEqual(len(list((self.secrets / 'example-agent').iterdir())), 1)

    def test_provider_state_binding_and_links_are_refused(self):
        self.create()
        alias = self.work / 'provider-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(SpawnError):
            ZulipIdentityProvider(alias, zuliprc=self.credential, secrets_root=self.secrets, allow_loopback_http=True)
        path = self.root / 'zulip.json'
        extra = self.work / 'extra-state'
        os.link(path, extra)
        with self.assertRaises(SpawnError):
            self.provider().list()
        extra.unlink()
        changed = ZulipIdentityProvider(self.root, zuliprc=self.credential,
                                        secrets_root=self.work / 'other-credentials', allow_loopback_http=True)
        with self.assertRaises(SpawnError):
            changed.list()

    def test_lost_post_response_is_reconciled_without_duplicate(self):
        self.http.lost = 'POST'
        handle = self.create()
        self.assertEqual(self.create(), handle)
        self.assertEqual(self.http.count('POST'), 1)
        self.assertEqual(len(self.http.bots), 1)

    def test_crash_between_post_and_receipt_is_discovered(self):
        from connectome_spawn import zulip
        original = zulip.write
        def crash(path, value):
            if value.get('operations') and any(o.get('state') == 'complete' for o in value['operations'].values()):
                raise OSError('simulated death before receipt')
            original(path, value)
        with patch.object(zulip, 'write', side_effect=crash), self.assertRaises(OSError):
            self.create()
        handle = self.create()
        self.assertEqual(self.create(), handle)
        self.assertEqual(self.http.count('POST'), 1)
        self.assertEqual(len(list((self.secrets / 'example-agent').iterdir())), 1)

    def test_pending_create_does_not_adopt_changed_inactive_or_foreign_bot(self):
        from connectome_spawn import zulip
        original = zulip.write
        def crash(path, value):
            if any(o['state'] == 'complete' for o in value.get('operations', {}).values()):
                raise OSError('interrupted receipt')
            original(path, value)
        for field, changed in (('full_name', 'ForeignPersona'), ('owner_id', 2), ('is_active', False)):
            self.http.bots.clear()
            self.root = self.work / field
            with patch.object(zulip, 'write', side_effect=crash), self.assertRaises(OSError):
                self.create()
            next(iter(self.http.bots.values()))[field] = changed
            count = self.http.count('POST')
            with self.assertRaises(SpawnError):
                self.create()
            self.assertEqual(self.http.count('POST'), count)
            self.assertEqual(len(self.http.bots), 1)

    def test_duplicate_full_name_is_not_silently_given_another_persona(self):
        self.http.add('unrelated-native-address', 'NewbornAgent')
        with self.assertRaises(SpawnError):
            self.create()
        self.assertEqual(self.http.count('POST'), 0)

    def test_http_protocol_failures_are_bounded_value_free_and_not_redirected(self):
        for status in (200, 302, 400, 403, 500, 503):
            self.http.failure = ('GET', status)
            self.http.calls.clear()
            with self.subTest(status=status), self.assertRaises(SpawnError) as caught:
                self.create()
            self.assertNotIn('private response', str(caught.exception))
            self.assertEqual(self.http.count('GET'), 1)
            self.assertEqual(self.http.count('POST'), 0)
        self.http.failure = None
        for mode in ('malformed', 'yaml', 'oversized'):
            self.http.response_mode = mode
            with self.subTest(mode=mode), self.assertRaises(SpawnError):
                self.create()
        self.http.response_mode = None
        self.create()
        self.assertEqual(self.http.count('POST'), 1)

    def test_crash_with_no_observable_effect_never_blindly_posts(self):
        from connectome_spawn import zulip
        with patch.object(zulip, 'api_call', side_effect=SpawnError('unavailable')):
            with self.assertRaises(SpawnError):
                self.create()
        # A preflight failure has not submitted intent and can be safely retried.
        self.create()
        self.assertEqual(self.http.count('POST'), 1)

    def test_parsed_post_refusal_releases_intent_and_allows_explicit_retry(self):
        for status in (400, 403, 408, 429):
            with self.subTest(status=status):
                self.root = self.work / ('refused-' + str(status))
                self.http.bots.clear()
                self.http.failure = ('POST', status)
                with self.assertRaises(SpawnError) as caught:
                    self.create()
                self.assertNotIn('private response', str(caught.exception))
                self.assertIn('reservation released', str(caught.exception))
                self.assertIn('retryable' if status in (408, 429) else 'terminal', str(caught.exception))
                state = read(self.root / 'zulip.json')
                self.assertEqual(state['operations'], {})
                self.assertEqual(state['identities'], {})
                self.assertEqual(self.http.bots, {})
                self.http.failure = None
                op = 'create' if status in (200, 400, 429) else 'different'
                handle = self.create(op=op)
                self.assertEqual(self.create(op=op), handle)

    def test_ambiguous_post_errors_keep_pending(self):
        for status, body in ((200, None), (500, None), (503, None), (503, 'truncated')):
            with self.subTest(status=status, body=body):
                self.root = self.work / ('ambiguous-' + str(status) + str(body))
                self.http.failure = ('POST', status) if body is None else ('POST', status, body)
                with self.assertRaises(SpawnError) as caught:
                    self.create()
                self.assertNotIn('private response', str(caught.exception))
                state = read(self.root / 'zulip.json')
                self.assertEqual(len(state['identities']), 1)
                self.assertEqual(next(iter(state['operations'].values()))['state'], 'pending')
                count = self.http.count('POST')
                self.http.failure = None
                with self.assertRaises(SpawnError):
                    self.create()
                self.assertEqual(self.http.count('POST'), count)

    def test_error_body_read_failure_keeps_pending(self):
        import http.client
        from connectome_spawn import zulip
        response = zulip._response
        def interrupted(body, status):
            if status == 503:
                raise http.client.IncompleteRead(b'private response')
            return response(body, status)
        self.http.failure = ('POST', 503)
        with patch.object(zulip, '_response', side_effect=interrupted):
            with self.assertRaises(SpawnError) as caught:
                self.create()
        self.assertNotIn('private response', str(caught.exception))
        state = read(self.root / 'zulip.json')
        self.assertEqual(len(state['identities']), 1)
        self.assertEqual(next(iter(state['operations'].values()))['state'], 'pending')
        self.http.failure = None
        with self.assertRaises(SpawnError):
            self.create()
        self.assertEqual(self.http.count('POST'), 1)

    def test_commit_then_500_discovers_exact_reserved_bot(self):
        self.http.add('foreign', 'UnrelatedBot', owner=2)
        self.http.committed_error = 'POST'
        provider = self.provider()
        with patch.object(provider, '_discover', return_value=None):
            with self.assertRaises(SpawnError):
                self.create(provider)
        state = read(self.root / 'zulip.json')
        self.assertEqual(len(state['operations']), 1)
        self.assertEqual(next(iter(state['operations'].values()))['state'], 'pending')
        reserved = next(iter(state['identities']))
        handle = self.create()
        self.assertEqual(handle['id'], reserved)
        self.assertEqual(len(self.http.bots), 2)
        self.assertEqual(self.http.count('POST'), 1)
        self.assertEqual(self.provider().list()[0]['email'],
                         self.http.bots[11]['username'])
        self.assertEqual(self.create(), handle)

    def test_refused_mutation_releases_intent_after_observation(self):
        for method in ('PATCH', 'DELETE'):
            with self.subTest(method=method):
                self.root = self.work / method
                self.http.bots.clear()
                provider = self.provider()
                handle = self.create(provider)
                if method == 'PATCH':
                    self.http.add('foreign', RECORD['name'], owner=2)
                else:
                    self.http.failure = (method, 400)
                with self.assertRaisesRegex(SpawnError, 'refused.*terminal.*intent released'):
                    if method == 'PATCH':
                        provider.rename(handle, RECORD, 'rename')
                    else:
                        provider.retire(handle, 'retire-refused')
                self.assertEqual(self.http.calls[-1][:2], ('GET', '/users/' + str(self.http.bots[10]['user_id'])))
                state = read(self.root / 'zulip.json')
                self.assertFalse(any(o['state'] == 'pending' for o in state['operations'].values()))
                self.assertEqual(state['identities'][handle['id']]['name'], 'NewbornAgent')
                self.assertFalse(state['identities'][handle['id']]['retired'])
                self.http.failure = None
                provider.retire(handle, 'retire')
                self.assertFalse(next(iter(self.http.bots.values()))['is_active'])

    def test_ambiguous_rename_then_refusal_keeps_intent(self):
        provider = self.provider()
        handle = self.create(provider)
        self.http.failure = ('PATCH', 503)
        with self.assertRaises(SpawnError):
            provider.rename(handle, RECORD, 'rename')
        self.http.failure = ('PATCH', 429)
        with self.assertRaises(SpawnError) as caught:
            provider.rename(handle, RECORD, 'rename')
        self.assertNotIn('intent released', str(caught.exception))
        state = read(self.root / 'zulip.json')
        self.assertEqual(state['operations']['example-agent:rename']['state'], 'pending')
        # The original request lands late; reconciliation must still find it.
        self.http.failure = None
        next(iter(self.http.bots.values()))['full_name'] = RECORD['name']
        self.assertEqual(provider.rename(handle, RECORD, 'rename'), handle)
        self.assertEqual(self.http.count('PATCH'), 2)

    def test_retryable_mutation_refusal_is_labelled(self):
        for method in ('PATCH', 'DELETE'):
            with self.subTest(method=method):
                self.root = self.work / method
                self.http.bots.clear()
                provider = self.provider()
                handle = self.create(provider)
                self.http.failure = (method, 429)
                with self.assertRaisesRegex(SpawnError, 'refused.*retryable.*intent released'):
                    if method == 'PATCH':
                        provider.rename(handle, RECORD, 'rename')
                    else:
                        provider.retire(handle, 'retire')
                self.http.failure = None

    def test_refused_mutation_observed_effect_completes(self):
        for method in ('PATCH', 'DELETE'):
            with self.subTest(method=method):
                self.root = self.work / method
                self.http.bots.clear()
                provider = self.provider()
                handle = self.create(provider)
                self.http.committed_error = (method, 400)
                if method == 'PATCH':
                    provider.rename(handle, RECORD, 'mutation')
                else:
                    provider.retire(handle, 'mutation')
                state = read(self.root / 'zulip.json')
                self.assertEqual(state['operations']['example-agent:mutation']['state'], 'complete')
                self.http.committed_error = None

    def test_refused_mutation_failed_observation_keeps_intent(self):
        for method in ('PATCH', 'DELETE'):
            with self.subTest(method=method):
                self.root = self.work / method
                self.http.bots.clear()
                provider = self.provider()
                handle = self.create(provider)
                self.http.failure = (method, 403)
                observe = provider._observe
                calls = []
                def fail_second(*args, **kwargs):
                    calls.append(True)
                    if len(calls) == 2:
                        raise SpawnError('observation unavailable')
                    return observe(*args, **kwargs)
                with patch.object(provider, '_observe', side_effect=fail_second):
                    with self.assertRaises(SpawnError):
                        if method == 'PATCH':
                            provider.rename(handle, RECORD, 'mutation')
                        else:
                            provider.retire(handle, 'mutation')
                state = read(self.root / 'zulip.json')
                self.assertEqual(state['operations']['example-agent:mutation']['state'], 'pending')
                self.http.failure = None

    def test_ambiguous_mutation_errors_keep_pending(self):
        for method in ('PATCH', 'DELETE'):
            for status in (200, 500, 503):
                with self.subTest(method=method, status=status):
                    self.root = self.work / (method + str(status))
                    self.http.bots.clear()
                    provider = self.provider()
                    handle = self.create(provider)
                    self.http.failure = (method, status)
                    with self.assertRaises(SpawnError):
                        if method == 'PATCH':
                            provider.rename(handle, RECORD, 'rename')
                        else:
                            provider.retire(handle, 'retire')
                    state = read(self.root / 'zulip.json')
                    self.assertEqual(sum(o['state'] == 'pending' for o in state['operations'].values()), 1)
                    self.http.failure = None

    def test_real_owned_bot_shape_delivers_key_and_lists(self):
        handle = self.create()
        bot = next(iter(self.http.bots.values()))
        self.assertEqual(set(self.http.listed(bot)), {'username', 'full_name', 'api_key', 'avatar_url',
                         'default_sending_stream', 'default_events_register_stream', 'default_all_public_streams'})
        self.assertEqual((self.secrets / handle['agent'] / (handle['id'] + '.key')).read_text(), bot['api_key'])
        self.assertTrue(self.provider().list()[0]['active'])

    def test_post_errors_leave_uncertain_intent_and_changed_id_is_refused(self):
        for status in (400, 403, 429, 500, 503):
            with self.subTest(status=status):
                self.root = self.work / ('provider-' + str(status))
                self.http.failure = ('POST', status, b'{invalid private response')
                with self.assertRaises(SpawnError) as caught:
                    self.create()
                self.assertNotIn('private response', str(caught.exception))
                count = self.http.count('POST')
                self.http.failure = None
                with self.assertRaises(SpawnError):
                    self.create()
                with self.assertRaises(SpawnError):
                    self.create(op='different')
                self.assertEqual(self.http.count('POST'), count)

    def test_name_collisions_never_adopt_existing_owned_or_foreign_bots(self):
        for owner, full, active in ((1, 'NewbornAgent', True), (1, 'DifferentAgent', True),
                                    (2, 'NewbornAgent', True), (1, 'NewbornAgent', False)):
            self.http.bots.clear()
            self.root = self.work / ('state-' + str(owner) + full + str(active))
            # The native short name is derived from the persisted random handle,
            # not an alternative display persona. Force a collision offline.
            self.http.add('spawn' + 'a' * 32, full, owner, active)
            with patch('connectome_spawn.zulip.uuid.uuid4') as uid:
                uid.return_value.hex = 'a' * 32
                with self.assertRaises(SpawnError):
                    self.create()
            self.assertEqual(len(self.http.bots), 1)
        # Owned/active collisions are caught before submission. The foreign and
        # inactive native collisions are definitive POST refusals, not adoption.
        self.assertEqual(self.http.count('POST'), 2)
        state = read(self.root / 'zulip.json')
        self.assertEqual(state['operations'], {})
        self.assertEqual(state['identities'], {})

    def test_foreign_or_changed_bot_during_reconciliation_is_refused(self):
        provider = self.provider()
        handle = self.create(provider)
        bot = next(iter(self.http.bots.values()))
        bot['full_name'] = 'ForeignPersona'
        with self.assertRaises(SpawnError):
            provider.rename(handle, RECORD, 'rename')
        with self.assertRaises(SpawnError):
            provider.retire(handle, 'retire')
        self.assertEqual(self.http.count('PATCH'), 0)
        self.assertEqual(self.http.count('DELETE'), 0)
        bot['full_name'] = 'NewbornAgent'
        bot['owner_id'] = 2
        with self.assertRaises(SpawnError):
            provider.retire(handle, 'retire')
        self.assertEqual(self.http.count('DELETE'), 0)

    def test_deactivated_bot_is_reported_and_never_resurrected(self):
        provider = self.provider()
        handle = self.create(provider)
        next(iter(self.http.bots.values()))['is_active'] = False
        self.assertFalse(provider.list()[0]['active'])
        with self.assertRaises(SpawnError):
            provider.rename(handle, RECORD, 'rename')
        provider.retire(handle, 'retire')
        self.assertEqual(self.http.count('DELETE'), 0)

    def test_lost_patch_and_delete_responses_reconcile(self):
        provider = self.provider()
        handle = self.create(provider)
        self.http.lost = 'PATCH'
        self.assertEqual(provider.rename(handle, RECORD, 'rename'), handle)
        self.http.lost = 'DELETE'
        provider.retire(handle, 'retire')
        self.assertTrue(provider.list()[0]['retired'])
        self.assertEqual(self.http.count('PATCH'), 1)
        self.assertEqual(self.http.count('DELETE'), 1)

    def test_operation_conflicts_handles_and_name_limits(self):
        provider = self.provider()
        handle = self.create(provider)
        for request in (lambda: self.create(provider, name='OtherAgent'),
                        lambda: self.create(provider, agent='other-agent', op='other'),
                        lambda: provider.rename(dict(handle, agent='other-agent'), RECORD, 'rename'),
                        lambda: provider.rename(handle, dict(RECORD, name='a' * 64), 'rename'),
                        lambda: self.create(provider, agent='../escape'),
                        lambda: self.create(provider, name='a' * 64),
                        lambda: self.create(provider, name='Bad Name'),
                        lambda: self.create(provider, name='A')):
            with self.assertRaises(SpawnError):
                request()
        provider.rename(handle, RECORD, 'rename')
        with self.assertRaises(SpawnError):
            provider.rename(handle, dict(RECORD, name='OtherAgent'), 'rename')

    def test_key_delivery_modes_are_exact_even_with_restrictive_umask(self):
        previous = os.umask(0o777)
        try:
            self.create()
        finally:
            os.umask(previous)
        directory = self.secrets / 'example-agent'
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(next(directory.iterdir()).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'zulip.json').stat().st_mode & 0o777, 0o600)

    def test_concurrent_agents_get_exclusive_bots_and_credentials(self):
        provider = self.provider()
        with ThreadPoolExecutor(max_workers=2) as executor:
            handles = list(executor.map(lambda i: self.create(provider, agent='agent-' + i,
                                name='Newborn' + i), ('one', 'two')))
        self.assertNotEqual(handles[0], handles[1])
        self.assertEqual(len(self.http.bots), 2)
        self.assertEqual(len(list(self.secrets.rglob('*.key'))), 2)

    def test_private_reader_refuses_modes_links_and_owner_before_network(self):
        for mode in (0o644, 0o640, 0o660, 0o400, 0o700):
            self.credential.chmod(mode)
            with self.subTest(mode=mode), self.assertRaises(SpawnError):
                self.create()
        self.credential.chmod(0o600)
        link = self.work / 'operator-link'
        os.link(self.credential, link)
        with self.assertRaises(SpawnError):
            self.create()
        link.unlink()
        saved = self.work / 'saved'
        self.credential.rename(saved)
        self.credential.symlink_to(saved)
        with self.assertRaises(SpawnError):
            self.create()
        self.credential.unlink()
        saved.rename(self.credential)
        with patch('connectome_spawn.sources.os.geteuid', return_value=os.geteuid() + 1), self.assertRaises(SpawnError):
            self.create()
        self.assertEqual(self.http.calls, [])

    def test_transport_and_config_errors_are_value_free(self):
        with self.assertRaises(SpawnError):
            ZulipIdentityProvider(self.root, zuliprc=self.credential, secrets_root=self.secrets)
        for data in ('not ini operator-fixture', '[api]\nkey=operator-fixture\n',
                     '[api]\nemail=noreply@users.noreply.github.com\nkey=operator-fixture\nsite=https://noreply:operator-fixture@users.noreply.github.com\n'):
            self.private(self.credential, data)
            with self.assertRaises(SpawnError) as caught:
                self.provider()
            self.assertNotIn('operator-fixture', str(caught.exception))

    def test_one_character_name_is_rejected_before_network(self):
        with self.assertRaises(SpawnError):
            self.create(name='A')
        self.assertEqual(self.http.calls, [])
        self.create(name='Ab')
        self.assertEqual(self.http.count('POST'), 1)

    def test_retire_retry_fsyncs_directory_after_interrupted_unlink(self):
        from connectome_spawn import zulip
        provider = self.provider()
        handle = self.create(provider)
        delivered = self.secrets / handle['agent'] / (handle['id'] + '.key')
        with patch.object(zulip, 'sync_dir', side_effect=OSError('interrupted fsync')):
            with self.assertRaises(OSError):
                provider.retire(handle, 'retire')
        self.assertFalse(delivered.exists())
        self.assertEqual(self.http.count('DELETE'), 1)
        self.assertEqual(read(self.root / 'zulip.json')['operations']['example-agent:retire']['state'], 'pending')
        with patch.object(zulip, 'sync_dir', wraps=zulip.sync_dir) as sync:
            provider.retire(handle, 'retire')
        sync.assert_called_once_with(delivered.parent)
        self.assertEqual(self.http.count('DELETE'), 1)
        self.assertTrue(provider.list()[0]['retired'])

    def test_retire_refuses_unsafe_key_replacement(self):
        provider = self.provider()
        handle = self.create(provider)
        delivered = self.secrets / handle['agent'] / (handle['id'] + '.key')
        delivered.chmod(0o644)
        with self.assertRaises(SpawnError):
            provider.retire(handle, 'retire')
        self.assertTrue(delivered.exists())
        self.assertFalse(next(iter(self.http.bots.values()))['is_active'])
        delivered.chmod(0o600)
        provider.retire(handle, 'retire')
        self.assertFalse(delivered.exists())
        self.assertEqual(self.http.count('DELETE'), 1)

    def test_staged_key_conflict_never_promotes_or_overwrites(self):
        path = self.work / 'key'
        staging = self.private(self.work / '.key.pending', 'different-fixture')
        with self.assertRaisesRegex(SpawnError, 'staging conflict'):
            write_secret_bytes(path, b'expected-fixture')
        self.assertEqual(staging.read_bytes(), b'different-fixture')
        self.assertFalse(path.exists())

    def test_staging_open_requires_exclusive_nofollow_flags(self):
        path = self.work / 'key'
        staging = self.work / '.key.pending'
        original = os.open
        seen = []
        def raced_open(target, flags, *args, **kwargs):
            if Path(target) == staging and flags & os.O_WRONLY:
                seen.append(flags)
                self.private(staging, 'raced-fixture')
            return original(target, flags, *args, **kwargs)
        with patch('connectome_spawn.persistence.os.open', side_effect=raced_open):
            with self.assertRaises(FileExistsError):
                write_secret_bytes(path, b'expected-fixture')
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0] & (os.O_EXCL | os.O_NOFOLLOW), os.O_EXCL | os.O_NOFOLLOW)
        self.assertEqual(staging.read_bytes(), b'raced-fixture')
        self.assertFalse(path.exists())

    def test_staging_write_error_unlinks_partial_secret(self):
        path = self.work / 'key'
        original = os.fsync
        def fail_file(fd):
            if not os.path.isdir('/proc/self/fd/' + str(fd)):
                raise OSError('simulated write error')
            return original(fd)
        with patch('connectome_spawn.persistence.os.fsync', side_effect=fail_file):
            with self.assertRaises(OSError):
                write_secret_bytes(path, b'expected-fixture')
        self.assertFalse((self.work / '.key.pending').exists())
        self.assertFalse(path.exists())

    def test_delivery_conflict_never_accepts_different_key(self):
        provider = self.provider()
        handle = self.create(provider)
        path = self.secrets / handle['agent'] / (handle['id'] + '.key')
        before = path.read_bytes()
        with self.assertRaisesRegex(SpawnError, 'delivery conflict'):
            provider._deliver(handle, 'different-fixture')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.http.count('POST'), 1)

    def test_full_name_preflight_is_case_insensitive(self):
        self.http.add('unrelated', 'newbornagent')
        with self.assertRaisesRegex(SpawnError, 'name collision'):
            self.create()
        self.assertEqual(self.http.count('POST'), 0)

    def test_retired_ledger_names_stay_reserved_without_active_bot(self):
        provider = self.provider()
        handle = self.create(provider)
        provider.retire(handle, 'retire')
        with self.assertRaisesRegex(SpawnError, 'name collision'):
            self.create(provider, agent='another-agent', name='newbornagent', op='new')
        self.assertEqual(self.http.count('POST'), 1)

    def test_observe_checks_native_email_before_mutating(self):
        provider = self.provider()
        handle = self.create(provider)
        next(iter(self.http.bots.values()))['username'] = 'changed-bot@users.noreply.github.com'
        with self.assertRaises(SpawnError):
            provider.retire(handle, 'retire')
        with self.assertRaises(SpawnError):
            provider.rename(handle, RECORD, 'rename')
        self.assertEqual(self.http.count('DELETE'), 0)
        self.assertEqual(self.http.count('PATCH'), 0)

    def test_discover_explicit_owner_check_uses_individual_user(self):
        # Even when /bots lists it as caller-owned, reject a changed owner in
        # the per-user response (e.g. a transfer racing discovery).
        self.http.user_overrides = {'bot_owner_id': 2}
        with self.assertRaises(SpawnError):
            self.create()
        self.assertEqual(self.http.count('POST'), 1)
        self.assertEqual(list(self.secrets.rglob('*.key')), [])
        self.http.user_overrides = {}
        self.create()
        self.assertEqual(self.http.count('POST'), 1)

    def test_large_realm_never_requires_whole_member_listing(self):
        for i in range(4000):
            self.http.add('foreign' + str(i), 'Foreign' + str(i), owner=2)
        self.create()
        self.assertFalse(any(path.split('?')[0] == '/users' for _, path, _ in self.http.calls))
        self.assertTrue(any(path.startswith('/users/spawn') for _, path, _ in self.http.calls))

    def test_secrets_are_name_to_private_file_metadata_only(self):
        provider = FileSecretProvider(self.secrets)
        key = self.private(self.secrets / 'example-agent' / 'EXAMPLE_KEY', 'secret-fixture')
        result = provider.resolve('example-agent', ['EXAMPLE_KEY'])
        self.assertEqual(result['EXAMPLE_KEY'], {'path': str(key), 'digest': hashlib.sha256(b'secret-fixture').hexdigest()})
        self.assertNotIn('secret-fixture', json.dumps(result))
        for name in ('../EXAMPLE_KEY', 'Bad Key', 'a/b'):
            with self.assertRaises(SpawnError):
                provider.resolve('example-agent', [name])
        for mode in (0o644, 0o640):
            key.chmod(mode)
            with self.assertRaises(SpawnError):
                provider.resolve('example-agent', ['EXAMPLE_KEY'])
        key.chmod(0o600)
        link = self.work / 'extra'
        os.link(key, link)
        with self.assertRaises(SpawnError):
            provider.resolve('example-agent', ['EXAMPLE_KEY'])
        link.unlink()
        key.unlink()
        key.symlink_to(self.credential)
        with self.assertRaises(SpawnError):
            provider.resolve('example-agent', ['EXAMPLE_KEY'])

    def test_direct_access_only_validates_key_and_receipts(self):
        key = self.private(self.work / 'direct.key', 'direct-fixture')
        provider = DirectKeyProvider(self.work / 'access', key_file=key)
        self.assertEqual(provider.plan('example-agent')['execution'], 'operator-required')
        result = provider.provision('example-agent', 'access')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['file'], {'path': str(key), 'digest': hashlib.sha256(b'direct-fixture').hexdigest()})
        self.assertEqual(DirectKeyProvider(self.work / 'access', key_file=key).provision('example-agent', 'access'), result)
        self.private(key, 'changed-fixture')
        with self.assertRaises(SpawnError):
            provider.provision('example-agent', 'access')
        self.assertEqual(self.http.calls, [])

    def test_gate_waits_for_operator_token_never_executes_or_contacts(self):
        key = self.work / 'gate.key'
        provider = InferenceGateProvider(self.work / 'access', token_file=key, pool='example-pool', limit=100)
        plan = provider.plan('example-agent')
        self.assertEqual(plan['inputs'], {'agent': 'example-agent', 'pool': 'example-pool', 'limit': 100})
        self.assertEqual(plan['execution'], 'operator-required')
        with patch('subprocess.Popen', side_effect=AssertionError('must not execute')):
            self.assertEqual(provider.provision('example-agent', 'access')['state'], 'awaiting-provisioning')
            self.assertEqual(provider.provision('example-agent', 'access')['state'], 'awaiting-provisioning')
            self.private(key, 'gate-fixture')
            self.assertEqual(provider.provision('example-agent', 'access')['state'], 'ready')
        changed = InferenceGateProvider(self.work / 'access', token_file=key, pool='other-pool', limit=100)
        with self.assertRaises(SpawnError):
            changed.provision('example-agent', 'access')
        self.assertNotIn('gate-fixture', (self.work / 'access' / 'access.json').read_text())
        self.assertEqual(self.http.calls, [])

    def test_ai_access_unsafe_file_never_counts_as_ready(self):
        for kind in (DirectKeyProvider, InferenceGateProvider):
            key = self.private(self.work / (kind.__name__ + '.key'))
            kwargs = {'key_file': key} if kind is DirectKeyProvider else {'token_file': key, 'pool': 'example-pool', 'limit': 10}
            provider = kind(self.work / kind.__name__, **kwargs)
            key.chmod(0o644)
            with self.assertRaises(SpawnError):
                provider.provision('example-agent', 'access')
            key.chmod(0o600)
            linked = self.work / 'extra-link'
            os.link(key, linked)
            with self.assertRaises(SpawnError):
                provider.provision('example-agent', 'access')
            linked.unlink()
            key.unlink()
            key.symlink_to(self.credential)
            with self.assertRaises(SpawnError):
                provider.provision('example-agent', 'access')


if __name__ == '__main__':
    unittest.main()
