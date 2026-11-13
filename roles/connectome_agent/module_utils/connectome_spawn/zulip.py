"""Zulip generic-bot identity provider, deliberately separate from deployment.

Only explicit calls perform HTTP. A durable reservation precedes POST; an
unanswered POST is reconciled by listing, never blindly repeated. Reservations
use unpredictable native short names to distinguish our uncertain create from
pre-existing operator-owned personas. Display full_name is always exact.
"""
import base64
import configparser
from copy import deepcopy
from datetime import datetime, timezone
import http.client
import os
import socket
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .data import SpawnError, decode, digest, fields, match, need
from .identity import validate_provider_name
from .persistence import locked, read, safe_path, sync_dir, write, write_secret_bytes
from .schema import NAME, name_record as validate_name_record
from .sources import read_file


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _credentials(path, allow_loopback_http):
    raw = read_file(path, private=True)
    try:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(raw.decode('utf-8'))
        need(parser.sections() == ['api'] and not parser.defaults() and
             set(parser['api']) == {'email', 'key', 'site'}, 'invalid operator credential file')
        result = {k: parser['api'][k].strip() for k in ('email', 'key', 'site')}
        need(all(result.values()) and all(not any(ord(c) < 32 or ord(c) == 127 for c in v)
                                        for v in result.values()), 'invalid operator credential file')
        need(':' not in result['email'], 'invalid operator credential file')
        site = urlsplit(result['site'])
        local = socket.inet_ntoa(bytes([127, 0, 0, 1]))
        need(site.hostname is not None and site.username is None and site.password is None and
             not site.query and not site.fragment and site.path in ('', '/') and
             (site.scheme == 'https' or allow_loopback_http and site.scheme == 'http' and site.hostname == local),
             'HTTPS operator site required (explicit loopback HTTP is test-only)')
        site.port  # Refuse malformed ports before constructing any request.
        result['site'] = result['site'].rstrip('/')
        return result
    except (configparser.Error, UnicodeError, ValueError):
        raise SpawnError('invalid operator credential file') from None


class ZulipRefusal(SpawnError):
    """Parsed 4xx refusal; never retain its private body."""
    def __init__(self, status):
        self.status = status
        self.retryable = status in (408, 429)
        super().__init__('Zulip request refused (HTTP ' + str(status) + ')')


def _response(response, status):
    raw = response.read(1024 * 1024 + 1)
    need(raw.lstrip().startswith(b'{'), 'invalid Zulip JSON response')
    value = decode(raw)
    need(type(value) is dict, 'invalid Zulip JSON response')
    if value.get('result') == 'error' and 400 <= status < 500:
        raise ZulipRefusal(status) from None
    need(200 <= status < 300 and value.get('result') == 'success',
         'Zulip request failed; reconcile provider state')
    return value


