"""Read the role's ONE catalog; no alternate public catalog schema or merging."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .data import SpawnError, decode, fields, json_data, match, need
from .sources import ARTIFACT_LIMIT, COMMIT, absolute, read_file, resolve_artifact

CATALOG_NAME = r'[a-z0-9][a-z0-9_-]{0,62}'
SHA = r'[0-9a-f]{64}'
VERSION = r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?'
# Explicit dispatch registry; adding a kind requires a validator and resolver.
KINDS = {'connector': 'connectors', 'runtime': 'artifacts',
         'adapter': 'artifacts', 'preflight': 'artifacts'}


@dataclass
class RoleCatalog:
    """Internal read result, not a second serialized catalog format."""
    descriptors: dict
    versions: dict


def descriptor_digest(entry):
    payload = {k: v for k, v in entry.items() if k != 'enabled'}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True, allow_nan=False).encode('utf-8')).hexdigest()


def argv(value):
    need(type(value) is list and bool(value) and all(type(v) is str and v and
         not any(ord(c) < 32 for c in v) for v in value), 'catalog argv must contain literal strings')


def artifact_relative(value):
    # The role permits package path characters beyond template-source paths.
    need(match(value, r'[A-Za-z0-9_.+@/-]+') and
         all(p not in ('', '.', '..') for p in value.split('/')),
         'literal contained artifact path required')


def descriptor(name, entry):
    need(match(name, CATALOG_NAME), 'invalid catalog name')
    json_data(entry)
    need(type(entry) is dict, 'catalog descriptor must be an object')
    kind = entry.get('kind', 'connector')
    need(type(kind) is str and kind in KINDS, 'unknown catalog kind')
    common = {'kind', 'enabled', 'requires', 'build', 'exports', 'required_secrets'}
    if kind == 'connector':
        fields(entry, ('repo', 'pin_ref', 'settings'), common)
        need(type(entry['repo']) is str and bool(entry['repo']) and '\x00' not in entry['repo'],
             'catalog repository required')
        need(type(entry['settings']) is dict, 'connector settings required')
    else:
        fields(entry, ('kind', 'pin_ref'), common | ({'run'} if kind == 'preflight' else set()))
    need(entry['pin_ref'] == KINDS[kind] + '.' + name, 'pin_ref must name its descriptor')
    need(type(entry.get('enabled', True)) is bool, 'enabled must be boolean')
    for field in ('requires', 'required_secrets'):
        items = entry.get(field, [])
        pattern = CATALOG_NAME if field == 'requires' else r'[A-Z_][A-Z0-9_]*'
        need(type(items) is list and all(match(v, pattern) for v in items) and
             len(items) == len(set(items)), 'invalid catalog reference list')
    need(type(entry.get('build', [])) is list, 'build must be argv lists')
    for command in entry.get('build', []):
        argv(command)
    if 'run' in entry:
        argv(entry['run'])
    if kind == 'preflight':
        need('run' in entry, 'preflight run argv required')
    exports = entry.get('exports', {})
    need(type(exports) is dict and all(type(k) is str for k in exports), 'invalid exports')
    for value in exports.values():
        artifact_relative(value)
    return kind


def validate_catalog(catalog):
    need(isinstance(catalog, RoleCatalog), 'load_catalog result required')
    fields(catalog.versions, ('runtime', 'connectors'), ('artifacts', 'platform'))
    for section in ('runtime', 'connectors', 'artifacts', 'platform'):
        need(type(catalog.versions.get(section, {})) is dict, 'invalid version section')
    need(type(catalog.descriptors) is dict, 'invalid descriptors')
    for name, entry in catalog.descriptors.items():
        descriptor(name, entry)
    return deepcopy(catalog)


def load_catalog(root):
    """Read connectors/*.yml + versions.yml exactly, without Ansible precedence.

    Unknown kinds fail immediately. Unselected placeholders are retained; a
    selected placeholder fails in resolve(), so a disabled unresolved entry
    cannot prevent planning unrelated selections.
    """
    root = Path(root)
    need(root.is_dir() and (root / 'connectors').is_dir(), 'catalog root required')
    descriptors = {}
    for path in sorted((root / 'connectors').glob('*.yml')):
        value = decode(read_file(path))
        need(set(value) == {path.stem} and path.stem not in descriptors,
             'one matching name per catalog descriptor required')
        descriptors.update(value)
    return validate_catalog(RoleCatalog(descriptors, decode(read_file(root / 'versions.yml'))))


def pin(catalog, name):
    entry = catalog.descriptors[name]
    kind = descriptor(name, entry)
    pins = catalog.versions.get(KINDS[kind], {})
    value = catalog.versions.get('artifacts', {}).get(name, pins.get(name))
    need(value is not None, 'catalog pin missing')
    if type(value) is str:
        need(kind == 'connector' and match(value, COMMIT) and value != '0' * 40,
             'full nonzero connector commit required')
    else:
        fields(value, ('archive', 'version', 'sha256', 'descriptor_sha256'),
               ('commit',) if kind == 'connector' else ())
        artifact_relative(value['archive'])
        need(value['archive'].startswith('inputs/'), 'archive must be under inputs')
        need(match(value['version'], VERSION), 'concrete artifact version required')
        for field in ('sha256', 'descriptor_sha256'):
            need(match(value[field], SHA) and value[field] != '0' * 64, 'nonzero artifact digest required')
        need(value['descriptor_sha256'] == descriptor_digest(entry), 'descriptor digest mismatch')
        if kind == 'connector':
            need(match(value.get('commit'), COMMIT) and value['commit'] != '0' * 40,
                 'full nonzero connector commit required')
    return value


def select(catalog, connectors, runtime, preflights):
    selected, active = set(), set()
    def visit(name):
        need(name in catalog.descriptors, 'unknown catalog selection')
        need(name not in active, 'catalog dependency cycle')
        if name in selected:
            return
        active.add(name)
        entry = catalog.descriptors[name]
        # T2 archive connectors implicitly depend on both local runtimes. The
        # legacy Git-source bridge retains T1's dependency-free default.
        archive_connector = (entry.get('kind', 'connector') == 'connector' and
                             type(pin(catalog, name)) is dict)
        default = ['node', 'bun'] if archive_connector else []
        for dependency in entry.get('requires', default):
            visit(dependency)
        active.remove(name)
        selected.add(name)
    for names, kinds in ((connectors, {'connector'}), (runtime, {'runtime', 'adapter'}),
                         (preflights, {'preflight'})):
        for name in names:
            need(name in catalog.descriptors and descriptor(name, catalog.descriptors[name]) in kinds,
                 'catalog selection kind mismatch')
            visit(name)
    return sorted(selected)


def local_roots(value):
    need(type(value) is dict, 'local object-store map required')
    for ref, path in value.items():
        need(match(ref, r'(?:runtime|connectors|artifacts)\.' + CATALOG_NAME), 'invalid local-store pin_ref')
        absolute(path)
    return value


def payload(root, path):
    """Archive roots and every ancestor must be literal and non-symlinked."""
    absolute(root)
    artifact_relative(path)
    candidate = Path(root) / path
    for component in (candidate, *candidate.parents):
        need(not component.is_symlink(), 'symlinked payload path refused')
    need(candidate.resolve().is_relative_to(Path(root).resolve()), 'payload escapes root')
    return read_file(candidate, ARTIFACT_LIMIT)


def resolve(catalog, name, stores):
    entry = catalog.descriptors[name]
    value = pin(catalog, name)
    ref = entry['pin_ref']
    need(ref in stores, 'selected artifact needs an explicit local store')
    if type(value) is str:
        record = resolve_artifact({'repo': stores[ref], 'commit': value})
        # A resolver's local cache path is transport provenance, not intent.
        record['repo'] = entry['repo']
    else:
        content = payload(stores[ref], value['archive'])
        computed = hashlib.sha256(content).hexdigest()
        need(computed == value['sha256'], 'archive digest mismatch')
        record = dict(value, kind='archive', size=len(content), sha256=computed,
                      descriptor_sha256=descriptor_digest(entry))
    return dict(record, name=name, catalogKind=entry.get('kind', 'connector'), pin_ref=ref)


def resolve_runtime(catalog, stores):
    """Bind existing role bridge runtime commits to explicit local object stores.

    versions.yml has no runtime repository locators; these are *not* inferred
    from URLs or remote origins. The map supplies the local checkout for each
    runtime pin. No second catalog or implicit repository defaults are added.
    """
    need({'cook', 'connectome-host', 'agent-framework'} <= catalog.versions['runtime'].keys(),
         'role runtime pins missing')
    records = {}
    for name, commit in sorted(catalog.versions['runtime'].items()):
        need(match(name, CATALOG_NAME), 'invalid runtime name')
        ref = 'runtime.' + name
        need(ref in stores, 'runtime pin needs an explicit local object store')
        records[ref] = resolve_artifact({'repo': stores[ref], 'commit': commit})
    return records
