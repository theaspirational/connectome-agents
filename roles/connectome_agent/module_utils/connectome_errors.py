"""Closed, value-free diagnostics crossing subprocess and Ansible boundaries."""
import json
import re

MESSAGES = {
    'E_ANCESTOR_WRITABLE': 'Existing ancestor is group/other-writable; inspect ancestor_path and repair explicitly; not changed.',
    'E_SYSTEMD_PATH': 'Systemd paths must be absolute literals without whitespace or special characters.',
    'E_ENVIRONMENT_FILE': 'Required unit environment file was not accepted by the user service manager.',
    'E_ARCHIVE_GITHEAD': 'Framework archive gitHead is missing or differs from the requested pin.',
    'E_ARCHIVE_IDENTITY': 'Framework archive package name or version differs from the declared release.',
    'E_ARCHIVE_HASH': 'Framework archive sha256 verification failed.',
    'E_ADAPTER_INPUT': 'Adapter input or local prerequisite is invalid.',
    'E_LOCK_INVALID': 'Cook lock validation failed.',
    'E_LOCK_STATE_PATH': 'Cook lock does not preserve the configured external state and layout.',
    'E_SECRETS_MODE': 'Secrets require a non-symlink directory 0700 and regular file 0600 owned by the agent account; not repaired.',
    'E_BUDGETS_UNSUPPORTED': 'Step/cost budgets are unsupported; remove budgets explicitly.',
    'E_COOK_FAILED': 'Cook command failed (output redacted).',
    'E_DEPLOY_FAILED': 'Deployment failed; previous build retained/restored where possible (output redacted).',
}
# Literal codes from the reviewed cook tree, not arbitrary E_* strings. Never
# forward the rest of a tool's line, even when it starts with a known code.
COOK_CODES = frozenset({
    'E_HOST_LAYOUT', 'E_HOST_UNIT',
    'E_PRIVATE_ANCESTOR', 'E_PRIVATE_FILE', 'E_PRIVATE_LINK_OWNER',
    'E_PRIVATE_MODE', 'E_PRIVATE_OWNER', 'E_PRIVATE_PATH', 'E_PRIVATE_UNSUPPORTED',
    'E_HOST_DEPENDENCY_ARCHIVE', 'E_HOST_DEPENDENCY_DUPLICATE',
    'E_HOST_DEPENDENCY_FILE', 'E_HOST_DEPENDENCY_HASH', 'E_HOST_DEPENDENCY_NAME',
    'E_HOST_DEPENDENCY_OPTION', 'E_HOST_DEPENDENCY_OVERLAP', 'E_HOST_DEPENDENCY_POLICY_FIELDS',
    'E_HOST_DEPENDENCY_RANGE', 'E_HOST_DEPENDENCY_REINSTALL', 'E_HOST_DEPENDENCY_RESOLUTION',
    'E_HOST_DEPENDENCY_SOURCE', 'E_HOST_DEPENDENCY_STORAGE', 'E_HOST_DEPENDENCY_TRUSTED',
    'E_HOST_DEPENDENCY_UNDECLARED', 'E_HOST_DEPENDENCY_VERSION',
    'E_HOST_ENGINE_CHANGED', 'E_HOST_ENGINE_CHECKOUT', 'E_HOST_ENGINE_CLONE',
    'E_HOST_ENGINE_DEPENDENCY', 'E_HOST_ENGINE_EXEC', 'E_HOST_ENGINE_OBJECT_FORMAT',
    'E_HOST_ENGINE_PACKAGE', 'E_HOST_ENGINE_PATH', 'E_HOST_ENGINE_PLAN',
    'E_HOST_ENGINE_RANGE', 'E_HOST_ENGINE_REQUIREMENT', 'E_HOST_ENGINE_RESERVED',
    'E_HOST_ENGINE_VERSION',
})


def cook_code(output):
    for match in re.finditer(rb'(?<![A-Za-z0-9_])(E_[A-Z_]+)(?![A-Za-z0-9_])', output):
        code = match[1].decode('ascii')
        if code in COOK_CODES:
            return code
    return None


def cook_code_from_stream(stream):
    stream.seek(0)
    tail = b''
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return None
        code = cook_code(tail + chunk)
        if code:
            return code
        tail = chunk[-128:]


def diagnostic(code, upstream=None):
    code = code if isinstance(code, str) and code in MESSAGES else 'E_DEPLOY_FAILED'
    result = {'error_code': code, 'msg': code + ': ' + MESSAGES[code]}
    if code == 'E_COOK_FAILED' and isinstance(upstream, str) and upstream in COOK_CODES:
        result['cook_error_code'] = upstream
        result['msg'] += ' (cook: ' + upstream + ')'
    return result


class SafeError(ValueError):
    """Internal context is never serialized; public() uses only closed literals."""
    def __init__(self, message, code='E_DEPLOY_FAILED', cook_error_code=None):
        super().__init__(message)
        self.code, self.cook_error_code = code, cook_error_code

    def public(self):
        return diagnostic(self.code, self.cook_error_code)


def adapter_diagnostic(output):
    """Accept only an exact closed diagnostic envelope; discard every raw byte."""
    fallback = diagnostic('E_COOK_FAILED')
    try:
        data = json.loads(output)
        if not isinstance(data, dict):
            return fallback
        safe = diagnostic(data.get('error_code'), data.get('cook_error_code'))
        return safe if data == safe else fallback
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return fallback
