#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
# Forward policy options, including --deny-file and --require-denylist, unchanged.
exec python3 scripts/check_public.py "$@"
