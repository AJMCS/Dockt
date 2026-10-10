@echo off
rem Dockt desktop app: double-click to start the local service + capture and open the window.
rem First run creates .venv and installs requirements. Pass --mock to run without the GX10.
cd /d "%~dp0.."
if not exist .venv\Scripts\pythonw.exe (
  echo Setting up Dockt for the first time...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\pip install -q -r requirements-server.txt -r requirements-agent.txt -r requirements-desktop.txt
)
start "" .venv\Scripts\pythonw.exe desktop\dockt_desktop.py %*
