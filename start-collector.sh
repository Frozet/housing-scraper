#!/bin/sh
set -eu

cd -- "$(dirname -- "$0")"
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.runtime/browsers"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONPATH="$PWD/backend"

collector_python=.runtime/venv/bin/python
if [ ! -x "$collector_python" ]; then
    python3 -m venv .runtime/venv
fi

"$collector_python" -m pip install --quiet -r backend/requirements-browser.txt
"$collector_python" -m playwright install chromium
exec "$collector_python" -m app.local_browser "$@"
