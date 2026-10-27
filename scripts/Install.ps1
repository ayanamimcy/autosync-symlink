#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param([switch]$OpenFirewall, [string]$PythonPath)
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
$Python = $PythonPath
if (-not $Python) {
foreach ($Candidate in @('python', 'python3')) {
    $Command = Get-Command $Candidate -ErrorAction SilentlyContinue
    if ($Command) {
        & $Command.Source -c 'import sys,venv; assert sys.version_info >= (3,11)'
        if ($LASTEXITCODE -eq 0) { $Python = $Command.Source; break }
    }
}
if (-not $Python) {
    $Launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($Launcher) {
        $Resolved = & $Launcher.Source -3 -c 'import sys,venv; assert sys.version_info >= (3,11); print(sys.executable)'
        if ($LASTEXITCODE -eq 0) { $Python = $Resolved.Trim() }
    }
}
}
if (-not $Python) { throw 'Install Python 3.11+ x64 from python.org, enable PATH, then retry.' }
& $Python -m venv (Join-Path $Root '.venv')
if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed.' }
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
& $VenvPython -m pip install -r (Join-Path $Root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Pillow installation failed. Check network or use a local wheel. Core service needs no extra packages.' }
$Data = Join-Path $Root 'data'
New-Item -ItemType Directory -Force -Path $Data | Out-Null
if (-not (Test-Path (Join-Path $Data 'config.json'))) {
    $Example = Join-Path $Root 'config.windows-server.example.json'
    if (-not (Test-Path $Example)) { $Example = Join-Path $Root 'config.example.json' }
    Copy-Item $Example (Join-Path $Data 'config.json')
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $Data /inheritance:r /grant:r "${Identity}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not restrict the data folder permissions.' }
if ($OpenFirewall) {
    $Config = Get-Content (Join-Path $Data 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $Port = [int]$Config.web.port
    $RuleName = 'CloudDrive Media Bridge'
    if (-not (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Profile Private,Domain | Out-Null
    }
}
Write-Host 'Installed. Review data\config.json and set the source/output paths before enabling libraries.'
Write-Host 'Next: run .\scripts\Start.ps1, then open http://127.0.0.1:8095 (unless the configured port differs).'
Write-Host 'Read data\admin.key for the random administrator key. Configure CloudDrive credentials in the Web UI.'
Write-Host 'Use the Web Tools page to initialize the source health marker before the first sync.'
