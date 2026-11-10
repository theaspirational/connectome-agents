"""Closed data-only v1 template and instance contracts (no execution policy)."""
from copy import deepcopy
import re

from .data import BUILD_BINDING, canonical, fields, json_data, match, need
from .sources import absolute
from .seed import seed_policy

NAME = r'[a-z][a-z0-9-]{0,62}'
CATALOG_NAME = r'[a-z0-9][a-z0-9_-]{0,62}'
LOGICAL = r'[A-Za-z][A-Za-z0-9_-]{0,62}'
MODULES = frozenset(('activity', 'channelMode', 'fleet', 'history', 'identity',
                     'instructions', 'lessons', 'mcplAdmin', 'notices', 'retrieval',
                     'subagents', 'subscriptionGc', 'wake', 'webui'))
FORBIDDEN = frozenset(('command', 'commands', 'cmd', 'args', 'argv', 'shell', 'exec',
                       'execute', 'script', 'scripts', 'hook', 'hooks', 'run', 'build',
                       'install', 'entrypoint', 'source', 'external', 'env', 'environment'))
PLACEHOLDER = re.compile(r'\$\{|\{\{|<[^>]+>|\b(?:TODO|TBD|CHANGEME|REPLACE[_ -]?ME|'
                         r'YOUR[_ -](?:NAME|AGENT|CHANNEL|PROMPT))\b', re.I)


def concrete(value):
    need(type(value) is str and bool(value.strip()) and value == value.strip() and
         not PLACEHOLDER.search(value) and BUILD_BINDING not in value and value.casefold() not in
         {'placeholder', 'name', 'summaryparticipant', 'systemprompt', 'channel',
          'home channel', 'none', 'null', 'changeme', 'todo', 'tbd'},
         'nonempty concrete value required')
    need(not any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in value),
         'control characters refused')
    return value


def data_only(value):
    json_data(value)
    need(len(canonical(value)) <= 1024 * 1024, 'document too large')
    def walk(item):
        if type(item) is dict:
            for key, val in item.items():
                need(re.sub('[^a-z]', '', key.lower()) not in FORBIDDEN,
                     'executable fields forbidden in data')
                walk(val)
        elif type(item) is list:
            for val in item:
                walk(val)
        elif type(item) is str:
            concrete(item)
    walk(value)


def names(value, allowed=None):
    need(type(value) is list and all(match(v, CATALOG_NAME) if allowed is None else v in allowed
                                    for v in value if type(v) is str) and
         all(type(v) is str for v in value) and len(value) == len(set(value)),
         'unique catalog names required')


def positive(value):
    need(type(value) is int and 0 < value <= 10000000, 'bounded positive integer required')


