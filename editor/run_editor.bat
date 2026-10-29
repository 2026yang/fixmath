@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

py -3.11 fixmath_editor.py

echo.
echo editor stopped.
pause
