"""Read and hash-pin controller-local operator files without text conversion."""
import importlib.util
from pathlib import Path
from ansible.errors import AnsibleError
from ansible.plugins.lookup import LookupBase

_spec = importlib.util.spec_from_file_location(
    'connectome_operator_inputs', Path(__file__).resolve().parents[1] / 'module_utils/connectome_inputs.py')
_inputs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_inputs)


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        try:
            return [_inputs.collect(config) for config in terms]
        except _inputs.ExternalDataArgError:
            raise AnsibleError('E_EXTERNAL_DATA_ARG: invalid external data argument input (content redacted)') from None
        except Exception:
            raise AnsibleError('operator input validation failed (content redacted)') from None
