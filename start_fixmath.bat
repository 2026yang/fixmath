@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Start the offline OCR service and the editor in their own windows, give the
rem model time to load, then start the capture helper.
start "FixMath OCR" /D "%~dp0ocr" py -3.11 recognize_server.py --port 8765
start "FixMath Editor" /D "%~dp0editor" py -3.11 fixmath_editor.py

echo Waiting for the OCR model to load...
timeout /t 6 /nobreak >nul

call "%~dp0capture\run_capture.bat"
