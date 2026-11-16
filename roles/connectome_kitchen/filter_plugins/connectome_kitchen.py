"""The public catalog step of the four layers. Catalog knowledge stays in the Python role; kitchen merges the layers."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    'connectome_config', Path(__file__).resolve().parents[2] / 'connectome_agent/module_utils/connectome_config.py')
_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_config)

_FLAGS = (('runtime', '--runtime'), ('entrypoint', '--entrypoint'), ('bun', '--bun-path'), ('node', '--node-path'))


def engines(argv):
    """The four engine paths of the role's cook command, by flag: what contract v1 takes as `engines.cook`."""
    try:
        cook = {key: argv[argv.index(flag) + 1] for key, flag in _FLAGS}
    except (ValueError, IndexError, AttributeError):
        raise ValueError('the cook command names no runtime, entrypoint, Bun or Node') from None
    if any(not isinstance(value, str) or not value.startswith('/') for value in cook.values()):
        raise ValueError('engine paths must be absolute')
    return {'cook': cook}


class FilterModule:
    def filters(self):
        return {'connectome_kitchen_public_catalog': _config.public_catalog, 'connectome_kitchen_engines': engines}
