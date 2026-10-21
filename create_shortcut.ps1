# Creates the shortcuts for ESP 32 RGB Controller.
#
# Run once:   powershell -ExecutionPolicy Bypass -File create_shortcut.ps1
#
# It creates two shortcuts:
#
#   Desktop  -> starts the controller, lights the strip, opens the dashboard
#   Startup  -> starts the controller and lights the strip at Windows login
#               (no browser window, so it does not steal focus)
#
# Both use pythonw.exe so no console window ever appears.

$ErrorActionPreference = 'Stop'

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher   = Join-Path $projectDir 'launcher.pyw'
$desktop    = [Environment]::GetFolderPath('Desktop')
$startup    = [Environment]::GetFolderPath('Startup')

if (-not (Test-Path $launcher)) {
    throw "launcher.pyw not found next to this script ($projectDir)"
}

# Prefer pythonw.exe so double-clicking never flashes a console window
$python  = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
$pythonw = if ($python) { Join-Path (Split-Path $python) 'pythonw.exe' } else { $null }
if (-not $pythonw -or -not (Test-Path $pythonw)) {
    $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
}
if (-not $pythonw) { $pythonw = $python }
if (-not $pythonw) {
    throw "Could not find Python. Install it or add it to PATH first."
}

$shell = New-Object -ComObject WScript.Shell

function New-AppShortcut {
    param([string]$Path, [string]$Arguments, [string]$Description)

    $link = $shell.CreateShortcut($Path)
    $link.TargetPath       = $pythonw
    $link.Arguments        = '"' + $launcher + '" ' + $Arguments
    $link.WorkingDirectory = $projectDir
    $link.Description      = $Description
    $link.IconLocation     = "$pythonw,0"
    $link.Save()
}

# --- desktop: start + open the dashboard -----------------------------------
$desktopShortcut = Join-Path $desktop 'ESP 32 RGB Controller.lnk'
New-AppShortcut -Path $desktopShortcut -Arguments '' `
    -Description 'Start ESP 32 RGB Controller - lights the LED strip and opens the dashboard'

# --- startup: start + light up, no browser ---------------------------------
$startupShortcut = Join-Path $startup 'ESP 32 RGB Controller.lnk'
New-AppShortcut -Path $startupShortcut -Arguments '--no-browser' `
    -Description 'Lights the LED strip automatically when Windows starts'

# Remove the old name from earlier versions
$oldShortcut = Join-Path $desktop 'Asif Lighting Control.lnk'
if (Test-Path $oldShortcut) {
    Remove-Item $oldShortcut -Force
    Write-Host "Removed old shortcut: $oldShortcut" -ForegroundColor DarkYellow
}

Write-Host ""
Write-Host "Desktop shortcut created:" -ForegroundColor Green
Write-Host "  $desktopShortcut"
Write-Host "Startup entry created:" -ForegroundColor Green
Write-Host "  $startupShortcut"
Write-Host ""
Write-Host "The app now lights the strip automatically at Windows login."
Write-Host "Manage it in Settings > Apps > Startup."
