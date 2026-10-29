# Stop the local Parallel server and its tray controller.
$listeners = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
foreach ($listener in $listeners) {
    Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
}
$tray = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match '-File\s+.*tray\.ps1' }
foreach ($item in $tray) {
    Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue
}
