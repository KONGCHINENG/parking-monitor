@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe tools\ap_test.py
echo.
echo Done. Now reconnect to your normal WiFi and tell Claude.
pause
