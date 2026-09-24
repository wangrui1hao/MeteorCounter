. "$PSScriptRoot\common.ps1"
& (Get-ProjectPython) (Join-Path $ProjectRoot 'src\app.py') @args
exit $LASTEXITCODE
