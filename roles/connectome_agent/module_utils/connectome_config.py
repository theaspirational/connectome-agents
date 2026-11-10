"""Layer semantics shared by the controller filter and offline tests."""
from copy import deepcopy


def _python_combine(*layers, recursive=False):
    """Test-only stand-in when ansible-core is absent; lists are replaced."""
    result = {}
    for layer in layers:
        for key, value in layer.items():
            if recursive and isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = _python_combine(result[key], value, recursive=True)
            else:
                result[key] = deepcopy(value)
    return result


try:
    from ansible.plugins.filter.core import combine
except ImportError:
    combine = _python_combine


def _prune(value):
    if isinstance(value, dict):
        return {key: _prune(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_prune(item) for item in value]
    return value


def merge_layers(layers):
    if len(layers) != 4 or any(not isinstance(layer, dict) for layer in layers):
        raise ValueError('exactly four mapping layers required')
    # The ONE production merge site. Keep tombstones until all layers are merged.
    result = {}
    # A left fold is essential: Ansible's multi-argument combine reverses the
    # fold, which can resurrect lower keys across an intermediate null.
    for layer in layers:
        result = combine(result, layer, recursive=True)
    result = _prune(result)
    for section in ('connectors', 'modules', 'catalog'):
        if section == 'catalog' and section not in result:
            continue
        entries = result.get(section, {})
        if not isinstance(entries, dict):
            raise ValueError('connectors and modules must be mappings')
        for entry in entries.values():
            if not isinstance(entry, dict) or not isinstance(entry.get('enabled', True), bool):
                raise ValueError('entries must be mappings with boolean enabled')
        result[section] = {name: entry for name, entry in entries.items()
                           if entry.get('enabled', True)}
    if not result.get('catalog'):
        result.pop('catalog', None)
    return result


def public_catalog(defaults, catalog):
    result = deepcopy(defaults)
    if 'connectors' in result:
        raise ValueError('public connectors belong in the catalog')
    result['connectors'] = {n: deepcopy(e) for n, e in catalog.items() if e.get('kind', 'connector') == 'connector'}
    artifacts = {n: deepcopy(e) for n, e in catalog.items() if e.get('kind', 'connector') != 'connector'}
    if artifacts:
        result['catalog'] = artifacts
    return result


def required_names(config):
    entries = list(config.get('connectors', {}).values()) + list(config.get('modules', {}).values())
    return sorted(set(config.get('required_secrets', [])) |
                  {entry['secret'] for entry in config.get('secret_files', [])} |
                  {name for entry in entries for name in entry.get('required_secrets', [])})