def api_call(credentials, method, path, params=None):
    """Bounded form-encoded REST call. No proxies, redirects, logs or raw errors."""
    params = {} if params is None else params
    body = urlencode(params).encode('utf-8')
    url = credentials['site'] + '/api/v1' + path
    if method == 'GET' and body:
        url += '?' + body.decode('ascii')
    auth = base64.b64encode((credentials['email'] + ':' + credentials['key']).encode()).decode('ascii')
    request = Request(url, data=None if method == 'GET' else body, method=method,
                      headers={'Authorization': 'Basic ' + auth,
                               'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=10) as response:
            return _response(response, response.status)
    except HTTPError as error:
        try:
            with error:
                return _response(error, error.code)
        except SpawnError as failure:
            raise failure from None
        except (OSError, http.client.HTTPException, ValueError):
            raise SpawnError('Zulip request failed; reconcile provider state') from None
    except SpawnError as failure:
        raise failure from None
    except (URLError, OSError, http.client.HTTPException, ValueError):
        raise SpawnError('Zulip request failed; reconcile provider state') from None


def _user_id(value):
    return type(value) is int and value > 0


def _email(value):
    return type(value) is str and bool(value) and not any(ord(c) < 33 or ord(c) == 127 for c in value)


class ZulipIdentityProvider:
    """Owned generic bots; handles and durable receipts never contain credentials.

    PATCH /bots/{id} supports full_name rename, NOT native email rename. The
    declared 63-character ASCII identifier grammar is a conservative supported
    subset, not a claim about all server/version/realm name limits. Servers may
    impose stricter policy; definitive refusals release create reservations.
    """
    supports_rename = True
    max_name_length = 63
    name_pattern = r'[A-Za-z][A-Za-z0-9_-]+'

    def __init__(self, root, *, zuliprc, secrets_root, allow_loopback_http=False):
        need(type(allow_loopback_http) is bool, 'invalid transport configuration')
        self.root = safe_path(root)
        self.path = self.root / 'zulip.json'
        self.zuliprc = safe_path(zuliprc)
        self.secrets_root = safe_path(secrets_root)
        self.allow_loopback_http = allow_loopback_http
        credentials = self._credentials()
        # Key rotation is allowed; changing realm/operator or delivery location
        # cannot silently adopt the old ownership ledger. Store no raw site/email.
        self.binding = digest({'site': credentials['site'], 'operator': credentials['email'],
                               'secretsRoot': str(self.secrets_root)})

    def _credentials(self):
        return _credentials(self.zuliprc, self.allow_loopback_http)

    def _session(self):
        credentials = self._credentials()
        need(self.binding == digest({'site': credentials['site'], 'operator': credentials['email'],
                                     'secretsRoot': str(self.secrets_root)}), 'operator binding changed')
        me = api_call(credentials, 'GET', '/users/me')
        need(_user_id(me.get('user_id')) and me.get('is_bot') is False and
             me.get('role') in (100, 200), 'human operator administrator required')
        return credentials, me['user_id']

    def _read(self):
        if not self.path.exists():
            return {'schema': 'connectome.zulip-identities/v1', 'binding': self.binding,
                    'identities': {}, 'operations': {}}
        value = read(self.path)
        fields(value, ('schema', 'binding', 'identities', 'operations'), ('audit',))
        need(type(value.get('audit', [])) is list, 'invalid provider audit')
        need(value['schema'] == 'connectome.zulip-identities/v1' and value['binding'] == self.binding and
             type(value['identities']) is dict and type(value['operations']) is dict, 'invalid Zulip provider state')
        for identity_id, row in value['identities'].items():
            fields(row, ('agent', 'name', 'kind', 'retired', 'short_name', 'native_id', 'email'))
            need(match(identity_id, r'[0-9a-f]{32}') and match(row['agent'], NAME) and
                 row['kind'] in ('temporary', 'permanent') and type(row['retired']) is bool and
                 row['short_name'] == 'spawn' + identity_id and
                 (row['native_id'] is None or _user_id(row['native_id'])) and
                 (row['email'] is None or _email(row['email'])), 'invalid owned Zulip identity')
            validate_provider_name(self, row['name'])
        for key, operation in value['operations'].items():
            fields(operation, ('digest', 'state', 'handle', 'action', 'target'))
            need(match(key, r'[a-z][a-z0-9-]{0,62}:[A-Za-z0-9_:-]{1,256}') and
                 match(operation['digest'], r'[0-9a-f]{64}') and operation['state'] in ('pending', 'complete') and
                 operation['action'] in ('provision', 'rename', 'retire'), 'invalid Zulip operation receipt')
            self._row(value, operation['handle'])
            need(key.startswith(operation['handle']['agent'] + ':'), 'invalid Zulip receipt owner')
            if operation['target'] is not None:
                validate_provider_name(self, operation['target'])
        return value

    @staticmethod
    def _row(value, handle):
        fields(handle, ('id', 'agent'))
        need(match(handle['id'], r'[0-9a-f]{32}') and match(handle['agent'], NAME), 'invalid Zulip handle')
        row = value['identities'].get(handle['id'])
        need(row is not None and row['agent'] == handle['agent'], 'unknown or foreign Zulip handle')
        return row

    def _prior(self, value, agent, operation_id, request):
        need(match(agent, NAME) and match(operation_id, r'[A-Za-z0-9_:-]{1,256}'), 'invalid identity operation')
        key = agent + ':' + operation_id
        prior = value['operations'].get(key)
        if prior is not None:
            need(prior['digest'] == digest(request), 'identity operation id conflict')
        need(prior is not None and prior['state'] == 'complete' or not any(
            k != key and op['state'] == 'pending' and op['handle']['agent'] == agent
            for k, op in value['operations'].items()), 'pending identity operation requires reconciliation')
        return key, prior

    def _intent(self, value, key, request, handle, action, target=None):
        value['operations'][key] = {'digest': digest(request), 'state': 'pending',
                                    'handle': deepcopy(handle), 'action': action, 'target': target}
        write(self.path, value)

    def _complete(self, value, key):
        value['operations'][key]['state'] = 'complete'
        write(self.path, value)

    @staticmethod
    def _bots(credentials):
        # get_bots_backend filters bot_owner=caller, is_active=True. It does
        # NOT return owner_id, user_id, is_active or /register realm_bots data.
        bots = api_call(credentials, 'GET', '/bots').get('bots')
        need(type(bots) is list and all(type(b) is dict and _email(b.get('username')) and
             type(b.get('full_name')) is str for b in bots), 'invalid owned-bot listing')
        return bots

    def _preflight(self, credentials, value, name, short_name=None, exclude=None):
        # Never adopt an unjournaled account. Retired ledger names stay reserved.
        need(not any(i != exclude and row['name'].casefold() == name.casefold()
                     for i, row in value['identities'].items()), 'identity name collision; never reuse')
        email = value['identities'][exclude]['email'] if exclude is not None else None
        for bot in self._bots(credentials):
            if email is not None and bot['username'] == email:
                continue
            need(bot['full_name'].casefold() != name.casefold() and
                 (short_name is None or not bot['username'].casefold().startswith(short_name.casefold() + '-bot@')),
                 'Zulip name collision; never adopt an existing account')
        # No realm-wide /users scan: full_name is not globally unique in Zulip.
        # Native-address collisions with foreign/inactive users are refused by
        # POST itself. Only a fresh successful create can be reconciled below.

    @staticmethod
    def _observe(credentials, operator, row, *, target=None):
        user = api_call(credentials, 'GET', '/users/' + str(row['native_id'])).get('user')
        need(type(user) is dict and user.get('user_id') == row['native_id'] and
             user.get('is_bot') is True and _user_id(user.get('bot_owner_id')) and user.get('bot_owner_id') == operator and
             user.get('email') == row['email'] and
             user.get('full_name') in (row['name'], target) and type(user.get('is_active')) is bool,
             'owned Zulip bot changed; reconciliation required')
        return user

    def _discover(self, credentials, operator, row):
        bots = self._bots(credentials)
        prefix = row['short_name'].casefold() + '-bot@'
        found = [b for b in bots if type(b.get('username')) is str and b['username'].casefold().startswith(prefix)]
        need(len(found) <= 1, 'ambiguous owned-bot listing')
        if not found:
            # An absent active owned bot is NOT permission to repeat POST.
            # Check owned-name collisions without receiving unrelated user data.
            self._preflight(credentials, {'identities': {}}, row['name'], row['short_name'])
            return None
        bot = found[0]
        need(_email(bot.get('username')) and bot.get('full_name') == row['name'],
             'Zulip bot collision; never adopt a different persona or owner')
        user = api_call(credentials, 'GET', '/users/' + quote(bot['username'], safe='')).get('user')
        need(type(user) is dict and _user_id(user.get('user_id')) and user.get('is_bot') is True and
             _user_id(user.get('bot_owner_id')) and user.get('bot_owner_id') == operator and user.get('full_name') == row['name'] and
             user.get('is_active') is True and user.get('email') == bot['username'],
             'Zulip bot collision or inactive bot; reconciliation required')
        need(type(bot.get('api_key')) is str and bool(bot['api_key']) and
             not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in bot['api_key']), 'invalid Zulip bot key')
        return bot, user['user_id']

    def _deliver(self, handle, key):
        path = safe_path(self.secrets_root / handle['agent'] / (handle['id'] + '.key'))
        raw = key.encode('utf-8')
        if os.path.lexists(path):
            need(read_file(path, root=self.secrets_root / handle['agent'], private=True) == raw,
                 'bot key delivery conflict; reconciliation required')
        else:
            write_secret_bytes(path, raw)

    def _provision(self, agent, name, kind, operation_id, record=None):
        validate_provider_name(self, name)
        request = {'action': 'provision', 'kind': kind, 'name': name, 'record': record}
        with locked(self.root):
            value = self._read()
            key, prior = self._prior(value, agent, operation_id, request)
            if prior and prior['state'] == 'complete':
                need(not self._row(value, prior['handle'])['retired'], 'bot identity retired; key invalidated')
                delivered = self.secrets_root / agent / (prior['handle']['id'] + '.key')
                need(bool(read_file(delivered, root=self.secrets_root / agent, private=True)),
                     'delivered bot key missing; reconciliation required')
                return deepcopy(prior['handle'])
            credentials, operator = self._session()
            if prior:
                handle = prior['handle']
                row = self._row(value, handle)
            else:
                need(not any(r['agent'] == agent and r['kind'] == kind and not r['retired']
                             for r in value['identities'].values()), 'agent already owns this identity kind')
                handle = {'id': uuid.uuid4().hex, 'agent': agent}
                need(handle['id'] not in value['identities'], 'identity reservation collision')
                row = {'agent': agent, 'name': name, 'kind': kind, 'retired': False,
                       'short_name': 'spawn' + handle['id'], 'native_id': None, 'email': None}
                self._preflight(credentials, value, name, row['short_name'])
                value['identities'][handle['id']] = row
                self._intent(value, key, request, handle, 'provision', name)
                try:
                    # Ignore returned key: one credential delivery path, by listing.
                    api_call(credentials, 'POST', '/bots', {'full_name': name,
                             'short_name': row['short_name'], 'bot_type': 1})
                except ZulipRefusal as refusal:
                    # Only a parsed 4xx is a definitive create refusal.
                    del value['operations'][key]
                    del value['identities'][handle['id']]
                    write(self.path, value)
                    category = 'retryable' if refusal.retryable else 'terminal (correct request/policy)'
                    raise SpawnError('Zulip create refused (HTTP ' + str(refusal.status) + '); ' +
                                     category + '; reservation released') from None
                except SpawnError:
                    pass  # POST may have committed. Listing is the authority.
            found = self._discover(credentials, operator, row)
            need(found is not None, 'uncertain Zulip create; awaiting reconciliation; POST not repeated')
            bot, native_id = found
            self._deliver(handle, bot['api_key'])
            row.update(native_id=native_id, email=bot['username'])
            self._complete(value, key)
            return deepcopy(handle)

    def provision_temporary(self, agent, name, operation_id):
        return self._provision(agent, name, 'temporary', operation_id)

    def provision_permanent(self, agent, name_record, operation_id):
        record = validate_name_record(name_record)
        return self._provision(agent, record['name'], 'permanent', operation_id, record)

    def rename(self, handle, name_record, operation_id):
        record = validate_name_record(name_record)
        validate_provider_name(self, record['name'])
        fields(handle, ('id', 'agent'))
        request = {'rename': handle, 'record': record}
        with locked(self.root):
            value = self._read()
            row = self._row(value, handle)
            key, prior = self._prior(value, handle['agent'], operation_id, request)
            if prior and prior['state'] == 'complete':
                return deepcopy(handle)
            need(row['kind'] == 'temporary' and not row['retired'] and row['native_id'] is not None,
                 'only a live owned temporary bot can be renamed')
            credentials, operator = self._session()
            user = self._observe(credentials, operator, row, target=record['name'] if prior else None)
            need(user['is_active'], 'inactive bot cannot be renamed')
            if not prior:
                need(not any(r['agent'] == handle['agent'] and r['kind'] == 'permanent' and not r['retired']
                             for r in value['identities'].values()), 'permanent identity already exists')
                self._preflight(credentials, value, record['name'], exclude=handle['id'])
                self._intent(value, key, request, handle, 'rename', record['name'])
            refusal = None
            if user['full_name'] != record['name']:
                try:
                    api_call(credentials, 'PATCH', '/bots/' + str(row['native_id']), {'full_name': record['name']})
                except ZulipRefusal as failure:
                    refusal = failure
                except SpawnError:
                    pass
            user = self._observe(credentials, operator, row, target=record['name'])
            # An earlier ambiguous PATCH may still commit after this refusal.
            # Only a first-attempt refusal can release the durable intent.
            if refusal is not None and not prior and user['full_name'] == row['name']:
                del value['operations'][key]
                write(self.path, value)
                raise SpawnError('Zulip rename refused (HTTP ' + str(refusal.status) +
                                 '); ' + ('retryable' if refusal.retryable else 'terminal (correct request/policy)') +
                                 '; intent released') from None
            if refusal is not None and prior and user['full_name'] == row['name']:
                raise SpawnError('Zulip rename refused (HTTP ' + str(refusal.status) +
                    '); ' + ('retryable' if refusal.retryable else 'terminal (correct request/policy)') +
                    '; intent pending; settle earlier PATCH then use audited recover_rename') from None
            need(user['full_name'] == record['name'] and user['is_active'], 'Zulip rename awaiting reconciliation')
            row.update(name=record['name'], kind='permanent')
            self._complete(value, key)
            return deepcopy(handle)

    def recover_rename(self, handle, operation_id, *, observed, reason, evidence_reference, settled=False):
        """Observe and resolve a pending rename, without PATCH/DELETE/replay.

        For not-applied, the operator must first settle/cancel every in-flight
        PATCH and cite that procedure's evidence. A GET alone cannot prove that
        an earlier ambiguous request will not land later. No journal editing.
        """
        need(match(operation_id, r'[A-Za-z0-9_:-]{1,256}') and
             observed in ('applied', 'not-applied') and type(settled) is bool,
             'explicit rename recovery observation required')
        for text in (reason, evidence_reference):
            need(type(text) is str and 0 < len(text.strip()) <= 2048 and
                 not any(ord(c) < 32 for c in text), 'bounded reason and evidence reference required')
        need(observed == 'applied' or settled, 'settle earlier PATCH before releasing rename intent')
        with locked(self.root):
            value = self._read()
            row = self._row(value, handle)
            key = handle['agent'] + ':' + operation_id
            pending = value['operations'].get(key)
            need(pending is not None and pending['state'] == 'pending' and
                 pending['action'] == 'rename' and pending['handle'] == handle,
                 'pending rename operation mismatch')
            credentials, operator = self._session()
            user = self._observe(credentials, operator, row, target=pending['target'])
            need(user['is_active'] and user['full_name'] == (
                pending['target'] if observed == 'applied' else row['name']),
                'rename recovery contradicts observation')
            value.setdefault('audit', []).append({'event': 'recover-rename', 'operation': operation_id,
                'handle': deepcopy(handle), 'intent': deepcopy(pending), 'observed': observed,
                'reason': reason, 'evidenceReference': evidence_reference, 'settled': settled,
                'who': {'uid': os.getuid(), 'euid': os.geteuid()},
                'when': datetime.now(timezone.utc).isoformat(),
                'observation': {'native_id': user['user_id'], 'name': user['full_name'], 'active': True}})
            if observed == 'applied':
                row.update(name=pending['target'], kind='permanent')
                pending['state'] = 'complete'
            else:
                del value['operations'][key]
            write(self.path, value)
            return deepcopy(handle)

    def retire(self, handle, operation_id):
        fields(handle, ('id', 'agent'))
        request = {'retire': handle}
        with locked(self.root):
            value = self._read()
            row = self._row(value, handle)
            key, prior = self._prior(value, handle['agent'], operation_id, request)
            if prior and prior['state'] == 'complete':
                return None
            need(row['native_id'] is not None, 'unresolved bot create requires reconciliation')
            credentials, operator = self._session()
            user = self._observe(credentials, operator, row)
            if not prior:
                self._intent(value, key, request, handle, 'retire')
            refusal = None
            if user['is_active']:
                try:
                    api_call(credentials, 'DELETE', '/bots/' + str(row['native_id']))
                except ZulipRefusal as failure:
                    refusal = failure
                except SpawnError:
                    pass
            user = self._observe(credentials, operator, row)
            if refusal is not None and user['is_active']:
                del value['operations'][key]
                write(self.path, value)
                raise SpawnError('Zulip retirement refused (HTTP ' + str(refusal.status) +
                                 '); ' + ('retryable' if refusal.retryable else 'terminal (correct request/policy)') +
                                 '; intent released') from None
            need(not user['is_active'], 'Zulip retirement awaiting reconciliation')
            delivered = safe_path(self.secrets_root / handle['agent'] / (handle['id'] + '.key'))
            if os.path.lexists(delivered):
                # Validate before deletion; never follow an unsafe replacement.
                read_file(delivered, root=self.secrets_root / handle['agent'], private=True)
                delivered.unlink()
            # Also sync on retry if death followed unlink but preceded fsync.
            if delivered.parent.exists():
                sync_dir(delivered.parent)
            row['retired'] = True
            self._complete(value, key)

    def list(self):
        """Observe only reserved/owned handles, never return any API key."""
        with locked(self.root):
            value = self._read()
            credentials, operator = self._session()
            result = []
            for identity_id, row in sorted(value['identities'].items()):
                pending = [o for o in value['operations'].values()
                           if o['state'] == 'pending' and o['handle']['id'] == identity_id]
                if row['native_id'] is None:
                    active = True if self._discover(credentials, operator, row) else None
                else:
                    target = pending[0]['target'] if pending else None
                    active = self._observe(credentials, operator, row, target=target)['is_active']
                result.append(dict(row, id=identity_id, active=active,
                    recovery='pending; retry identical provider operation to reconcile' if pending else
                    'retired; never reused' if row['retired'] else 'owned; reconcile lifecycle liveness separately'))
            return result
