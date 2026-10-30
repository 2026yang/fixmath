# Build the FixMath executable with PyInstaller.
# Run from the repository root in an elevated/normal PowerShell:
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "Installing PyInstaller (needs network)..."
py -3.11 -m pip install pyinstaller

Write-Host "Building..."
py -3.11 -m PyInstaller packaging\fixmath.spec --noconfirm --clean

$dist = Join-Path $root "dist\FixMath"

# Bundle the OCR model so the portable zip works on a machine without
# %APPDATA%\pix2text.  Prefer the copy already committed in vendor\.
$modelSrc = Join-Path $root "vendor\pix2text\1.1\mfr-1.5-onnx"
if (-not (Test-Path $modelSrc)) {
    $modelSrc = Join-Path $env:APPDATA "pix2text\1.1\mfr-1.5-onnx"
}
if (Test-Path $modelSrc) {
    $modelDst = Join-Path $dist "models\pix2text\1.1\mfr-1.5-onnx"
    New-Item -ItemType Directory -Force -Path (Split-Path $modelDst -Parent) | Out-Null
    Copy-Item $modelSrc $modelDst -Recurse -Force
    Write-Host "Bundled OCR model: $modelDst"
} else {
    Write-Warning "OCR model not found at $modelSrc; the zip will need %APPDATA%\pix2text."
}

Copy-Item (Join-Path $PSScriptRoot "run_portable.bat") (Join-Path $dist "run_fixmath.bat") -Force

Write-Host ""
Write-Host "Done: dist\FixMath\FixMath.exe"
Write-Host "Portable: dist\FixMath\run_fixmath.bat"
Write-Host "Next: compile packaging\fixmath.iss or run packaging\release.ps1."
