param([switch]$Offline, [string]$ModelEndpoint = 'https://huggingface.co')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.build-venv\Scripts\python.exe')) {
    python -m venv .build-venv
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create build environment' }
}
$buildPython = Join-Path $PSScriptRoot '.build-venv\Scripts\python.exe'
& $buildPython -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Cannot install build dependencies' }
if ($Offline) {
    & $buildPython tools\prepare_model.py --endpoint $ModelEndpoint
    if ($LASTEXITCODE -ne 0) { throw 'Cannot prepare verified offline model' }
}
& $buildPython -m PyInstaller --noconfirm --clean VoiceToAI.spec
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
& $buildPython tools\prepare_release.py
if ($LASTEXITCODE -ne 0) { throw 'Release preparation failed' }
