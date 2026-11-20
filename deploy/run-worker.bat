@echo off
setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found. Please run: powershell -ExecutionPolicy Bypass -File deploy\install.ps1
    pause
    exit /b 1
)

echo Starting GoodSkrapper Worker (Scraper + Telegram alerts)...
".venv\Scripts\python.exe" -m goodskrapper worker
if errorlevel 1 pause
