#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param([switch]$AtStartup, [switch]$GlobalMountsConfirmed)
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw 'Run scripts\Install.ps1 first.' }
if ($AtStartup -and -not $GlobalMountsConfirmed) {
    throw 'AtStartup uses SYSTEM, which cannot see user-session drive letters. Verify that M: and Y: are global mounts, then pass -GlobalMountsConfirmed.'
}
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + (Join-Path $Root 'scripts\Run-Task.ps1') + '"'
$Action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument $Arguments -WorkingDirectory $Root
if ($AtStartup) {
    $Trigger = New-ScheduledTaskTrigger -AtStartup
    $Principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
} else {
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity
    $Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive -RunLevel Highest
}
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'CloudDriveMediaBridge' -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null
Write-Host 'Task installed. Stop any foreground instance before starting it.'
Write-Host 'Start-ScheduledTask -TaskName CloudDriveMediaBridge'
Write-Host 'Stop-ScheduledTask -TaskName CloudDriveMediaBridge'
if ($AtStartup) {
    Write-Host 'Task runs as SYSTEM at startup, including before user login.'
} else {
    Write-Host 'Task runs only while this administrator is logged in (RDP disconnect is fine; sign-out stops it).'
}
