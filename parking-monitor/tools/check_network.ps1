# Checks the 24/7 setup. Changes nothing - only looks.
#
#     powershell -ExecutionPolicy Bypass -File tools\check_network.ps1
#
# Expected with the USB WiFi adapter: one WiFi on Nax_..., one on your normal
# WiFi, camera reachable AND internet reachable at the same time.

function Check($name, $ok, $hint) {
    if ($ok) { Write-Host "  [OK]   $name" -ForegroundColor Green }
    else     { Write-Host "  [FAIL] $name" -ForegroundColor Red; if ($hint) { Write-Host "         -> $hint" } }
}
function TcpOpen($hostName, $port) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $ok = $c.ConnectAsync($hostName, $port).Wait(3000) -and $c.Connected
        $c.Close(); return $ok
    } catch { return $false }
}

Write-Host "`nWiFi adapters:"
$wifi = netsh wlan show interfaces
$names = ($wifi | Select-String '^\s+Name\s+:').ForEach({ $_.Line.Split(':', 2)[1].Trim() })
$ssids = ($wifi | Select-String '^\s+SSID\s+:').ForEach({ $_.Line.Split(':', 2)[1].Trim() })
for ($i = 0; $i -lt $names.Count; $i++) { Write-Host ("  {0,-12} -> {1}" -f $names[$i], ($ssids[$i], '(not connected)' | Select-Object -First 1)) }

Write-Host "`nChecks:"
Check "Two WiFi adapters" ($names.Count -ge 2) "Plug in the USB WiFi adapter."
Check "One adapter on the camera WiFi (Nax_...)" ($ssids -match '^Nax') "Connect the USB adapter to Nax_5c002000E0F3."
Check "Camera reachable (192.168.169.1:6123)" (TcpOpen "192.168.169.1" 6123) "Camera off, or the Nax adapter is not connected."
Check "Internet reachable (ntfy.sh:443)" (TcpOpen "ntfy.sh" 443) `
    "Windows may be routing internet via the camera WiFi. Admin PowerShell: Set-NetIPInterface -InterfaceAlias '<Nax adapter name>' -InterfaceMetric 500"
Check "App page running (127.0.0.1:8080)" (TcpOpen "127.0.0.1" 8080) "Start the monitor (run_preview.bat, run_monitor.bat, or the startup task)."

$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $ts -and (Test-Path "C:\Program Files\Tailscale\tailscale.exe")) { $ts = "C:\Program Files\Tailscale\tailscale.exe" }
Check "Tailscale installed" ([bool]$ts) "Install from https://tailscale.com/download and sign in."
if ($ts) {
    $serve = & $ts serve status 2>&1 | Out-String
    Check "Tailscale serving the app (tailscale serve)" ($serve -match '8080') "Run: tailscale serve --bg 8080"
    if ($serve -match '(https://\S+)') { Write-Host "         Phone address: $($Matches[1])" }
}

$task = Get-ScheduledTask -TaskName ParkingMonitor -ErrorAction SilentlyContinue
Check "Startup task installed" ([bool]$task) "Run: powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1"

$ac = (powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE | Select-String 'Current AC').Line
Check "PC never sleeps on charger" ($ac -match '0x00000000') "Settings -> System -> Power -> sleep when plugged in = Never"
Write-Host ""
