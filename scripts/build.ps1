. "$PSScriptRoot\common.ps1"
& (Get-ProjectPython) (Join-Path $PSScriptRoot 'build_release.py') @args
exit $LASTEXITCODE
