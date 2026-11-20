# GoodSkrapper automated installer for Windows (PowerShell)
# Usage:  .\deploy\install.ps1
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  GoodSkrapper Windows Setup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# 1. Locate Python
$PythonExe = $null
foreach ($cmd in @("python", "py -3", "python3", "$env:USERPROFILE\.local\bin\python3.14.exe")) {
    try {
        $test = Invoke-Expression "$cmd --version" 2>&1
        if ($LASTEXITCODE -eq 0 -and $test -match "Python 3\.") {
            $PythonExe = $cmd
            Write-Host "==> Found Python: $test ($cmd)" -ForegroundColor Green
            break
        }
    } catch {}
}

if (-not $PythonExe) {
    Write-Error "Python 3 was not found in PATH. Please install Python 3.10+ from python.org and check 'Add Python to PATH'."
    exit 1
}

# 2. Virtual environment
$VenvDir = Join-Path $ProjectDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip = Join-Path $VenvDir "Scripts\pip.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "==> Creating virtual environment in $VenvDir" -ForegroundColor Yellow
    Invoke-Expression "$PythonExe -m venv `"$VenvDir`""
} else {
    Write-Host "==> Existing virtual environment found in $VenvDir" -ForegroundColor Green
}

# 3. Install requirements
Write-Host "==> Installing dependencies from requirements.txt" -ForegroundColor Yellow
$env:PYYAML_FORCE_LIBYAML = "0"
& $VenvPython -m pip install --upgrade pip setuptools wheel --quiet
& $VenvPip install -r (Join-Path $ProjectDir "requirements.txt")

# 4. Data directory & configuration
$DataDir = Join-Path $ProjectDir "data"
if (-not (Test-Path $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
}

$ConfigYaml = Join-Path $ProjectDir "config.yaml"
$ConfigExample = Join-Path $ProjectDir "config.example.yaml"

if (-not (Test-Path $ConfigYaml)) {
    Copy-Item $ConfigExample $ConfigYaml
    Write-Host "==> Created config.yaml from config.example.yaml" -ForegroundColor Green
} else {
    Write-Host "==> Existing config.yaml preserved." -ForegroundColor Green
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Setup Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Next steps:"
Write-Host "  1. Edit your configuration file: $ConfigYaml" -ForegroundColor Yellow
Write-Host "     (Add your telegram.bot_token, chat_id, and targets)"
Write-Host "  2. Test Telegram bot connection:"
Write-Host "     & `"$VenvPython`" -m goodskrapper telegram-chat-id" -ForegroundColor White
Write-Host "     & `"$VenvPython`" -m goodskrapper telegram-test" -ForegroundColor White
Write-Host "  3. Run GoodSkrapper (Worker + Dashboard):"
Write-Host "     .\deploy\run-all.bat" -ForegroundColor White
Write-Host "     or: & `"$VenvPython`" -m goodskrapper all" -ForegroundColor White
Write-Host "  4. Dashboard will be available at http://localhost:8080"
Write-Host ""
