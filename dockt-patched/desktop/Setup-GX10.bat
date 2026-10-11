@echo off
rem One-time: let this laptop reach the GX10's models through an SSH tunnel. See setup-gx10.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-gx10.ps1"
echo.
echo Press any key to close.
pause >nul
