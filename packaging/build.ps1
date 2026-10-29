# Build the FixMath executable with PyInstaller.
# Run from the repository root in an elevated/normal PowerShell:
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "Installing PyInstaller (needs network)..."
py -3.11 -m pip install pyinstaller

Write-Host "Building..."
py -3.11 -m PyInstaller packaging\fixmath.spec --noconfirm --clean

Write-Host ""
Write-Host "Done: dist\FixMath\FixMath.exe"
Write-Host "Next: compile packaging\fixmath.iss with Inno Setup to get an installer."
