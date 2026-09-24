param([string]$PythonExecutable)
. "$PSScriptRoot\common.ps1"
$EnvironmentPython = Join-Path $ProjectEnvironment 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $EnvironmentPython)) {
    if ($PythonExecutable) {
        $SystemPython = & $PythonExecutable -I -c "import sys; print(sys.executable)"
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $SystemPython = & py -3.12 -I -c "import sys; print(sys.executable)"
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $SystemPython = & python -I -c "import sys; print(sys.executable)"
    } else { throw 'Install Python 3.12 x64 with Tcl/Tk, then rerun this script.' }
    if ($LASTEXITCODE -ne 0) { throw 'Unable to locate Python 3.12.' }
    & $SystemPython -I -c "import sys,tkinter,venv; assert sys.version_info[:2] == (3,12) and sys.maxsize > 2**32, 'Python 3.12 x64 required'; print('System Python:', sys.executable)"
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 x64 with Tcl/Tk is required.' }
    & $SystemPython -I -m venv $ProjectEnvironment
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
& $EnvironmentPython -I -c "import sys,tkinter; assert sys.version_info[:2] == (3,12) and sys.maxsize > 2**32 and sys.prefix != sys.base_prefix, 'Python 3.12 x64 virtual environment required'"
if ($LASTEXITCODE -ne 0) { throw "Invalid environment: $ProjectEnvironment" }
& $EnvironmentPython -I -m pip install -r (Join-Path $ProjectRoot 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $EnvironmentPython -I -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed.' }
& $EnvironmentPython -I -c "import cv2,numpy,PIL,mss,tkinter,PyInstaller; print('Project Python is ready.')"
if ($LASTEXITCODE -ne 0) { throw 'Project dependency import failed.' }
Write-Host "Environment: $ProjectEnvironment"
