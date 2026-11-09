"""Production adapter for the reviewed cook source tree; standard library only.

Invoked by scripts/cook-adapter.py. No service activation and no secret lookup.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile

from connectome_deploy import secret_names, validate_config, resolve_pins, DeployError
from connectome_lock import read_lock, LockError, text, credential_shaped, repository

COOK_COMMIT = '7955a3132cb54ee975e5a672578cf8922abc6e22'
FRAMEWORK = '@' + 'animalabs/agent-framework'
BASE_ENV = {'PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR'}


class AdapterError(ValueError):
    pass


def need(condition, message):
    if not condition:
        raise AdapterError(message)


def command(argv, cwd=None, env=None, capture=False):
    need(isinstance(argv, list) and argv and all(text(arg) and not credential_shaped(arg) for arg in argv),
         'command arguments contain invalid or credential-shaped values')
    result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False)
    need(result.returncode == 0, 'cook adapter command failed (output redacted)')
    return result.stdout if capture else None


def clean_environment():
    # No arbitrary inherited values, COOK_PASS_ENV, Bun preload options, Git
    # overrides, provider credentials, or cook env-file input reach the child.
    return {key: value for key, value in os.environ.items() if key in BASE_ENV}


def absolute_path(value):
    need(text(value) and not credential_shaped(value) and not any(c in value for c in '?#'),
         'invalid or credential-shaped adapter path')
    path = Path(value)
    need(path.is_absolute(), 'absolute adapter path required')
    return path


def verify_cook(entrypoint, pin, env):
    entrypoint = absolute_path(entrypoint)
    need(entrypoint.is_absolute() and entrypoint.is_file(), 'absolute cook entrypoint required')
    root = entrypoint.parent.parent
    need(entrypoint.resolve() == root.resolve() / 'bin/cook', 'use the reviewed source bin/cook entrypoint')
    def git(*args):
        return command(['git', '-C', str(root), *args], env=env, capture=True).decode().strip()
    need(git('rev-parse', 'HEAD') == pin, 'cook checkout differs from requested pin')
    need(git('rev-parse', 'HEAD^{tree}') == git('rev-parse', COOK_COMMIT + '^{tree}'),
         'unsupported cook source tree')
    need(not git('status', '--porcelain', '--untracked-files=normal'), 'cook source checkout must be clean')


def build_commands(entry):
    builds = entry.get('build', [])
    need(isinstance(builds, list) and all(isinstance(argv, list) and argv and
         all(text(arg) and not credential_shaped(arg) for arg in argv) for argv in builds),
         'descriptor build must contain nonempty argv lists')
    return ' && '.join(shlex.join(argv) for argv in builds)


def source(entry, pin, target):
    need(repository(entry.get('repo')), 'source requires a safe HTTPS, SSH/scp or absolute local repository')
    return {'url': entry['repo'], 'ref': pin, 'inContainer': {'path': target},
            'install': {'run': build_commands(entry), 'runtime': 'custom'}}


def recipe_from_spec(spec):
    """Only the effective entries are translated. No discovery/catalog reads.

    settings are native cook/host settings, not a guessed connector launch API.
    Missing launch metadata refuses rather than inventing an upstream command.
    """
    need(isinstance(spec, dict) and set(spec) == {'config', 'pins'}, 'normalized deployment spec required')
    config, pins = spec['config'], spec['pins']
    validate_config(config)
    # Revalidate full pins even when used without the Ansible role.
    versions = {'runtime': pins['runtime'], 'connectors': pins['connectors']}
    for name, entry in config.get('connectors', {}).items():
        need(entry.get('pin_ref') == 'connectors.' + name, 'connector pin_ref must name its resolved connector')
    need(resolve_pins(config, versions) == pins, 'normalized pins required')
    agent = config.get('agent')
    need(isinstance(agent, dict) and isinstance(agent.get('systemPrompt'), str) and agent['systemPrompt'],
         'agent.systemPrompt is required by cook')
    recipe = {'name': config['name'], 'agent': deepcopy(agent), 'modules': {},
              'mcpServers': {}, 'extensions': {}, 'requirements': {}}
    if 'description' in config:
        recipe['description'] = config['description']
    for section in ('connectors', 'modules'):
        for name, entry in config.get(section, {}).items():
            need(entry.get('enabled', True) is True, 'adapter input must already exclude disabled entries')
            settings = deepcopy(entry.get('settings', {}))
            need(isinstance(settings, dict), 'descriptor settings must be a mapping')
            if section == 'connectors':
                allowed = {'command', 'args', 'env', 'url', 'transport', 'token', 'toolPrefix',
                           'enabledFeatureSets', 'disabledFeatureSets', 'enabledTools', 'disabledTools',
                           'reconnect', 'reconnectIntervalMs', 'channelSubscription'}
                need(settings.keys() <= allowed and isinstance(settings.get('command'), str),
                     'connector settings require native stdio command metadata')
                need(not ('token' in settings and not re.fullmatch(r'\$\{[A-Z_][A-Z0-9_]*\}', settings['token'])),
                     'connector token must be a secret-name reference')
                settings['source'] = source(entry, pins['connectors'][name], '/connectors/' + name)
                recipe['mcpServers'][name] = settings
            else:
                need('repo' not in entry and 'build' not in entry,
                     'source-backed modules require a separate reviewed extension adapter')
                # Empty settings enable a native module. Disabled entries never
                # reach this loop; the role already pruned them.
                recipe['modules'][name] = settings or True
    names = secret_names(config)
    need(not set(names) & BASE_ENV and not set(names) & {'DATA_DIR', 'WORKSPACE_DIR'},
         'secret names overlap adapter-owned environment')
    for name in names:
        recipe['requirements'][name] = {'exposeAs': name, 'required': False}
    # A secret-looking env field must be a reference, never a literal/default.
    for server in recipe['mcpServers'].values():
        for key, value in server.get('env', {}).items():
            if key in names or re.search(r'TOKEN|SECRET|PASSWORD|API_KEY', key):
                need(key in names and value == '${' + key + '}',
                     'connector secret env must reference an explicitly declared name')
    return recipe, names


def framework_input(config, pin):
    """Trusted artifact/hash pair with npm's gitHead provenance assertion.

    Cook itself validates name/version/range and the installed Bun lock graph.
    A gitHead assertion is not source authentication; operators must attest the
    archive/hash pair, just as they attest runtime executables and build tools.
    """
    artifact = config['cook']['framework_archive']
    need(isinstance(artifact, dict) and set(artifact) == {'path', 'sha256'},
         'cook.framework_archive requires path and sha256')
    path = absolute_path(artifact['path'])
    need(path.is_absolute() and path.is_file() and not path.is_symlink(),
         'framework archive must be an absolute regular file')
    need(isinstance(artifact['sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', artifact['sha256']) and
         hashlib.sha256(path.read_bytes()).hexdigest() == artifact['sha256'], 'framework archive hash mismatch')
    with tarfile.open(path, 'r:gz') as archive:
        manifests = [member for member in archive.getmembers()
                     if re.fullmatch(r'[^/]+/package.json', member.name) and member.isfile()]
        need(len(manifests) == 1 and manifests[0].size <= 1024 * 1024, 'framework archive manifest required')
        manifest = json.load(archive.extractfile(manifests[0]))
    need(manifest.get('name') == FRAMEWORK and manifest.get('gitHead') == pin,
         'framework archive gitHead differs from requested pin')
    return FRAMEWORK + '=' + str(path) + '#sha256=' + artifact['sha256']


def adapt(spec_path, build_dir, state_dir, runtime, entrypoint, bun_path, node_path):
    build, state, spec_path = map(absolute_path, (build_dir, state_dir, spec_path))
    for value in (runtime, entrypoint, bun_path, node_path):
        absolute_path(value)
    need(all(path.is_absolute() and path.resolve() == path for path in (build, state, spec_path)),
         'adapter paths must be absolute without symlink aliases')
    need(build.is_dir() and set(build.iterdir()) == {spec_path} and spec_path.name == 'deployment.json',
         'cook requires a fresh build containing only deployment.json')
    need(not state.is_relative_to(build) and not build.is_relative_to(state), 'state must be separate from build')
    need(all(Path(p).is_absolute() and os.access(p, os.X_OK) for p in (runtime, bun_path, node_path)),
         'absolute executable runtime, Bun and Node paths required')
    spec = json.loads(spec_path.read_text())
    recipe, names = recipe_from_spec(spec)
    config, pins = spec['config'], spec['pins']['runtime']
    settings = config.get('cook', {})
    need(isinstance(settings, dict), 'cook settings must be a mapping')
    need(set(settings) == {'host_repo', 'framework_archive', 'connector_state'},
         'cook requires host_repo, framework_archive and explicit connector_state')
    need(repository(settings['host_repo']), 'cook.host_repo requires a safe HTTPS, SSH/scp or absolute local repository')
    dependency = framework_input(config, pins['agent-framework'])
    recipe_path = build / 'cook-recipe.json'
    recipe_path.write_text(json.dumps(recipe, sort_keys=True, indent=2) + '\n')
    # Config/credentials are per-request, state/workspace persist. All roots
    # are siblings/non-overlapping as required by cook's split layout.
    request = build.name
    layout = {'schema': 'connectome.host-layout/v1', 'runtime': str(build),
              'config': str(state.parent / 'cook-config' / request),
              'credentials': str(state.parent / 'cook-credentials' / request),
              'state': str(state), 'workspace': str(state.parent / 'workspace'),
              'connectorState': settings['connector_state']}
    layout_path = build / 'cook-layout.json'
    layout_path.write_text(json.dumps(layout, sort_keys=True, indent=2) + '\n')
    argv = [runtime, entrypoint, 'install', str(recipe_path), '--out', str(build),
            '--no-prompts', '--yes', '--strict', '--ch-repo', settings['host_repo'],
            '--ch-ref', pins['connectome-host'], '--bun-path', bun_path, '--node-path', node_path,
            '--layout', str(layout_path), '--host-dependency', dependency,
            '--user-unit', 'connectome-' + config['name'] + '.service']
    # Validate all input before even the verification Git subprocesses spawn.
    need(all(text(arg) and not credential_shaped(arg) for arg in argv), 'invalid cook arguments')
    env = clean_environment()
    verify_cook(entrypoint, pins['cook'], env)
    command(argv, cwd=build, env=env)
    lock = read_lock(build / 'connectome.lock', host_pin=pins['connectome-host'], required_names=names)
    need(lock.get('layout') == layout and lock.get('state') == {'path': str(state)},
         'cook lock did not preserve the requested persistent layout')
    expected = {(entry['repo'], spec['pins']['connectors'][name])
                for name, entry in config.get('connectors', {}).items()}
    actual = {(entry.get('url'), entry.get('commit')) for entry in lock['components']}
    need(actual == expected and all(c['kind'] == 'git' and c['role'] == 'mcp' for c in lock['components']),
         'cook component provenance differs from enabled inputs')
    need(any(d.get('name') == FRAMEWORK and d.get('sha256') == settings['framework_archive']['sha256']
             for d in lock.get('hostDependencies', [])), 'cook omitted pinned framework input')
    # No values were supplied. Refuse unexpected value-bearing generated env,
    # so run.sh cannot override the role's runtime EnvironmentFile secrets.
    env_file = Path(layout['config']) / '.env'
    need(not env_file.exists() or not env_file.read_text().strip(), 'cook unexpectedly wrote runtime env values')
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('spec', 'out', 'state', 'runtime', 'entrypoint', 'bun-path', 'node-path'):
        parser.add_argument('--' + option, required=True)
    args = parser.parse_args()
    try:
        adapt(args.spec, args.out, args.state, args.runtime, args.entrypoint, args.bun_path, args.node_path)
    except (AdapterError, LockError, DeployError) as error:
        parser.exit(1, str(error) + '\n')
    except Exception:
        parser.exit(1, 'cook adapter failed; malformed input or unavailable local prerequisite (output redacted)\n')
