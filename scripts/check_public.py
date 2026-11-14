#!/usr/bin/env python3
"""Conservative publication gate. Diagnostics never echo suspected values."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import yaml

NOREPLY_DOMAIN = 'users.noreply.github.com'
# RFC 2606 / RFC 6761 names, matched only at whole DNS-label boundaries.
RESERVED_EMAIL_DOMAINS = ('example.com', 'example.net', 'example.org',
                          'invalid', 'example', 'test', 'localhost')
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")
# Include every local-part character so overlapping addresses stay in one token.
EMAIL_TOKEN = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~@-]+")
ALLOWED_REPOS = {
    'anima-research': {'connectome-host', 'agent-framework', 'connectome-cook',
                       'slack-mcpl', 'github-mcpl', 'terminal-sessions-mcp', 'heartbeat-mcpl'},
    'theaspirational': {'telegram-bot-mcpl', 'connectome-cook', 'slack-mcpl',
                       'zulip_mcp', 'terminal-sessions-mcp'},
    'antra-tess': {'zulip_mcp'},
}


def private_email(content):
    # Allowances must never hide another address in a malformed multi-at-sign chain.
    if any(token.group().count('@') > 1 for token in EMAIL_TOKEN.finditer(content)):
        return True
    return any(domain != NOREPLY_DOMAIN and not any(
               domain == reserved or domain.endswith('.' + reserved)
               for reserved in RESERVED_EMAIL_DOMAINS) for domain in
               (match.group().lower().rsplit('@', 1)[1] for match in EMAIL.finditer(content)))


def unreviewed_repo(content):
    for match in re.finditer(r'https://github\.com/([\w.-]+)/([\w.-]+)', content):
        owner, repo = match.groups()
        if repo.removesuffix('.git') not in ALLOWED_REPOS.get(owner, set()):
            return True
    return False


# Every rule consumes both raw text and the same config-context views.
RULES = [
    ('private path', re.compile('/' + 'srv' + r'(?:/|\b)|/' + 'home' + r'/[\w.-]+|/' + 'opt' + '/' + 'support-agents').search),
    ('IPv4 address', re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b').search),
    ('IPv6 address', re.compile(r'(?<!\w)(?:[a-fA-F0-9]{0,4}:){2,}[a-fA-F0-9:]+(?!\w)').search),
    ('private hostname', re.compile(r'\b[\w-]+\.(?:internal|lan|local|ts\.net)\b', re.I).search),
    ('provider token', re.compile(r'(?:ghp|github_pat|glpat)_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}').search),
    ('API token', re.compile(r'\bsk-[A-Za-z0-9_-]{16,}|\bhvs\.[A-Za-z0-9_-]+').search),
    ('bot token or private key', re.compile(r'\b\d{8,10}:[A-Za-z0-9_-]{30,}|' + '-----BEGIN .*' + 'PRIVATE KEY' + '-----').search),
    ('JWT', re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+').search),
    ('secret store path', re.compile(r'(?i)(?:vault|secret)/[a-z][\w-]+/[\w/-]+').search),
    ('inventory key', re.compile(r'(?im)^\s*(?:ansible_host|ansible_user|hostname|user_name)\s*:').search),
    ('literal credential', re.compile(r"(?im)^\s*(?:[A-Z_]*(?:TOKEN|PASSWORD|API_KEY|SECRET))\s*[:=]\s*(['\"])[^\r\n]+\1\s*$").search),
    ('non-public email address', private_email),
    ('unreviewed repository identity', unreviewed_repo),
]


def load_denylist(paths):
    """Read private policy from external UTF-8 files, never from public defaults."""
    terms = set()
    for path in paths:
        for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
            term = line.strip()
            if term and not term.startswith('#'):
                terms.add(term)
    return sorted(terms)


def rules_with_denylist(terms):
    rules = list(RULES)
    if terms:
        # Literal terms, case-insensitive, bounded by non-word characters.
        pattern = re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(term) for term in terms) + r')(?!\w)', re.I)
        rules.append(('denied identifier', pattern.search))
    return rules

# Only these existing Ansible expressions need physical multiline scalars.
# Entries are (repository-relative file, exact node path); never basename/glob
# exemptions. They relax multiline syntax only, not escapes or publication rules.
MULTILINE_SCALARS = {
    ('roles/connectome_agent/tasks/main.yml',
     (3, 'ansible.builtin.set_fact', 'connectome_effective')),
    ('roles/connectome_agent/tasks/main.yml',
     (5, 'block', 1, 'ansible.builtin.set_fact', 'connectome_resolved_secrets')),
}
CONFIG_SUFFIXES = {'.yml', '.yaml', '.json'}
SCALAR_POLICY = 'config scalar syntax'


def config_views(content, *, path):
    """Refuse non-plain config syntax before applying rules with key context.

    Single-line quote delimiters are OK (including JSON), but no escape
    sequences, multiline scalars, explicit tags, anchors, aliases or merge keys.
    Compose nodes only: never construct tagged objects.
    """
    views = []
    rejected = False

    def visit(node, location=(), parent='scalar'):
        nonlocal rejected
        if isinstance(node, yaml.ScalarNode):
            source = content[node.start_mark.index:node.end_mark.index]
            multiline = (node.style in ('|', '>') or
                         any(char in source + node.value for char in '\r\n\x85\u2028\u2029'))
            allowed = (path, location) in MULTILINE_SCALARS
            # Doubled apostrophes are YAML single-quote escapes, too.
            if ('\\' in source or (node.style == "'" and "''" in source[1:-1]) or
                    multiline and not allowed or node.tag == 'tag:yaml.org,2002:merge'):
                rejected = True
            # Scan physical AND logical lines for the narrow multiline exceptions.
            for line in node.value.splitlines() + (source.splitlines() if allowed else []):
                views.extend((line, 'scalar: ' + line, line + ':',
                              parent + ': "' + line + '"'))
        elif isinstance(node, yaml.SequenceNode):
            for index, value in enumerate(node.value):
                visit(value, location + (index,), parent)
        elif isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if not isinstance(key, yaml.ScalarNode):
                    rejected = True
                    continue
                visit(key, location + ('<key>',))
                visit(value, location + (key.value,), key.value)

    try:
        if Path(path).suffix.lower() == '.json':
            # JSON config must actually be JSON, not merely YAML-compatible text.
            json.loads(content)
        tokens = tuple(yaml.scan(content, Loader=yaml.SafeLoader))
        if any(isinstance(token, (yaml.tokens.TagToken, yaml.tokens.AnchorToken,
                                  yaml.tokens.AliasToken)) for token in tokens):
            return [], True
        for node in yaml.compose_all(content, Loader=yaml.SafeLoader):
            if node is not None:
                visit(node)
    except (yaml.YAMLError, ValueError, RecursionError):
        return [], True
    return ([], True) if rejected else (views, False)


def hits(content, *, config_path=None, rules=None):
    # Raw text remains mandatory for every file, path and metadata field.
    views = [content]
    rejected = False
    if config_path is not None:
        extra, rejected = config_views(content, path=config_path)
        views.extend(extra)
    found = [name for name, rule in (RULES if rules is None else rules)
             if any(rule(view) for view in views)]
    if rejected:
        found.append(SCALAR_POLICY)
    return found


def range_hits(revision_range, *, rules=None):
    # %B includes the subject AND full body. All active rules apply to identities.
    output = subprocess.check_output([
        'git', 'log', '--no-show-signature',
        '--format=%H%x00%B%x00Author %an <%ae>%x00Committer %cn <%ce>%x00',
        '--end-of-options', revision_range, '--'], stderr=subprocess.DEVNULL)
    fields = output.decode('utf-8').split('\0')
    for index in range(0, len(fields) - 1, 4):
        commit, message, author, committer = fields[index:index + 4]
        for label, value in [('message', message), ('author', author), ('committer', committer)]:
            found = hits(value, rules=rules)
            if found:
                yield commit.strip(), label, found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--range', dest='revision_range', help='also scan commit messages and identities in A..B')
    parser.add_argument('--deny-file', action='append', default=[], metavar='PATH',
                        help='external UTF-8 denylist; repeatable, combined with CONNECTOME_PUBLIC_DENYLIST')
    parser.add_argument('--require-denylist', action='store_true',
                        help='fail closed unless the external denylist has at least one term')
    parser.add_argument('files', nargs='*')
    args = parser.parse_args(argv)
    deny_paths = args.deny_file + ([os.environ['CONNECTOME_PUBLIC_DENYLIST']]
                                 if os.environ.get('CONNECTOME_PUBLIC_DENYLIST') else [])
    try:
        terms = load_denylist(deny_paths)
    except (OSError, UnicodeError, ValueError):
        parser.error('unable to read external denylist')
    if not terms:
        if args.require_denylist:
            parser.error('--require-denylist needs a nonempty external denylist')
        print('WARNING: no denylist terms loaded; running generic rules only. '
              'Private identifiers are NOT checked.', file=sys.stderr)
    rules = rules_with_denylist(terms)
    if args.files:
        files = [Path(p) for p in args.files]
    else:
        output = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'])
        files = [Path(name.decode()) for name in output.split(b'\0') if name]
    failed = False
    for path in sorted(set(files)):
        if not path.is_file():
            continue
        found = hits(path.as_posix(), rules=rules) + hits(
            path.read_text(encoding='utf-8'),
            config_path=path.as_posix() if path.suffix.lower() in CONFIG_SUFFIXES else None,
            rules=rules)
        if found:
            print(f'{path}: rejected public-content rule(s) {found}', file=sys.stderr)
            failed = True
    if args.revision_range:
        try:
            for commit, label, found in range_hits(args.revision_range, rules=rules):
                print(f'{commit} {label}: rejected public-content rule(s) {found}', file=sys.stderr)
                failed = True
        except (subprocess.CalledProcessError, UnicodeError):
            print('rejected: unable to scan requested commit range', file=sys.stderr)
            failed = True
    if not failed:
        scope = 'working tree + requested commit range' if args.revision_range else 'working tree; no Git metadata'
        print(f'PASS: public content scan ({scope}; not all refs)')
    return int(failed)


if __name__ == '__main__':
    sys.exit(main())
