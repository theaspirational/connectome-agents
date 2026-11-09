"""Closed operator-input schemas and byte verification (controller and target)."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat


def need(condition, message='invalid operator input'):
    if not condition:
        raise ValueError(message)


def validate(config):
    need(isinstance(config.get('activation', 'start'), str) and
         config.get('activation', 'start') in ('start', 'install_only'),
         'activation must be start or install_only')
    exclusive = config.get('exclusive_with', [])
    need(isinstance(exclusive, list), 'exclusive_with must be a list')
    seen = set()
    for record in exclusive:
        need(isinstance(record, dict) and set(record) == {'scope', 'unit'},
             'closed exclusive_with record required')
        need(isinstance(record['scope'], str) and record['scope'] in ('system', 'user') and
             isinstance(record['unit'], str) and
             re.fullmatch(r'[A-Za-z0-9_@.-]+\.service', record['unit']) and
             not record['unit'].startswith(('.', '-')), 'invalid exclusive unit')
        key = (record['scope'], record['unit'])
        need(key not in seen, 'duplicate exclusive unit')
        seen.add(key)
    if 'recipe_file' in config:
        record = config['recipe_file']
        need(isinstance(record, dict) and set(record) == {'path', 'sha256'}, 'closed recipe_file record required')
        value = record['path']
        need(isinstance(value, str) and value.startswith('/') and '{' not in value and '}' not in value
             and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value),
             'controller recipe requires an absolute literal path')
        need(value == os.path.abspath(value), 'controller recipe path must be canonical')
        need(isinstance(record['sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', record['sha256']),
             'invalid recipe sha256')
    if 'secret_files' in config:
        records = config['secret_files']
        need(isinstance(records, list), 'secret_files must be a list')
        seen = set()
        for record in records:
            need(isinstance(record, dict) and set(record) == {'secret', 'env'}, 'closed secret_files record required')
            need(all(isinstance(value, str) and re.fullmatch(r'[A-Z_][A-Z0-9_]*', value)
                     for value in record.values()), 'invalid secret file name')
            need(record['env'] not in seen and record['env'] not in {'DATA_DIR', 'WORKSPACE_DIR', 'PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR'},
                 'duplicate or reserved secret file environment name')
            seen.add(record['env'])


def verify_bytes(content, record):
    need(hashlib.sha256(content).hexdigest() == record['sha256'], 'recipe digest mismatch')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            need(key not in result, 'duplicate recipe JSON key')
            result[key] = value
        return result
    def invalid(_value):
        raise ValueError('nonfinite recipe JSON number')
    try:
        recipe = json.loads(content, object_pairs_hook=unique, parse_constant=invalid)
        need(isinstance(recipe, dict), 'recipe must be a JSON object')
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError('invalid recipe JSON') from None
    return content


def read_file(path, record):
    path = Path(path)
    try:
        need(path.resolve() == path and stat.S_ISREG(path.lstat().st_mode),
             'recipe must be a regular file without symlink aliases')
        return verify_bytes(path.read_bytes(), record)
    except OSError:
        raise ValueError('recipe input unavailable') from None


def collect(config):
    """Controller-only lookup; payloads are transport, never part of the spec."""
    validate(config)
    if 'recipe_file' not in config:
        return {}
    record = config['recipe_file']
    return {'cook-recipe.json': base64.b64encode(read_file(record['path'], record)).decode('ascii')}


def payloads(config, supplied=None):
    validate(config)
    expected = {'cook-recipe.json'} if 'recipe_file' in config else set()
    if supplied is None:  # Standalone engine: caller is the controller too.
        supplied = collect(config)
    need(isinstance(supplied, dict) and set(supplied) == expected, 'recipe transport inventory mismatch')
    if not expected:
        return {}
    try:
        content = base64.b64decode(supplied['cook-recipe.json'], validate=True)
    except (ValueError, TypeError):
        raise ValueError('invalid recipe transport') from None
    return {'cook-recipe.json': verify_bytes(content, config['recipe_file'])}


def installed(build, config):
    """Retained builds need only their installed bytes, never controller files."""
    validate(config)
    if 'recipe_file' in config:
        path = Path(build) / 'cook-recipe.json'
        read_file(path, config['recipe_file'])
        info = path.lstat()
        need(stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid(),
             'installed recipe mode or owner mismatch')
