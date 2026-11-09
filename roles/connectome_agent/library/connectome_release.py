#!/usr/bin/python
"""Ansible boundary for the shared, offline-tested deployment engine."""
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.connectome_deploy import Deployment, DeployError
import fcntl


def main():
    module = AnsibleModule(argument_spec={
        'operation': {'type': 'str', 'choices': ['prepare', 'activate'], 'required': True},
        'base': {'type': 'path'},
        'root': {'type': 'path', 'required': True},
        'unit_dir': {'type': 'path', 'required': True},
        'config': {'type': 'dict', 'required': True},
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
            deployment.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Serialize mutations of this agent's user units and current pointer.
            with open(deployment.lock_path(), 'a') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                result = execute()
    except DeployError as error:
        # Engine diagnostics are fixed strings, never config/secret values.
        module.fail_json(msg=str(error))
    except Exception:
        # Config, tool and provider exceptions must never expose secret values.
        module.fail_json(msg='deployment failed; previous build retained/restored where possible; inspect private logs')
    module.exit_json(**result)


if __name__ == '__main__':
    main()
