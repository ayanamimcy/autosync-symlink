#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
$Task = Get-ScheduledTask -TaskName 'CloudDriveMediaBridge' -ErrorAction SilentlyContinue
if ($Task) {
    Stop-ScheduledTask -TaskName 'CloudDriveMediaBridge'
    Unregister-ScheduledTask -TaskName 'CloudDriveMediaBridge' -Confirm:$false
}
Get-NetFirewallRule -DisplayName 'CloudDrive Media Bridge' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host 'Task and firewall rule removed. Configuration, state, covers and media files were retained.'
