<#
  Builds a single-file Windows executable of the DBSentinel agent.

      cd agent
      powershell -ExecutionPolicy Bypass -File .\build.ps1

  Output: agent\dist\dbsentinel-agent.exe, plus the config example, the auto-start scripts and the README,
  so the dist folder can be zipped and copied to the laptop you want to monitor.
#>
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' } else { 'python' }
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Write-Host 'Creating build environment (.venv)...'
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create a virtual environment. Is Python 3.10+ installed?' }
}
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

Write-Host 'Installing build dependencies...'
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }

Write-Host 'Building dbsentinel-agent.exe...'
& $venvPython -m PyInstaller --onefile --noconfirm --clean --name dbsentinel-agent `
    --specpath build --workpath build\work --distpath dist agent.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

foreach ($f in 'agent.config.example.json', 'install-autostart.ps1', 'uninstall-autostart.ps1', 'README.md') {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $f) -Destination 'dist' -Force
}
Write-Host ''
Write-Host "Done: $(Join-Path $PSScriptRoot 'dist\dbsentinel-agent.exe')"
