"""Local test provider: look up only requested names in a controller JSON file."""
import json
from ansible.errors import AnsibleError
from ansible.plugins.lookup import LookupBase


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        try:
            with open(kwargs['path'], encoding='utf-8') as stream:
                values = json.load(stream)
            result = [values[name] for name in terms]
            if any(not isinstance(value, str) for value in result):
                raise ValueError()
            return result
        except (OSError, ValueError, KeyError, TypeError):
            raise AnsibleError('local secret lookup failed (values redacted)') from None
