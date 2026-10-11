@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating the Dockt Python environment...
  py -3 -m venv .venv
  if errorlevel 1 goto :failed
)

if not exist ".venv\.dockt-deps-installed-v4" (
  echo Installing Dockt desktop requirements. This can take a few minutes the first time...
  ".venv\Scripts\python.exe" -m pip install -r requirements-server.txt -r requirements-agent.txt -r requirements-desktop.txt
  if errorlevel 1 goto :failed
  type nul > ".venv\.dockt-deps-installed-v4"
)

echo Starting the Dockt desktop app, capture service and quick chat...
call "desktop\Dockt.bat" %*
exit /b 0

:failed
echo.
echo Dockt could not start. Read the message above, then press any key to close.
pause >nul
exit /b 1
