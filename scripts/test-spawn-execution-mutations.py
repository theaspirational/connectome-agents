#!/usr/bin/env python3
"""Isolated executor/recovery mutation gate; never changes production sources."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path('roles/connectome_agent/module_utils/connectome_spawn')
MUTATIONS = [
    ('blind-retry-after-lost-ack', 'lifecycle.py',
     "need(value['pending'].get('ready', False) and",
     "need(True and", 'blind_retry_is_refused_even_when_executor_would_succeed'),
    ('accept-unacknowledged-effect', 'execution.py',
     "need(type(receipt) is dict and receipt.get('state') == 'applied' and\n"
     "         type(receipt.get('evidence')) is dict and bool(receipt['evidence']),", "need(True,", 'default_executor_cannot_claim_deployment'),
    ('build-starts-agent', 'execution.py',
     "effect('cook-build', dict(inputs, activation='install_only', noStart=True))",
     "effect('cook-build', dict(inputs, activation='start', noStart=True))", 'clean_replace_and_retire_keep_memory'),
    ('reset-memory-binding', 'execution.py',
     "'stateDirectory': context['store']", "'stateDirectory': build + '/memory'", 'clean_replace_and_retire_keep_memory'),
    ('service-ready-without-mcp', 'execution.py',
     "proven(p, 'initialized') and proven(p, 'toolsListed')", "True", 'verify_stages_are_separate_and_require_mcp_tools'),
    ('infer-authentication-from-service', 'execution.py',
     "'success' if proven(auth, 'authenticated')", "'success' if service", 'verify_stages_are_separate_and_require_mcp_tools'),
    ('retire-renamed-identity', 'lifecycle.py',
     "                    value['temporaryIdentity'] = None\n                else:",
     "                    pass\n                else:", 'rename_promotion_never_retires_same_identity'),
    ('effect-before-durable-intent', 'lifecycle.py',
     "value['pending']['effect'] = descriptor\n                write(path, value)\n                result = action()",
     "value['pending']['effect'] = descriptor\n                result = action()\n                write(path, value)",
     'durable_subeffect_intent_and_no_blind_retry'),
    ('allow-empty-recovery-reason', 'lifecycle.py',
     "0 < len(reason.strip()) <= 2048", "0 <= len(reason.strip()) <= 2048", 'recovery_requires_reason_and_never_replays'),
]


def run(work, pattern=None):
    command = [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests',
               '-p', 'test_spawn_execution.py', '-v']
    if pattern:
        command += ['-k', pattern]
    return subprocess.run(command, cwd=work, capture_output=True, text=True)


def main():
    (ROOT / '.cache').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='executor-mutations-', dir=ROOT / '.cache') as temporary:
        work = Path(temporary)
        shutil.copytree(ROOT / PACKAGE, work / PACKAGE, ignore=shutil.ignore_patterns('__pycache__'))
        (work / 'tests').mkdir()
        for filename in ('test_spawn_execution.py', 'test_spawn.py'):
            shutil.copyfile(ROOT / 'tests' / filename, work / 'tests' / filename)
        (work / 'scripts').mkdir()
        shutil.copyfile(ROOT / 'scripts/connectome-spawn', work / 'scripts/connectome-spawn')
        baseline = run(work)
        print(baseline.stdout + baseline.stderr, end='')
        if baseline.returncode:
            return 1
        killed = 0
        for name, filename, before, after, pattern in MUTATIONS:
            path = work / PACKAGE / filename
            original = path.read_text()
            if before not in original:
                print('Mutation anchor missing: ' + name)
                return 1
            try:
                path.write_text(original.replace(before, after))
                result = run(work, pattern)
            finally:
                path.write_text(original)
            valid = 'Ran 1 test' in result.stderr and 'ERROR' not in result.stderr
            if result.returncode and valid and 'FAIL' in result.stderr:
                killed += 1
                print('KILLED ' + name)
            else:
                print('SURVIVED/INVALID ' + name)
                print(result.stdout + result.stderr, end='')
        print('Mutations killed: ' + str(killed) + '/' + str(len(MUTATIONS)))
        return 0 if killed == len(MUTATIONS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
