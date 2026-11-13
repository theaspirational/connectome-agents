#!/usr/bin/env python3
"""Isolated provider mutations, including review regressions L5a-g; production sources are never edited."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path('roles/connectome_agent/module_utils/connectome_spawn')
MUTATIONS = [
    ('accept-unsafe-private-input', 'sources.py',
     '            if private:', '            if False:',
     'private_reader_refuses_modes_links_and_owner'),
    ('ignore-native-owner', 'zulip.py',
     "user.get('bot_owner_id') == operator and", 'True and',
     'foreign_or_changed_bot_during_reconciliation'),
    ('ignore-native-persona', 'zulip.py',
     "user.get('full_name') in (row['name'], target) and", 'True and',
     'foreign_or_changed_bot_during_reconciliation'),
    ('blind-post-on-pending-create', 'zulip.py',
     '                row = self._row(value, handle)\n',
     "                row = self._row(value, handle)\n"
     "                try:\n"
     "                    api_call(credentials, 'POST', '/bots', {'full_name': name,\n"
     "                             'short_name': row['short_name'], 'bot_type': 1})\n"
     "                except SpawnError:\n"
     "                    pass\n",
     'crash_between_post_and_receipt_is_discovered'),
    ('accept-conflicting-staged-key-L5a', 'persistence.py',
     'need(read_file(staging, root=path.parent, private=True) == raw,', 'need(True,',
     'staged_key_conflict_never_promotes'),
    ('remove-staging-exclusive-nofollow-L5b', 'persistence.py',
     'os.O_EXCL | os.O_NOFOLLOW, 0o600)', '0, 0o600)',
     'staging_open_requires_exclusive_nofollow'),
    ('remove-staging-exclusive-L5b', 'persistence.py',
     'os.O_EXCL | os.O_NOFOLLOW, 0o600)', 'os.O_NOFOLLOW, 0o600)',
     'staging_open_requires_exclusive_nofollow'),
    ('remove-staging-nofollow-L5b', 'persistence.py',
     'os.O_EXCL | os.O_NOFOLLOW, 0o600)', 'os.O_EXCL, 0o600)',
     'staging_open_requires_exclusive_nofollow'),
    ('leave-partial-staging-L5c', 'persistence.py',
     '            staging.unlink()', '            pass',
     'staging_write_error_unlinks_partial'),
    ('accept-different-delivered-key-L5d', 'zulip.py',
     "need(read_file(path, root=self.secrets_root / handle['agent'], private=True) == raw,",
     'need(True,', 'delivery_conflict_never_accepts'),
    ('case-sensitive-owned-full-name-L5e', 'zulip.py',
     "bot['full_name'].casefold() != name.casefold()", "bot['full_name'] != name",
     'full_name_preflight_is_case_insensitive'),
    ('ignore-ledger-name-collision-L5f', 'zulip.py',
     'need(not any(i != exclude and', 'need(True or not any(i != exclude and',
     'retired_ledger_names_stay_reserved'),
    ('ignore-observed-email-L5g', 'zulip.py',
     "user.get('email') == row['email'] and", 'True and',
     'observe_checks_native_email_before_mutating'),
    ('retain-definitively-refused-intent', 'zulip.py',
     "del value['operations'][key]", 'pass',
     'parsed_post_refusal_releases_intent'),
    ('allow-one-character-name', 'zulip.py',
     "name_pattern = r'[A-Za-z][A-Za-z0-9_-]+'", "name_pattern = r'[A-Za-z][A-Za-z0-9_-]*'",
     'one_character_name_is_rejected_before_network'),
    ('retain-retired-key-file', 'zulip.py',
     '                delivered.unlink()', '                pass',
     'provision_rename_retire_restart_and_no_credentials_in_state'),
    ('claim-ready-without-token', 'providers.py',
     "'state': 'awaiting-provisioning'", "'state': 'ready'",
     'gate_waits_for_operator_token'),
]


def run(work, pattern=None):
    command = [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests',
               '-p', 'test_spawn_providers.py', '-v']
    if pattern:
        command += ['-k', pattern]
    return subprocess.run(command, cwd=work, capture_output=True, text=True)


def main():
    (ROOT / '.cache').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='provider-mutations-', dir=ROOT / '.cache') as temporary:
        work = Path(temporary)
        shutil.copytree(ROOT / PACKAGE, work / PACKAGE, ignore=shutil.ignore_patterns('__pycache__'))
        (work / 'tests').mkdir()
        shutil.copyfile(ROOT / 'tests/test_spawn_providers.py', work / 'tests/test_spawn_providers.py')
        baseline = run(work)
        print(baseline.stdout + baseline.stderr, end='')
        if baseline.returncode:
            print('Baseline failed; mutation results would be invalid.', flush=True)
            return 1
        killed = 0
        for name, filename, before, after, pattern in MUTATIONS:
            path = work / PACKAGE / filename
            original = path.read_text()
            if before not in original:
                print('Mutation anchor missing: ' + name, flush=True)
                return 1
            try:
                path.write_text(original.replace(before, after))
                result = run(work, pattern)
            finally:
                path.write_text(original)
            # A fixture import/setup error is not evidence of a killed mutation.
            valid = 'Ran 1 test' in result.stderr and 'ERROR' not in result.stderr
            if result.returncode != 0 and valid and 'FAIL' in result.stderr:
                killed += 1
                print('KILLED ' + name, flush=True)
                print(result.stdout + result.stderr, end='')
            else:
                print('SURVIVED/INVALID ' + name, flush=True)
                print(result.stdout + result.stderr, end='')
        print('Mutations killed: ' + str(killed) + '/' + str(len(MUTATIONS)), flush=True)
        return 0 if killed == len(MUTATIONS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
