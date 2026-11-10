"""Digest-pinned, agent-local artifact closure. No acquisition or secret lookup.

Catalogs are trusted executable policy, not user templates. Archives must contain
all build inputs. The path audit is a launch-surface check, not a code sandbox.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

try:
    from ansible.module_utils.connectome_archive import ArchiveError
except ImportError:
    from connectome_archive import ArchiveError


def need(ok, message):
    if not ok:
        raise ArchiveError(message, 'E_CATALOG_INVALID')


def active(config):
    return 'recipe_file' not in config and bool(config.get('catalog'))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def descriptor_digest(entry):
    return hashlib.sha256(canonical({k: v for k, v in entry.items() if k != 'enabled'})).hexdigest()


def digest(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) and value != '0' * 64


def relative(value):
    return (isinstance(value, str) and bool(value) and
            re.fullmatch(r'[A-Za-z0-9_.+@/-]+', value) and
            not PurePosixPath(value).is_absolute() and
            all(p not in ('', '.', '..') for p in value.split('/')))


def entries_for(config):
    result = dict(config.get('catalog', {}))
    need(not result.keys() & config.get('connectors', {}).keys(), 'duplicate artifact name')
    result.update(config.get('connectors', {}))
    return result


def kind(entry):
    return entry.get('kind', 'connector')


def requires(entry):
    return entry.get('requires', ['node', 'bun'] if kind(entry) == 'connector' else [])


def resolve(config, versions):
    entries = entries_for(config)
    need(isinstance(entries, dict) and entries, 'selected catalog entries required')
    result = {}
    for name, entry in entries.items():
        need(isinstance(name, str) and re.fullmatch('[a-z0-9][a-z0-9_-]{0,62}', name), 'invalid artifact name')
        need(isinstance(entry, dict) and 'pin_ref' in entry and
             entry.keys() <= {'kind', 'requires', 'enabled', 'build', 'exports', 'settings', 'run', 'required_secrets', 'pin_ref', 'repo'},
             'closed catalog descriptor required')
        need(entry.get('enabled', True) is True and kind(entry) in
             ('runtime', 'connector', 'adapter', 'preflight'), 'invalid artifact kind')
        need(isinstance(requires(entry), list) and all(isinstance(n, str) and n in entries
             for n in requires(entry)), 'selected artifact dependency missing')
        exports = entry.get('exports', {})
        need(isinstance(exports, dict) and all(isinstance(k, str) and relative(v)
             for k, v in exports.items()), 'invalid artifact exports')
        need(isinstance(entry.get('build', []), list), 'build must be an argv list')
        for argv in entry.get('build', []) + ([entry['run']] if 'run' in entry else []):
            need(isinstance(argv, list) and argv and all(isinstance(a, str) and a and
                 not any(ord(c) < 32 for c in a) for a in argv), 'artifact commands must be argv')
        need((kind(entry) == 'preflight') == ('run' in entry), 'preflight requires run argv')
        section = 'connectors' if kind(entry) == 'connector' else 'artifacts'
        need(entry['pin_ref'] == section + '.' + name, 'pin_ref must match artifact identity')
        pin = versions.get('artifacts', {}).get(name, versions.get(section, {}).get(name))
        need(isinstance(pin, dict) and set(pin) == {'archive', 'sha256', 'descriptor_sha256', 'version'} | ({'commit'} if kind(entry) == 'connector' else set()) and
             relative(pin['archive']) and pin['archive'].startswith('inputs/') and
             digest(pin['sha256']) and digest(pin['descriptor_sha256']) and
             isinstance(pin['version'], str) and re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?', pin['version']),
             'unresolved artifact pin; supply archive, version and both digests')
        if kind(entry) == 'connector':
            try:
                from ansible.module_utils.connectome_lock import repository
            except ImportError:
                from connectome_lock import repository
            need(repository(entry.get('repo')), 'connector requires reviewed repository identity')
            need(isinstance(pin['commit'], str) and re.fullmatch('[a-f0-9]{40}', pin['commit']) and
                 pin['commit'] != '0' * 40, 'unresolved connector source commit')
        need(pin['descriptor_sha256'] == descriptor_digest(entry), 'catalog descriptor digest mismatch')
        result[name] = deepcopy(pin)
    order(entries)  # cycles fail before IO
    for name in ('node', 'bun'):
        need(name in entries and kind(entries[name]) == 'runtime' and
             name in entries[name].get('exports', {}), 'catalog requires pinned node and bun exports')
    need(result['node']['version'].split('.')[0] == '24', 'catalog requires Node v24')
    platform = versions.get('platform', {})
    need(isinstance(platform, dict), 'platform closure must be a mapping')
    for path, sha in platform.items():
        need(isinstance(path, str) and path.startswith('/') and Path(path).as_posix() == os.path.normpath(path)
             and Path(path).parent in (Path('/bin'), Path('/usr/bin'), Path('/usr/local/bin'))
             and digest(sha), 'unresolved or non-platform tool pin')
    return {'artifacts': result, 'platform': deepcopy(platform)}


def order(entries):
    result, visiting = [], set()
    def visit(name):
        need(name not in visiting, 'artifact dependency cycle')
        if name in result:
            return
        visiting.add(name)
        for dep in requires(entries[name]):
            visit(dep)
        visiting.remove(name)
        result.append(name)
    for name in sorted(entries):
        visit(name)
    return result


def expand(value, build, entries):
    if isinstance(value, dict):
        return {k: expand(v, build, entries) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, build, entries) for v in value]
    if not isinstance(value, str):
        return value
    def replace(match):
        name = match[1]
        need(name in entries, 'reference to unselected artifact')
        return str(Path(build) / 'catalog' / name)
    result = re.sub(r'\{artifact:([a-z0-9_-]+)\}', replace, value)
    need('{artifact:' not in result, 'malformed artifact reference')
    return result


def stream_sha256(stream):
    result = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        result.update(block)
    return result.hexdigest()


def sha256(path):
    with open(path, 'rb') as stream:
        return stream_sha256(stream)


def check_platform(pins):
    for path, sha in pins.get('platform', {}).items():
        need(Path(path).is_file() and Path(path).resolve().parent in
             (Path('/bin'), Path('/usr/bin'), Path('/usr/local/bin')) and
             sha256(path) == sha, 'platform digest mismatch or non-platform target')


# Absolute references inside argv, env assignments, PATH lists, JSON and unit
# directives. URLs are removed first; they are endpoints, not local file paths.
ABSOLUTE = re.compile(r'(?<![A-Za-z0-9_./-])/+(?:[A-Za-z0-9_.+@%${}-]+/?)*')


def audit(value, root, state, platform):
    if isinstance(value, dict):
        for key, child in value.items():
            audit(key, root, state, platform)
            audit(child, root, state, platform)
    elif isinstance(value, list):
        for child in value:
            audit(child, root, state, platform)
    elif isinstance(value, str):
        need(value != '/', 'root dependency refused')
        need(not re.search(r'file://(?!/)', value), 'nonlocal file URI refused')
        value = value.replace('file://', '')
        need('file:' not in value or 'file:/' in value, 'relative file URI refused')
        value = re.sub(r'\b(?:https?|wss?)://[^\s\"\'<>]+', '', value)
        for match in ABSOLUTE.finditer(value):
            raw = match[0].rstrip('/')
            need(bool(raw), 'root dependency refused')
            path = Path(raw)
            need('..' not in path.parts and not any(c in raw for c in '$%{}'), 'dynamic or escaping absolute path')
            need((path.is_relative_to(root) and path.resolve().is_relative_to(root.resolve())) or
                 (path.is_relative_to(state) and path.resolve().is_relative_to(state.resolve())) or
                 raw in platform, 'absolute dependency outside agent tree and pinned platform closure')


def tree_inventory(directory):
    need(directory.is_dir() and directory.resolve() == directory, 'artifact directory alias refused')
    result = {}
    for path in sorted(directory.rglob('*')):
        need(not path.is_symlink(), 'artifact symlinks are refused')
        if path.is_dir():
            continue
        need(path.is_file(), 'artifact special file refused')
        result[str(path.relative_to(directory))] = {'sha256': sha256(path), 'mode': path.stat().st_mode & 0o777}
    return result


def extract(path, target, expected):
    need(path.resolve() == path and path.is_file(), 'archive must be an agent-local regular file')
    # Hash and extract the same open file. No links, devices, duplicate members,
    # traversal, owner restoration or unbounded decompression.
    with open(path, 'rb') as stream:
        need(stream_sha256(stream) == expected, 'artifact archive digest mismatch')
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode='r:*') as archive:
            members, seen, size = [], set(), 0
            for member in archive:
                name = member.name.rstrip('/')
                need(relative(name) and name not in seen and (member.isdir() or member.isfile()), 'unsafe archive member')
                seen.add(name)
                size += member.size
                need(size <= 1024 ** 3 and len(seen) <= 100000, 'artifact extraction limit exceeded')
                members.append((member, name))
            target.mkdir()
            for member, name in members:
                dest = target / name
                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as src, open(dest, 'xb') as dst:
                        import shutil
                        shutil.copyfileobj(src, dst)
                    dest.chmod(0o755 if member.mode & 0o111 else 0o644)


def install(config, pins, build):
    build = Path(build)
    root, state = build.parent.parent, Path(config['state_dir'])
    entries = entries_for(config)
    check_platform(pins)
    directory = build / 'catalog'
    directory.mkdir()
    env = {'HOME': str(build / 'catalog-home'), 'TMPDIR': str(build / 'catalog-tmp'),
           'PATH': '', 'NO_UPDATE_NOTIFIER': '1', 'npm_config_offline': 'true',
           'npm_config_cache': str(build / 'catalog-cache')}
    for key in ('HOME', 'TMPDIR', 'npm_config_cache'):
        Path(env[key]).mkdir()
    for name in order(entries):
        entry, pin = entries[name], pins['artifacts'][name]
        archive = root / pin['archive']
        need(archive.is_relative_to(root) and archive.resolve().is_relative_to(root.resolve()), 'archive escapes agent tree')
        extract(archive, directory / name, pin['sha256'])
        env['PATH'] = os.pathsep.join(str(directory / n / Path(e['exports'][n]).parent)
                                      for n, e in entries.items() if n in ('node', 'bun') and
                                      (directory / n).exists())
        for argv in expand(entry.get('build', []), build, entries):
            if argv[0] in ('bun', 'node'):
                argv[0] = runtime(config, build, argv[0])
            audit(argv, root, state, pins['platform'])
            need(Path(argv[0]).is_absolute() and Path(argv[0]).is_file(), 'build executable must be absolute and installed')
            # Output is private; build scripts cannot print credentials into a result.
            result = subprocess.run(argv, cwd=directory / name, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            need(result.returncode == 0, 'artifact build failed')
        for value in entry.get('exports', {}).values():
            need((directory / name / value).is_file(), 'artifact export missing after build')
    record = {'schema': 'connectome.catalog-lock/v1', 'pins': {k: pins[k] for k in ('artifacts', 'platform')},
              'descriptors': {n: descriptor_digest(e) for n, e in entries.items()},
              'recipe_sha256': sha256(build / 'cook-recipe.json'),
              'files': tree_inventory(directory)}
    (build / 'catalog.lock.json').write_bytes(canonical(record) + b'\n')
    return record


def runtime(config, build, name):
    entry = entries_for(config)[name]
    return str(Path(build) / 'catalog' / name / entry['exports'][name])


def preflights(config, build):
    entries = entries_for(config)
    return [expand(entries[name]['run'], build, entries)
            for name in order(entries) if kind(entries[name]) == 'preflight']


def preflight_unit(config, build):
    lines = []
    for argv in preflights(config, build):
        need(Path(argv[0]).is_absolute(), 'preflight executable must be absolute')
        # No systemd specifiers or environment expansion in executable argv.
        need(all(not any(c in arg for c in '%$\n\r\\\"') for arg in argv), 'unsafe preflight unit argv')
        lines.append('ExecStartPre=' + ' '.join('"' + arg + '"' for arg in argv))
    return '\n'.join(lines) + ('\n' if lines else '')


def verify(config, pins, build, lock=None):
    build = Path(build)
    root, state = build.parent.parent, Path(config['state_dir'])
    try:
        record = json.loads((build / 'catalog.lock.json').read_text())
        expected = {'schema': 'connectome.catalog-lock/v1',
                    'pins': {k: pins[k] for k in ('artifacts', 'platform')},
                    'descriptors': {n: descriptor_digest(e) for n, e in entries_for(config).items()},
                    'recipe_sha256': sha256(build / 'cook-recipe.json'),
                    'files': tree_inventory(build / 'catalog')}
        need(record == expected, 'catalog installed inventory differs from lock')
        check_platform(pins)
        recipe = json.loads((build / 'cook-recipe.json').read_text())
        audit(recipe, root, state, pins['platform'])
        audit(config.get('health', {}), root, state, pins['platform'])
        audit(preflight_unit(config, build), root, state, pins['platform'])
        if lock:
            audit(lock, root, state, pins['platform'])
            need(lock['recipe']['sha256'] == record['recipe_sha256'], 'cook recipe digest mismatch')
            external = [{'name': n, 'command': s['command']} for n, s in sorted(recipe.get('mcpServers', {}).items())]
            need(lock.get('external', []) == external and not lock['components'] and not lock['localExtensions'],
                 'cook closure differs from catalog')
            need(set(lock.get('engines', {})) == {'bun', 'node'}, 'catalog requires engine provenance')
            for name, engine in lock['engines'].items():
                need(engine['path'] == engine['realPath'] == runtime(config, build, name), 'engine outside pinned catalog')
                need(engine['version'].removeprefix('v') == pins['artifacts'][name]['version'], 'engine version mismatch')
            paths = [lock['launch']['script']] + [u['path'] for u in lock.get('units', [])]
            for name in paths:
                path = build / name
                need(path.resolve().is_relative_to(build), 'launch path escapes build')
                audit(path.read_text(), root, state, pins['platform'])
    except (OSError, ValueError, KeyError, TypeError):
        raise ArchiveError('catalog lock or launch surface invalid', 'E_CATALOG_INVALID') from None


def input_paths(config, root):
    """Catalog acquisition inputs must already belong to this agent."""
    settings = config.get('cook', {})
    paths = [settings.get('host_repo')]
    if 'framework_archive' in settings:
        paths.append(settings['framework_archive']['path'])
    paths += [d['archive'] for d in settings.get('host_dependencies', [])]
    for value in paths:
        need(isinstance(value, str) and Path(value).is_absolute(), 'catalog requires local cook inputs')
        path = Path(value)
        need(path.is_relative_to(root) and path.resolve().is_relative_to(root.resolve()) and
             '..' not in path.parts, 'cook input is outside the agent tree')


def environment(config, pins, build):
    """Only installed runtimes and individually pinned platform tools on PATH."""
    build = Path(build)
    tools = build / 'platform-bin'
    tools.mkdir()
    for value in pins['platform']:
        path = Path(value)
        target = tools / path.name
        need(not target.exists() and not target.is_symlink(), 'platform tool basename collision')
        target.symlink_to(path)
    paths = [str(Path(runtime(config, build, name)).parent) for name in ('bun', 'node')]
    paths.append(str(tools))
    return {'PATH': os.pathsep.join(paths), 'HOME': str(build / 'catalog-home'),
            'TMPDIR': str(build / 'catalog-tmp'), 'NO_UPDATE_NOTIFIER': '1',
            'npm_config_offline': 'true', 'npm_config_cache': str(build / 'catalog-cache'),
            'BUN_INSTALL_CACHE_DIR': str(build / 'catalog-cache')}
