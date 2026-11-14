import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('public_check', Path(__file__).resolve().parents[1] / 'scripts/check_public.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class PublicTests(unittest.TestCase):
    DENY_TERMS = ('examplecorp', 'agent-zeta')

    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        (root / '.cache').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=root / '.cache')
        self.addCleanup(self.tmp.cleanup)
        self.deny_file = Path(self.tmp.name) / 'deny.txt'
        self.deny_file.write_text('# Synthetic policy only\n\n' + '\n'.join(self.DENY_TERMS) + '\n')
        # Unit and CLI tests both load synthetic policy from an external file.
        rules = checker.rules_with_denylist(checker.load_denylist([self.deny_file]))
        policy = patch.object(checker, 'RULES', rules)
        policy.start()
        self.addCleanup(policy.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('CONNECTOME_PUBLIC_DENYLIST', None)

    # Each rule has a fixture; generic secret/address samples are constructed.
    RULE_SAMPLES = [
        ('denied identifier', 'examplecorp'),
        ('private path', '/' + 'home' + '/private-account/config'),
        ('IPv4 address', '.'.join(['192', '0', '2', '1'])),
        ('IPv6 address', ':'.join(['2001', 'db8', '', '1'])),
        ('private hostname', 'worker' + '.internal'),
        ('provider token', 'ghp_' + 'z' * 32),
        ('API token', 'sk-' + 'z' * 32),
        ('bot token or private key', '12345678' + ':' + 'z' * 32),
        ('JWT', 'eyJ' + 'abc.def.ghi'),
        ('secret store path', 'vault' + '/example/credential'),
        ('inventory key', 'ansible_' + 'host'),
        ('literal credential', 'EXAMPLE_' + 'TOKEN: "literal-value"'),
        ('non-public email address', 'person' + '@' + 'company.io'),
        ('unreviewed repository identity', 'https://github.' + 'com/unreviewed/connector.git'),
    ]

    @staticmethod
    def escaped(sample, kind):
        return ''.join(('\\' + kind + {'x': '%02x', 'u': '%04x', 'U': '%08x'}[kind]) % ord(char)
                       for char in sample)

    def test_single_rule_table_on_plain_config_keys_and_values(self):
        self.assertEqual({name for name, _ in self.RULE_SAMPLES},
                         {name for name, _ in checker.RULES})
        for name, sample in self.RULE_SAMPLES:
            # Include root scalars, mapping keys/values, nested sequence members.
            quoted = "'" + sample + "'"
            for body in (quoted, quoted + ': example',
                         'outer: [{example: ' + quoted + '}]'):
                with self.subTest(rule=name, body=body):
                    self.assertIn(name, checker.hits(body, config_path='probe.yml'))

    def test_earlier_escaped_key_and_value_probes_are_refused(self):
        for name, sample in self.RULE_SAMPLES + [('neutral', 'example')]:
            for kind in ('x', 'u', 'U'):
                encoded = self.escaped(sample, kind)
                for body in ('"' + encoded + '": example',
                             'example: "' + encoded + '"'):
                    for document in (body, 'outer: [{' + body + '}]',
                                     '? {' + body + '}\n: example'):
                        with self.subTest(rule=name, kind=kind, document=document):
                            self.assertIn(checker.SCALAR_POLICY,
                                          checker.hits(document, config_path='probe.yaml'))
        for key in ('ansible_' + 'user', 'EXAMPLE_' + 'TOKEN'):
            for kind in ('x', 'u', 'U'):
                for escape_key, escape_value in ((True, False), (False, True), (True, True)):
                    encoded_key = self.escaped(key, kind) if escape_key else key
                    value = self.escaped('literal-value', kind) if escape_value else 'literal-value'
                    body = '{"' + encoded_key + '": "' + value + '"}'
                    self.assertIn(checker.SCALAR_POLICY,
                                  checker.hits(body, config_path='probe.yml'))

    def test_review_five_multiline_names_and_credentials_refused(self):
        person = 'agent-zeta'
        for key, value in (('owner', 'neutral\n' + person),
                           ('EXAMPLE_' + 'TOKEN', 'first\nsecond')):
            # Review 5: decoded newline in a quoted scalar, both block styles,
            # physical quoted/plain multiline, escaped continuation, JSON values.
            bodies = [key + ': ' + json.dumps(value), json.dumps({key: value}),
                      key + ': |\n  ' + value.replace('\n', '\n  ') + '\n',
                      key + ': >-\n  ' + value.replace('\n', '\n  ') + '\n',
                      key + ': "' + value.replace('\n', '\n  ') + '"',
                      key + ": '" + value.replace('\n', '\n  ') + "'",
                      key + ': ' + value.replace('\n', '\n  '),
                      key + ': "' + value.replace('\n', chr(92) + '\n  ') + '"']
            for suffix in ('.yaml', '.yml', '.json'):
                for body in bodies:
                    with self.subTest(suffix=suffix, body=body):
                        self.assertIn(checker.SCALAR_POLICY,
                                      checker.hits(body, config_path='probe' + suffix))
        for key in ('owner\n' + person, 'EXAMPLE_\nTOKEN', 'ansible_\nhost'):
            for body in (json.dumps({key: 'neutral'}),
                         '? |\n  ' + key.replace('\n', '\n  ') + '\n: neutral'):
                self.assertIn(checker.SCALAR_POLICY,
                              checker.hits(body, config_path='probe.yaml'))

    def test_config_refuses_nonplain_syntax_even_when_neutral(self):
        probes = ['owner: !!binary ZXhhbXBsZQ==', 'owner: !!str example',
                  'owner: !custom example', '&loop [*loop, example]',
                  'first: &name example\nsecond: *name', '<<: {}',
                  'owner: |\n  example\n', 'owner: >\n  example\n',
                  '"own' + chr(92) + 'ner": example',
                  "owner: 'it''s neutral'", 'owner: "ex' + chr(92) + '/ample"',
                  'owner: "first' + chr(92) + 'tsecond"',
                  '? [example]\n: neutral', 'owner: [', '%TAG !e! tag:example.invalid,2026:\n---\n!e!x example']
        for probe in probes:
            with self.subTest(probe=probe):
                self.assertIn(checker.SCALAR_POLICY,
                              checker.hits(probe, config_path='probe.yml'))
        for probe in ('{example: safe}', '{"example": "safe",}', ''):
            self.assertIn(checker.SCALAR_POLICY,
                          checker.hits(probe, config_path='probe.json'))

    def test_plain_config_and_json_safe_values_allowed(self):
        for body in ('name: example', 'owner: "Example Contributor"',
                     "owner: 'café'", 'required_secrets: [EXAMPLE_API_KEY]',
                     '---\nname: example\n---\nother: safe',
                     'name: example # safe comment', 'empty: null',
                     'number: 42\nenabled: true', '"00000000"',
                     'https://github.com/anima-research/connectome-cook.git'):
            self.assertFalse(checker.hits(body, config_path='probe.yml'), body)
        for body in ('{"name": "example", "enabled": true, "count": 1}',
                     '["example", null, 2]', '"example"'):
            self.assertFalse(checker.hits(body, config_path='probe.json'), body)
        # Delimiters inside otherwise unescaped credentials cannot bypass context.
        for value in ('literal-value', "one'two", 'one"two'):
            body = "EXAMPLE_" + "TOKEN: '" + value + "'"
            self.assertIn('literal credential', checker.hits(body, config_path='probe.yml'))

    def test_allowlist_is_exact_and_checks_every_rule_on_each_line(self):
        root = Path(__file__).resolve().parents[1]
        path = 'roles/connectome_agent/tasks/main.yml'
        content = (root / path).read_text()
        self.assertFalse(checker.hits(content, config_path=path))
        # Same basename/content elsewhere, or an extra block in the allowed file,
        # is not exempt. Nor are escapes/tags in an allowlisted scalar.
        self.assertIn(checker.SCALAR_POLICY,
                      checker.hits(content, config_path='other/' + path))
        self.assertIn(checker.SCALAR_POLICY, checker.hits(
            content + '\n- extra: |\n    neutral\n', config_path=path))
        for name, sample in self.RULE_SAMPLES:
            with self.subTest(rule=name), patch.object(
                    checker, 'MULTILINE_SCALARS', {('probe.yml', ('command',))}):
                body = 'command: |\n  neutral\n  ' + sample + '\n'
                self.assertIn(name, checker.hits(body, config_path='probe.yml'))
        key = 'EXAMPLE_' + 'TOKEN'
        with patch.object(checker, 'MULTILINE_SCALARS', {('probe.yml', (key,))}):
            self.assertIn('literal credential', checker.hits(
                key + ': |\n  first\n  second\n', config_path='probe.yml'))
            self.assertIn(checker.SCALAR_POLICY, checker.hits(
                key + ': !!str |\n  neutral\n', config_path='probe.yml'))
            self.assertIn(checker.SCALAR_POLICY, checker.hits(
                key + ': "neutral' + chr(92) + 'nneutral"', config_path='probe.yml'))

    def test_multiline_exception_preserves_each_physical_line_parent(self):
        seen = []
        with patch.object(checker, 'MULTILINE_SCALARS', {('probe.yml', ('command',))}), patch.object(
                checker, 'RULES', [('record', lambda view: seen.append(view) or False)]):
            self.assertFalse(checker.hits(
                'command: >-\n  first\n  second\n', config_path='probe.yml'))
        # Folded YAML has one logical value; physical lines must not disappear.
        for line in ('  first', '  second', 'first second'):
            self.assertIn('command: "' + line + '"', seen)

    def test_raw_scan_all_inputs_including_docs_and_comments(self):
        for name, sample in self.RULE_SAMPLES:
            if name == 'inventory key':
                sample += ': example'
            with self.subTest(rule=name):
                self.assertIn(name, checker.hits('Text example\n' + sample))
        host = 'worker' + '.internal'
        self.assertIn('private hostname', checker.hits(
            '# ' + host + '\nname: example', config_path='probe.yml'))
        # Markdown is raw text, not a config parser/decoder; examples may show
        # nonplain syntax without themselves violating the scalar policy.
        self.assertFalse(checker.hits('```yaml\nowner: |\n  Example Contributor\n```'))

    def test_cli_refuses_yaml_and_json_and_redacts_diagnostics(self):
        root = Path(__file__).resolve().parents[1]
        (root / '.cache').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root / '.cache') as tmp:
            for suffix in ('.YAML', '.yml', '.JSON'):
                path = Path(tmp) / ('probe' + suffix)
                for name, sample in self.RULE_SAMPLES:
                    encoded = self.escaped(sample, 'u')
                    for body in ('{"' + encoded + '": "example"}',
                                 '{"example": "' + encoded + '"}'):
                        path.write_text(body, encoding='utf-8')
                        result = subprocess.run(
                            [sys.executable, str(root / 'scripts/check_public.py'), path.name],
                            cwd=tmp, capture_output=True, text=True)
                        with self.subTest(rule=name, suffix=suffix, body=body):
                            self.assertEqual(result.returncode, 1)
                            self.assertIn(checker.SCALAR_POLICY, result.stderr)
                            self.assertNotIn(sample, result.stderr)
                path.write_text('{"example": "safe"}', encoding='utf-8')
                result = subprocess.run(
                    [sys.executable, str(root / 'scripts/check_public.py'), path.name],
                    cwd=tmp, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0)

    def test_private_identifiers_addresses_paths_and_values_rejected(self):
        samples = [*self.DENY_TERMS, '/' + 'home' + '/private-account/config',
                   '/' + 'srv' + '/data', '/' + 'opt' + '/support-agents/example',
                   '.'.join(['192', '0', '2', '1']), ':'.join(['2001', 'db8', '', '1']),
                   'worker' + '.internal', 'ansible_' + 'user: account',
                   'ghp_' + 'z' * 32, 'xoxb-' + 'z' * 32,
                   'https://github.' + 'com/unreviewed/connector.git',
                   'EXAMPLE_' + 'TOKEN: "literal-value"']
        for sample in samples:
            with self.subTest(kind=len(sample)):
                self.assertTrue(checker.hits(sample))

    def test_neutral_names_secret_names_and_public_upstreams_allowed(self):
        for content in ('name: example', 'required_secrets: [EXAMPLE_API_KEY]',
                        'https://github.com/anima-research/connectome-cook.git'):
            self.assertFalse(checker.hits(content))


    def test_denylisted_config_values_and_agent_lists_rejected(self):
        person = 'agent-zeta'
        samples = ['name: ' + person, 'name: "' + person + '"',
                   'owner: ' + person, 'agent_names: [example, ' + person + ']',
                   'agents:\n  - ' + person, '{"name": "' + person + '"}',
                   'names = ["' + person + '"]']
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertTrue(checker.hits(sample))
        for name in self.DENY_TERMS:
            self.assertTrue(checker.hits('agents: [example, ' + name + ']'))

    def test_denylist_terms_are_literal_case_insensitive_and_bounded(self):
        self.deny_file.write_text('# comment\n\n examplecorp \nagent-zeta\nexample+team\n')
        # Do not rely on setUp's patched table for this policy construction.
        with patch.object(checker, 'RULES', []):
            rules = checker.rules_with_denylist(checker.load_denylist([self.deny_file]))
        for term in ('EXAMPLECORP', 'Agent-Zeta', 'example+team'):
            self.assertIn('denied identifier', checker.hits('Text: ' + term, rules=rules))
        for term in ('examplecorpus', 'myagent-zeta', 'exampleeeeteam', 'comment'):
            self.assertFalse(checker.hits(term, rules=rules))

    def test_cli_external_policy_and_fail_closed(self):
        root = Path(__file__).resolve().parents[1]
        probe = Path(self.tmp.name) / 'probe.md'
        probe.write_text('examplecorp and agent-zeta\n')

        def scan(*options, env_policy=None, wrapper=False):
            env = dict(os.environ)
            if env_policy is not None:
                env['CONNECTOME_PUBLIC_DENYLIST'] = str(env_policy)
            command = ([str(root / 'scripts/check-public.sh')] if wrapper else
                       [sys.executable, str(root / 'scripts/check_public.py')])
            cwd = root if wrapper else self.tmp.name
            target = str(probe.relative_to(root)) if wrapper else probe.name
            return subprocess.run(command + list(options) + [target], cwd=cwd,
                                  env=env, capture_output=True, text=True)

        result = scan()
        self.assertEqual(result.returncode, 0)
        self.assertIn('WARNING', result.stderr)
        self.assertIn('generic rules only', result.stderr)
        for wrapper in (False, True):
            self.assertNotEqual(scan('--require-denylist', wrapper=wrapper).returncode, 0)
            result = scan('--deny-file', str(self.deny_file), '--require-denylist', wrapper=wrapper)
            self.assertEqual(result.returncode, 1)
            self.assertIn('denied identifier', result.stderr)
            self.assertNotIn('WARNING', result.stderr)
            for term in self.DENY_TERMS:
                self.assertNotIn(term, result.stderr)

        result = scan('--require-denylist', env_policy=self.deny_file)
        self.assertEqual(result.returncode, 1)
        self.assertIn('denied identifier', result.stderr)
        other = Path(self.tmp.name) / 'other.txt'
        other.write_text('example+team\n')
        probe.write_text('example+team\n')
        # CLI and environment policies are combined, not precedence alternatives.
        self.assertEqual(scan('--deny-file', str(other), env_policy=self.deny_file).returncode, 1)
        probe.write_text('examplecorp\n')
        self.assertEqual(scan('--deny-file', str(other), env_policy=self.deny_file).returncode, 1)
        self.assertEqual(scan('--deny-file', str(other), '--deny-file', str(self.deny_file)).returncode, 1)

        other.write_text('# comments only\n\n')
        result = scan('--deny-file', str(other))
        self.assertEqual(result.returncode, 0)
        self.assertIn('WARNING', result.stderr)
        self.assertNotEqual(scan('--deny-file', str(other), '--require-denylist').returncode, 0)
        for invalid in ('missing.txt', '.'):
            result = scan('--deny-file', str(Path(self.tmp.name) / invalid))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('unable to read external denylist', result.stderr)
        other.write_bytes(bytes([255]))
        self.assertNotEqual(scan('--deny-file', str(other)).returncode, 0)

        # Generic checks remain active without a policy.
        probe.write_text('worker' + '.internal\n')
        result = scan()
        self.assertEqual(result.returncode, 1)
        self.assertIn('private hostname', result.stderr)

    def test_cli_denylist_checks_paths_and_config_context(self):
        root = Path(__file__).resolve().parents[1]
        probe = Path(self.tmp.name) / 'agent-zeta.yml'
        probe.write_text('name: example\n')
        command = [sys.executable, str(root / 'scripts/check_public.py'),
                   '--deny-file', str(self.deny_file), '--require-denylist']
        result = subprocess.run(command + [probe.name], cwd=self.tmp.name,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('denied identifier', result.stderr)
        probe.unlink()
        probe = Path(self.tmp.name) / 'probe.yml'
        for content in ('name: Agent-Zeta', 'examplecorp: neutral',
                        'names: [example, agent-zeta]', '# examplecorp\nname: example'):
            probe.write_text(content)
            result = subprocess.run(command + [probe.name], cwd=self.tmp.name,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('denied identifier', result.stderr)

    def test_email_allowlist(self):
        allowed = ('example.com', 'example.net', 'example.org',
                   'a.example.com', 'a.example.net', 'a.example.org',
                   'deep.a.example.com', 'EXAMPLE.COM', 'A.EXAMPLE.NET',
                   'invalid', 'example', 'test', 'localhost',
                   'example.invalid', 'EXAMPLE.INVALID', 'mail.invalid',
                   'mail.example', 'mail.test', 'mail.localhost',
                   'deep.mail.test', 'MAIL.LOCALHOST',
                   'users.noreply.github.com.evil.invalid',
                   'users.noreply.github.com')
        for domain in allowed:
            for local in ('noreply', 'contributors', 'person', 'first.last+tag', 'noreply+person'):
                address = local + '@' + domain
                with self.subTest(address=address):
                    self.assertFalse(checker.hits(address))
                    self.assertFalse(checker.hits('--author=Fixture Initiator <' + address + '>'))
                    self.assertFalse(checker.hits('user.email=' + address))
        rejected = ('gmail.com', 'company.io', 'example.io', 'example.co', 'example.dev',
                    'example.invalid.attacker.com', 'notexample.com', 'badexample.com',
                    'example.com.evil.io', 'example.net.evil.io', 'example.org.evil.io',
                    'badexample.net', 'badexample.org', 'notinvalid', 'nottest',
                    'notexample', 'notlocalhost', 'mail.test.evil.io',
                    'mail.example.evil.io', 'mail.localhost.evil.io',
                    'example.invalid.evil', 'users.noreply.github.com.evil.io',
                    'sub.users.noreply.github.com')
        for domain in rejected:
            address = 'name' + '@' + domain
            with self.subTest(domain=domain):
                self.assertIn('non-public email address', checker.hits(address))
                # One allowed address cannot mask another, rejected address.
                self.assertTrue(checker.private_email('person@example.com ' + address))
        # Construct rejected chains so the publication gate can scan this fixture.
        chains = ('x' + '@' + 'test' + '@' + 'gmail.com',
                  'x' + '@' + 'jane.test' + '@' + 'corp.io',
                  'x' + '@' + 'localhost' + '@' + 'corp.io',
                  'a' + '@' + 'example.com' + '@' + 'gmail.com',
                  'x' + '@' + 'users.noreply.github.com' + '@' + 'gmail.com',
                  'x' + '@' + 'test' + '@' + 'example.com',
                  'x' + '@' + '@' + 'example.com')
        for address in chains:
            with self.subTest(chain=address):
                self.assertIn('non-public email address', checker.hits(address))
        # Reserved domains do not bypass the independent denylist rule.
        self.assertIn('denied identifier', checker.hits('agent-zeta' + '@' + 'example.invalid'))
        self.assertFalse(checker.hits('unit charset [A-Za-z0-9_.@-]+'))

    # Split real-address fixtures so the publication gate can scan its tests.
    EMAIL_REVIEW_PROBES = (
        ('@@ -1,2 +1,2 @@', False),
        ('@@decorator', False),
        ('@scope/pkg@^1.2.3', False),
        ('"@types/node@^22.0.0"', False),
        ('"@types/node@^22.0.0", ...', False),
        ('npm install @scope/pkg@^1.2.3', False),
        ('@scope/pkg@1.2.3', False),
        ('"@types/node@22.0.0"', False),
        ('lodash@4.17.21', False),
        ('name@1.0.0@abc123', False),
        ("import x from '@scope/pkg'", False),
        ("'@scope/pkg'", False),
        ('node_modules/@types/node', False),
        ('"node_modules/@types/node": {', False),
        ('resolved "https://registry.yarnpkg.com/@types/node/-/node-22.0.0.tgz#abc123"', False),
        ('@Component({})', False),
        ('a @ b', False),
        ('"@types/node": "^22"', False),
        ('@scope/pkg', False),
        (' @scope/pkg', False),
        ('"@scope/pkg"', False),
        ('/@scope/pkg', False),
        ('(@scope/pkg)', False),
        ('[@scope/pkg]', False),
        ('@1.2.3', False),
        ('@4.17.21', False),
        ('@22.0.0', False),
        ('x' + '@' + 'test' + '@' + 'gmail.com', True),
        ('x' + '@' + 'test.' + '@' + 'gmail.com', True),
        ('x' + '@' + 'test-' + '@' + 'gmail.com', True),
        ('x' + '@' + 'test.-' + '@' + 'gmail.com', True),
        ('x' + '@' + 'users.noreply.github.com.' + '@' + 'gmail.com', True),
        ('x@test,' + '@gmail.com', False),
        ('x@test:' + '@gmail.com', False),
        ('x@test ' + '@gmail.com', False),
        ('jane.doe' + '@' + 'corp.io', True),
        ('j+tag' + '@' + 'mail.company.co.uk', True),
        ('git' + '@' + 'github.com:org/repo', True),
        ('git' + '@' + 'github.com:anima-research/repo', True),
        # Keep label-bounded allowances and sentence punctuation working.
        ('name' + '@' + 'example.com.evil.io', True),
        ('name' + '@' + 'corp.io.', True),
    )

    def test_email_review_probes(self):
        for content, rejected in self.EMAIL_REVIEW_PROBES:
            with self.subTest(content=content, rejected=rejected):
                self.assertEqual(checker.private_email(content), rejected)
                self.assertEqual('non-public email address' in checker.hits(content), rejected)

    def test_cli_npm_and_patch_content_allowed(self):
        root = Path(__file__).resolve().parents[1]
        fixtures = (
            ('README.md', 'npm install @scope/pkg@^1.2.3\n'),
            ('lodash+4.17.21.patch', '@@ -1,2 +1,2 @@\n'),
            ('package-lock.json', '{"packages": {"node_modules/@types/node": {}}}\n'),
            ('yarn.lock', '"@types/node@^22.0.0":\n'
             '  resolved "https://registry.yarnpkg.com/@types/node/-/node-22.0.0.tgz#abc123"\n'),
        )
        for name, body in fixtures:
            with self.subTest(name=name, body=body):
                probe = Path(self.tmp.name) / name
                probe.write_text(body, encoding='utf-8')
                result = subprocess.run(
                    [sys.executable, str(root / 'scripts/check_public.py'), name],
                    cwd=self.tmp.name, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_commit_range_messages_and_both_identities_scanned(self):
        root = Path(__file__).resolve().parents[1]
        (root / '.cache').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root / '.cache') as tmp:
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
            good = 'noreply@' + 'example.invalid'
            env.update(GIT_AUTHOR_NAME='Contributors', GIT_COMMITTER_NAME='Contributors',
                       GIT_AUTHOR_EMAIL=good, GIT_COMMITTER_EMAIL=good)

            def git(*args, overrides=None):
                return subprocess.check_output(
                    ['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'commit.gpgsign=false', *args],
                    cwd=tmp, env=dict(env, **(overrides or {})), stderr=subprocess.DEVNULL).decode().strip()

            def scan(revisions):
                return subprocess.run([sys.executable, str(root / 'scripts/check_public.py'),
                                       '--deny-file', str(self.deny_file), '--require-denylist',
                                       '--range', revisions], cwd=tmp, env=env,
                                      capture_output=True, text=True)

            git('init', '-q')
            Path(tmp, 'neutral.yml').write_text('name: example\n')
            git('add', 'neutral.yml')
            git('commit', '-qm', 'Neutral baseline')
            base = git('rev-parse', 'HEAD')
            git('commit', '--allow-empty', '-qm', 'Neutral subject\n\n' + self.DENY_TERMS[0])
            message = git('rev-parse', 'HEAD')
            git('commit', '--allow-empty', '-qm', 'Neutral author test',
                overrides={'GIT_AUTHOR_EMAIL': 'person@' + 'company.io'})
            author = git('rev-parse', 'HEAD')
            git('commit', '--allow-empty', '-qm', 'Neutral committer test',
                overrides={'GIT_COMMITTER_EMAIL': 'person@' + 'company.io'})
            committer = git('rev-parse', 'HEAD')
            result = scan(base + '..HEAD')
            self.assertEqual(result.returncode, 1)
            self.assertIn(message + ' message:', result.stderr)
            self.assertIn(author + ' author:', result.stderr)
            self.assertIn(committer + ' committer:', result.stderr)
            self.assertNotIn('person@', result.stderr)
            self.assertNotIn(self.DENY_TERMS[0], result.stderr)
            # Deny terms also apply to raw identity names, not only emails.
            for field in ('GIT_AUTHOR_NAME', 'GIT_COMMITTER_NAME'):
                previous = git('rev-parse', 'HEAD')
                git('commit', '--allow-empty', '-qm', 'Neutral identity policy probe',
                    overrides={field: self.DENY_TERMS[1]})
                result = scan(previous + '..HEAD')
                self.assertEqual(result.returncode, 1)
                self.assertIn('denied identifier', result.stderr)
                self.assertNotIn(self.DENY_TERMS[1], result.stderr)
            # Generic rules permit neutral attribution with an approved domain.
            identity = {'GIT_AUTHOR_NAME': 'Example Contributor',
                        'GIT_COMMITTER_NAME': 'Example Contributor',
                        'GIT_AUTHOR_EMAIL': 'contributors@' + 'users.noreply.github.com',
                        'GIT_COMMITTER_EMAIL': 'contributors@' + 'users.noreply.github.com'}
            previous = git('rev-parse', 'HEAD')
            git('commit', '--allow-empty', '-qm', 'Neutral approved identity', overrides=identity)
            self.assertEqual(scan(previous + '..HEAD').returncode, 0)
            self.assertNotEqual(scan('missing-base..HEAD').returncode, 0)
