#Requires -Version 5.1
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw 'Run scripts\Install.ps1 first.' }
Set-Location $Root
& $Python (Join-Path $Root 'run.py') --data-dir (Join-Path $Root 'data') serve
exit $LASTEXITCODE
