@echo off
setlocal
if "%~1"=="" (
  echo Usage: start.cmd PROJECT_ROOT
  exit /b 2
)
python "%~dp0server.py" --root "%~1"
pause
