$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (Test-Path -LiteralPath $bundledPython) { & $bundledPython server.py } else { python server.py }
