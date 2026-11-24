#!/bin/zsh
set -eu
cd -- "${0:A:h}"
mkdir -p .runtime
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.runtime/browsers"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONPATH="$PWD/backend"
if [[ ! -x .runtime/venv/bin/python ]]; then
  python3 -m venv .runtime/venv
fi
.runtime/venv/bin/python -m pip install --quiet -r backend/requirements-browser.txt
if [[ ! -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]]; then
  .runtime/venv/bin/python -m playwright install chromium
fi
.runtime/venv/bin/python -m app.local_browser
read -r "?Сборщик остановлен. Нажмите Enter, чтобы закрыть окно."
