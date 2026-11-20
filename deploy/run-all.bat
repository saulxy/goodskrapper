@echo off
setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found. Please run: powershell -ExecutionPolicy Bypass -File deploy\install.ps1
    pause
    exit /b 1
)

echo Starting GoodSkrapper (Worker + Dashboard)...
echo Dashboard available at http://localhost:8080
echo Press Ctrl+C to stop.
echo.

".venv\Scripts\python.exe" -m goodskrapper all
if errorlevel 1 pause
