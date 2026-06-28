#requires -Version 5.1
<#
.SYNOPSIS
  Build the distributable Windows Himmy installer (.exe) from a clean checkout.

.DESCRIPTION
  The Windows analogue of scripts/build-mac.sh. Produces a SELF-CONTAINED app: the Python
  backend is frozen with PyInstaller and bundled inside the installer, so an end user needs no
  Python, no venv, and no terminal — they just run the .exe installer and launch Himmy.

  This script must run ON Windows (PyInstaller does not cross-compile, and electron-builder
  needs Windows to produce a Windows installer). It assumes the project .venv already exists at
  the repo root with the framework + this app installed editable, e.g.:

      uv venv --python 3.12 .venv
      uv pip install --python .venv\Scripts\python.exe -e "..\himmy-framework[toolkit,api,openai,embeddings,nepal,cron]"
      uv pip install --python .venv\Scripts\python.exe -e .

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\build-win.ps1

.OUTPUTS
  desktop\release\Himmy Setup <version>.exe
#>

# `set -e` equivalent: stop on the first error.
$ErrorActionPreference = "Stop"

# --- Resolve the repo root (this script lives in <root>\scripts) ---------------------------
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Resolve-Path (Join-Path $ScriptDir "..")
Set-Location $Root

$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Error "No project venv at .venv\Scripts\python.exe — create it first (uv venv --python 3.12 .venv && uv pip install -e ...)."
}

# --- [1/4] Ensure PyInstaller is available --------------------------------------------------
Write-Host "==> [1/4] Ensuring PyInstaller is available"
& $VenvPy -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $VenvPy -m pip install "pyinstaller>=6.6"
    if ($LASTEXITCODE -ne 0) { Write-Error "pip install pyinstaller failed." }
}

# --- [2/4] Freeze the Python backend (himmy-backend) ----------------------------------------
Write-Host "==> [2/4] Freezing the Python backend (himmy-backend)"
& $VenvPy -m PyInstaller "packaging\himmy-backend.spec" `
    --distpath "packaging\dist" --workpath "packaging\build" --noconfirm
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller freeze failed." }

# Quick smoke test of the frozen backend before we wrap it in an app.
Write-Host "    smoke-testing the frozen backend..."
$BackendExe = Join-Path $Root "packaging\dist\himmy-backend\himmy-backend.exe"
if (-not (Test-Path $BackendExe)) {
    Write-Error "Frozen backend not found at $BackendExe"
}

$SmokeDir = Join-Path ([System.IO.Path]::GetTempPath()) ("himmy-build-smoke-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $SmokeDir | Out-Null
$SmokeOut = Join-Path $SmokeDir "stdout.log"
$SmokeErr = Join-Path $SmokeDir "stderr.log"

# Use a spare port so a running dev backend (8131) doesn't clash.
$env:HIMMY_APP_PORT = "8159"
$env:HIMMY_APP_DATA_DIR = $SmokeDir
$env:HIMMY_SECRETS = "env"

$proc = Start-Process -FilePath $BackendExe -PassThru `
    -RedirectStandardOutput $SmokeOut -RedirectStandardError $SmokeErr
$ok = $false
foreach ($i in 1..20) {
    if ($proc.HasExited) { break }
    try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:8159/health" | Out-Null
        $ok = $true
        break
    } catch {
        Start-Sleep -Seconds 1
    }
}
if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }

if (-not $ok) {
    Write-Host "----- backend stdout -----"
    if (Test-Path $SmokeOut) { Get-Content $SmokeOut }
    Write-Host "----- backend stderr -----"
    if (Test-Path $SmokeErr) { Get-Content $SmokeErr }
    Remove-Item -Recurse -Force $SmokeDir -ErrorAction SilentlyContinue
    Write-Error "Frozen backend failed to start — see logs above."
}
Remove-Item -Recurse -Force $SmokeDir -ErrorAction SilentlyContinue
Write-Host "    frozen backend OK"

# --- [3/4] Build the web UI (vite) ----------------------------------------------------------
Write-Host "==> [3/4] Building the web UI (vite)"
Set-Location (Join-Path $Root "desktop")
if (-not (Test-Path "node_modules")) {
    npm install
    if ($LASTEXITCODE -ne 0) { Write-Error "npm install failed." }
}
npm run build
if ($LASTEXITCODE -ne 0) { Write-Error "vite build failed." }

# --- [4/4] Package the Windows app + installer (electron-builder) ---------------------------
Write-Host "==> [4/4] Packaging the Windows app + installer (electron-builder)"
npx --no-install electron-builder --win
if ($LASTEXITCODE -ne 0) { Write-Error "electron-builder --win failed." }

Write-Host ""
Write-Host "Done. Installer(s):"
$installers = Get-ChildItem -Path (Join-Path $Root "desktop\release") -Filter "*.exe" -ErrorAction SilentlyContinue
if ($installers) {
    $installers | ForEach-Object { Write-Host "  $($_.FullName)" }
} else {
    Write-Host "  (no .exe found in desktop\release — check electron-builder output above)"
}
Write-Host ""
Write-Host "First launch on another PC (unsigned app): Windows SmartScreen will warn —"
Write-Host "click 'More info' -> 'Run anyway'."
