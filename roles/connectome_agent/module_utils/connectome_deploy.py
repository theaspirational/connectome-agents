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
    from ansible.module_utils.connectome_lock import read_lock, LockError
except ImportError:
    from connectome_lock import read_lock, LockError


class DeployError(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def atomic_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def link(path, target):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    identifier(config.get('name', 'example'))
    for name in config.get('connectors', {}):
        identifier(name)


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


def run(argv, cwd=None):
    # Never relay stdout/stderr: upstream tools may print resolved credentials.
    result = subprocess.run(argv, cwd=cwd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False)
    if result.returncode:
        raise DeployError('command failed (output redacted)')


def secret_names(config):
    entries = list(config.get('connectors', {}).values()) + list(config.get('modules', {}).values())
    names = set(config.get('required_secrets', []))
    for entry in entries:
        names.update(entry.get('required_secrets', []))
    if any(not isinstance(name, str) or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', name) for name in names):
        raise DeployError('invalid secret name')
    if names & {'DATA_DIR', 'WORKSPACE_DIR'}:
        raise DeployError('secret names overlap runtime state environment')
    return sorted(names)


def resolve_pins(config, versions):
    runtime = versions['runtime']
    result = {'runtime': runtime, 'connectors': {}}
    for name, entry in config.get('connectors', {}).items():
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
                 check=False, base=None):
        # This constructor must stay mutation-free: the Ansible boundary creates
        # the root/serialization lock only AFTER all identifiers are validated.
        validate_config(config)
        self.base = base_path(base if base is not None else root)
        self.root = inside(self.base, Path(os.path.abspath(Path(root).expanduser())))
        self.unit_dir = base_path(unit_dir)
        self.config = config
        self.pins = resolve_pins(config, versions)
        self.cook_command = cook_command
        self.systemctl = systemctl or ['systemctl', '--user']
        self.check = check
        self.rendered_units = {}
        self.names = secret_names(config)
        self.spec = {'config': config, 'pins': self.pins}
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
        self._validate_state()
        self.manifest_path = self.state / 'ownership.json'
        self.lock_path()

    def validate_paths(self):
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
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
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
            raise DeployError('secrets file check failed: require non-symlink directory 0700 and regular file 0600, both owned by the agent account (see plan secrets_file); not repaired')

    def _read_secrets(self):
        self._check_secrets_file()
        try:
            values = json.loads(self.secrets_file.read_text())
            return {name: values[name] for name in self.names}
        except (OSError, ValueError, KeyError, TypeError):
            raise DeployError('agent secrets file lookup failed (values redacted)') from None

    def plan(self):
        return {'state_dir': str(self.state_dir), 'secrets_file': str(self.secrets_file),
                'budgets': self.config.get('budgets', {})}

    def lock_path(self):
        return inside(self.root, '.deploy-lock')

    def _lock(self, build=None):
        build = build or self.build
        inside(self.builds, build)
        try:
            data = read_lock(inside(build, 'connectome.lock'),
                             host_pin=self.pins['runtime']['connectome-host'] if build == self.build else None,
                             required_names=self.names if build == self.build else ())
        except LockError as error:
            raise DeployError(str(error)) from None
        state = data.get('state')
        if not state:
            raise DeployError('cook lock must declare configured external state_dir')
        if state:
            path = Path(state['path'])
            expected = self.state_dir if build == self.build else Path(self._config(build)['state_dir'])
            if not path.is_absolute() or state['path'] != str(expected):
                raise DeployError('cook state does not match configured state_dir')
            base_path(path)  # Previous/rollback locks must not alias state either.
            if path.resolve().is_relative_to(self.root.resolve()):
                raise DeployError('state must be outside deployment root/builds')
        return data

    def prepare(self):
        self.validate_paths()
        self._check_secrets_file()
        self._load_ownership()
        self._check_build_ownership()
        if not self.check:
            self._ensure_state()
        marker = inside(self.build, '.prepared')
        if marker.is_file() and marker.read_text() == self.request_id:
            self._activation_plan()
            return {'changed': False, 'build_dir': str(self.build), 'secret_names': self.names, 'plan': self.plan()}
        if self.check:
            return {'changed': True, 'build_dir': str(self.build), 'secret_names': self.names, 'plan': self.plan()}
        if not self.cook_command:
            raise DeployError('configure a pinned cook adapter command')
        if self.current.is_symlink() and self.current.resolve() == self.build:
            raise DeployError('refusing to rebuild active directory')
        if any(path != self.build and path.is_relative_to(self.build) for path in self.owned):
            raise DeployError('refusing to rebuild a directory with manifest-owned unit files')
        if self.build.exists():
            # _check_build_ownership refused unknown directories, even if they
            # contained a plausible .prepared marker. Only our own incomplete,
            # inactive request directory may be recursively discarded.
            shutil.rmtree(self.build)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.builds.mkdir(exist_ok=True, mode=0o700)
        self.build.mkdir(mode=0o700)
        self.state.mkdir(exist_ok=True, mode=0o700)
        self.owned[self.build] = {'kind': 'directory'}
        self._save_ownership()  # persist ownership before invoking cook
        recipe = inside(self.build, 'deployment.json')
        atomic_write(recipe, canonical(self.spec))
        substitutions = {'recipe': str(recipe),
                         'build_dir': str(self.build), 'state_dir': str(self.state_dir)}
        run([arg.format_map(substitutions) for arg in self.cook_command], cwd=self.build)
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
                    'WorkingDirectory=' + self._systemd_quote(self._config(build)['state_dir']) + '\nExecStart=' +
                    self._systemd_quote(str(script)) + '\nRestart=on-failure\n[Install]\nWantedBy=default.target\n')
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
    def _systemd_quote(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'

    def _health(self, units, lock, build):
        for name in units:
            run(self.systemctl + ['is-active', '--quiet', name])
        health = lock.get('health', self._config(build).get('health', {}))
        if health.get('command'):
            if not isinstance(health['command'], list):
                raise DeployError('health command must be argv')
            run(health['command'], cwd=build)

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

    def _verify_ownership(self):
        for path, record in self.owned.items():
            if not self._matches(path, record):
                raise DeployError('refusing unmanaged, modified or missing artifact; ownership mismatch')

    def _save_ownership(self):
        atomic_write(self.manifest_path, canonical({'version': 1, 'entries': [
            dict(path=str(path), **record) for path, record in sorted(self.owned.items())]}))

    def _put(self, path, record, content=None):
        # Callers preflight the entire transaction before the first mutation.
        self._preflight({path: (record, content)})
        if path in self.owned and self.owned[path] == record:
            return
        if record['kind'] == 'link':
            link(path, record['target'])
        else:
            atomic_write(path, content, 0o644 if path in self.rendered_units else 0o600)
        self.owned[path] = record
        self._save_ownership()

    def _remove(self, path):
        if path not in self.owned or not self._matches(path, self.owned[path]):
            raise DeployError('refusing to remove unmanaged or modified artifact')
        path.unlink()
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
            additions = self._enable_links(name, source)
            if source in self.rendered_units:
                body = self.rendered_units[source]
                additions[source] = (self._file_record(body), body)
            additions[self._unit_path(name)] = (self._link_record(source), None)
            body = ('[Service]\nEnvironment=' +
                    self._systemd_quote('DATA_DIR=' + self._config(build)['state_dir']) +
                    '\nEnvironmentFile=' +
                    self._systemd_quote(str(inside(build, 'secrets.env'))) + '\n')
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
        artifacts = self._artifacts(units, build)
        self._preflight(artifacts)
        for name in old_units.keys() - units.keys():
            if self._unit_path(name) in self.owned:
                # Disable by deleting only our recorded enable links below;
                # systemctl disable may delete additional, unowned aliases.
                run(self.systemctl + ['stop', name])
        # Remove only exact manifest-owned unit-dir artifacts, including enable
        # links. Generated unit files in old builds remain tracked and retained.
        for path in list(self.owned):
            if path.is_relative_to(self.unit_dir) and path not in artifacts:
                self._remove(path)
                if path.parent != self.unit_dir and not list(path.parent.iterdir()):
                    path.parent.rmdir()
        for path, (record, content) in artifacts.items():
            self._put(path, record, content)
        run(self.systemctl + ['daemon-reload'])
        for name in units:
            run(self.systemctl + ['restart', name])

    def _activation_plan(self):
        lock = self._lock()
        units = self._units(self.build, lock)
        previous = self.current.resolve() if self.current.is_symlink() else None
        previous_lock = self._lock(previous) if previous else None
        previous_units = self._units(previous, previous_lock) if previous else {}
        artifacts = self._artifacts(units, self.build)
        # Include current and generated units before env/pointer/service changes.
        self._preflight(artifacts | {
            self.current: (self._link_record(self.build), None)})
        return lock, units, previous, previous_lock, previous_units, artifacts

    def activate(self, secrets=None):
        self.validate_paths()
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
        if any(any(c in value for c in ('\x00', '\n', '\r')) for value in secrets.values()):
            raise DeployError('multiline/NUL env secrets are unsupported')
        if self.check and not (self.build / '.prepared').is_file():
            return {'changed': True}
        lock, units, previous, previous_lock, previous_units, artifacts = self._activation_plan()
        env = ''.join(name + '=' + '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"\n'
                      for name, value in sorted(secrets.items()))
        env_path = inside(self.build, 'secrets.env')
        same_env = (env_path.is_file() and env_path.read_text() == env and
                    env_path.stat().st_mode & 0o777 == 0o600)
        same_units = all(path in self.owned and self.owned[path] == record
                         for path, (record, _) in artifacts.items())
        retired = any(path.is_relative_to(self.unit_dir) and path not in artifacts for path in self.owned)
        if self.check:
            return {'changed': previous != self.build or not same_units or retired}
        self._ensure_state()
        if previous == self.build and same_env and same_units and not retired:
            self._health(units, lock, self.build)
            return {'changed': False}
        old_env = env_path.read_text() if env_path.exists() else None
        atomic_write(env_path, env)
        try:
            self._put(self.current, self._link_record(self.build))
            self._install(units, previous_units, self.build)
            self._health(units, lock, self.build)
        except Exception:
            try:
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
                raise DeployError('activation failed; rollback recovery also failed') from None
            raise DeployError('activation failed; previous deployment restored') from None
        return {'changed': True, 'build_dir': str(self.build), 'build_id': lock['build']['id']}
