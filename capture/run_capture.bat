@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem --- P0: use the bundled offline Python runtime -------------------------
set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%PY%" set "PY=python"

echo FixMath capture helper
echo   hotkey : Ctrl+Alt+7   (change it in %%LOCALAPPDATA%%\FixMath\config.json)
echo   quit   : Ctrl+C in this window
echo.

"%PY%" -m fixmath.app

echo.
echo capture helper stopped.
pause
