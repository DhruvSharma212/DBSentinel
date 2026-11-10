<#
  Starts the DBSentinel agent automatically every time you log in to Windows.

  Put dbsentinel-agent.exe, agent.config.json and this script in one folder, then run:

      powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1

  The agent runs in the background without a window and writes agent.log next to the exe.
  Use -RunElevated (from an Administrator PowerShell) to run it with administrator rights, which lets
  it read every process on the machine. Remove it again with uninstall-autostart.ps1.
#>
param(
    [string]$TaskName = 'DBSentinel Agent',
    [string]$Exe,
    [switch]$RunElevated
)
$ErrorActionPreference = 'Stop'

if (-not $Exe) { $Exe = Join-Path $PSScriptRoot 'dbsentinel-agent.exe' }
if (-not (Test-Path -LiteralPath $Exe)) { throw "Agent not found: $Exe (use -Exe to point at dbsentinel-agent.exe)" }
$Exe = (Resolve-Path -LiteralPath $Exe).Path
$dir = Split-Path -Parent $Exe
if (-not (Test-Path -LiteralPath (Join-Path $dir 'agent.config.json'))) {
    throw "agent.config.json not found in $dir. Copy agent.config.example.json to agent.config.json and fill it in first."
}
if ($RunElevated) {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) { throw '-RunElevated needs an Administrator PowerShell window.' }
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $Exe -Argument '--background' -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$runLevel = if ($RunElevated) { 'Highest' } else { 'Limited' }
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel $runLevel

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Sends this machine''s OS and PostgreSQL telemetry to a DBSentinel server.' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Installed scheduled task '$TaskName' for $user (run level: $runLevel)."
Write-Host "The agent is starting now and will start at every logon. Log file: $(Join-Path $dir 'agent.log')"
