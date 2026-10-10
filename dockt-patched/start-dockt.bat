@echo off
setlocal
cd /d "%~dp0"

set "MOCK_MODE=0"
if /i "%~1"=="--mock" set "MOCK_MODE=1"

if not exist ".venv\Scripts\python.exe" (
  echo Creating the Dockt Python environment...
  py -3 -m venv .venv
  if errorlevel 1 goto :failed
)

if not exist ".venv\.dockt-deps-installed" (
  echo Installing Dockt requirements. This can take a few minutes the first time...
  ".venv\Scripts\python.exe" -m pip install -r requirements-server.txt -r requirements-agent.txt
  if errorlevel 1 goto :failed
  type nul > ".venv\.dockt-deps-installed"
)

set "DOCKT_PORT_FILE=%TEMP%\dockt-port-%RANDOM%.txt"
".venv\Scripts\python.exe" "agent\find_port.py" > "%DOCKT_PORT_FILE%"
if errorlevel 1 goto :failed
set /p "DOCKT_PORT=" < "%DOCKT_PORT_FILE%"
del "%DOCKT_PORT_FILE%" >nul 2>&1
if not defined DOCKT_PORT (
  echo No free local port found for Dockt in the range 8000-8099.
  goto :failed
)
set "DOCKT_URL=http://127.0.0.1:%DOCKT_PORT%"

if "%MOCK_MODE%"=="1" (
  set "VLM_URL=http://127.0.0.1:9000/v1"
  set "VLM_MODEL=mock"
  set "LLM_MODEL=mock"
  set "EMBED_MODEL=mock"
  start "Dockt mock models" /D "%~dp0" "%~dp0.venv\Scripts\python.exe" -m uvicorn --app-dir tools mock_models:app --host 127.0.0.1 --port 9000
) else (
  if not defined DOCKT_GX10 if exist gx10.txt set /p "DOCKT_GX10=" < gx10.txt
  if not defined DOCKT_GX10 (
    echo Set DOCKT_GX10 or add the GX10 IP to gx10.txt next to this launcher.
    goto :failed
  )
  set "VLM_URL=http://%DOCKT_GX10%:11434/v1"
  set "VLM_MODEL=gemma4:26b"
  set "LLM_MODEL=nemotron3:33b"
  set "EMBED_MODEL=nomic-embed-text"
)

set "DATA_DIR=%USERPROFILE%\.dockt\data"
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"

echo Starting the local Dockt service on port %DOCKT_PORT%...
start "Dockt service" /D "%~dp0" "%~dp0.venv\Scripts\python.exe" "%~dp0server\app.py"

echo Waiting for Dockt at %DOCKT_URL% ...
".venv\Scripts\python.exe" -c "import time,urllib.request; u='%DOCKT_URL%/api/health'; exec('for i in range(60):\n try:\n  urllib.request.urlopen(u,timeout=12).read(); print(\"Dockt service is ready.\"); break\n except Exception:\n  time.sleep(1)\nelse:\n raise SystemExit(1)')"
if errorlevel 1 (
  echo Dockt did not start within 60 seconds. Check the Dockt service window.
  goto :failed
)

start "Dockt capture" /D "%~dp0" "%~dp0.venv\Scripts\python.exe" "%~dp0agent\capture.py" --server %DOCKT_URL% --name %COMPUTERNAME%
start "" msedge --app=%DOCKT_URL%
start "Dockt Quick Chat" /min ".venv\Scripts\pythonw.exe" agent\quickchat.py
echo Dockt is running. Press Ctrl+Space for Quick Chat; Escape hides it.
exit /b 0

:failed
echo.
echo Dockt could not start. Read the message above, then press any key to close.
pause >nul
exit /b 1
