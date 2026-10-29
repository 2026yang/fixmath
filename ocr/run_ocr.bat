@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo FixMath offline OCR (Pix2Text mfr-1.5 / ONNX, CPU)
echo   endpoint : http://127.0.0.1:8765
echo   quit     : Ctrl+C
echo.

py -3.11 recognize_server.py --port 8765

echo.
echo OCR service stopped.
pause
