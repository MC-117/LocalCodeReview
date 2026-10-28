@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_CMD=py -3"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3.12 or newer was not found. Install Python from https://www.python.org/downloads/windows/ and run this script again.
    exit /b 1
  )
  set "PYTHON_CMD=python"
)
call %PYTHON_CMD% configure_plugin.py
if errorlevel 1 exit /b 1
where codex >nul 2>nul
if errorlevel 1 (
  echo Codex CLI was not found. Install or enable Codex Desktop's CLI, then run this script again.
  exit /b 1
)
call codex plugin marketplace add "%~dp0." --json
if errorlevel 1 exit /b 1
call codex plugin add local-code-review@local-review-tools --json
if errorlevel 1 exit /b 1
echo Plugin installed. Reload plugins or open a new Codex chat if needed.
