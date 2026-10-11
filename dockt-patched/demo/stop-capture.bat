@echo off
rem Demo: stop any running Dockt capture agent.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*agent*capture.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
