# Starts the a9-v720 fake server at log on (only needed for the A9 camera in
# "fakeserver" mode). Run from the parking-monitor folder:
#
#     powershell -ExecutionPolicy Bypass -File deploy\windows\install_fakeserver_task.ps1 -A9Dir C:\path\to\a9-v720
#
# Expects a venv at <A9Dir>\.venv (README Step 1B).
# Remove with:  Unregister-ScheduledTask -TaskName A9FakeServer -Confirm:$false

param([Parameter(Mandatory = $true)][string]$A9Dir)

$ErrorActionPreference = "Stop"
$A9Dir = (Resolve-Path $A9Dir).Path
$pythonw = Join-Path $A9Dir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "venv not found at $A9Dir\.venv" }

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "src\a9_naxclow.py -s" -WorkingDirectory $A9Dir
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
              -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable

Register-ScheduledTask -TaskName "A9FakeServer" -Action $action -Trigger $trigger `
    -Settings $settings -Description "a9-v720 fake cloud server for the parking camera" -Force | Out-Null
Start-ScheduledTask -TaskName "A9FakeServer"
Write-Host "Installed and started A9FakeServer."
