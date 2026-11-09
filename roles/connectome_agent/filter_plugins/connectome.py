"""Controller-side filter; usable with a role loaded by absolute path."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    'connectome_config', Path(__file__).resolve().parents[1] / 'module_utils/connectome_config.py')
_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_config)


class FilterModule:
    def filters(self):
        return {'connectome_layers': _config.merge_layers,
                'connectome_public_catalog': _config.public_catalog}
