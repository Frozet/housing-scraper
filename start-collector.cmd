@echo off
setlocal
cd /d "%~dp0" || exit /b 1
set "PLAYWRIGHT_BROWSERS_PATH=%CD%\.runtime\browsers"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PYTHONPATH=%CD%\backend"
set "PYTHONUTF8=1"
set "collector_python=.runtime\venv\Scripts\python.exe"

if not exist "%collector_python%" (
    where py.exe >nul 2>&1
    if errorlevel 1 (
        python -m venv .runtime\venv || exit /b 1
    ) else (
        py -3 -m venv .runtime\venv || exit /b 1
    )
)

"%collector_python%" -m pip install --quiet -r backend\requirements-browser.txt || exit /b 1
"%collector_python%" -m playwright install chromium || exit /b 1
"%collector_python%" -m app.local_browser %*
exit /b %errorlevel%
