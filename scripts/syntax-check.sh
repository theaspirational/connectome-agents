#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if command -v ansible-playbook >/dev/null 2>&1; then
    exec ansible-playbook -i examples/private-skeleton/inventory/hosts.yml tests/syntax.yml --syntax-check
fi
printf '%s\n' 'SKIP: ansible-playbook is not installed; no downloads permitted.'
