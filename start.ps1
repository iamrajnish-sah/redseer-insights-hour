# Redseer Insight Hour — one-command start (loads .env, auto-reloads on code changes)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $parts = $line -split "=", 2
        if ($parts.Count -lt 2) { return }
        $name = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        Set-Item -Path "Env:$name" -Value $value
    }
    Write-Host "[ok] Loaded settings from .env" -ForegroundColor Green
} else {
    Write-Host "[warn] No .env file found. Copy .env.example to .env and add your API keys." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Redseer Insight Hour starting at http://localhost:8000" -ForegroundColor Cyan
Write-Host "- Code/HTML changes reload automatically (watch this window)." -ForegroundColor DarkGray
Write-Host "- Browser: press F5 to refresh the page." -ForegroundColor DarkGray
Write-Host "- Stop server: Ctrl+C" -ForegroundColor DarkGray
Write-Host ""

py -m uvicorn app:app --reload --host 127.0.0.1 --port 8000
