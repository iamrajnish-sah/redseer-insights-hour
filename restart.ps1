# Insights Hour — stop old server and start fresh (use after .env changes or if stuck)
$ErrorActionPreference = "SilentlyContinue"

Write-Host "Stopping any running Insights Hour server ..." -ForegroundColor Yellow
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "uvicorn app:app" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Start-Sleep -Seconds 1
Write-Host "Starting Insights Hour ..." -ForegroundColor Green
& "$PSScriptRoot\start.ps1"
