"""Production adapter for the reviewed cook source tree; standard library only.

Invoked by scripts/cook-adapter.py. No service activation and no secret lookup.
"""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

import connectome_inputs as inputs
from connectome_deploy import environment_names, validate_config, resolve_pins, DeployError
from connectome_lock import read_lock, LockError, text, credential_shaped, repository
from connectome_errors import SafeError, diagnostic, cook_code_from_stream
from connectome_archive import FRAMEWORK, ArchiveError, framework_provenance, dependency_provenance, records_metadata

COOK_COMMIT = '28236dd51045c99604ddc7ba6a2eb0c02679fd3d'
BASE_ENV = {'PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR'}


AdapterError = ArchiveError


def need(condition, message, code='E_ADAPTER_INPUT'):
    if not condition:
        raise AdapterError(message, code)


def validate_arguments(argv, flag_indices=()):
    # Only trusted flag positions are exempt; a value equal to a flag is not.
    need(isinstance(argv, list) and argv and all(
        text(arg) and not credential_shaped(arg) and (not arg.startswith('-') or index in flag_indices)
        for index, arg in enumerate(argv)),
         'command arguments contain invalid or credential-shaped values')


def command(argv, cwd=None, env=None, capture=False, flag_indices=()):
    validate_arguments(argv, flag_indices)
    # Capture privately and scan with bounded memory. No raw output or
    # exception containing argv ever crosses the adapter boundary.
    with tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                    stderr=errors, check=False)
        except OSError:
            raise AdapterError('local command unavailable', 'E_COOK_FAILED') from None
        if result.returncode:
            upstream = cook_code_from_stream(errors) if not capture else None
            raise AdapterError('cook adapter command failed (output redacted)',
                               'E_COOK_FAILED', upstream)
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
    # The reviewed checkout may be owned by another account (root) and read-only.
    # Trust exactly this resolved path for these calls only; never '*', never a
    # gitconfig write. No fsmonitor, hooks or optional index-lock refreshes run.
    checkout = str(root.resolve())
    prefix = ['git', '-c', 'safe.directory=' + checkout, '-c', 'core.fsmonitor=false',
              '-c', 'core.hooksPath=/dev/null', '--no-optional-locks', '-C', checkout]
    prefix_flags = (1, 3, 5, 7, 8)
    def git(*args, flag_indices=()):
        return command(prefix + list(args), env=env, capture=True,
                       flag_indices=prefix_flags + tuple(index + len(prefix) for index in flag_indices)
                       ).decode().strip()
    need(git('rev-parse', 'HEAD') == pin, 'cook checkout differs from requested pin')
    need(git('rev-parse', 'HEAD^{tree}') == git('rev-parse', COOK_COMMIT + '^{tree}'),
         'unsupported cook source tree')
    need(not git('status', '--porcelain', '--untracked-files=normal', flag_indices=(1, 2)),
         'cook source checkout must be clean')


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


def recipe_from_spec(spec, recipe_bytes=None):
    """Only the effective entries are translated. No discovery/catalog reads.

    settings are native cook/host settings, not a guessed connector launch API.
    Missing launch metadata refuses rather than inventing an upstream command.
    """
    need(isinstance(spec, dict) and set(spec) == {'config', 'pins'}, 'normalized deployment spec required')
    config, pins = spec['config'], spec['pins']
    validate_config(config)
    # Revalidate full pins even when used without the Ansible role.
    versions = {'runtime': pins['runtime'], 'connectors': pins['connectors']}
    for name, entry in ({} if 'recipe_file' in config else config.get('connectors', {})).items():
        need(entry.get('pin_ref') == 'connectors.' + name, 'connector pin_ref must name its resolved connector')
    need(resolve_pins(config, versions) == pins, 'normalized pins required')
    if config.get('budgets'):
        raise AdapterError('step/cost budgets not supported by host recipe schema at ' +
                           pins['runtime']['connectome-host'] + '; remove budgets explicitly', 'E_BUDGETS_UNSUPPORTED')
    if 'recipe_file' in config:
        record = config['recipe_file']
        try:
            content = (inputs.read_file(record['path'], record) if recipe_bytes is None else
                       inputs.verify_bytes(recipe_bytes, record))
        except ValueError as error:
            raise AdapterError(str(error)) from None
        names = environment_names(config)
        need(not set(names) & BASE_ENV and not set(names) & {'DATA_DIR', 'WORKSPACE_DIR'},
             'secret names overlap adapter-owned environment')
        return json.loads(content), names
    agent = config.get('agent')
    need(isinstance(agent, dict) and isinstance(agent.get('systemPrompt'), str) and agent['systemPrompt'],
         'agent.systemPrompt is required by cook')
    recipe = {'name': config['name'], 'agent': deepcopy(agent), 'modules': {},
              'mcpServers': {}, 'extensions': {}}
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
    names = environment_names(config)
    need(not set(names) & BASE_ENV and not set(names) & {'DATA_DIR', 'WORKSPACE_DIR'},
         'secret names overlap adapter-owned environment')
    # Cook needs nonempty declarations to record runtime-only names in its
    # lock. Never emit an empty cook-only block to the host configuration.
    if names:
        recipe['requirements'] = {}
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
    """Compatibility helper; provenance is recorded separately from cook's lock."""
    framework_provenance(config, pin)
    artifact = config['cook']['framework_archive']
    return FRAMEWORK + '=' + artifact['path'] + '#sha256=' + artifact['sha256']


