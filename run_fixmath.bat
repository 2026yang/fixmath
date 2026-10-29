@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Run windowless so no console stays open; logs go to
rem %LOCALAPPDATA%\FixMath\logs.
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3.11 fixmath_app.py
) else (
    start "" pythonw fixmath_app.py
)
exit /b 0
