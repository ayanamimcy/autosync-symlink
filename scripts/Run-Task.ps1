#Requires -Version 5.1
$ErrorActionPreference = 'Continue'
$Root = Split-Path $PSScriptRoot -Parent
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Data = Join-Path $Root 'data'
$Log = Join-Path $Data 'service.log'
Set-Location $Root
$env:PYTHONIOENCODING = 'utf-8'
if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 5MB) {
    Move-Item $Log ($Log + '.previous') -Force
}
& $Python (Join-Path $Root 'run.py') --data-dir $Data serve *>> $Log
exit $LASTEXITCODE
