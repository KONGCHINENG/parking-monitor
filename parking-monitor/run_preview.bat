@echo off
rem PREVIEW: the phone app running on the sample video (no camera needed).
rem Use it to set up Tailscale and "Add to Home Screen" before the WiFi adapter arrives.
rem Notifications are off so the looping video doesn't spam your phone.
cd /d "%~dp0"
if not exist test_video.avi .venv\Scripts\python.exe tools\make_test_video.py
.venv\Scripts\python.exe parking_monitor.py run --source file --no-notify
pause
