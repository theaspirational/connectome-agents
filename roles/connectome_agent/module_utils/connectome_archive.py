"""Verify local hash-pinned npm archives; no acquisition, extraction or scripts."""
import base64
import hashlib
import json
from pathlib import Path
import re
import tarfile
from urllib.parse import urlsplit

try:
    from ansible.module_utils.connectome_errors import SafeError
    from ansible.module_utils.connectome_lock import text, credential_shaped, version, digest
except ImportError:
    from connectome_errors import SafeError
    from connectome_lock import text, credential_shaped, version, digest

FRAMEWORK = '@' + 'animalabs/agent-framework'
ARTIFACT_PAYLOAD = 'operator-supplied prebuilt payload, verified out of band'
REGISTRY_COMPARISON = 'bytes were not compared against the registry'


class ArchiveError(SafeError):
    def __init__(self, message, code='E_ADAPTER_INPUT', cook_error_code=None):
        super().__init__(message, code, cook_error_code)


def need(condition, message, code='E_ADAPTER_INPUT'):
    if not condition:
        raise ArchiveError(message, code)


def npm_name(value):
    # npm's URL-safe, lowercase new-package names (including optional scope).
    return (text(value) and len(value) <= 214 and value not in {'node_modules', 'favicon.ico'} and
            re.fullmatch(r'(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*', value) is not None)


