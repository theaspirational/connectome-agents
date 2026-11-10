"""Transactional, unprivileged deployment engine. Standard library only."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import tempfile

try:
    from ansible.module_utils import connectome_inputs as inputs
    from ansible.module_utils import connectome_catalog as catalog
    from ansible.module_utils.connectome_lock import read_lock, LockError
    from ansible.module_utils.connectome_errors import SafeError, adapter_diagnostic
    from ansible.module_utils.connectome_archive import (framework_provenance, validate_provenance,
                                                        dependency_provenance, dependency_inputs,
                                                        records_metadata, provenance_record)
except ImportError:
    import connectome_inputs as inputs
    import connectome_catalog as catalog
    from connectome_lock import read_lock, LockError
    from connectome_errors import SafeError, adapter_diagnostic
    from connectome_archive import (framework_provenance, validate_provenance,
                                   dependency_provenance, dependency_inputs,
                                   records_metadata, provenance_record)


class DeployError(SafeError):
    pass


class UnitVerificationFailed(DeployError):
    """Closed tool identity and numeric status only; never command output."""
    def __init__(self, returncode=None):
        self.returncode = returncode
        detail = ('unavailable' if returncode is None else 'exited ' + str(returncode))
        super().__init__('systemd-analyze ' + detail, 'E_UNIT_VERIFY')

    def public(self):
        result = super().public()
        result['msg'] += ' (' + str(self) + ')'
        return result


class ActivationFailed(DeployError):
    """Activation context built only from fixed steps and sanitized causes."""
    def __init__(self, step, cause, restored):
        status = 'previous deployment restored' if restored else 'rollback recovery also failed'
        super().__init__('activation failed at ' + step + ': ' + str(cause) + '; ' + status,
                         cause.code if restored else 'E_DEPLOY_FAILED')

    def public(self):
        result = super().public()
        result['msg'] += ' (' + str(self) + ')'
        return result


class UnitActive(DeployError):
    """Unit names and scopes have passed the closed input/unit-name schema."""
    def __init__(self, unit, scope='user', code='E_UNIT_ACTIVE'):
        super().__init__('unit is active: ' + unit, code)
        self.unit, self.scope = unit, scope

    def public(self):
        result = dict(super().public(), unit=self.unit, scope=self.scope)
        result['msg'] += ' (' + self.scope + ': ' + self.unit + ')'
        return result


class AncestorWritable(DeployError):
    """Only a locally inspected directory path, never tool output, is public."""
    def __init__(self, path):
        super().__init__('group/other-writable ancestor: ' + str(path), 'E_ANCESTOR_WRITABLE')
        self.path = str(path)

    def public(self):
        return dict(super().public(), ancestor_path=self.path)


def check_ancestors(path):
    for parent in reversed(Path(path).parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(info.st_mode) and info.st_mode & 0o022:
            raise AncestorWritable(parent)


class ExternalCommandUnsafe(DeployError):
    """Only the lock-validated server name is public, never the path or reason."""
    def __init__(self, name, reason, code='E_EXTERNAL_COMMAND'):
        super().__init__('unsafe external server path: ' + reason, code)
        self.name = name

    def public(self):
        return dict(super().public(), external_server=self.name)


def path_like_arg(arg):
    """Cook's external-server rule: a flag's =value, or a bare arg naming a file."""
    value = arg.partition('=')[2] if arg.startswith('-') else arg
    if not value or re.match(r'[a-z][a-z0-9+.-]*://', value, re.I):
        return None
    if '/' in value or value.startswith(('.', '~')) or re.search(r'\.(?:[cm]?js|ts|py)$', value):
        return value
    return None


def check_external_command(name, command, arg=False, account=None):
    """Resolve like realpath, but lstat every visited component and link.

    The path, every link in its chain, every directory traversed and the
    final target must be root-owned; directories and the target must not be
    group/other-writable. Symlink modes are meaningless; their parents count.
    The agent account (owner, else group, else other bits, as the kernel
    applies them) must be able to read and traverse every directory and read
    and execute the command; a path argument may be a readable file or a
    readable, traversable directory.
    """
    uid, groups = account or (os.geteuid(), {os.getegid(), *os.getgroups()})

    def allowed(info, bits):
        shift = 6 if info.st_uid == uid else 3 if info.st_gid in groups else 0
        return (info.st_mode >> shift) & bits == bits

    def inspect(path, last):
        try:
            info = path.lstat()
        except OSError:
            raise ExternalCommandUnsafe(name, 'missing or unreadable path') from None
        if info.st_uid != 0:
            raise ExternalCommandUnsafe(name, 'not root-owned')
        if stat.S_ISLNK(info.st_mode):
            return info
        directory = stat.S_ISDIR(info.st_mode)
        if not (stat.S_ISREG(info.st_mode) or (arg and directory) if last else directory):
            raise ExternalCommandUnsafe(name, 'not a regular file' if last else 'not a directory')
        if info.st_mode & 0o022:
            raise ExternalCommandUnsafe(name, 'group/other-writable')
        if not allowed(info, 0o5 if directory or not arg else 0o4):
            raise ExternalCommandUnsafe(name, 'not readable/executable by the agent account',
                                        'E_EXTERNAL_ACCESS')
        return info

    current = Path('/')
    inspect(current, False)
    pending = list(Path(command).parts[1:])
    links = 0
    while pending:
        part = pending.pop(0)
        if part in ('', '.'):
            continue
        if part == '..':
            # current is always a fully inspected real directory.
            current = current.parent
            continue
        candidate = current / part
        info = inspect(candidate, not pending)
        if stat.S_ISLNK(info.st_mode):
            links += 1
            if links > 40:
                raise ExternalCommandUnsafe(name, 'too many symbolic links')
            target = Path(os.readlink(candidate))
            if target.is_absolute():
                current = Path('/')
            pending = list(target.parts[1:] if target.is_absolute() else target.parts) + pending
            if not pending:
                raise ExternalCommandUnsafe(name, 'not a regular file')
            continue
        current = candidate
    if current == Path('/'):
        raise ExternalCommandUnsafe(name, 'not a regular file')
    return current


def check_external_data_arg(name, value, account=None, home=None, kind='file'):
    """Private argv data only; never read content, follow links or repair modes."""
    def unsafe():
        raise ExternalCommandUnsafe(name, 'invalid private data argument', 'E_EXTERNAL_DATA_ARG')

    if not inputs.external_data_path(value) or kind not in ('file', 'dir'):
        unsafe()
    path = Path(value)
    uid, groups = account or (os.geteuid(), {os.getegid(), *os.getgroups()})
    try:
        info = path.lstat()
        matches_kind = stat.S_ISREG(info.st_mode) if kind == 'file' else stat.S_ISDIR(info.st_mode)
        if not matches_kind:
            unsafe()
        # Root-maintained paths never use the private-data exception.
        if info.st_uid == 0:
            return check_external_command(name, value, True, account)
        home = Path(home if home is not None else pwd.getpwuid(uid).pw_dir)
        if (not home.is_absolute() or not path.is_relative_to(home) or path == home or
                info.st_uid != uid or
                stat.S_IMODE(info.st_mode) != (0o600 if kind == 'file' else 0o700)):
            unsafe()
        for parent in reversed(path.parents):
            ancestor = parent.lstat()
            owners = {0, uid} if parent.is_relative_to(home) else {0}
            if (not stat.S_ISDIR(ancestor.st_mode) or ancestor.st_uid not in owners or
                    ancestor.st_mode & 0o022):
                unsafe()
            shift = 6 if ancestor.st_uid == uid else 3 if ancestor.st_gid in groups else 0
            if (ancestor.st_mode >> shift) & 0o5 != 0o5:
                raise ExternalCommandUnsafe(name, 'ancestor denies agent access', 'E_EXTERNAL_ACCESS')
        # Owner read/traverse is guaranteed by the exact 0600/0700 modes above.
        return path
    except (OSError, KeyError):
        unsafe()


def external_paths(lock, recipe, data_args=()):
    """(name, path, is_arg, private_data) with exact invocation and positional/flag binding."""
    try:
        inputs.validate_external_data_args(list(data_args))
    except inputs.ExternalDataArgError:
        raise DeployError('invalid private data declaration', 'E_EXTERNAL_DATA_ARG') from None
    servers = recipe.get('mcpServers') if isinstance(recipe, dict) else None
    external = {record['name'] for record in lock.get('external', [])}
    for entry in data_args:
        server = servers.get(entry['server']) if isinstance(servers, dict) else None
        if not (entry['server'] in external and isinstance(server, dict) and
                isinstance(server.get('source'), dict) and 'external' in server['source'] and
                server.get('command') == entry['command'] and
                server.get('args', []) == entry['args'] and
                entry['path'] != server.get('command') and
                (entry['path'] in server.get('args', []) or
                 ('flag' in entry and entry['flag'] + '=' + entry['path'] in server.get('args', [])))):
            raise ExternalCommandUnsafe(entry['server'], 'unbound data argument', 'E_EXTERNAL_DATA_ARG')
    exemptions = {(entry['server'], entry['path']): entry for entry in data_args}
    result = []
    for record in lock.get('external', []):
        server = servers.get(record['name']) if isinstance(servers, dict) else None
        if not (isinstance(server, dict) and isinstance(server.get('source'), dict) and
                'external' in server['source'] and server.get('command') == record['command'] and
                isinstance(server.get('args', []), list) and
                all(isinstance(arg, str) for arg in server.get('args', []))):
            raise DeployError('external server differs from the installed recipe', 'E_LOCK_INVALID')
        args = server.get('args', [])
        result.append((record['name'], record['command'], False, False))
        if any(entry['server'] == record['name'] for entry in data_args):
            # Only these resolved interpreter targets consume args[0] as a
            # script. Check the lstat-walk's real target, not a symlink alias.
            try:
                interpreter = check_external_command(record['name'], record['command'])
                if (interpreter.name not in {'node', 'python3', 'bun'} and
                        not re.fullmatch(r'python3\.[0-9]+', interpreter.name)):
                    raise ValueError
            except (ExternalCommandUnsafe, ValueError):
                raise ExternalCommandUnsafe(record['name'], 'invalid private data interpreter',
                                            'E_EXTERNAL_DATA_ARG') from None
            # args[0] is immediately after the interpreter: no interpreter
            # options (not even --) or option values can precede this script.
            # It is non-executable data for the pinned interpreter.
            script = args[0] if args else ''
            try:
                if not script.startswith('/'):
                    raise ValueError
                checked = check_external_command(record['name'], script, True)
                mode = checked.lstat().st_mode
                if not stat.S_ISREG(mode) or mode & 0o111:
                    raise ValueError
            except (ExternalCommandUnsafe, OSError, ValueError):
                raise ExternalCommandUnsafe(record['name'], 'invalid first-argument script',
                                            'E_EXTERNAL_DATA_ARG') from None
        for index, raw in enumerate(args):
            value = path_like_arg(raw)
            if value is None:
                continue
            if not value.startswith('/'):
                raise ExternalCommandUnsafe(record['name'], 'relative path argument')
            entry = exemptions.get((record['name'], value))
            flag = entry.get('flag') if entry else None
            private_data = entry is not None and (raw == value or
                                                 (flag is not None and raw == flag + '=' + value))
            if private_data:
                if flag:
                    bound = (raw == flag + '=' + value or
                             (index > 0 and args[index - 1] == flag))
                else:
                    bound = index > 0 and not args[index - 1].startswith('-')
                if index < 1 or not bound:
                    raise ExternalCommandUnsafe(record['name'], 'unbound data position or flag',
                                                'E_EXTERNAL_DATA_ARG')
            result.append((record['name'], value, True, private_data))
    return result


def make_directory(path, mode=0o700):
    """Set exact modes only on directories this call creates, never repair."""
    path = Path(path)
    if not path.parent.exists():
        make_directory(path.parent, 0o755)
    try:
        path.mkdir(mode=mode)
    except FileExistsError:
        if not path.is_dir() or path.is_symlink():
            raise DeployError('refusing non-directory or symlinked directory') from None
    else:
        # mkdir's mode is masked by umask; chmod only our new directory.
        path.chmod(mode)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    make_directory(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb' if isinstance(content, bytes) else 'w') as stream:
            stream.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def link(path, target):
    path = Path(path)
    make_directory(path.parent)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.link-')
    os.close(fd)
    tmp = Path(temporary)
    try:
        tmp.unlink()
        tmp.symlink_to(target)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', value):
        raise DeployError('invalid agent or connector identifier')
    return value


def validate_config(config):
    try:
        inputs.validate(config)
    except inputs.ExternalDataArgError as error:
        raise DeployError(str(error), 'E_EXTERNAL_DATA_ARG') from None
    except ValueError as error:
        raise DeployError(str(error)) from None
    identifier(config.get('name', 'example'))
    for name in config.get('connectors', {}):
        if not catalog.active(config):
            identifier(name)
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', name):
            raise DeployError('invalid connector identifier')
    if 'host_dependencies' in config.get('cook', {}):
        # Shape/duplicate preflight only; archive IO remains in the plan/adapter.
        dependency_inputs(config, '1' * 40)


def base_path(value):
    # Do not silently trust a redirected base (including symlinked ancestors).
    path = Path(os.path.abspath(Path(value).expanduser()))
    if path.resolve() != path:
        raise DeployError('symlinked base directory is unsupported')
    return path


def inside(root, value):
    path = root / value
    if not path.resolve().is_relative_to(root.resolve()):
        raise DeployError('artifact path escapes base')
    return path


def run(argv, cwd=None, adapter=False, unit_verify=False):
    # Only prepare may accept the adapter's closed JSON envelope. Systemctl and
    # health output cannot impersonate adapter diagnostics.
    with tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(argv, cwd=cwd, stdout=subprocess.DEVNULL,
                                    stderr=errors if adapter else subprocess.DEVNULL, check=False)
        except OSError:
            if unit_verify:
                raise UnitVerificationFailed() from None
            raise DeployError('command unavailable', 'E_COOK_FAILED' if adapter else 'E_DEPLOY_FAILED') from None
        if result.returncode:
            if unit_verify:
                raise UnitVerificationFailed(result.returncode) from None
            if adapter:
                errors.seek(0)
                safe = adapter_diagnostic(errors.read(65537))
                raise DeployError(safe['msg'], safe['error_code'], safe.get('cook_error_code'))
            raise DeployError('command failed (output redacted)')


def direct_secret_names(config):
    entries = list(config.get('connectors', {}).values()) + list(config.get('modules', {}).values())
    if catalog.active(config):
        entries += list(config['catalog'].values())
    names = set(config.get('required_secrets', []))
    for entry in entries:
        names.update(entry.get('required_secrets', []))
    if any(not isinstance(name, str) or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', name) for name in names):
        raise DeployError('invalid secret name')
    if names & {'DATA_DIR', 'WORKSPACE_DIR'}:
        raise DeployError('secret names overlap runtime state environment')
    return sorted(names)


def secret_names(config):
    direct = set(direct_secret_names(config))
    records = config.get('secret_files', [])
    if direct & {record['env'] for record in records}:
        raise DeployError('secret file environment name conflicts with direct secret delivery')
    return sorted(direct | {record['secret'] for record in records})


def environment_names(config):
    if config.get('secret_files'):
        secret_names(config)  # Also reject direct/file destination collisions in standalone adapters.
    return sorted(set(direct_secret_names(config)) | {record['env'] for record in config.get('secret_files', [])})


def resolve_pins(config, versions):
    runtime = versions['runtime']
    result = {'runtime': runtime, 'connectors': {}}
    if catalog.active(config):
        result.update(catalog.resolve(config, versions))
    for name, entry in ({} if 'recipe_file' in config or catalog.active(config) else config.get('connectors', {})).items():
        value = versions
        try:
            for part in entry['pin_ref'].split('.'):
                value = value[part]
        except (KeyError, TypeError):
            raise DeployError('unknown connector pin reference') from None
        result['connectors'][name] = value
    pins = [runtime[key] for key in ('connectome-host', 'agent-framework', 'cook')]
    pins += list(result['connectors'].values())
    if any(not isinstance(pin, str) or not re.fullmatch(r'[a-f0-9]{40}', pin) or
           pin == '0' * 40 for pin in pins):
        raise DeployError('unresolved or invalid full commit pin')
    return result


class Deployment:
    def __init__(self, root, unit_dir, config, versions, cook_command, systemctl=None,
                 check=False, base=None, operator_files=None, analyze_command=None):
        # This constructor must stay mutation-free: the Ansible boundary creates
        # the root/serialization lock only AFTER all identifiers are validated.
        validate_config(config)
        self.base = base_path(base if base is not None else root)
        self.root = inside(self.base, Path(os.path.abspath(Path(root).expanduser())))
        self.unit_dir = base_path(unit_dir)
        if catalog.active(config):
            catalog.input_paths(config, self.root)
        self.config = config
        self.activation = config.get('activation', 'start')
        self.operator_files = operator_files
        self.pins = resolve_pins(config, versions)
        self.cook_command = cook_command
        self.systemctl = systemctl or ['systemctl', '--user']
        # None discovers the host verifier; an explicit argv is injectable like
        # cook/systemctl. An empty argv models an unavailable optional verifier.
        self.analyze_command = analyze_command
        self.check = check
        self.rendered_units = {}
        self.names = secret_names(config)
        # Runtime activation policy is not cook input or release identity. An
        # explicit start/empty list must also match legacy request/lock bytes.
        build_config = {key: value for key, value in config.items()
                        if key not in ('activation', 'exclusive_with')}
        self.spec = {'config': build_config, 'pins': self.pins}
        # Deployment request identity differs from cook's emitted build identity.
        self.request_id = hashlib.sha256(canonical({'spec': self.spec, 'cook': cook_command}).encode()).hexdigest()
        self.builds = inside(self.root, 'builds')
        self.build = inside(self.builds, self.request_id)
        self.current = inside(self.root, 'current')
        # Deployment bookkeeping is not agent/runtime state.
        self.state = inside(self.root, 'state')
        value = config.get('state_dir')
        if (not isinstance(value, str) or not Path(value).is_absolute() or
                value != os.path.abspath(value) or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise DeployError('state_dir must be a canonical absolute path outside the deployment root')
        self.state_dir = Path(os.path.abspath(value))
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
        value = config.get('secrets_file', str(home / '.config/connectome-secrets' /
                                               (config.get('name', 'example') + '.json')))
        if (not isinstance(value, str) or not Path(value).is_absolute() or
                value != os.path.abspath(value) or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise DeployError('secrets_file must be a canonical absolute path')
        self.secrets_file = Path(os.path.abspath(value))
        self._path_preflight()
        self._validate_state()
        self.manifest_path = self.state / 'ownership.json'
        self.lock_path()

    def _path_preflight(self):
        for path in (self.root, self.state_dir, self.secrets_file, self.unit_dir):
            self._systemd_path(str(path))
            check_ancestors(path)
        # Root and unit_dir become parents of installed files too.
        for path in (self.root, self.unit_dir):
            check_ancestors(path / '.path-check')

    def validate_paths(self):
        self._path_preflight()
        validate_config(self.config)
        base_path(self.base)
        base_path(self.unit_dir)
        base_path(self.state)
        inside(self.base, self.root)
        for path in (self.builds, self.build, self.current, self.state):
            inside(self.root, path)
        # Containment alone permits aliases within the tree. Check every build
        # and state parent before mkdir, recursive cleanup or any other write.
        for path in (self.root, self.builds, self.build, self.state):
            base_path(path)
            self._safe_parent(path / '.path-check')
        self._validate_state()
        self.lock_path()

    def _validate_state(self):
        # Reject both lexical and resolved overlaps, including links into builds.
        for path in (self.state_dir, self.state_dir.resolve()):
            if path.is_relative_to(self.root) or self.root.is_relative_to(path):
                raise DeployError('state_dir must be outside and separate from the deployment root/builds')
        if self.unit_dir.is_relative_to(self.state_dir) or self.state_dir.is_relative_to(self.unit_dir):
            raise DeployError('state_dir must be separate from unit_dir')
        base_path(self.state_dir)
        self._safe_parent(self.state_dir / '.path-check')
        if self.state_dir.exists():
            info = self.state_dir.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or
                    info.st_uid != os.geteuid()):
                raise DeployError('state_dir must be a real directory, mode 0700, owned by the agent account; not repaired')

    def _ensure_state(self):
        self._validate_state()
        make_directory(self.state_dir)
        self._validate_state()

    def _check_secrets_file(self):
        path = self.secrets_file
        try:
            valid = path.resolve() == path and path.parent.resolve() == path.parent
            directory, info = path.parent.lstat(), path.lstat()
            valid = (valid and stat.S_ISDIR(directory.st_mode) and
                     stat.S_IMODE(directory.st_mode) == 0o700 and
                     directory.st_uid == os.geteuid() and stat.S_ISREG(info.st_mode) and
                     stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid())
        except OSError:
            valid = False
        if not valid:
            raise DeployError('secrets file check failed: require non-symlink directory 0700 and regular file 0600, both owned by the agent account (see plan secrets_file); not repaired', 'E_SECRETS_MODE')

    def _read_secrets(self):
        self._check_secrets_file()
        try:
            values = json.loads(self.secrets_file.read_text())
            return {name: values[name] for name in self.names}
        except (OSError, ValueError, KeyError, TypeError):
            raise DeployError('agent secrets file lookup failed (values redacted)') from None

    def plan(self):
        result = {'state_dir': str(self.state_dir), 'secrets_file': str(self.secrets_file),
                  'budgets': self.config.get('budgets', {})}
        if self.activation == 'install_only':
            result['activation'] = self.activation
        if self.config.get('exclusive_with'):
            result['exclusive_with'] = self.config['exclusive_with']
        if self.config.get('external_data_args'):
            result['external_data_args'] = [dict(entry) for entry in self.config['external_data_args']]
        if 'recipe_file' in self.config:
            result['recipe_file'] = dict(self.config['recipe_file'])
        if self.config.get('secret_files'):
            result['secret_files'] = [dict(record, path=str(self._secret_file_path(record['env'])))
                                      for record in self.config['secret_files']]
        if 'framework_archive' in self.config.get('cook', {}):
            lock = self.build / 'framework.lock.json'
            if (self.build / '.prepared').is_file():
                result['framework_archive'] = (validate_provenance(
                    json.loads(lock.read_text()), self.config, self.pins['runtime']['agent-framework'])
                    if lock.is_file() else {'provenance': 'not_recorded_legacy_build'})
            else:
                result['framework_archive'] = framework_provenance(
                    self.config, self.pins['runtime']['agent-framework'])
        if records_metadata(self.config):
            if (self.build / '.prepared').is_file():
                data = self._lock()
                entries = dependency_inputs(self.config, self.pins['runtime']['agent-framework'])
                result['host_dependencies'] = [provenance_record(dict(entry, version=dep['version']))
                    for entry, dep in zip(entries, data.get('hostDependencies', []))]
            else:
                result['host_dependencies'] = dependency_provenance(
                    self.config, self.pins['runtime']['agent-framework'])
        return result

    def lock_path(self):
        return inside(self.root, '.deploy-lock')

    def _exclusive_status(self, refuse=False):
        result = []
        for record in self.config.get('exclusive_with', []):
            command = [arg for arg in self.systemctl if arg not in ('--user', '--system')]
            try:
                state = subprocess.run(command + ['--' + record['scope'], 'is-active', record['unit']],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
            except OSError:
                state = None
            value = state.stdout.decode('ascii', errors='replace').strip() if state else 'unavailable'
            if (not state or value not in ('active', 'activating', 'reloading', 'deactivating',
                                          'inactive', 'failed', 'unknown') or state.returncode not in (0, 3, 4)):
                if refuse:
                    raise DeployError('cannot inspect exclusive unit') from None
                value = 'unavailable'
            if refuse and value not in ('inactive', 'failed', 'unknown'):
                raise UnitActive(record['unit'], record['scope'], 'E_UNIT_EXCLUSIVE')
            result.append(dict(record, state=value))
        return result

    def _refuse_active(self, names):
        for name in sorted(names):
            self._unit_path(name)
            try:
                result = subprocess.run(self.systemctl + ['show', '-p', 'ActiveState', '--value', name],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
            except OSError:
                raise DeployError('cannot inspect unit state') from None
            if result.returncode:
                raise DeployError('cannot inspect unit state')
            if result.stdout.strip() not in (b'inactive', b'failed'):
                raise UnitActive(name)

    def preflight_activation(self):
        """Read-only guard, including before Ansible creates root/lock files."""
        if self.check:
            return
        if self.activation == 'start':
            self._exclusive_status(refuse=True)
            return
        names = {'connectome-' + self.config.get('name', 'example') + '.service'}
        builds = {self.current.resolve()} if self.current.is_symlink() else set()
        if (self.build / '.prepared').is_file():
            builds.add(self.build)
        for build in builds:
            names.update(self._units(build, self._lock(build)))
        self._refuse_active(names)

    def _staged(self):
        path = self.state / 'activation'
        self._safe_parent(path)
        if path.is_symlink() or (path.exists() and
                (not path.is_file() or path.read_text() != 'install_only\n')):
            raise DeployError('invalid activation bookkeeping')
        return path.exists()

    def _status(self, result):
        if self.config.get('external_data_args'):
            result['external_data_args'] = [dict(entry) for entry in self.config['external_data_args']]
        if self.activation == 'install_only' and not self.check:
            result.update(activation='install_only', unit_state='inactive')
            if self.config.get('exclusive_with'):
                result['exclusive_with'] = self._exclusive_status()
        return result

    def _lock(self, build=None):
        build = build or self.build
        inside(self.builds, build)
        config = self.config if build == self.build else self._config(build)
        pin = (self.pins['runtime']['agent-framework'] if build == self.build else
               json.loads((build / 'deployment.json').read_text())['pins']['runtime']['agent-framework'])
        recorded = None
        if 'framework_archive' in config.get('cook', {}) and (build / 'framework.lock.json').exists():
            try:
                recorded = validate_provenance(json.loads((build / 'framework.lock.json').read_text()), config, pin)
            except (OSError, ValueError):
                raise DeployError('invalid framework provenance', 'E_LOCK_INVALID') from None
        try:
            data = read_lock(inside(build, 'connectome.lock'), config=config, framework_pin=pin,
                             framework_record=recorded,
                             host_pin=self.pins['runtime']['connectome-host'] if build == self.build else None,
                             required_names=environment_names(config) if build == self.build else ())
        except LockError as error:
            raise DeployError(str(error), 'E_LOCK_INVALID') from None
        if catalog.active(config):
            catalog_pins = (self.pins if build == self.build else
                            json.loads((build / 'deployment.json').read_text())['pins'])
            catalog.verify(config, catalog_pins, build, data)
        state = data.get('state')
        if not state:
            raise DeployError('cook lock must declare configured external state_dir', 'E_LOCK_STATE_PATH')
        if state:
            path = Path(state['path'])
            expected = self.state_dir if build == self.build else Path(self._config(build)['state_dir'])
            if not path.is_absolute() or state['path'] != str(expected):
                raise DeployError('cook state does not match configured state_dir', 'E_LOCK_STATE_PATH')
            base_path(path)  # Previous/rollback locks must not alias state either.
            if path.resolve().is_relative_to(self.root.resolve()):
                raise DeployError('state must be outside deployment root/builds', 'E_LOCK_STATE_PATH')
        config = self.config if build == self.build else self._config(build)
        if ('framework_archive' in config.get('cook', {}) and
                ((build / 'framework.lock.json').exists() or
                 config['cook']['framework_archive'].get('registry_release') is True)):
            # Legacy gitHead-only builds predate the sidecar; do not adopt the
            # registry exception without a provenance record.
            # Use recorded request pin for retained builds, not today's pins.
            pin = json.loads((build / 'deployment.json').read_text())['pins']['runtime']['agent-framework']
            try:
                recorded = json.loads((build / 'framework.lock.json').read_text())
            except (OSError, ValueError):
                raise DeployError('framework provenance lock missing or malformed', 'E_LOCK_INVALID') from None
            expected = validate_provenance(recorded, config, pin)
            if not any(
                    dep['name'] == expected['name'] and dep['version'] == expected['version'] and
                    dep['sha256'] == expected['sha256'] for dep in data.get('hostDependencies', [])):
                raise DeployError('framework provenance lock mismatch', 'E_LOCK_INVALID')
        try:
            inputs.installed(build, config)
        except ValueError as error:
            raise DeployError(str(error), 'E_LOCK_INVALID') from None
        # Role lock view; the request's deployment.json retains this opt-in.
        # Do not claim cook emitted it or rewrite cook's build identity.
        if config.get('external_data_args'):
            data['external_data_args'] = [dict(entry) for entry in config['external_data_args']]
        return data

    def prepare(self):
        self.validate_paths()
        self.preflight_activation()
        self._check_secrets_file()
        self._load_ownership()
        self._check_build_ownership()
        if not self.check and 'recipe_file' not in self.config:
            self._ensure_state()
        marker = inside(self.build, '.prepared')
        if marker.is_file() and marker.read_text() == self.request_id:
            if not self.check and 'recipe_file' in self.config:
                self._ensure_state()
            self._activation_plan()
            return {'changed': False, 'build_dir': str(self.build), 'secret_names': self.names, 'plan': self.plan()}
        try:
            payloads = inputs.payloads(self.config, self.operator_files)
        except ValueError as error:
            raise DeployError(str(error)) from None
        if self.check:
            return {'changed': True, 'build_dir': str(self.build), 'secret_names': self.names, 'plan': self.plan()}
        if not self.cook_command:
            raise DeployError('configure a pinned cook adapter command')
        if 'recipe_file' in self.config:
            self._ensure_state()
        if self.current.is_symlink() and self.current.resolve() == self.build:
            raise DeployError('refusing to rebuild active directory')
        if any(path != self.build and path.is_relative_to(self.build) for path in self.owned):
            raise DeployError('refusing to rebuild a directory with manifest-owned unit files')
        if self.build.exists():
            # _check_build_ownership refused unknown directories, even if they
            # contained a plausible .prepared marker. Only our own incomplete,
            # inactive request directory may be recursively discarded.
            shutil.rmtree(self.build)
        make_directory(self.root)
        make_directory(self.builds)
        make_directory(self.build)
        make_directory(self.state)
        self.owned[self.build] = {'kind': 'directory'}
        self._save_ownership()  # persist ownership before invoking cook
        recipe = inside(self.build, 'deployment.json')
        atomic_write(recipe, canonical(self.spec))
        if 'recipe_file' in self.config:
            self._write_input(self.build / 'cook-recipe.json', payloads['cook-recipe.json'], 0o600)
        substitutions = {'recipe': str(recipe),
                         'build_dir': str(self.build), 'state_dir': str(self.state_dir)}
        run([arg.format_map(substitutions) for arg in self.cook_command], cwd=self.build, adapter=True)
        # Surface safe ownership diagnostics before Ansible's no_log secret
        # activation task, and before creating env/current/service state.
        self._activation_plan()
        atomic_write(inside(self.build, '.prepared'), self.request_id)
        return {'changed': True, 'build_dir': str(self.build), 'secret_names': self.names, 'plan': self.plan()}

    def _config(self, build):
        config = json.loads(inside(build, 'deployment.json').read_text())['config']
        validate_config(config)
        return config

    def _units(self, build, lock):
        units = lock.get('units')  # planned extension: explicit path+digest records
        if units is None:
            launch = lock.get('launch', {})
            if launch.get('kind') != 'script':
                raise DeployError('host script launcher required')
            script = inside(build, launch['script']).resolve()
            if not script.is_file():
                raise DeployError('missing launcher')
            name = self._config(build).get('name', 'example')
            identifier(name)
            unit = inside(build, 'connectome-' + name + '.service')
            body = ('[Unit]\nDescription=Connectome agent\n[Service]\nType=simple\n'
                    'WorkingDirectory=' + self._systemd_path(self._config(build)['state_dir']) + '\nExecStart=' +
                    self._systemd_path(str(script)) + '\nRestart=on-failure\n[Install]\nWantedBy=default.target\n')
            self.rendered_units[unit] = body
            return {unit.name: unit}
        result = {}
        for entry in units:
            path = inside(build, entry['path']).resolve()
            if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', path.name) or path.name in result:
                raise DeployError('invalid or duplicate user unit')
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                raise DeployError('unit output digest mismatch')
            # Install our own copy, so every installed unit file has exact
            # manifest ownership; cook's original output is not adopted.
            installed = inside(build, 'managed-units/' + path.name)
            self.rendered_units[installed] = path.read_bytes().decode('utf-8')
            result[path.name] = installed
        if not result:
            raise DeployError('no units to activate')
        return result

    @staticmethod
    def _systemd_path(value):
        # EnvironmentFile/WorkingDirectory take paths, not shell-quoted words.
        # Refuse specifiers, globs, escapes, whitespace and other ambiguous input
        # instead of attempting directive-dependent escaping.
        if not re.fullmatch(r'/[A-Za-z0-9_./:@+-]+', value):
            raise DeployError('systemd paths require an absolute literal path without whitespace or special characters',
                              'E_SYSTEMD_PATH')
        return value

    def _write_input(self, path, content, mode):
        self._safe_parent(path)
        if path.exists() or path.is_symlink():
            raise DeployError('operator input destination already exists')
        make_directory(path.parent)
        # Byte mode is essential: no JSON reserialization or newline conversion.
        with open(path, 'xb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)

    def _check_environment_file(self, name, build):
        expected = self._systemd_path(str(inside(build, 'secrets.env')))
        try:
            result = subprocess.run(self.systemctl + ['show', '-p', 'EnvironmentFiles', '--value', name],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        except OSError:
            raise DeployError('cannot inspect unit environment file', 'E_ENVIRONMENT_FILE') from None
        # systemctl show is the documented machine-readable manager view, not
        # the on-disk text. Require this exact non-optional file in every unit.
        accepted = re.search(rb'(?:^|\s)' + re.escape(expected.encode()) +
                             rb' \(ignore_errors=no\)(?:\s|$)', result.stdout)
        if result.returncode or not accepted:
            raise DeployError('required unit environment file not accepted', 'E_ENVIRONMENT_FILE')

    def _health(self, units, lock, build):
        if self.activation == 'install_only':
            self._lock(build)
            if not self.current.is_symlink() or self.current.resolve() != build:
                raise DeployError('current does not point at installed release')
            for name in units:
                if not self._unit_path(name).is_file():
                    raise DeployError('installed unit file missing')
                self._check_environment_file(name, build)
            self._refuse_active(units)
            analyze = self.analyze_command
            if analyze is None:
                executable = shutil.which('systemd-analyze')
                analyze = [executable] if executable else []
            if analyze and units:
                run(analyze + ['--user', 'verify'] + [str(self._unit_path(name)) for name in units],
                    unit_verify=True)
            return
        for name in units:
            run(self.systemctl + ['is-active', '--quiet', name])
            if secret_names(self._config(build)):
                self._check_environment_file(name, build)
        health = lock.get('health', self._config(build).get('health', {}))
        if health.get('command'):
            if not isinstance(health['command'], list):
                raise DeployError('health command must be argv')
            run(health['command'], cwd=build)

    def _secret_file_path(self, env):
        return self.state / 'secret-files' / env

    def _secret_file_snapshot(self, previous, config):
        old = self._config(previous).get('secret_files', []) if previous else []
        before = {record['env'] for record in old}
        after = {record['env'] for record in config.get('secret_files', [])}
        if not before and not after:
            return {}, set()
        return self._inspect_secret_files(before), before | after

    def _inspect_secret_files(self, allowed):
        directory = self.state / 'secret-files'
        self._safe_parent(directory / '.path-check')
        if not directory.exists():
            return {}
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or
                info.st_uid != os.geteuid()):
            raise DeployError('secret file directory must be agent-owned mode 0700; not repaired')
        result = {}
        for path in directory.iterdir():
            info = path.lstat()
            if (path.name not in allowed or not stat.S_ISREG(info.st_mode) or
                    stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid()):
                raise DeployError('secret file must be declared, regular and agent-owned mode 0600; not repaired')
            result[path.name] = path.read_bytes()
        return result

    def _set_secret_files(self, content, allowed):
        existing = self._inspect_secret_files(allowed)
        directory = self.state / 'secret-files'
        if content:
            make_directory(directory)
        for env in existing.keys() - content.keys():
            self._secret_file_path(env).unlink()
        for env, value in content.items():
            atomic_write(self._secret_file_path(env), value)
        if directory.exists() and not list(directory.iterdir()):
            directory.rmdir()

    def _unit_path(self, name):
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', name):
            raise DeployError('invalid user unit')
        # Unit links intentionally point outside unit_dir; contain their parents,
        # and check the leaf's ownership separately before replacing/removing it.
        return self.unit_dir / name

    def _dropin(self, name):
        self._unit_path(name)
        directory = self.unit_dir / (name + '.d')
        if directory.is_symlink():
            raise DeployError('refusing symlinked drop-in directory (aliased parent or escape)')
        return directory / '90-connectome-env.conf'

    @staticmethod
    def _file_record(content):
        return {'kind': 'file', 'sha256': hashlib.sha256(content.encode()).hexdigest()}

    @staticmethod
    def _link_record(target):
        return {'kind': 'link', 'target': str(target)}

    def _safe_parent(self, path):
        if path.parent.resolve() != path.parent:
            raise DeployError('refusing symlinked artifact parent directory')
        if any(parent.exists() and not parent.is_dir() for parent in path.parents):
            raise DeployError('refusing non-directory artifact parent')
        check_ancestors(path)

    def _matches(self, path, record):
        self._safe_parent(path)
        if record['kind'] == 'directory':
            return path.exists() and stat.S_ISDIR(path.lstat().st_mode)
        if record['kind'] == 'link':
            return path.is_symlink() and os.readlink(path) == record['target']
        return (path.exists() and stat.S_ISREG(path.lstat().st_mode) and
                hashlib.sha256(path.read_bytes()).hexdigest() == record['sha256'])

    def _load_ownership(self):
        self.owned = {}
        if self.manifest_path.is_symlink():
            raise DeployError('refusing symlinked ownership manifest')
        if not self.manifest_path.exists():
            return
        if not stat.S_ISREG(self.manifest_path.lstat().st_mode):
            raise DeployError('invalid ownership manifest; regular file required')
        try:
            data = json.loads(self.manifest_path.read_text())
            if (not isinstance(data, dict) or set(data) != {'version', 'entries'} or
                    data['version'] != 1 or not isinstance(data['entries'], list)):
                raise ValueError()
            for entry in data['entries']:
                if not isinstance(entry, dict):
                    raise ValueError()
                path = Path(entry['path'])
                record = {key: value for key, value in entry.items() if key != 'path'}
                valid = (set(record) == {'kind', 'target'} and record['kind'] == 'link' and
                         isinstance(record['target'], str)) or (
                             set(record) == {'kind', 'sha256'} and record['kind'] == 'file' and
                             re.fullmatch(r'[a-f0-9]{64}', record['sha256'])) or (
                                 record == {'kind': 'directory'} and path.parent == self.builds and
                                 re.fullmatch(r'[a-f0-9]{64}', path.name))
                if (not valid or not path.is_absolute() or str(path) != os.path.abspath(path) or
                        path in self.owned or not (path == self.current or
                        path.is_relative_to(self.builds) or path.is_relative_to(self.unit_dir))):
                    raise ValueError()
                self.owned[path] = record
        except (ValueError, TypeError, KeyError):
            raise DeployError('invalid ownership manifest; operator recovery required') from None
        self._verify_ownership()

    def _check_build_ownership(self):
        if self.build.exists() and self.owned.get(self.build) != {'kind': 'directory'}:
            raise DeployError('refusing unmanaged build directory; no matching ownership manifest entry')

    def _missing_unit_link(self, path, record):
        # systemctl disable removes unit/enablement symlinks. Recreate only
        # recorded, absent unit-dir links; never adopt a replacement or a file.
        return (record['kind'] == 'link' and path.is_relative_to(self.unit_dir) and
                path.name.endswith('.service') and not path.exists() and not path.is_symlink())

    def _verify_ownership(self):
        for path, record in self.owned.items():
            if not self._matches(path, record) and not self._missing_unit_link(path, record):
                raise DeployError('refusing unmanaged, modified or missing artifact; ownership mismatch')

    def _save_ownership(self):
        atomic_write(self.manifest_path, canonical({'version': 1, 'entries': [
            dict(path=str(path), **record) for path, record in sorted(self.owned.items())]}))

    def _put(self, path, record, content=None):
        # Callers preflight the entire transaction before the first mutation.
        self._preflight({path: (record, content)})
        if path in self.owned and self.owned[path] == record and self._matches(path, record):
            return
        if record['kind'] == 'link':
            link(path, record['target'])
        else:
            atomic_write(path, content, 0o644 if path in self.rendered_units else 0o600)
        self.owned[path] = record
        self._save_ownership()

    def _remove(self, path):
        if path not in self.owned:
            raise DeployError('refusing to remove unmanaged or modified artifact')
        if self._missing_unit_link(path, self.owned[path]):
            pass
        elif self._matches(path, self.owned[path]):
            path.unlink()
        else:
            raise DeployError('refusing to remove unmanaged or modified artifact')
        del self.owned[path]
        self._save_ownership()

    def _enable_links(self, name, source):
        # Do not let `systemctl enable` implicitly replace untracked links.
        # Support a small explicit Install subset, with no specifier expansion.
        body = self.rendered_units.get(source)
        if body is None:
            body = source.read_text()
        section = None
        settings = {}
        result = {}
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith(('#', ';')):
                continue
            if line.startswith('[') and line.endswith(']'):
                section = line
                continue
            if section != '[Install]':
                continue
            key, sep, value = line.partition('=')
            key = key.strip()
            if not sep or key not in ('WantedBy', 'RequiredBy', 'Alias'):
                raise DeployError('unsupported unit Install directive; use explicit target links')
            if not value.strip():
                settings[key] = []
            else:
                settings.setdefault(key, []).extend(value.split())
        for key, targets in settings.items():
            for target in targets:
                if not re.fullmatch(r'[A-Za-z0-9_@-]+\.(?:target|service)', target):
                    raise DeployError('unsupported unit Install target or alias')
                if key == 'Alias':
                    path = self._unit_path(target)
                else:
                    suffix = '.wants' if key == 'WantedBy' else '.requires'
                    path = self.unit_dir / (target + suffix) / name
                result[path] = (self._link_record(source), None)
        return result

    def _artifacts(self, units, build):
        result = {}
        for name, source in units.items():
            enable_links = self._enable_links(name, source)
            additions = enable_links if self.activation == 'start' else {}
            if source in self.rendered_units:
                body = self.rendered_units[source]
                additions[source] = (self._file_record(body), body)
            additions[self._unit_path(name)] = (self._link_record(source), None)
            body = ('[Service]\nEnvironment=' +
                    'DATA_DIR=' + self._systemd_path(self._config(build)['state_dir']) +
                    '\nEnvironmentFile=' +
                    self._systemd_path(str(inside(build, 'secrets.env'))) + '\n')
            if catalog.active(self._config(build)):
                config = self._config(build)
                pins = json.loads(inside(build, 'deployment.json').read_text())['pins']
                unit_body = self.rendered_units.get(source, None)
                if unit_body is None:
                    unit_body = source.read_text()
                cwd = catalog.audit_unit(unit_body, self.root, Path(config['state_dir']), pins['platform'])
                body += catalog.preflight_unit(config, build, cwd, pins['platform'])
            additions[self._dropin(name)] = (self._file_record(body), body)
            for path, artifact in additions.items():
                if path in result and result[path] != artifact:
                    raise DeployError('conflicting unit installation paths')
                result[path] = artifact
        return result

    def _preflight(self, artifacts):
        base_path(self.unit_dir)
        self._verify_ownership()
        for path in artifacts:
            self._safe_parent(path)
            if path not in self.owned and (path.exists() or path.is_symlink()):
                raise DeployError('refusing unmanaged artifact; no matching ownership manifest entry')

    def _install(self, units, old_units, build):
        if self.activation == 'start':
            self._exclusive_status(refuse=True)
        artifacts = self._artifacts(units, build)
        self._preflight(artifacts)
        for name in old_units.keys() - units.keys():
            if self._unit_path(name) in self.owned:
                # Disable by deleting only our recorded enable links below;
                # systemctl disable may delete additional, unowned aliases.
                if self.activation == 'start':
                    run(self.systemctl + ['stop', name])
        # Remove only exact manifest-owned unit-dir artifacts, including enable
        # links. Generated unit files in old builds remain tracked and retained.
        for path in list(self.owned):
            if path.is_relative_to(self.unit_dir) and path not in artifacts:
                self._remove(path)
                if path.parent != self.unit_dir and path.parent.exists() and not list(path.parent.iterdir()):
                    path.parent.rmdir()
        if artifacts:
            make_directory(self.unit_dir)
        for path, (record, content) in artifacts.items():
            self._put(path, record, content)
        run(self.systemctl + ['daemon-reload'])
        if self.activation == 'start':
            self._exclusive_status(refuse=True)
            for name in units:
                run(self.systemctl + ['restart', name])

    def _activation_plan(self):
        lock = self._lock()
        # The legacy bridge trusts host-installed servers as root-maintained
        # files. Catalog mode instead verified its own digest-pinned inventory
        # and exact engine/server paths in _lock above.
        if not catalog.active(self.config) and (lock.get('external') or self.config.get('external_data_args')):
            try:
                recipe = json.loads(inside(self.build, 'cook-recipe.json').read_text())
            except (OSError, ValueError):
                raise DeployError('external servers require the installed recipe', 'E_LOCK_INVALID') from None
            data_args = self.config.get('external_data_args', [])
            kinds = {(entry['server'], entry['path']): entry['kind'] for entry in data_args}
            for name, path, arg, private_data in external_paths(lock, recipe, data_args):
                if private_data:
                    check_external_data_arg(name, path, kind=kinds[(name, path)])
                else:
                    check_external_command(name, path, arg)
        units = self._units(self.build, lock)
        previous = self.current.resolve() if self.current.is_symlink() else None
        previous_lock = self._lock(previous) if previous else None
        previous_units = self._units(previous, previous_lock) if previous else {}
        if self.activation == 'install_only' and not self.check:
            self._refuse_active(units.keys() | previous_units.keys())
        artifacts = self._artifacts(units, self.build)
        # Include current and generated units before env/pointer/service changes.
        self._preflight(artifacts | {
            self.current: (self._link_record(self.build), None)})
        return lock, units, previous, previous_lock, previous_units, artifacts

    def activate(self, secrets=None):
        self.validate_paths()
        self.preflight_activation()
        self._check_secrets_file()
        if secrets is None:
            secrets = {} if self.check else self._read_secrets()
        self.rendered_units = {}
        self._load_ownership()
        self._check_build_ownership()
        if set(secrets) != set(self.names) and not self.check:
            raise DeployError('secret provider must return exactly the declared names')
        if any(not isinstance(v, str) for v in secrets.values()):
            raise DeployError('secret provider must return exactly the declared names')
        if any(any(c in secrets[name] for c in ('\x00', '\n', '\r'))
               for name in direct_secret_names(self.config) if name in secrets):
            raise DeployError('multiline/NUL env secrets are unsupported')
        if self.check and not (self.build / '.prepared').is_file():
            return {'changed': True}
        lock, units, previous, previous_lock, previous_units, artifacts = self._activation_plan()
        file_snapshot, file_names = self._secret_file_snapshot(previous, self.config)
        file_values = {record['env']: secrets[record['secret']].encode('utf-8')
                       for record in self.config.get('secret_files', [])} if not self.check else {}
        direct = set(direct_secret_names(self.config))
        env_values = {name: value for name, value in secrets.items() if name in direct}
        env_values.update({record['env']: self._systemd_path(str(self._secret_file_path(record['env'])))
                           for record in self.config.get('secret_files', [])})
        env = ''.join(name + '=' + '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"\n'
                      for name, value in sorted(env_values.items()))
        env_path = inside(self.build, 'secrets.env')
        same_env = (env_path.is_file() and env_path.read_text() == env and
                    env_path.stat().st_mode & 0o777 == 0o600)
        same_units = all(path in self.owned and self.owned[path] == record and self._matches(path, record)
                         for path, (record, _) in artifacts.items())
        retired = any(path.is_relative_to(self.unit_dir) and path not in artifacts for path in self.owned)
        same_activation = self._staged() == (self.activation == 'install_only')
        if self.check:
            return {'changed': previous != self.build or not same_units or retired or not same_activation}
        self._ensure_state()
        if (previous == self.build and same_env and same_units and not retired and
                file_values == file_snapshot and same_activation):
            if file_names:
                self._set_secret_files(file_values, file_names)
            self._health(units, lock, self.build)
            return self._status({'changed': False})
        old_env = env_path.read_text() if env_path.exists() else None
        atomic_write(env_path, env)
        step = 'secret files'
        try:
            if file_names:
                self._set_secret_files(file_values, file_names)
            step = 'release pointer'
            self._put(self.current, self._link_record(self.build))
            step = 'unit installation'
            self._install(units, previous_units, self.build)
            step = 'health'
            self._health(units, lock, self.build)
            step = 'activation state'
            if self.activation == 'install_only':
                atomic_write(self.state / 'activation', 'install_only\n')
            else:
                (self.state / 'activation').unlink(missing_ok=True)
        except Exception as error:
            # Use only closed diagnostics, never exception text, command argv,
            # stderr or environment values. Preserve the *first* failure even
            # when recovery fails too; public() still uses the existing code.
            cause = (error if isinstance(error, UnitVerificationFailed) else
                     DeployError(SafeError.public(error)['msg'], error.code)
                     if isinstance(error, SafeError) else
                     DeployError('unexpected activation exception (details redacted)'))
            try:
                if file_names:
                    self._set_secret_files(file_snapshot, file_names)
                if previous:
                    if previous == self.build and old_env is not None:
                        atomic_write(env_path, old_env)
                    self._put(self.current, self._link_record(previous))
                    self._install(previous_units, units, previous)
                    self._health(previous_units, previous_lock, previous)
                else:
                    self._remove(self.current)
                    self._install({}, units, self.build)
            except Exception:
                raise ActivationFailed(step, cause, restored=False) from cause
            raise ActivationFailed(step, cause, restored=True) from cause
        return self._status({'changed': True, 'build_dir': str(self.build), 'build_id': lock['build']['id']})
