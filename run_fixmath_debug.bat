@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Debug launcher: keeps a console and shows Python errors.
py -3.11 fixmath_app.py
if errorlevel 1 pause
