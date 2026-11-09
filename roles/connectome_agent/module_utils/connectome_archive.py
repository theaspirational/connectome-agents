"""Verify only a local hash-pinned framework archive; no acquisition or extraction."""
import hashlib
import json
from pathlib import Path
import re
import tarfile

try:
    from ansible.module_utils.connectome_errors import SafeError
    from ansible.module_utils.connectome_lock import text, credential_shaped, version
except ImportError:
    from connectome_errors import SafeError
    from connectome_lock import text, credential_shaped, version

FRAMEWORK = '@' + 'animalabs/agent-framework'


class ArchiveError(SafeError):
    def __init__(self, message, code='E_ADAPTER_INPUT', cook_error_code=None):
        super().__init__(message, code, cook_error_code)


def framework_provenance(config, pin):
    def need(condition, message, code='E_ADAPTER_INPUT'):
        if not condition:
            raise ArchiveError(message, code)

    artifact = config['cook']['framework_archive']
    need(isinstance(artifact, dict) and {'path', 'sha256'} <= artifact.keys() and
         artifact.keys() <= {'path', 'sha256', 'registry_release', 'name', 'version'},
         'cook.framework_archive requires path and sha256')
    marked = 'registry_release' in artifact
    if marked:
        need(artifact['registry_release'] is True and artifact.get('name') == FRAMEWORK and
             version(artifact.get('version')) and not credential_shaped(artifact['version']), 'registry release requires explicit name and version',
             'E_ARCHIVE_IDENTITY')
    else:
        need(set(artifact) == {'path', 'sha256'}, 'release identity requires registry_release: true')
    value = artifact['path']
    need(text(value) and not credential_shaped(value) and not any(c in value for c in '?#'),
         'invalid or credential-shaped archive path')
    path = Path(value)
    need(path.is_absolute() and path.is_file() and not path.is_symlink(),
         'framework archive must be an absolute regular file')
    need(isinstance(artifact['sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', artifact['sha256']) and
         hashlib.sha256(path.read_bytes()).hexdigest() == artifact['sha256'],
         'framework archive hash mismatch', 'E_ARCHIVE_HASH')
    with tarfile.open(path, 'r:gz') as archive:
        manifests = [member for member in archive.getmembers()
                     if re.fullmatch(r'[^/]+/package.json', member.name) and member.isfile()]
        need(len(manifests) == 1 and manifests[0].size <= 1024 * 1024, 'framework archive manifest required')
        manifest = json.load(archive.extractfile(manifests[0]))
    need(isinstance(manifest, dict), 'framework archive manifest must be a mapping')
    # Presence, not truthiness: null/empty/mismatched gitHead never falls back.
    verifiable = 'gitHead' in manifest
    need(manifest.get('gitHead') == pin if verifiable else marked,
         'framework archive gitHead differs from requested pin', 'E_ARCHIVE_GITHEAD')
    need(manifest.get('name') == FRAMEWORK and version(manifest.get('version')) and
         not credential_shaped(manifest['version']) and
         (not marked or (manifest['name'] == artifact['name'] and manifest['version'] == artifact['version'])),
         'framework archive package identity mismatch', 'E_ARCHIVE_IDENTITY')
    return {'schema': 'connectome.framework-archive/v1', 'sha256': artifact['sha256'],
            'name': manifest['name'], 'version': manifest['version'], 'requested_commit': pin,
            'registry_release': marked, 'commit_verifiable': verifiable}


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