def adapt(spec_path, build_dir, state_dir, runtime, entrypoint, bun_path, node_path):
    build, state, spec_path = map(absolute_path, (build_dir, state_dir, spec_path))
    for value in (runtime, entrypoint, bun_path, node_path):
        absolute_path(value)
    need(all(path.is_absolute() and path.resolve() == path for path in (build, state, spec_path)),
         'adapter paths must be absolute without symlink aliases')
    need(build.is_dir() and spec_path.name == 'deployment.json', 'fresh deployment.json required')
    need(build.parent.name == 'builds', 'adapter output must be ROOT/builds/REQUEST')
    root = build.parent.parent
    need(not state.is_relative_to(root) and not root.is_relative_to(state),
         'state must be separate from deployment root/builds')
    need(all(Path(p).is_absolute() and os.access(p, os.X_OK) for p in (runtime, bun_path, node_path)),
         'absolute executable runtime, Bun and Node paths required')
    spec = json.loads(spec_path.read_text())
    staged = build / 'cook-recipe.json'
    supplied_recipe = 'recipe_file' in spec.get('config', {})
    allowed = {spec_path}
    content = None
    if supplied_recipe:
        if staged.exists() or staged.is_symlink():
            allowed.add(staged)
            try:
                content = inputs.read_file(staged, spec['config']['recipe_file'])
            except (ValueError, TypeError):
                raise AdapterError('invalid staged recipe') from None
        else:
            try:
                content = inputs.read_file(spec['config']['recipe_file']['path'],
                                           spec['config']['recipe_file'])
            except (ValueError, TypeError, KeyError):
                raise AdapterError('invalid recipe input') from None
    need(set(build.iterdir()) == allowed, 'cook requires a fresh build containing only deployment.json and optional recipe')
    recipe, names = recipe_from_spec(spec, content)
    config, pins = spec['config'], spec['pins']['runtime']
    need(config.get('state_dir') == str(state), 'adapter state must match configured state_dir')
    settings = config.get('cook', {})
    need(isinstance(settings, dict), 'cook settings must be a mapping')
    need({'host_repo', 'connector_state'} <= settings.keys() and
         settings.keys() <= {'host_repo', 'framework_archive', 'host_dependencies', 'connector_state'} and
         ('framework_archive' in settings or 'host_dependencies' in settings),
         'cook requires host_repo, local dependencies and explicit connector_state')
    need(repository(settings['host_repo']), 'cook.host_repo requires a safe HTTPS, SSH/scp or absolute local repository')
    dependencies = dependency_provenance(config, pins['agent-framework'])
    provenance = None
    if 'framework_archive' in settings:
        provenance = framework_provenance(config, pins['agent-framework'])
        (build / 'framework.lock.json').write_text(json.dumps(provenance, sort_keys=True, indent=2) + '\n')
    recipe_path = build / 'cook-recipe.json'
    if supplied_recipe:
        recipe_path.write_bytes(content)
        recipe_path.chmod(0o600)
    else:
        recipe_path.write_text(json.dumps(recipe, sort_keys=True, indent=2) + '\n')
    # Config/credentials are per-request, state/workspace persist. All roots
    # are non-overlapping as required by cook's split layout. Do not assume
    # the parent of an arbitrary external state_dir is writable by this uid.
    request = build.name
    layout = {'schema': 'connectome.host-layout/v1', 'runtime': str(build),
              'config': str(root / 'cook-config' / request),
              'credentials': str(root / 'cook-credentials' / request),
              'state': str(state), 'workspace': str(root / 'workspace'),
              'connectorState': settings['connector_state']}
    layout_path = build / 'cook-layout.json'
    layout_path.write_text(json.dumps(layout, sort_keys=True, indent=2) + '\n')
    argv = [runtime, entrypoint, 'install', str(recipe_path)]
    flag_indices = []
    for flag, value in (
            ('--out', str(build)), ('--no-prompts', None), ('--yes', None), ('--strict', None),
            ('--ch-repo', settings['host_repo']), ('--ch-ref', pins['connectome-host']),
            ('--bun-path', bun_path), ('--node-path', node_path), ('--layout', str(layout_path)),
            *((('--host-dependency', d['name'] + '=' + d['archive'] + '#sha256=' + d['sha256'])
              for d in dependencies)),
            ('--user-unit', 'connectome-' + config['name'] + '.service')):
        flag_indices.append(len(argv))
        argv.append(flag)
        if value is not None:
            argv.append(value)
    # Validate all input before even the verification Git subprocesses spawn.
    validate_arguments(argv, flag_indices)
    env = clean_environment()
    verify_cook(entrypoint, pins['cook'], env)
    command(argv, cwd=build, env=env, flag_indices=flag_indices)
    lock = read_lock(build / 'connectome.lock', host_pin=pins['connectome-host'], required_names=names)
    need(lock.get('layout') == layout and lock.get('state') == {'path': str(state)},
         'cook lock did not preserve the requested persistent layout', 'E_LOCK_STATE_PATH')
    expected = {(entry['repo'], spec['pins']['connectors'][name])
                for name, entry in ({} if supplied_recipe else config.get('connectors', {})).items()}
    actual = {(entry.get('url'), entry.get('commit')) for entry in lock['components']}
    if not supplied_recipe:
        need(actual == expected and all(c['kind'] == 'git' and c['role'] == 'mcp' for c in lock['components']),
             'cook component provenance differs from enabled inputs')
    # Validate cook's inventory before adding config-only provenance. Legacy
    # configs never rewrite cook's lock, argv or build identity.
    expected = {d['name']: d for d in dependencies}
    need(len(lock.get('hostDependencies', [])) == len(expected) and
         {d['name'] for d in lock.get('hostDependencies', [])} == set(expected),
         'cook dependency inventory differs from inputs', 'E_LOCK_INVALID')
    for dep in lock.get('hostDependencies', []):
        record = expected[dep['name']]
        need(dep['version'] == record['version'] and dep['sha256'] == record['sha256'],
             'cook dependency identity differs from inputs', 'E_LOCK_INVALID')
    if records_metadata(config):
        # Keep cook's raw schema (not the reader's derived units) on disk.
        path = build / 'connectome.lock'
        raw = json.loads(path.read_text())
        for dep in raw['hostDependencies']:
            dep['provenance'] = expected[dep['name']]
        path.write_text(json.dumps(raw, sort_keys=True, indent=2) + '\n')
    lock = read_lock(build / 'connectome.lock', host_pin=pins['connectome-host'], required_names=names,
                     config=config, framework_pin=pins['agent-framework'], framework_record=provenance)
    # No values were supplied. Refuse unexpected value-bearing generated env,
    # so run.sh cannot override the role's runtime EnvironmentFile secrets.
    env_file = Path(layout['config']) / '.env'
    need(not env_file.exists() or not env_file.read_text().strip(), 'cook unexpectedly wrote runtime env values')
    return lock


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, json.dumps(diagnostic('E_ADAPTER_INPUT')) + '\n')


def main():
    parser = SafeParser(description=__doc__)
    for option in ('spec', 'out', 'state', 'runtime', 'entrypoint', 'bun-path', 'node-path'):
        parser.add_argument('--' + option, required=True)
    args = parser.parse_args()
    try:
        adapt(args.spec, args.out, args.state, args.runtime, args.entrypoint, args.bun_path, args.node_path)
    except SafeError as error:
        parser.exit(1, json.dumps(error.public()) + '\n')
    except LockError:
        parser.exit(1, json.dumps(diagnostic('E_LOCK_INVALID')) + '\n')
    except Exception:
        parser.exit(1, json.dumps(diagnostic('E_ADAPTER_INPUT')) + '\n')
