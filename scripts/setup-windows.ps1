param([ValidateSet('gpu', 'cpu')][string]$Mode = 'gpu')
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$backend = Join-Path $root 'backend'
$venv = Join-Path $backend '.venv'
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw 'Install Python 3.13 x64, then rerun.' }
& py -3.13 -c "import sys; assert sys.maxsize > 2**32; print(sys.version)"
if ($LASTEXITCODE) { throw 'Python 3.13 x64 is required.' }
if (-not (Test-Path $venv)) { & py -3.13 -m venv $venv }
$python = Join-Path $venv 'Scripts\\python.exe'
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $backend 'requirements.lock.txt')
if ($Mode -eq 'gpu') {
    & $python -m pip uninstall -y onnxruntime
    & $python $PSScriptRoot/preflight-windows.py --require-gpu
} else {
    & $python -m pip install 'onnxruntime==1.29.0'
    & $python -m pip uninstall -y onnxruntime-gpu
    & $python $PSScriptRoot/preflight-windows.py
}
Write-Host "Setup complete ($Mode). Copy .env.example to .env before real use."
