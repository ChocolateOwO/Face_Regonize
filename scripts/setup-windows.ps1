# Reconize - one-time setup on a Windows PC (run from the checkout, any folder).
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1            # NVIDIA GPU
#   powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1 -Mode cpu  # no NVIDIA GPU
#
# Checks the prerequisites (and says how to install a missing one), creates the
# backend virtual environment from the pinned lock, creates .env with a fresh
# signing secret and admin password (never overwrites an existing .env), and
# installs the frontend packages. Safe to run again: finished steps are kept.
param(
    [ValidateSet('gpu', 'cpu')][string]$Mode = 'gpu',
    [switch]$SkipFrontend
)
# Continue, not Stop: in Windows PowerShell 5.1 'Stop' turns a native tool's stderr
# into a crash. Every native step is checked by its exit code (Check-Exit) instead.
$ErrorActionPreference = 'Continue'
$root = Split-Path $PSScriptRoot -Parent
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$venv = Join-Path $backend '.venv'
$python = Join-Path $venv 'Scripts\python.exe'

function Fail([string]$what, [string]$fix) {
    Write-Host ""
    Write-Host "SETUP STOPPED: $what" -ForegroundColor Red
    Write-Host "  Fix: $fix" -ForegroundColor Yellow
    Write-Host "  Then run this script again; finished steps are kept."
    exit 1
}
function Step([string]$text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Check-Exit([string]$what, [string]$fix) { if ($LASTEXITCODE -ne 0) { Fail $what $fix } }

Step "Checking prerequisites"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail 'git is not installed (needed for web updates).' 'winget install --id Git.Git -e   (or https://git-scm.com/download/win), then open a NEW terminal.'
}
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Fail 'The Python launcher (py) was not found.' 'Install Python 3.13 x64: winget install --id Python.Python.3.13 -e   (or python.org; tick "py launcher").'
}
& py -3.13 -c "import sys; assert sys.maxsize > 2**32" 2>$null
Check-Exit 'Python 3.13 (64-bit) is not installed.' 'winget install --id Python.Python.3.13 -e   (Reconize is tested on Python 3.13 x64).'
Write-Host "  Python: $(& py -3.13 -c 'import sys; print(sys.version.split()[0])')"
if (-not $SkipFrontend) {
    if (-not (Get-Command node -ErrorAction SilentlyContinue) -or -not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Fail 'Node.js (node and npm) is not installed.' 'winget install --id OpenJS.NodeJS -e   (Node 20.19+ or 22.12+; tested on the version in .node-version).'
    }
    $nodeVersion = (& node --version).TrimStart('v')
    $parts = $nodeVersion.Split('.')
    $major = [int]$parts[0]; $minor = [int]$parts[1]
    # Vite 8: Node ^20.19.0 or >=22.12.0
    if (-not (($major -eq 20 -and $minor -ge 19) -or ($major -eq 22 -and $minor -ge 12) -or $major -gt 22)) {
        Fail "Node.js $nodeVersion is not supported by the frontend build (needs 20.19+ or 22.12+)." 'winget upgrade --id OpenJS.NodeJS -e'
    }
    $pinned = (Get-Content (Join-Path $root '.node-version') -ErrorAction SilentlyContinue | Select-Object -First 1)
    Write-Host "  Node: $nodeVersion$(if ($pinned -and $pinned.Trim() -ne $nodeVersion) { "  (tested: $($pinned.Trim()))" })"
}

Step "Backend environment ($Mode)"
if (-not (Test-Path $python)) {
    & py -3.13 -m venv $venv
    Check-Exit 'Could not create the Python virtual environment.' 'Check free disk space and that the folder is writable.'
}
& $python -m pip install --upgrade pip --quiet
Check-Exit 'pip could not be upgraded.' 'Check the internet connection (pip downloads packages).'
# --no-deps: the lock is the complete pinned set. Without it pip also pulls the
# CPU 'onnxruntime' that InsightFace declares (unpinned, newest version); it and
# 'onnxruntime-gpu' share ONE module folder, so removing either one afterwards
# deletes the other's files and 'import onnxruntime' fails (found 2026-11-26).
& $python -m pip install --no-deps -r (Join-Path $backend 'requirements.lock.txt')
Check-Exit 'Installing the backend packages failed.' 'Read the pip error above; usually network or disk space. A proxy needs HTTPS_PROXY set.'
# Exactly one ONNX Runtime flavour, installed last and alone.
$runtime = if ($Mode -eq 'gpu') { 'onnxruntime-gpu==1.29.0' } else { 'onnxruntime==1.29.0' }
& $python -m pip uninstall -y onnxruntime onnxruntime-gpu 2>$null | Out-Null
& $python -m pip install --no-deps --force-reinstall $runtime --quiet
Check-Exit "Installing $runtime failed." 'Check the internet connection.'
if ($Mode -eq 'gpu') {
    & $python (Join-Path $PSScriptRoot 'preflight-windows.py') --require-gpu
    Check-Exit 'The GPU check failed (see above).' 'Update the NVIDIA driver (nvidia-smi must work), or run this script with -Mode cpu.'
} else {
    & $python (Join-Path $PSScriptRoot 'preflight-windows.py')
    Check-Exit 'The CPU check failed (see above).' 'Read the error above.'
}

Step "Configuration (.env)"
& $python (Join-Path $PSScriptRoot 'init_env.py')
Check-Exit 'Could not create .env.' 'Copy .env.example to .env by hand and set JWT_SECRET and ADMIN_PASSWORD.'

if (-not $SkipFrontend) {
    Step "Frontend packages"
    Push-Location $frontend
    try {
        & npm ci --no-audit --no-fund
        Check-Exit 'npm ci failed.' 'Read the npm error above; usually network. Delete frontend\node_modules and retry.'
    } finally { Pop-Location }
}

Step "Done"
Write-Host "  Start Reconize:      start.bat   (then open http://localhost:5173)"
Write-Host "  Network mode:        start-network.bat"
Write-Host "  First sign-in:       admin + the password printed above (only when .env was created now)"
Write-Host "  The first start downloads the face model (~300 MB, internet needed) and creates an empty database."
Write-Host "  Connect the Hub:     README.md, section 'Connecting the Mice Hub'"