def registry_origin(value):
    if not text(value) or credential_shaped(value) or any(c.isspace() or c in '%\\?#' for c in value):
        return False
    try:
        url = urlsplit(value)
        return (url.scheme == 'https' and url.username is None and url.password is None and
                url.path in {'', '/'} and not url.query and not url.fragment and
                bool(url.hostname and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', url.hostname)) and
                (url.port is None or 0 < url.port <= 65535))
    except ValueError:
        return False


def sha512_integrity(value):
    if not text(value) or not value.startswith('sha512-'):
        return False
    try:
        decoded = base64.b64decode(value[7:], validate=True)
        return len(decoded) == 64 and base64.b64encode(decoded).decode() == value[7:]
    except ValueError:
        return False


def validate_dependency(entry):
    need(isinstance(entry, dict) and {'name', 'archive', 'sha256', 'kind', 'version'} <= entry.keys() and
         entry.keys() <= {'name', 'archive', 'sha256', 'kind', 'version', 'commit',
                          'registry_url', 'registry_integrity'},
         'host dependency requires a closed name/archive/sha256/kind/version declaration')
    need(npm_name(entry['name']) and not credential_shaped(entry['name']) and
         version(entry['version']) and not credential_shaped(entry['version']),
         'invalid host dependency identity', 'E_ARCHIVE_IDENTITY')
    need(isinstance(entry['kind'], str) and entry['kind'] in {'registry_release', 'artifact', 'git_commit'}, 'invalid archive kind')
    if entry['kind'] == 'git_commit':
        need(digest(entry.get('commit'), 40) and entry['commit'] != '0' * 40,
             'git_commit requires a full commit')
    else:
        need('commit' not in entry, 'commit required only for git_commit')
    if entry['kind'] == 'registry_release':
        need(registry_origin(entry.get('registry_url')) and sha512_integrity(entry.get('registry_integrity')),
             'registry_release requires registry_url and registry_integrity (sha512)')
    else:
        need(not {'registry_url', 'registry_integrity'} & entry.keys(),
             'registry identity is allowed only for registry_release')
    value = entry['archive']
    need(text(value) and not credential_shaped(value) and not any(c in value for c in '?#') and
         Path(value).is_absolute(), 'absolute local archive path required')
    need(digest(entry['sha256']), 'invalid archive hash', 'E_ARCHIVE_HASH')
    return entry


def legacy_dependency(config, pin):
    artifact = config['cook']['framework_archive']
    need(isinstance(artifact, dict) and {'path', 'sha256'} <= artifact.keys() and
         artifact.keys() <= {'path', 'sha256', 'registry_release', 'name', 'version'},
         'cook.framework_archive requires path and sha256')
    marked = 'registry_release' in artifact
    if marked:
        need(artifact['registry_release'] is True and artifact.get('name') == FRAMEWORK and
             version(artifact.get('version')) and not credential_shaped(artifact['version']),
             'registry release requires explicit name and version', 'E_ARCHIVE_IDENTITY')
    else:
        need(set(artifact) == {'path', 'sha256'}, 'release identity requires registry_release: true')
    result = {'name': FRAMEWORK, 'archive': artifact['path'], 'sha256': artifact['sha256'],
              'kind': 'artifact' if marked else 'git_commit', 'version': artifact.get('version')}
    if not marked:
        result['commit'] = pin
    # Legacy unmarked declarations infer version from the verified manifest.
    validate_dependency(dict(result, version=result['version'] or '0.0.0'))
    return result


def dependency_inputs(config, pin):
    settings = config.get('cook', {})
    entries = settings.get('host_dependencies', [])
    need(isinstance(entries, list), 'host_dependencies must be a list')
    result = [legacy_dependency(config, pin)] if 'framework_archive' in settings else []
    for entry in entries:
        result.append(dict(validate_dependency(entry)))
    names = [entry['name'] for entry in result]
    need(len(names) == len(set(names)), 'duplicate host dependencies refused')
    return result


def archive_manifest(entry):
    path = Path(entry['archive'])
    need(path.is_file() and not path.is_symlink(), 'archive must be an absolute regular file')
    try:
        data = path.read_bytes()
        need(hashlib.sha256(data).hexdigest() == entry['sha256'],
             'archive hash mismatch', 'E_ARCHIVE_HASH')
        if entry['kind'] == 'registry_release':
            observed = 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode()
            need(observed == entry['registry_integrity'], 'registry integrity mismatch', 'E_ARCHIVE_HASH')
        with tarfile.open(path, 'r:gz') as archive:
            manifests = [member for member in archive.getmembers()
                         if re.fullmatch(r'[^/]+/package.json', member.name) and member.isfile()]
            need(len(manifests) == 1 and manifests[0].size <= 1024 * 1024, 'archive manifest required')
            manifest = json.load(archive.extractfile(manifests[0]))
    except ArchiveError:
        raise
    except (OSError, ValueError, tarfile.TarError):
        raise ArchiveError('unreadable archive or manifest') from None
    need(isinstance(manifest, dict), 'archive manifest must be a mapping')
    need(manifest.get('name') == entry['name'] and version(manifest.get('version')) and
         not credential_shaped(manifest['version']) and
         (entry['version'] is None or manifest['version'] == entry['version']),
         'archive package identity mismatch', 'E_ARCHIVE_IDENTITY')
    return manifest


def dependency_provenance(config, pin):
    result = []
    for index, entry in enumerate(dependency_inputs(config, pin)):
        manifest = archive_manifest(entry)
        legacy = index == 0 and 'framework_archive' in config.get('cook', {})
        head = manifest.get('gitHead')
        if legacy:
            need(head == pin if 'gitHead' in manifest else entry['kind'] == 'artifact',
                 'framework archive gitHead differs from requested pin', 'E_ARCHIVE_GITHEAD')
        else:
            need(head == entry['commit'] if entry['kind'] == 'git_commit' else
                 ('gitHead' not in manifest or digest(head, 40)),
                 'archive gitHead missing or invalid', 'E_ARCHIVE_GITHEAD')
        record = provenance_record(dict(entry, version=manifest['version']))
        result.append(record)
    return result


def provenance_record(entry):
    record = dict(entry, commit_verifiable=entry['kind'] == 'git_commit')
    if entry['kind'] == 'artifact':
        record['payload'] = ARTIFACT_PAYLOAD
    if entry['kind'] == 'registry_release':
        record['registry_comparison'] = REGISTRY_COMPARISON
    return record


def records_metadata(config):
    settings = config.get('cook', {})
    return ('host_dependencies' in settings or
            settings.get('framework_archive', {}).get('registry_release') is True)


def framework_provenance(config, pin):
    # Preserve the v1 sidecar as a compatibility record of the legacy request
    # flag, NOT a registry-origin claim. Normalized provenance is artifact.
    record = dependency_provenance({'cook': {'framework_archive': config['cook']['framework_archive']}}, pin)[0]
    manifest = archive_manifest(legacy_dependency(config, pin))
    return {'schema': 'connectome.framework-archive/v1', 'sha256': record['sha256'],
            'name': record['name'], 'version': record['version'], 'requested_commit': pin,
            'registry_release': config['cook']['framework_archive'].get('registry_release') is True,
            'commit_verifiable': 'gitHead' in manifest}


def validate_dependency_record(record):
    need(isinstance(record, dict), 'invalid dependency provenance', 'E_LOCK_INVALID')
    extra = {'commit_verifiable'} | ({'payload'} if record.get('kind') == 'artifact' else set())
    if record.get('kind') == 'registry_release':
        extra.add('registry_comparison')
    validate_dependency({key: value for key, value in record.items() if key not in extra})
    need(type(record.get('commit_verifiable')) is bool and
         record['commit_verifiable'] == (record['kind'] == 'git_commit') and
         (record.get('payload') == ARTIFACT_PAYLOAD if record.get('kind') == 'artifact'
          else 'payload' not in record) and
         (record.get('registry_comparison') == REGISTRY_COMPARISON if record.get('kind') == 'registry_release'
          else 'registry_comparison' not in record), 'invalid dependency provenance', 'E_LOCK_INVALID')


def check_dependencies(data, config, pin, framework_record=None):
    """Bind the entire lock inventory to config, without reopening input archives."""
    expected = dependency_inputs(config, pin)
    actual = data.get('hostDependencies', [])
    need([d['name'] for d in actual] == [d['name'] for d in expected],
         'host dependency inventory mismatch', 'E_LOCK_INVALID')
    indexed = {d['name']: d for d in actual}
    generic = 'host_dependencies' in config.get('cook', {})
    for entry in expected:
        dep = indexed[entry['name']]
        expected_version = entry['version']
        if expected_version is None and framework_record is not None and entry['name'] == FRAMEWORK:
            expected_version = framework_record['version']
        need(dep['sha256'] == entry['sha256'] and
             (expected_version is None or dep['version'] == expected_version),
             'host dependency identity mismatch', 'E_LOCK_INVALID')
        if generic or 'provenance' in dep:
            record = dep.get('provenance')
            validate_dependency_record(record)
            wanted = provenance_record(dict(entry, version=dep['version']))
            need(record == wanted, 'host dependency provenance mismatch', 'E_LOCK_INVALID')


def validate_provenance(record, config, pin):
    """Validate a retained role lock without requiring the original input file."""
    artifact = config['cook']['framework_archive']
    keys = {'schema', 'sha256', 'name', 'version', 'requested_commit', 'registry_release', 'commit_verifiable'}
    valid = (isinstance(record, dict) and set(record) == keys and
             record['schema'] == 'connectome.framework-archive/v1' and
             record['sha256'] == artifact['sha256'] and record['name'] == FRAMEWORK and
             version(record['version']) and not credential_shaped(record['version']) and record['requested_commit'] == pin and
             type(record['registry_release']) is bool and type(record['commit_verifiable']) is bool and
             record['registry_release'] == (artifact.get('registry_release') is True) and
             (record['commit_verifiable'] or record['registry_release']) and
             (not record['registry_release'] or (record['name'] == artifact.get('name') and
                                                 record['version'] == artifact.get('version'))))
    if not valid:
        raise ArchiveError('framework provenance lock mismatch', 'E_LOCK_INVALID')
    return record
