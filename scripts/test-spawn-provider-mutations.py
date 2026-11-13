#!/usr/bin/env python3
"""Five small, isolated provider mutations; production sources are never edited."""
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
