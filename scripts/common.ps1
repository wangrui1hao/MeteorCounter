$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ProjectEnvironment = Join-Path $env:LOCALAPPDATA 'MeteorCounter\python-env'
function Get-ProjectPython {
    $Candidate = Join-Path $ProjectEnvironment 'Scripts\python.exe'
    if (Test-Path -LiteralPath $Candidate) { return $Candidate }
    throw 'Python environment is missing. Run scripts\setup.ps1 first.'
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
