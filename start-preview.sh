#!/bin/sh
set -eu
COPILOT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
COPILOT_PYTHON=${COPILOT_PYTHON:-}
if [ -z "$COPILOT_PYTHON" ]; then
  for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3 python3; do
    if "$candidate" -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' >/dev/null 2>&1; then
      COPILOT_PYTHON=$candidate
      break
    fi
  done
fi
if [ -z "$COPILOT_PYTHON" ]; then
  printf '%s\n' '缺少 GTK3/PyGObject。macOS 可安装：brew install gtk+3 pygobject3' 'Raspberry Pi OS：sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0'
  exit 1
fi
export PYTHONPATH="$COPILOT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$COPILOT_PYTHON" -m typix_copilot --preview "$@"
