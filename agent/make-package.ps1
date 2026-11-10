<#
  Builds a ready-to-run zip for ONE person: the agent plus a configuration that already holds the server
  address and a one-time enrollment code. The person only has to double-click dbsentinel-agent.exe.

  1. In the dashboard open Settings > Device access and click "Create code".
  2. Run (from the agent folder, after build.ps1 has produced dist\):

      powershell -ExecutionPolicy Bypass -File .\make-package.ps1 `
          -Server https://dbsentinel.onrender.com -Code K7P2-9XQM-4TWH -Name "Rahul"

  The zip is written to the current folder as DBSentinel-Agent-<name>.zip. The code works once and expires
  (24 hours by default); the agent trades it for its own token on first start and deletes it from the file.
  Do not post the zip publicly, and never put it in the repository.
#>
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Code,
    [string]$Name = '',
    [string]$DistDir,
    [string]$Output
)
$ErrorActionPreference = 'Stop'

if (-not $DistDir) { $DistDir = Join-Path $PSScriptRoot 'dist' }
$exe = Join-Path $DistDir 'dbsentinel-agent.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "dbsentinel-agent.exe not found in $DistDir. Run build.ps1 first." }

$letters = ($Code -replace '[^A-Za-z0-9]', '')
if ($letters.Length -ne 12) { throw "The code should have 12 letters and digits, like K7P2-9XQM-4TWH (got '$Code')." }
$Server = $Server.TrimEnd('/')
if ($Server -notmatch '^https://' -and $Server -notmatch '^http://(localhost|127\.0\.0\.1)') {
    throw "Use an https:// server address (plain http would send the code unencrypted)."
}

$safe = ($Name -replace '[^A-Za-z0-9_-]', '')
if (-not $safe) { $safe = 'Device' }
if (-not $Output) { $Output = Join-Path (Get-Location) "DBSentinel-Agent-$safe.zip" }

$stage = Join-Path ([IO.Path]::GetTempPath()) ("dbsentinel-package-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
try {
    foreach ($file in 'dbsentinel-agent.exe', 'README.md', 'START-HERE.txt', 'install-autostart.ps1', 'uninstall-autostart.ps1') {
        $source = Join-Path $DistDir $file
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $stage }
    }
    $config = [ordered]@{
        api_url          = $Server
        enroll_code      = $letters.ToUpper().Insert(8, '-').Insert(4, '-')
        device_name      = $Name
        interval_seconds = 5
        postgres         = [ordered]@{ enabled = $false; dsn = ''; send_query_text = $true }
    }
    $json = $config | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText((Join-Path $stage 'agent.config.json'), $json + "`n", (New-Object Text.UTF8Encoding $false))
    if (Test-Path -LiteralPath $Output) { Remove-Item -LiteralPath $Output -Force }
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $Output
}
finally {
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Created $Output"
Write-Host "It contains a one-time code for '$Name' that expires; send the zip only to that person."