def template(value):
    need(not (type(value) is dict and value.get('schema') == 'connectome.spawn-template/v1'),
         'migrate template v1 to v2: replace seedPolicy with seed.mode')
    data_only(value)
    fields(value, ('schema', 'runtime', 'agent', 'connectors', 'modules', 'seed'), ('preflights', 'identity'))
    need(value['schema'] == 'connectome.spawn-template/v2', 'unsupported template schema')
    names(value['runtime'])
    names(value.get('preflights', []))
    names(value['connectors'])
    names(value['modules'], MODULES)
    seed_policy(value['seed'])
    from .identity import identity_policy
    need('identity' not in value or type(value['identity']) is dict, 'identity policy must be an object')
    identity_policy(value.get('identity'))
    agent = value['agent']
    fields(agent, ('provider', 'model', 'systemPrompt', 'strategy'),
           ('contextBudgetTokens', 'maxStreamTokens', 'responses', 'codex', 'proseRouting'))
    for field in ('provider', 'model'):
        if field in agent:
            need(match(agent[field], r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}'), 'invalid model/provider name')
    need(agent['provider'] in ('anthropic', 'openai-responses', 'openai-codex', 'openrouter', 'bedrock', 'mock'),
         'unsupported provider')
    concrete(agent['systemPrompt'])
    strategy = agent['strategy']
    ints = {'recentWindowTokens', 'headWindowTokens', 'targetChunkTokens',
            'maxMessageTokens', 'maxSpeculativeL1s', 'mergeThreshold', 'compressionRecallBudgetTokens'}
    bools = {'adaptiveResolution', 'enforceBudget'}
    ratios = {'compressionSlackRatio', 'overBudgetGraceRatio'}
    fields(strategy, ('type',), ints | bools | ratios | {'foldingStrategy', 'compressionModel'})
    need(strategy['type'] == 'autobiographical', 'unsupported strategy')
    if 'compressionModel' in strategy:
        need(match(strategy['compressionModel'], r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}'), 'invalid compression model')
    if 'foldingStrategy' in strategy:
        need(strategy['foldingStrategy'] in ('kv-unified', 'kv-stable', 'flat-profile', 'oldest-first'),
             'unsupported folding strategy')
    for key in ints & strategy.keys():
        positive(strategy[key])
    for key in bools & strategy.keys():
        need(type(strategy[key]) is bool, 'boolean strategy setting required')
    for key in ratios & strategy.keys():
        need(type(strategy[key]) in (int, float) and 0 <= strategy[key] <= 1, 'ratio out of range')
    if 'recentWindowTokens' in strategy and 'contextBudgetTokens' in agent:
        positive(agent['contextBudgetTokens'])
        need(strategy['recentWindowTokens'] <= agent['contextBudgetTokens'], 'tail exceeds context budget')
    if 'responses' in agent:
        fields(agent['responses'], ('reasoningEffort',))
        need(agent['responses']['reasoningEffort'] in ('none', 'low', 'medium', 'high', 'xhigh', 'max'),
             'unsupported reasoning effort')
    if 'codex' in agent:
        fields(agent['codex'], ('fastMode',))
        need(type(agent['codex']['fastMode']) is bool, 'boolean fastMode required')
    for key in ('contextBudgetTokens', 'maxStreamTokens'):
        if key in agent:
            positive(agent[key])
    if 'proseRouting' in agent:
        need(agent['proseRouting'] in ('locus', 'explicit', 'hybrid', 'disabled'), 'invalid prose routing')
    result = deepcopy(value)
    result['identity'] = identity_policy(value.get('identity'))
    return result


def instance(value):
    data_only(value)
    fields(value, ('schema', 'instance', 'unixName', 'agent', 'homeDirectory', 'home', 'rights', 'target'))
    need(value['schema'] == 'connectome.spawn-instance/v1', 'unsupported instance schema')
    need(match(value['instance'], NAME) and match(value['unixName'], NAME), 'invalid instance/unix name')
    fields(value['agent'], ('logicalName',))
    need(match(value['agent']['logicalName'], LOGICAL), 'explicit logicalName required')
    absolute(value['homeDirectory'])
    fields(value['home'], ('transport', 'channel'))
    need(match(value['home']['transport'], CATALOG_NAME), 'transport catalog name required')
    need(match(value['home']['channel'], r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}') and
         '..' not in value['home']['channel'].split('/'), 'literal home channel required')
    fields(value['rights'], ('administration',))
    need(type(value['rights']['administration']) is bool, 'explicit administration boolean required')
    fields(value['target'], ('os', 'architecture'))
    need(value['target']['os'] == 'linux' and value['target']['architecture'] in ('x86_64', 'aarch64'),
         'unsupported target')
    return deepcopy(value)


def name_record(value):
    """Initiator-written data, not an approval or executable configuration."""
    import unicodedata
    json_data(value)
    fields(value, ('name', 'displayName', 'summaryParticipant'), ('systemPrompt',))
    for key, limit in (('name', 63), ('displayName', 128), ('systemPrompt', 65536),
                       ('summaryParticipant', 63)):
        if key not in value:
            continue
        concrete(value[key])
        need(len(value[key]) <= limit and not any(
            unicodedata.category(c).startswith('C') and not (key == 'systemPrompt' and c in '\n\t')
            for c in value[key]), 'unsafe name record value')
    need(match(value['name'], LOGICAL) and match(value['summaryParticipant'], LOGICAL),
         'unsafe identity identifier')
    need(match(value['displayName'], r'[\w .-]+'), 'unsafe display name')
    return deepcopy(value)


def read_name_record(path, *, ingress_root=None, with_provenance=False):
    """Single name ingress: a legacy file path or an explicit file/repo source.

    Repo identity is the canonical local repository path, not a remote URL or
    an authorization claim. A repo source always reads committed object bytes.
    """
    from pathlib import Path
    from .data import decode, digest
    from .sources import pinned_blob, read_file
    source = path
    if type(source) is not dict:
        source = {'kind': 'file', 'path': source}
    need(source.get('kind') in ('file', 'repo'), 'unsupported name record source')
    if source['kind'] == 'file':
        fields(source, ('kind', 'path'))
        raw = read_file(source['path'], root=ingress_root)
        provenance = {'kind': 'file', 'path': str(Path(source['path']).absolute())}
    else:
        fields(source, ('kind', 'repo', 'commit', 'path'))
        need(ingress_root is None, 'file ingress root does not authorize a repository source')
        raw, provenance = pinned_blob(source['repo'], source['commit'], source['path'])
    record = name_record(decode(raw))
    result = {'record': record, 'recordHash': digest(record), 'source': provenance}
    return result if with_provenance else record


def name_receipt(value):
    """Validate persisted provenance without rereading a mutable local source."""
    from .sources import COMMIT, absolute, relative
    fields(value, ('recordHash', 'source'))
    need(match(value['recordHash'], r'[0-9a-f]{64}'), 'invalid name record hash')
    source = value['source']
    need(type(source) is dict and source.get('kind') in ('file', 'repo'), 'invalid name source')
    if source['kind'] == 'file':
        fields(source, ('kind', 'path'))
        need(type(source['path']) is str and source['path'].startswith('/') and
             '..' not in source['path'].split('/'), 'invalid name file path')
    else:
        fields(source, ('kind', 'repo', 'commit', 'path', 'blob', 'author', 'committer'))
        absolute(source['repo'])
        relative(source['path'])
        need(all(match(source[key], COMMIT) and source[key] != '0' * 40 for key in ('commit', 'blob')),
             'invalid pinned name object')
        need(all(type(source[key]) is str for key in ('author', 'committer')), 'invalid provenance')
    return value


def activation_identity(value, home):
    record = name_record(value)
    fields(home, ('transport', 'channel'))
    concrete(home['transport'])
    concrete(home['channel'])
    need(match(home['transport'], CATALOG_NAME) and
         match(home['channel'], r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}') and
         '..' not in home['channel'].split('/'), 'unsafe home binding')
    return record
