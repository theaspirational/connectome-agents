#!/usr/bin/python
"""Ansible boundary for the shared, offline-tested deployment engine."""
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.connectome_deploy import Deployment, make_directory
from ansible.module_utils.connectome_errors import SafeError, diagnostic
import fcntl


def main():
    module = AnsibleModule(argument_spec={
        'operation': {'type': 'str', 'choices': ['prepare', 'activate'], 'required': True},
        'base': {'type': 'path'},
        'root': {'type': 'path', 'required': True},
        'unit_dir': {'type': 'path', 'required': True},
        'config': {'type': 'dict', 'required': True},
        'operator_files': {'type': 'dict', 'default': {}, 'no_log': True},
        'versions': {'type': 'dict', 'required': True},
        'cook_command': {'type': 'list', 'elements': 'str', 'required': True},
        'systemctl': {'type': 'list', 'elements': 'str', 'default': ['systemctl', '--user']},
        'secrets': {'type': 'dict', 'no_log': True},
    }, supports_check_mode=True)
    params = dict(module.params)
    operation = params.pop('operation')
    secrets = params.pop('secrets')
    params['check'] = module.check_mode
    try:
        deployment = Deployment(**params)
        def execute():
            return deployment.prepare() if operation == 'prepare' else deployment.activate(secrets)
        if module.check_mode:
            result = execute()
        else:
            deployment.validate_paths()
            deployment.preflight_activation()
            make_directory(deployment.root)
            # Serialize mutations of this agent's user units and current pointer.
            with open(deployment.lock_path(), 'a') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                result = execute()
    except SafeError as error:
        # Suppress Ansible's implicit argv/config invocation and debug traceback
        # as well as raw tool output, including under verbose callbacks.
        module.fail_json(invocation={}, exception=None, **error.public())
    except Exception:
        # Config, tool and provider exceptions must never expose secret values.
        module.fail_json(invocation={}, exception=None, **diagnostic('E_DEPLOY_FAILED'))
    module.exit_json(**result)


if __name__ == '__main__':
    main()
