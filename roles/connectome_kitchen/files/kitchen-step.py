#!/usr/bin/python3 -I
"""One step of kitchen's runner, started and read for the role.

    kitchen-step.py MODE ARGV...

Starts ARGV (kitchen's runner for MODE, as the role words it: a time limit, the account, a closed environment, Node,
scripts/takeover.mjs, MODE, --input, the request), waits, and prints:

  - the closed part of what the runner wrote: its own `takeover:` lines and the steps that say what was asked, answered,
    refused, started and measured, each cut to 900 characters. A failure of the engine that is not one of its refusals
    has an exception's text: that line is counted and not printed; it stays in the account's log;
  - a last line, `result ` and one JSON object: the runner's exit status (`rc`), the engine's answer to `plan` (`plan`),
    whether the secrets file gives the new process the environment and the secret files the running one has
    (`same_environment`; verify and start), and the count of lines not printed (`unclosed`).

Exit status 0 when the runner could be started and ended, whatever its own status; 2 otherwise.
"""
import json, subprocess, sys

SHOWN = ('plan answered', 'apply answered', 'engine refused', 'build named', 'tool started', 'tool failed', 'tool refused', 'secrets file', 'running deployment', 'end')
MEANING = {0: 'done', 1: 'the engine refused; the line above says why', 2: 'not a contract request', 3: 'the runner refused; the last line says why',
           124: 'no answer within the time given; the runner was stopped', 137: 'no answer within the time given; the runner was killed'}


def main(argv):
    if len(argv) < 3 or argv[1] not in ('check', 'prepare', 'verify', 'start'):
        sys.stderr.write(__doc__)
        return 2
    try:
        done = subprocess.run(argv[2:], stdin=subprocess.DEVNULL, capture_output=True, text=True, errors='replace')
    except OSError as error:
        print('kitchen-step: the runner could not be started (%s)' % type(error).__name__)
        return 2
    result = {'rc': done.returncode, 'plan': {}, 'same_environment': False, 'unclosed': 0}
    for line in done.stderr.splitlines():
        if line.startswith('takeover:'):
            print(line[:900])
            continue
        try:
            entry = json.loads(line)
            step = entry['step']
        except (ValueError, KeyError, TypeError):
            continue
        if step == 'engine failure that is not a refusal':
            result['unclosed'] += 1
        if step not in SHOWN:
            continue
        print(line if len(line) <= 900 else line[:900] + '…')
        if step == 'plan answered' and isinstance(entry.get('core'), dict):
            result['plan'] = entry['core']
        if step == 'secrets file':
            files = entry.get('secret_files_are_what_start_would_write')
            result['same_environment'] = entry.get('environment_file_of_current_build_is_what_start_would_write') is True and isinstance(files, dict) and all(value is True for value in files.values())
    print('runner %s: exit %d (%s)' % (argv[1], done.returncode, MEANING.get(done.returncode, 'see the log of the account')))
    if result['unclosed']:
        print('an engine failure that is not a refusal was logged. Its text is not shown here; it is in takeover.log of the account (0600)')
    print('result ' + json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
