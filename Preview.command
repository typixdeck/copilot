#!/bin/sh
set -eu
COPILOT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$COPILOT_ROOT/start-preview.sh" "$@"
