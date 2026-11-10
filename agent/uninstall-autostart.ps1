<#
  Stops the DBSentinel agent and removes its auto-start task.

      powershell -ExecutionPolicy Bypass -File .\uninstall-autostart.ps1
#>
param([string]$TaskName = 'DBSentinel Agent')
$ErrorActionPreference = 'Stop'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "No scheduled task named '$TaskName' - nothing to remove."
    return
}
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'. The agent files were left in place."
