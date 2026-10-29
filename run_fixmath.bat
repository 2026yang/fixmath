@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Unified launcher: single instance + OCR supervision + editor.
py -3.11 fixmath_app.py

if errorlevel 1 pause
