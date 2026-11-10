"""Read-only compiler; a plan is neither authorization nor a deployment receipt."""
from copy import deepcopy
import hashlib
from pathlib import PurePosixPath
import re
import shlex

from .catalog import (artifact_relative, local_roots, pin, resolve, resolve_runtime, select, validate_catalog)
from .data import BUILD_BINDING, canonical, digest, fields, json_data, match, need
from .schema import concrete, instance, template

ARTIFACT_TOKEN = re.compile(r'\{artifact:([a-z0-9][a-z0-9_-]{0,62})\}')


def symbolic_fields(value, path=''):
    """JSON pointers to *every* scalar containing the fixed build token."""
    found = []
    if type(value) is dict:
        for key, item in sorted(value.items()):
            escaped = key.replace('~', '~0').replace('/', '~1')
            found.extend(symbolic_fields(item, path + '/' + escaped))
    elif type(value) is list:
        for index, item in enumerate(value):
            found.extend(symbolic_fields(item, path + '/' + str(index)))
    elif type(value) is str and BUILD_BINDING in value:
        found.append(path)
    return found


def require_bound(value):
    """Mandatory admission guard for future apply inputs, NOT an apply method.

    Draft files must not be installed/executed as-is. A future apply adapter
    must bind the actual role-owned request directory, then run this guard
    and revalidate/hash the resulting recipe and paths before any writes.
    """
    json_data(value)
    need(not symbolic_fields(value), 'unbound build directory in apply input')
    def keys(item):
        if type(item) is dict:
            need(not any(BUILD_BINDING in key for key in item), 'unbound build directory in apply input')
            for val in item.values():
                keys(val)
        elif type(item) is list:
            for val in item:
                keys(val)
    keys(value)
    return deepcopy(value)


def expand(value, selected):
    if type(value) is dict:
        return {k: expand(v, selected) for k, v in value.items()}
    if type(value) is list:
        return [expand(v, selected) for v in value]
    if type(value) is str:
        need(BUILD_BINDING not in value, 'catalog must use artifact references, not build tokens')
        def substitute(found):
            name = found.group(1)
            need(name in selected, 'artifact reference is not selected or required')
            return BUILD_BINDING + '/catalog/' + name
        result = ARTIFACT_TOKEN.sub(substitute, value)
        need('{artifact:' not in result, 'malformed artifact reference')
        # Paths containing artifact references cannot escape their installed root.
        if BUILD_BINDING in result:
            need(not any(part in ('.', '..') for part in result.split('/')) and
                 '\\' not in result, 'artifact path escape')
        return result
    return value


def connector_recipe(name, entry, record, selected, state):
    settings = deepcopy(entry['settings'])
    allowed = {'command', 'args', 'env', 'url', 'transport', 'token', 'toolPrefix',
               'enabledFeatureSets', 'disabledFeatureSets', 'enabledTools', 'disabledTools',
               'reconnect', 'reconnectIntervalMs', 'channelSubscription'}
    if record['kind'] == 'archive':
        allowed.add('data_args')
    need(settings.keys() <= allowed and type(settings.get('command')) is str,
         'connector requires supported native launch metadata')
    concrete(settings['command'])
    need(type(settings.get('args', [])) is list and all(type(a) is str and '\x00' not in a
         for a in settings.get('args', [])), 'invalid connector args')
    names = entry.get('required_secrets', [])
    if 'token' in settings:
        need(type(settings['token']) is str and settings['token'] in ('${' + n + '}' for n in names),
             'connector token must reference a declared secret name')
    environment = settings.get('env', {})
    need(type(environment) is dict and all(match(k, r'[A-Z_][A-Z0-9_]*') and type(v) is str
         for k, v in environment.items()), 'invalid connector env')
    for key, value in environment.items():
        if key in names or re.search('TOKEN|SECRET|PASSWORD|API_KEY', key):
            need(key in names and value == '${' + key + '}', 'secret values forbidden in recipe')
    if record['kind'] == 'archive':
        settings = expand(settings, selected)
        if 'data_args' in settings:
            data = settings.pop('data_args')
            need(type(data) is dict, 'data_args must be a mapping')
            for flag, value in data.items():
                need(match(flag, r'--data-[a-z][a-z0-9-]*') and type(value) is str and
                     value.startswith('/'), 'invalid data argument')
                artifact_relative(value[1:])
                need(PurePosixPath(value).is_relative_to(state), 'data argument outside state')
            settings['args'] = settings.get('args', []) + [k + '=' + v for k, v in data.items()]
        settings['source'] = {'external': True}
    else:
        need(not any('{artifact:' in s for s in (settings['command'], *settings.get('args', []))),
             'legacy source connector cannot reference staged artifacts')
        settings['source'] = {'url': entry['repo'], 'ref': record['commit'],
                              'inContainer': {'path': '/connectors/' + name},
                              'install': {'run': ' && '.join(shlex.join(a) for a in entry.get('build', [])),
                                          'runtime': 'custom'}}
    return settings


