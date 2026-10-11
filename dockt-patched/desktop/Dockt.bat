@echo off
rem Dockt desktop app: double-click to start the local service + capture and open the window.
rem First run creates .venv and installs requirements. Pass --mock to run without the GX10.
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (
  echo Setting up Dockt for the first time...
  py -3 -m venv .venv || python -m venv .venv
  if errorlevel 1 goto :failed
)
if not exist .venv\.dockt-deps-installed-v4 (
  echo Installing Dockt desktop requirements. This can take a few minutes the first time...
  .venv\Scripts\pip install -q -r requirements-server.txt -r requirements-agent.txt -r requirements-desktop.txt
  if errorlevel 1 goto :failed
  type nul > .venv\.dockt-deps-installed-v4
)
start "" .venv\Scripts\pythonw.exe desktop\dockt_desktop.py %*
exit /b 0

:failed
echo Dockt setup failed. Read the message above, then press any key to close.
pause >nul
exit /b 1
