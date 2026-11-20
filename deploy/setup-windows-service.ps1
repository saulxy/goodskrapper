# Registers GoodSkrapper as a background Task in Windows Task Scheduler
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\deploy\setup-windows-service.ps1             # Register & start
#   powershell -ExecutionPolicy Bypass -File .\deploy\setup-windows-service.ps1 -Uninstall  # Remove tasks
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [ValidateSet("all", "worker", "dashboard")]
    [string]$Mode = "all"
)

$TaskPrefix = "GoodSkrapper"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$VenvPython = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"  # pythonw runs windowless (no pop-up console)

if (-not (Test-Path $VenvPython)) {
    $VenvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
}

if (-not (Test-Path $VenvPython)) {
    Write-Error "Virtual environment not found at $ProjectDir\.venv. Run deploy\install.ps1 first."
    exit 1
}

$Tasks = @()
if ($Mode -eq "all") {
    $Tasks += @{ Name = "$TaskPrefix-All"; Args = "-m goodskrapper all"; Desc = "GoodSkrapper Worker and Web Dashboard" }
} elseif ($Mode -eq "worker") {
    $Tasks += @{ Name = "$TaskPrefix-Worker"; Args = "-m goodskrapper worker"; Desc = "GoodSkrapper Worker (Scraper + Alerts)" }
} elseif ($Mode -eq "dashboard") {
    $Tasks += @{ Name = "$TaskPrefix-Dashboard"; Args = "-m goodskrapper dashboard"; Desc = "GoodSkrapper Dashboard" }
}

if ($Uninstall) {
    Write-Host "==> Removing GoodSkrapper Scheduled Tasks..." -ForegroundColor Yellow
    Get-ScheduledTask -TaskName "$TaskPrefix*" -ErrorAction SilentlyContinue | ForEach-Object {
        Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
        Write-Host "  Removed task: $($_.TaskName)" -ForegroundColor Green
    }
    Write-Host "Done." -ForegroundColor Green
    exit 0
}

Write-Host "==> Registering GoodSkrapper Scheduled Tasks..." -ForegroundColor Cyan

foreach ($t in $Tasks) {
    $TaskName = $t.Name
    $Action = New-ScheduledTaskAction -Execute $VenvPython -Argument $t.Args -WorkingDirectory $ProjectDir
    $Trigger = New-ScheduledTaskTrigger -AtLogOn
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)

    # Register task for current user
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description $t.Desc -Force | Out-Null
    Write-Host "  Registered: $TaskName (Triggers At Log-on, restart on failure)" -ForegroundColor Green

    # Start task now
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "  Started task: $TaskName" -ForegroundColor Green
}

Write-Host ""
Write-Host "GoodSkrapper is now running in the background as a Scheduled Task!" -ForegroundColor Green
Write-Host "  - To check status: Get-ScheduledTask GoodSkrapper*"
Write-Host "  - To stop:         Stop-ScheduledTask -TaskName <TaskName>"
Write-Host "  - To remove:       .\deploy\setup-windows-service.ps1 -Uninstall"
Write-Host "  - Web Dashboard:   http://localhost:8080"
Write-Host ""
