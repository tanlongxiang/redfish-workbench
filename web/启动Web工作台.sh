#!/usr/bin/env bash
set -e
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [ -x "$HERE/../.venv/bin/python" ]; then
  exec "$HERE/../.venv/bin/python" -I "$HERE/start_web.py" "$@"
fi
if [ -x "$HERE/.venv/bin/python" ]; then
  exec "$HERE/.venv/bin/python" -I "$HERE/start_web.py" "$@"
fi
exec python3 -I "$HERE/start_web.py" "$@"
