@echo off
rem DEMO: monitor + debug window, app page also open on the camera WiFi (--lan).
rem The camera allows only ONE connection, so the 24/7 background monitor is
rem paused while this window runs and started again when you close it.
rem Press q in the camera window to stop.
cd /d "%~dp0"
schtasks /End /TN ParkingMonitor >nul 2>&1
timeout /t 3 /nobreak >nul
.venv\Scripts\python.exe parking_monitor.py run --show --lan
echo.
echo Restarting the background monitor...
schtasks /Run /TN ParkingMonitor >nul 2>&1
pause
