. "$PSScriptRoot\common.ps1"
$env:PYTHONDONTWRITEBYTECODE = '1'
& (Get-ProjectPython) (Join-Path $PSScriptRoot 'clean.py')
exit $LASTEXITCODE
