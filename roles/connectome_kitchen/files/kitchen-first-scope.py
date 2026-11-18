"""First-install ownership checks. Never follows an artifact symlink or prints file contents."""
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path


class Refused(Exception):
    pass


def require(ok, reason):
    if not ok:
        raise Refused(reason)


def env(key):
    return os.environ['KITCHEN_' + key]


def literal(value):
    path = Path(value)
    require(re.fullmatch(r'/[A-Za-z0-9_./:@+-]+', value) and str(path) == value and '..' not in path.parts,
            'first-install paths must be canonical absolute paths')
    return path


def info(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def directory(path):
    for parent in (*reversed(path.parents), path):
        entry = info(parent)
        require(entry is None or stat.S_ISDIR(entry.st_mode), 'redirected or non-directory path: ' + str(parent))
    return info(path)


def regular(path, owner, mode=None):
    entry = info(path)
    require(entry and stat.S_ISREG(entry.st_mode) and entry.st_uid == owner and entry.st_nlink == 1
            and (mode is None or stat.S_IMODE(entry.st_mode) == mode),
            'unsafe or missing file: ' + str(path))


def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


def identity(entry):
    return None if entry is None else [entry.st_dev, entry.st_ino]


def mounts_below(path):
    # Includes bind mounts, which --one-file-system alone cannot distinguish.
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        mount = Path(re.sub(r'\\([0-7]{3})', lambda hit: chr(int(hit[1], 8)), line.split()[4]))
        require(mount != path and path not in mount.parents, 'mounted entry in removal scope: ' + str(mount))


def main(mode, build, state_flag):
    home, root, state, unit_dir, secrets, save = (literal(env(key)) for key in
                                                  ('ACCOUNT_HOME', 'ROOT', 'STATE_DIR', 'UNIT_DIR', 'SECRETS_FILE', 'SAVE'))
    name, account, uid, operator = env('NAME'), env('ACCOUNT'), int(env('UID')), int(env('OPERATOR_UID'))
    unit = 'connectome-' + name + '.service'
    log = home / 'kitchen-takeover'
    runtime = literal(env('RUNTIME_DIR'))
    require(root.name == name and all(home in path.parents for path in (root, state, unit_dir, secrets))
            and not overlaps(home, save), 'deployment scopes must be under the account home and save separate')
    scopes = (root, state, unit_dir, secrets, save, log, runtime)
    for i, a in enumerate(scopes):
        for b in scopes[i + 1:]:
            require(not overlaps(a, b), 'overlapping first-install scopes: %s and %s' % (a, b))
    for value in json.loads(env('PROTECTED_PATHS')):
        if value:
            # Installed tool paths may legitimately use symlinks; protect the path and its destination.
            protected = Path(value)
            for path in (protected, protected.resolve()):
                for scope in (root, state):
                    require(not overlaps(scope, path), 'deployment overlaps a protected input: ' + str(scope))

    root_entry, state_entry, _, save_entry = map(directory, (root, state, unit_dir, save))
    directory(log / name / 'inputs')
    directory(unit_dir / 'default.target.wants')
    dropin_dir = unit_dir / (unit + '.d')
    directory(dropin_dir)
    directory(secrets.parent)
    if info(secrets) is not None:
        regular(secrets, uid)
    for path in (root, state, unit_dir):
        mounts_below(path)
    if root_entry:
        require(root_entry.st_uid == uid, 'deployment root is not owned by the account')
    if state_entry:
        require(state_entry.st_uid == uid, 'state is not owned by the account')
    if save_entry:
        require(save_entry.st_uid == operator and stat.S_IMODE(save_entry.st_mode) == 0o700,
                'save must belong to the operator, mode 0700')
    if operator != uid:
        # An agent must not be able to replace the operator's evidence by renaming an ancestor.
        for parent in save.parents:
            entry = info(parent)
            require(entry and entry.st_uid == operator and not entry.st_mode & 0o022
                    or entry is None, 'save has a writable or non-operator-owned ancestor')
    if mode == 'scope':
        return

    builds, current = root / 'builds', root / 'current'
    unit_link, wants, dropin = unit_dir / unit, unit_dir / 'default.target.wants' / unit, dropin_dir / '90-connectome-env.conf'
    marker = save / 'first-install'
    present = info(marker) is not None
    require(info(save / 'links') is None, 'save holds the save of a takeover')
    if mode == 'fresh':
        require(not present and (root_entry is None or not any(root.iterdir())), 'refusing to claim a nonempty deployment root')
        require(all(info(save / leaf) is None for leaf in ('verified', 'ownership.prepared.sha256')),
                'save contains other deployment records')
        return
    if not present and mode == 'remove' and not state_flag and root_entry is None and not any(map(info, (unit_link, wants, dropin_dir))):
        print('absent')
        return
    require(present or mode == 'create', 'save holds no marker of a first install')
    require(re.fullmatch(r'[a-f0-9]{64}', build), 'first-install operation needs the expected build pin')

    binding = dict(version=2, name=name, account=account, uid=uid, build=build, home=str(home),
                   root=str(root), unit_dir=str(unit_dir), state=str(state), secrets=str(secrets), save=str(save),
                   runtime=str(runtime), protected=json.loads(env('PROTECTED_PATHS')))
    record = {}
    if present:
        regular(marker, operator, 0o600)
        record = json.loads(marker.read_bytes())
        require(isinstance(record, dict) and all(record.get(key) == value for key, value in binding.items()),
                'first-install marker does not match the deployment paths and expected build')
        require(root_entry is None or record.get('root_identity') == identity(root_entry),
                'deployment root was replaced since it was held')
    if mode == 'create':
        require(not present and root_entry and not any(root.iterdir()), 'deployment root is no longer fresh')
        save_marker(marker, dict(binding, root_identity=identity(root_entry), state_identity=identity(state_entry)))
        return

    require(mode in ('remove', 'deactivate', 'recheck', 'removing') or root_entry, 'held root has disappeared; remove before preparing again')
    target = builds / build
    builds_entry, target_entry = directory(builds), directory(target)
    if builds_entry:
        require(all(path == target for path in builds.iterdir()),
                'builds holds more than the expected build (or a different build)')
    if target_entry:
        require(target_entry.st_uid == uid and record.get('build_identity', identity(target_entry)) == identity(target_entry),
                'the expected build directory was replaced')
    if state_flag and state_entry:
        require(record.get('state_identity') == identity(state_entry), 'state directory was not held or was replaced')

    # The root is dedicated, but unit files live in a shared tree. Require engine ownership and independent
    # exact path/type/target checks there. Missing entries are permitted for retries of interrupted cleanup.
    manifest = root / 'state' / 'ownership.json'
    directory(manifest.parent)
    owned = {}
    if info(manifest):
        regular(manifest, uid)
        data = json.loads(manifest.read_bytes())
        require(data.get('version') == 1 and isinstance(data.get('entries'), list), 'invalid engine ownership record')
        for entry in data['entries']:
            path = literal(entry['path'])
            require(path not in owned and (path == current or path == target or target in path.parents or path in (unit_link, wants, dropin)),
                    'unexpected engine-owned path')
            owned[path] = entry

    if target_entry:
        require((mode in ('remove', 'recheck', 'removing') and record.get('removing') and record.get('build_identity') == identity(target_entry))
                or owned.get(target) == {'path': str(target), 'kind': 'directory'}, 'build has no first-install ownership evidence')
    source = target / 'managed-units' / unit
    if info(unit_link) and unit_link.is_symlink() and os.readlink(unit_link) == str(target / unit):
        source = target / unit  # engine-generated legacy launcher
    elif not info(unit_link) and owned.get(unit_link, {}).get('target') == str(target / unit):
        source = target / unit
    for path, expected in ((current, target), (unit_link, source), (wants, source)):
        if info(path):
            require(path.is_symlink() and info(path).st_uid == uid and os.readlink(path) == str(expected)
                    and owned.get(path) == dict(path=str(path), kind='link', target=str(expected)),
                    'unowned or redirected link: ' + str(path))
    if info(unit_link) or info(wants):
        directory(source.parent)
        regular(source, uid)
        require(owned.get(source) == dict(path=str(source), kind='file', sha256=hashlib.sha256(source.read_bytes()).hexdigest()),
                'installed unit source does not match its ownership record')
    if info(dropin):
        regular(dropin, uid)
        body = ('[Service]\nEnvironment=DATA_DIR=%s\nEnvironmentFile=%s\n' % (state, target / 'secrets.env')).encode()
        require(dropin.read_bytes() == body and owned.get(dropin) ==
                dict(path=str(dropin), kind='file', sha256=hashlib.sha256(body).hexdigest()),
                'drop-in does not belong to the expected build')

    for leaf in ('verified', 'ownership.prepared.sha256'):
        if info(save / leaf):
            regular(save / leaf, operator, 0o600)
    if mode == 'claim':
        require(target_entry is not None, 'prepared build is missing')
        save_marker(marker, dict(record, build_identity=identity(target_entry), state_identity=identity(state_entry)))
    if mode == 'removing':
        save_marker(marker, dict(record, removing=True, build_identity=identity(target_entry) if target_entry else record.get('build_identity')))
    if mode in ('remove', 'deactivate', 'recheck'):
        # The shell checks the manager's fragment before asking it to stop anything.
        print(source)
        print('owned-dropin' if dropin in owned else 'no-owned-dropin')
        print('owned-unit' if owned.get(unit_link) == dict(path=str(unit_link), kind='link', target=str(source)) else 'no-owned-unit')


def save_marker(path, record):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode='w') as temp:
        try:
            json.dump(record, temp, sort_keys=True)
            temp.flush()
            os.replace(temp.name, path)
        finally:
            if os.path.exists(temp.name):
                os.unlink(temp.name)


if __name__ == '__main__':
    try:
        main(sys.argv[1], sys.argv[2], sys.argv[3] == 'state')
    except (Refused, OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as error:
        # Parser and I/O errors may refer to inputs; do not echo their text.
        print(str(error) if isinstance(error, Refused) else 'first-install paths or ownership records could not be validated')
        sys.exit(60)
