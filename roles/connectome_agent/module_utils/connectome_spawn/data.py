"""Bounded, strict JSON/YAML decoding and a versioned canonical JSON encoding."""
import hashlib
import json
import math
import re

import yaml

LIMIT = 1024 * 1024
BUILD_BINDING = '__CONNECTOME_BUILD_DIR__'


class SpawnError(ValueError):
    """Value-free public refusal; input may contain protected prompt text."""


def need(condition, message):
    if not condition:
        raise SpawnError(message)


def fields(value, required, optional=()):
    need(type(value) is dict and set(required) <= value.keys() <= set(required) | set(optional),
         'missing or unknown fields')


def match(value, pattern):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def json_data(value, depth=0):
    need(depth <= 32, 'document nesting limit exceeded')
    if type(value) is dict:
        need(all(type(k) is str for k in value), 'string keys required')
        for key, item in value.items():
            json_data(key, depth + 1)
            json_data(item, depth + 1)
    elif type(value) is list:
        for item in value:
            json_data(item, depth + 1)
    elif type(value) is str:
        need(not any(0xD800 <= ord(c) <= 0xDFFF for c in value), 'invalid Unicode scalar')
    else:
        need(value is None or type(value) in (bool, int) or
             type(value) is float and math.isfinite(value), 'JSON-compatible finite data required')


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(type(key) is str and key not in result, 'duplicate or non-string key')
        result[key] = value
    return result


class StrictLoader(yaml.SafeLoader):
    pass


def mapping(loader, node):
    return unique([(loader.construct_object(k), loader.construct_object(v)) for k, v in node.value])


StrictLoader.add_constructor('tag:yaml.org,2002:map', mapping)


def decode(raw):
    """Auto-detect JSON objects/arrays; malformed JSON never falls back to YAML.

    YAML uses SafeLoader scalar rules, but rejects aliases, tags, merge keys,
    duplicate keys, multiple documents and non-JSON values (including dates).
    """
    try:
        need(type(raw) in (bytes, str) and len(raw) <= LIMIT, 'document too large')
        text = raw.decode('utf-8') if type(raw) is bytes else raw
        need(len(text.encode('utf-8')) <= LIMIT, 'document too large')
        if text.lstrip().startswith(('{', '[')):
            def invalid(_):
                raise SpawnError('nonfinite number')
            value = json.loads(text, object_pairs_hook=unique, parse_constant=invalid)
        else:
            depth = 0
            for event in yaml.parse(text, Loader=StrictLoader):
                need(not isinstance(event, yaml.AliasEvent) and
                     not getattr(event, 'anchor', None) and not getattr(event, 'tag', None),
                     'YAML tags and aliases refused')
                if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                    depth += 1
                    need(depth <= 32, 'document nesting limit exceeded')
                elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                    depth -= 1
            value = yaml.load(text, Loader=StrictLoader)
        json_data(value)
        need(type(value) is dict, 'document must be an object')
        return value
    except (ValueError, TypeError, UnicodeError, yaml.YAMLError, RecursionError):
        raise SpawnError('invalid JSON/YAML document') from None


def canonical(value):
    """v1: sorted keys, UTF-8, no ASCII escaping, whitespace or trailing LF.

    Array order and number types are significant; no Unicode normalization.
    This is a Python encoding contract, not a claim of RFC 8785 compliance.
    """
    json_data(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()