def make_plan(template_value, instance_value, catalog, stores, *, observed=None):
    """Shared API: explicit role catalog, local stores and observation only.

    No Ansible vars/facts/become, secrets, target probes, network, or writes.
    All archive and descriptor hashes are computed and compared to catalog
    pins; a self-hash does not authenticate a publisher or qualify a runtime.
    """
    spec, declaration = template(template_value), instance(instance_value)
    trusted = validate_catalog(catalog)
    stores = local_roots(stores)
    observed = deepcopy(observed) if observed is not None else {'deploymentHash': None}
    fields(observed, ('deploymentHash',))
    need(observed['deploymentHash'] is None or match(observed['deploymentHash'], r'[0-9a-f]{64}'),
         'invalid observed deployment hash')
    need(declaration['home']['transport'] == 'console' or
         declaration['home']['transport'] in spec['connectors'], 'home transport not selected')
    selected = select(trusted, spec['connectors'], spec['runtime'], spec.get('preflights', []))
    artifacts = {name: resolve(trusted, name, stores) for name in selected}
    runtime_sources = resolve_runtime(trusted, stores)
    # Local object-store location is not intent; future consumers re-resolve
    # pin_ref and verify the bound content hashes rather than reuse cache paths.
    for ref, record in runtime_sources.items():
        record.pop('repo')
        record['pin_ref'] = ref
    platform = deepcopy(trusted.versions.get('platform', {}))
    from .sources import absolute
    for path, value in platform.items():
        absolute(path)
        need(match(value, r'[0-9a-f]{64}') and value != '0' * 64, 'invalid platform pin')
    logical = declaration['agent']['logicalName']
    agent = deepcopy(spec['agent'])
    agent['name'] = logical
    agent['strategy']['summaryParticipant'] = logical
    recipe = {'name': logical, 'agent': agent, 'modules': {n: True for n in spec['modules']},
              'mcpServers': {}, 'extensions': {}}
    requirements, preflights, exports = set(), {}, {}
    home, name = declaration['homeDirectory'], declaration['instance']
    state = home + '/.local/state/connectome/' + name
    staged = {name for name, record in artifacts.items() if record['kind'] == 'archive'}
    for name in selected:
        entry = trusted.descriptors[name]
        requirements.update(entry.get('required_secrets', []))
        kind = entry.get('kind', 'connector')
        if kind == 'connector':
            recipe['mcpServers'][name] = connector_recipe(name, entry, artifacts[name], staged, state)
        if kind == 'preflight':
            preflights[name] = expand(entry['run'], staged)
        if entry.get('exports'):
            exports[name] = {k: BUILD_BINDING + '/catalog/' + name + '/' + v for k, v in entry['exports'].items()}
    if requirements:
        recipe['requirements'] = {n: {'exposeAs': n, 'required': False} for n in sorted(requirements)}
    home, name = declaration['homeDirectory'], declaration['instance']
    paths = {'runtime': home + '/.local/share/connectome/' + name,
             'config': home + '/.config/connectome/' + name,
             'state': home + '/.local/state/connectome/' + name,
             'workspace': home + '/work/' + name,
             'credentials': home + '/.config/connectome/' + name + '/credentials'}
    layout = dict(paths, schema='connectome.host-layout/v1', connectorState={})
    files = []
    for filename, value in (('cook-recipe.json', recipe), ('cook-layout.json', layout)):
        content = canonical(value).decode('utf-8') + '\n'
        files.append({'path': paths['config'] + '/' + filename, 'mode': '0600',
                      'content': content, 'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()})
    selection = {'descriptors': {n: trusted.descriptors[n] for n in selected},
                 'pins': {n: pin(trusted, n) for n in selected},
                 'runtime': trusted.versions['runtime'], 'platform': platform}
    plan = {'schema': 'connectome.spawn-plan/v1', 'compilerVersion': 1,
            'templateHash': digest(spec), 'instanceHash': digest(declaration),
            'catalogHash': digest(selection), 'catalogSelection': selection, 'instance': declaration,
            'identity': {'instance': name, 'unixName': declaration['unixName'],
                         'logicalName': logical, 'participantNamespace': 'agents/' + logical,
                         'summaryParticipant': logical},
            'runtimeSources': runtime_sources, 'artifacts': artifacts, 'recipe': recipe, 'layout': layout,
            'exports': exports, 'preflights': preflights, 'platformPins': platform,
            'seedPolicy': spec['seedPolicy'], 'home': declaration['home'], 'rights': declaration['rights'],
            'providerActions': [], 'files': files, 'observed': observed,
            'activationAllowed': False, 'filesWritten': False,
            'limitations': ['planning-only', 'seed-policy-not-enforced', 'home-binding-not-provisioned',
                            'rights-not-authorized', 'runtime-closure-not-qualified', 'platform-not-probed']}
    # Fixed symbolic token, never a guessed role request hash. File hashes
    # describe these draft bytes, NOT the eventual bound/installed recipe.
    plan['symbolicFields'] = symbolic_fields(plan)
    plan_hash = digest(plan)
    key = digest({'schema': 'connectome.spawn-idempotence/v1', 'planHash': plan_hash,
                  'target': declaration['target'], 'observed': observed})
    return {'plan': plan, 'planHash': plan_hash, 'idempotenceKey': key}
