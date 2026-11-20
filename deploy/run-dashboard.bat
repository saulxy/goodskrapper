@echo off
setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found. Please run: powershell -ExecutionPolicy Bypass -File deploy\install.ps1
    pause
    exit /b 1
)

echo Starting GoodSkrapper Dashboard on http://localhost:8080 ...
".venv\Scripts\python.exe" -m goodskrapper dashboard
if errorlevel 1 pause
