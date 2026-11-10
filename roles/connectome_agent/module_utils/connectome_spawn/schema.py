"""Closed data-only v1 template and instance contracts (no execution policy)."""
from copy import deepcopy
import re

from .data import BUILD_BINDING, canonical, fields, json_data, match, need
from .sources import absolute

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
    data_only(value)
    fields(value, ('schema', 'runtime', 'agent', 'connectors', 'modules', 'seedPolicy'), ('preflights',))
    need(value['schema'] == 'connectome.spawn-template/v1', 'unsupported template schema')
    names(value['runtime'])
    names(value.get('preflights', []))
    names(value['connectors'])
    names(value['modules'], MODULES)
    need(value['seedPolicy'] in ('required', 'clean', 'optional'), 'unknown seed policy')
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
    return deepcopy(value)


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
