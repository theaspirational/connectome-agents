"""Fail-closed reader for cook's host lock v2. Never include input in errors."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


class LockError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise LockError(message)


def text(value):
    return isinstance(value, str) and bool(value) and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)


def digest(value, size=64):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{%d}' % size, value) is not None


def version(value):
    # Cook, npm and engine provenance use concrete versions, never ranges.
    return text(value) and re.fullmatch(
        r'v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
        r'(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?', value) is not None


def credential_shaped(value):
    """Credential shapes covered by the publication gate, plus provider variants.

    Not a guarantee against arbitrary secrets: schemas and names-only inputs
    remain mandatory. Call on every string, not just secret-labelled fields.
    """
    return bool(re.search(
        r'(?:gh[oprsu]_|github_pat_|glpat[-_]|xox[baprs]-|sk-)[A-Za-z0-9_-]{12,}'
        r'|\bhvs\.[A-Za-z0-9_-]+|\b[0-9]{8,10}:[A-Za-z0-9_-]{30,}'
        r'|-----' r'BEGIN .*PRIVATE KEY-----|\beyJ[\w-]+\.[\w-]+\.[\w-]+', value)
        or re.search(r'\b[A-Z_]*(?:TOKEN|PASSWORD|API_KEY|SECRET)\s*[:=]\s*\S+', value, re.I)
        or re.search(r'://[^/\s]*:[^/\s]*@|[?&](?:token|password|secret|api_key)=', value, re.I)
        or re.search(r'(?:vault|secret)/[a-z][\w-]+/[\w/-]+', value, re.I))


def repository(value):
    """Allow only credential-free HTTPS, SSH/scp, or absolute local sources."""
    if not text(value) or credential_shaped(value):
        return False
    decoded = unquote(value)
    if not text(decoded) or credential_shaped(decoded) or any(c in decoded for c in '?#\\'):
        return False
    if value.startswith('/'):
        return True
    if any(c.isspace() for c in decoded):
        return False
    try:
        url = urlsplit(value)
        if url.scheme in {'https', 'ssh'}:
            # Accessing port also rejects malformed/non-numeric port syntax.
            port = url.port
            return bool(url.hostname and url.path and url.password is None
                        and (url.scheme != 'ssh' or not unquote(url.hostname).startswith('-'))
                        and (port is None or 0 < port <= 65535)
                        and (url.username is None or (url.scheme == 'ssh' and re.fullmatch(
                            r'[A-Za-z0-9._][A-Za-z0-9._-]*', unquote(url.username)))))
    except ValueError:
        return False
    # scp syntax has no URI scheme. Refuse known transport/helper prefixes
    # rather than reinterpreting a disallowed URI as an scp hostname.
    if re.match(r'(?:https?|ssh|git|file|ftp|ext):', decoded, re.I):
        return False
    return re.fullmatch(r'(?:[A-Za-z0-9._][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*:(?!:).+', decoded) is not None and '://' not in decoded


def names_only(value):
    """Defense in depth, including all nested strings and mapping keys."""
    if isinstance(value, dict):
        for key, item in value.items():
            names_only(key)
            compact = re.sub('[^a-z]', '', key.lower())
            require(compact not in {'value', 'values', 'secret', 'secrets', 'token',
                                    'password', 'apikey', 'apitoken', 'authtoken', 'secretvalue', 'resolvedvalue'},
                    'lock contains a value-looking secret field')
            names_only(item)
    elif isinstance(value, list):
        for item in value:
            names_only(item)
    elif isinstance(value, str):
        require(not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value), 'lock contains control characters')
        require(not credential_shaped(value), 'lock contains a value-looking secret')


def contained_file(build, value):
    require(text(value), 'lock artifact path required')
    path = build / value
    require(path.resolve().is_relative_to(build.resolve()), 'lock artifact path escapes build')
    require(path.is_file() and not path.is_symlink(), 'lock artifact must be a regular file')
    return path


def map_lock(data, build, host_pin=None, required_names=()):
    """Return the role's deployment record; do not rewrite cook's identity.

    launch.userUnit is cook's actual export hint. The role consumes path/hash
    units, so hash the contained export here, not a guessed upstream units field.
    """
    build = Path(build)
    require(isinstance(data, dict) and type(data.get('version')) is int and data['version'] == 2,
            'host lock v2 required; legacy and unknown versions refused')
    require(data.get('backend') == 'host', 'host lock v2 required')
    required = {'version', 'backend', 'cook', 'recipe', 'recipePath', 'createdAt',
                'connectomeHost', 'components', 'localExtensions', 'requirements',
                'build', 'credentials', 'outputs', 'launch'}
    optional = {'state', 'layout', 'engines', 'hostDependencies', 'incomplete', 'units', 'health'}
    require(required <= data.keys(), 'lock missing required v2 fields')
    require(data.keys() <= required | optional, 'lock contains unsupported fields')
    names_only(data)
    require('incomplete' not in data, 'incomplete cook install refused')
    identity = data['build']
    require(isinstance(identity, dict) and set(identity) == {'id', 'dir'} and
            digest(identity['id']) and text(identity['dir']) and Path(identity['dir']).is_absolute() and
            Path(identity['dir']).resolve() == build.resolve(), 'lock build identity/directory mismatch')
    require(isinstance(data['cook'], dict) and set(data['cook']) <= {'version', 'commit'} and
            version(data['cook'].get('version')), 'lock cook provenance required')
    if 'commit' in data['cook']:
        require(digest(data['cook']['commit'], 40), 'invalid cook commit')
    recipe = data['recipe']
    require(isinstance(recipe, dict) and set(recipe) == {'path', 'sha256'} and
            text(recipe['path']) and digest(recipe['sha256']) and
            text(data['recipePath']) and recipe['path'] == data['recipePath'] and text(data['createdAt']),
            'invalid lock recipe provenance')
    try:
        require(re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z', data['createdAt'])
                and datetime.fromisoformat(data['createdAt'].replace('Z', '+00:00')), 'invalid lock timestamp')
    except ValueError:
        raise LockError('invalid lock timestamp') from None
    host = data['connectomeHost']
    require(isinstance(host, dict) and set(host) <= {'url', 'ref', 'commit', 'path'} and
            repository(host.get('url')) and text(host.get('ref')) and digest(host.get('commit'), 40) and
            ('path' not in host or text(host['path'])) and
            (host_pin is None or host['commit'] == host_pin), 'lock host pin differs from request')
    requirements = data['requirements']
    require(isinstance(requirements, list) and all(
        isinstance(r, dict) and set(r) == {'name', 'envName', 'origin'} and
        text(r['name']) and text(r['envName']) and re.fullmatch(r'[A-Z_][A-Z0-9_]*', r['envName']) and
        text(r['origin']) and
        r['origin'] in {'env-file', 'process-env', 'probe', 'prompt', 'unresolved'}
        for r in requirements), 'requirements must be names-only v2 records')
    credentials = data['credentials']
    require(isinstance(credentials, list) and all(isinstance(n, str) and
            re.fullmatch(r'(?:[A-Z_][A-Z0-9_]*|[A-Za-z0-9_./-]+#[A-Za-z0-9_-]+)', n)
            for n in credentials), 'invalid credential names; values forbidden')
    declared = set(credentials) | {r['envName'] for r in requirements}
    require(set(required_names) <= declared, 'lock omits a declared required secret')
    require(isinstance(data['components'], list) and isinstance(data['localExtensions'], list) and
            isinstance(data['outputs'], list), 'invalid lock inventories')
    for component in data['components']:
        require(isinstance(component, dict) and component.get('kind') in {'git', 'npm'},
                'invalid component provenance')
        if component['kind'] == 'git':
            require(set(component) <= {'kind', 'key', 'role', 'url', 'ref', 'commit', 'path', 'install'} and
                    all(text(component.get(k)) for k in ('key', 'ref', 'path')) and
                    repository(component.get('url')) and isinstance(component.get('install'), str) and
                    digest(component.get('commit'), 40), 'invalid git component provenance')
        else:
            require(set(component) <= {'kind', 'key', 'role', 'name', 'spec', 'version', 'integrity'} and
                    all(text(component.get(k)) for k in ('key', 'name', 'spec')) and
                    ('version' not in component or version(component['version'])) and
                    ('integrity' not in component or (text(component['integrity']) and re.fullmatch(
                        r'(?:sha(?:1|256|384|512)-[A-Za-z0-9+/]+={0,2})(?: (?:sha(?:1|256|384|512)-[A-Za-z0-9+/]+={0,2}))*',
                        component['integrity']))), 'invalid npm provenance')
        require(component.get('role') in {'mcp', 'extension'}, 'invalid component role')
    for output in data['outputs']:
        require(isinstance(output, dict) and set(output) <= {'path', 'mode', 'sha256', 'sha256Omitted'} and
                text(output.get('path')) and type(output.get('mode')) is int and 0 <= output['mode'] <= 0o777,
                'invalid output record')
        require('sha256' not in output or digest(output['sha256']), 'invalid output digest')
        require('sha256Omitted' not in output or output['sha256Omitted'] == 'contains-requirement-value',
                'invalid output digest omission')
    for extension in data['localExtensions']:
        require(isinstance(extension, dict) and set(extension) == {'name', 'hostDir', 'path'} and
                all(text(v) for v in extension.values()), 'invalid local extension record')
    if 'state' in data:
        require(isinstance(data['state'], dict) and set(data['state']) == {'path'} and
                text(data['state']['path']) and Path(data['state']['path']).is_absolute(), 'invalid state record')
    if 'engines' in data:
        require(isinstance(data['engines'], dict) and set(data['engines']) == {'bun', 'node'}, 'invalid engine records')
        for engine in data['engines'].values():
            require(isinstance(engine, dict) and set(engine) == {'path', 'realPath', 'version'} and
                    text(engine['path']) and text(engine['realPath']) and version(engine['version']), 'invalid engine record')
    if 'layout' in data:
        layout = data['layout']
        roots = {'runtime', 'config', 'state', 'workspace', 'credentials'}
        require(isinstance(layout, dict) and set(layout) == roots | {'schema', 'connectorState'} and
                layout['schema'] == 'connectome.host-layout/v1' and
                all(text(layout[k]) and Path(layout[k]).is_absolute() for k in roots) and
                isinstance(layout['connectorState'], dict), 'invalid layout record')
        for bindings in layout['connectorState'].values():
            require(isinstance(bindings, dict) and all(
                re.fullmatch(r'[A-Z_][A-Z0-9_]*', k) and text(v) for k, v in bindings.items()),
                'invalid connector state names/paths')
    if 'hostDependencies' in data:
        require(isinstance(data['hostDependencies'], list), 'invalid host dependency inventory')
        for dependency in data['hostDependencies']:
            require(isinstance(dependency, dict) and set(dependency) == {
                'name', 'originalRange', 'version', 'sha256', 'size', 'snapshot', 'packageJsonDelta'} and
                all(text(dependency[k]) for k in ('name', 'originalRange', 'snapshot')) and
                version(dependency['version']) and
                digest(dependency['sha256']) and type(dependency['size']) is int and dependency['size'] > 0,
                'invalid host dependency record')
            delta = dependency['packageJsonDelta']
            require(isinstance(delta, dict) and set(delta) == {'direct', 'override'}, 'invalid dependency delta')
            for kind, sections in (('direct', {'dependencies', 'devDependencies', 'optionalDependencies'}),
                                   ('override', {'overrides', 'resolutions'})):
                edit = delta[kind]
                require(isinstance(edit, dict) and set(edit) == {'section', 'name', 'before', 'after'} and
                        edit['section'] in sections and text(edit['name']) and text(edit['after']) and
                        (text(edit['before']) if kind == 'direct' else edit['before'] is None),
                        'invalid dependency policy edit')
    if 'health' in data:
        health = data['health']
        require(isinstance(health, dict) and set(health) == {'command'} and
                isinstance(health['command'], list) and health['command'] and
                all(text(arg) for arg in health['command']), 'invalid planned health command')
    launch = data['launch']
    require(isinstance(launch, dict) and set(launch) <= {'kind', 'script', 'userUnit'} and
            launch.get('kind') == 'script', 'host script launcher required')
    contained_file(build, launch.get('script'))
    result = dict(data)
    if 'userUnit' in launch:
        require('units' not in data, 'ambiguous lock unit declarations')
        path = contained_file(build, launch['userUnit'])
        require(re.fullmatch(r'[A-Za-z0-9_.-]+\.service', path.name), 'invalid exported user unit')
        result['units'] = [{'path': str(path.relative_to(build)),
                            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]
    if 'units' in result:
        require(isinstance(result['units'], list) and bool(result['units']), 'invalid unit records')
        for unit in result['units']:
            require(isinstance(unit, dict) and set(unit) == {'path', 'sha256'} and
                    digest(unit['sha256']), 'invalid unit record')
            path = contained_file(build, unit['path'])
            require(path.suffix == '.service' and hashlib.sha256(path.read_bytes()).hexdigest() == unit['sha256'],
                    'unit output digest mismatch')
    return result


def read_lock(path, **kwargs):
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate lock fields refused')
            result[key] = value
        return result

    def invalid_number(_value):
        raise LockError('nonfinite lock number refused')

    try:
        data = json.loads(Path(path).read_text(), object_pairs_hook=unique_fields,
                          parse_constant=invalid_number)
        return map_lock(data, Path(path).parent, **kwargs)
    except LockError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise LockError('malformed or unreadable cook lock v2') from None
