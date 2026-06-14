# Auto-start the stock dashboard pipeline at logon (no admin required).
# It drops a shortcut into the user's Startup folder pointing to: pythonw.exe supervisor.py
# Install : powershell -ExecutionPolicy Bypass -File install_autostart.ps1
# Remove  : delete StockDashboard.lnk in the Startup folder (full path printed below)

$Root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$Pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $Pythonw) { $Pythonw = "$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe" }
if (-not (Test-Path $Pythonw)) { Write-Error "pythonw.exe not found - check Python install path"; exit 1 }

$Startup = [Environment]::GetFolderPath('Startup')
$LnkPath = Join-Path $Startup 'StockDashboard.lnk'

$Wsh = New-Object -ComObject WScript.Shell
$Lnk = $Wsh.CreateShortcut($LnkPath)
$Lnk.TargetPath       = $Pythonw
$Lnk.Arguments        = 'supervisor.py'
$Lnk.WorkingDirectory = $Root
$Lnk.WindowStyle      = 7   # hidden (pythonw has no console anyway)
$Lnk.Description       = 'Stock dashboard public live page - auto-start publish pipeline at logon'
$Lnk.Save()

Write-Host "[OK] Logon auto-start shortcut created:" -ForegroundColor Green
Write-Host "     $LnkPath"
Write-Host "     Pipeline (app.py + publish_worker via supervisor) will start on next Windows logon."
Write-Host "     Start now manually : pythonw `"$Root\supervisor.py`""
Write-Host "     Disable auto-start : Remove-Item `"$LnkPath`""
