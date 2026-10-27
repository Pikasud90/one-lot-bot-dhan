# One Lot Desk - PowerShell launcher.
# Use this if you prefer PowerShell to cmd.exe. Behaviour is identical to start_win.bat.
param(
  [switch]$Mock,
  [switch]$Lan,
  [int]$Port = 8787,
  [switch]$Reinstall
)
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$Venv = Join-Path $Root ".venv"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "python not found on PATH. Install Python 3.10+ from python.org." }

$ok = & python -c "import sys;print(1 if sys.version_info>=(3,10) else 0)"
if ($ok.Trim() -ne "1") { throw "Python 3.10 or newer is required." }

if ($Reinstall -and (Test-Path -LiteralPath $Venv)) { Remove-Item -Recurse -Force -LiteralPath $Venv }

$VenvPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPy)) {
  Write-Host "==> creating virtual environment at $Venv"
  & python -m venv $Venv
  & $VenvPy -m pip install --quiet --upgrade pip
  Write-Host "==> installing dependencies (first run only)"
  & $VenvPy -m pip install --quiet -r (Join-Path $Root "requirements.txt")
  & $VenvPy -m pip install --quiet -e $Root
}

if (-not (Test-Path -LiteralPath (Join-Path $Root ".env"))) {
  Write-Host "NOTE: no .env found. Paste your Dhan credentials in the Settings tab, or copy .env.example to .env."
}

New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs"),(Join-Path $Root "data") | Out-Null

$extra = @()
if ($Mock) { $extra += "--mock" }
if ($Lan)  { $extra += "--lan" }
Write-Host "==> starting One Lot Desk (http://127.0.0.1:$Port/)"
Write-Host "    press Ctrl-C to stop"
& $VenvPy -m onelot run --port $Port @extra
