<#
  Builds the Windows agent: one file, nothing else to ship.

      cd agent
      powershell -ExecutionPolicy Bypass -File .\build.ps1

  Output (in agent\dist):
      dbsentinel-agent.exe        the whole product. First start: a guided setup asks for an enrollment code.
                                  Later starts: a menu (run, auto-start on/off, change settings, uninstall).
      DBSentinel-Agent.zip        the same exe in a zip, for sending through chat or e-mail.

  An enrollment code comes from the dashboard (Settings > Device access).
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

# The zip holds only the exe, taken by name so nothing else can slip in (a dist folder you have run the agent
# from may also hold a real agent.config.json).
$zip = Join-Path $PSScriptRoot 'dist\DBSentinel-Agent.zip'
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path (Join-Path $PSScriptRoot 'dist\dbsentinel-agent.exe') -DestinationPath $zip

Write-Host ''
Write-Host "Done: $(Join-Path $PSScriptRoot 'dist\dbsentinel-agent.exe')"
Write-Host "Send:  $zip"
