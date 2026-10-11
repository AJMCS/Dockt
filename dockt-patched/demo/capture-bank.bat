@echo off
rem Demo: capture as deal-laptop (Explorer and the Claude window are skipped).
cd /d "%~dp0.."
call "%~dp0stop-capture.bat"
start "" .venv\Scripts\pythonw.exe agent\capture.py --server http://127.0.0.1:8000 --name deal-laptop --monitor 2 --exclude "explorer,claude,searchhost,shellexperiencehost,startmenuexperiencehost"
exit /b 0
