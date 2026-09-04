<#
    VelcrowAI - bring the whole thing up with one command (spec 15).

    Six processes: three FastAPI services and three Vite apps. The demo has to
    run "with zero terminal use", which starts with not needing six terminals
    to get there.

    Each service is started detached, then polled until it actually answers -
    reporting "started" for a process that died on a bad import would be worse
    than reporting nothing. Ports are freed first, because the usual reason a
    restart fails is that the last run is still holding one.

        .\run_all.ps1              start everything
        .\run_all.ps1 -Stop        stop everything and leave the ports clear
        .\run_all.ps1 -Logs        show where each service is writing its log
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Logs
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$logDir = Join-Path $root ".logs"

# Health probes are per-service on purpose: a shop has no /health, so asking
# for one would mark a working shop as broken.
$services = @(
    @{ Name = "FreshKart API";      Port = 8001; Probe = "/catalog";
       Dir = $root;                    Env = @{ SHOP = "grocery" }
       Cmd = "python -m uvicorn shop.app:create_app --factory --port 8001" }
    @{ Name = "Loomcraft API";      Port = 8002; Probe = "/catalog";
       Dir = $root;                    Env = @{ SHOP = "apparel" }
       Cmd = "python -m uvicorn shop.app:create_app --factory --port 8002" }
    @{ Name = "SilkRoute API";      Port = 8004; Probe = "/catalog";
       Dir = $root;                    Env = @{ SHOP = "apparel2" }
       Cmd = "python -m uvicorn shop.app:create_app --factory --port 8004" }
    @{ Name = "DailyMandi API";     Port = 8005; Probe = "/catalog";
       Dir = $root;                    Env = @{ SHOP = "grocery2" }
       Cmd = "python -m uvicorn shop.app:create_app --factory --port 8005" }
    @{ Name = "UrbanNest API";      Port = 8006; Probe = "/catalog";
       Dir = $root;                    Env = @{ SHOP = "home" }
       Cmd = "python -m uvicorn shop.app:create_app --factory --port 8006" }
    @{ Name = "MittiCraft API";     Port = 8007; Probe = "/catalog";
       Dir = $root;                    Env = @{ SHOP = "home2" }
       Cmd = "python -m uvicorn shop.app:create_app --factory --port 8007" }
    @{ Name = "VelcrowAI agent";    Port = 8003; Probe = "/health";
       Dir = $root;                    Env = @{}
       Cmd = "python -m uvicorn agent.app:create_app --factory --port 8003" }
    @{ Name = "FreshKart shop";     Port = 5173; Probe = "/";
       Dir = (Join-Path $root "web-shop");  Env = @{ VITE_SHOP = "grocery" }
       Cmd = "npx vite --port 5173" }
    @{ Name = "Loomcraft shop";     Port = 5174; Probe = "/";
       Dir = (Join-Path $root "web-shop");  Env = @{ VITE_SHOP = "apparel" }
       Cmd = "npx vite --port 5174" }
    @{ Name = "Buyer app";          Port = 5175; Probe = "/";
       Dir = (Join-Path $root "web-agent"); Env = @{}
       Cmd = "npx vite --port 5175" }
)

function Clear-Port([int]$Port) {
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1 |
        ForEach-Object {
            try { Stop-Process -Id $_.OwningProcess -Force -ErrorAction Stop } catch {}
        }
}

function Test-Service([int]$Port, [string]$Probe) {
    # Both spellings, because they are not the same address: uvicorn binds
    # IPv4 127.0.0.1 while Vite answers on localhost, which Windows resolves
    # to ::1 first. Probing only one marks a healthy service as dead - the
    # same IPv4/IPv6 split already recorded in BREAKAGE.md.
    foreach ($host_ in @("127.0.0.1", "localhost")) {
        try {
            $resp = Invoke-WebRequest -Uri "http://${host_}:$Port$Probe" -TimeoutSec 3 `
                                      -UseBasicParsing -ErrorAction Stop
            if ($resp.StatusCode -lt 500) { return $true }
        } catch {
            # A 4xx still means something is listening and routing.
            if ($_.Exception.Response) { return $true }
        }
    }
    return $false
}

if ($Logs) {
    foreach ($s in $services) {
        "{0,-16} {1}" -f $s.Name, (Join-Path $logDir ("{0}.log" -f $s.Port))
    }
    return
}

if ($Stop) {
    foreach ($s in $services) { Clear-Port $s.Port }
    Start-Sleep -Milliseconds 800
    Write-Host "All six ports are clear." -ForegroundColor Yellow
    return
}

if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Host "No .env found. Razorpay test keys, OPENAI_API_KEY and MANDATE_SECRET" -ForegroundColor Yellow
    Write-Host "come from there; the agent will degrade and payments will fail without it." -ForegroundColor Yellow
}
foreach ($app in @("web-shop", "web-agent")) {
    if (-not (Test-Path (Join-Path $root "$app\node_modules"))) {
        Write-Host "Installing $app dependencies (first run only)..." -ForegroundColor Cyan
        Push-Location (Join-Path $root $app); npm install --silent; Pop-Location
    }
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Write-Host "`nStarting VelcrowAI" -ForegroundColor Cyan

foreach ($s in $services) {
    Clear-Port $s.Port
    $log = Join-Path $logDir ("{0}.log" -f $s.Port)
    # Each service gets its own PowerShell so its env vars cannot leak into
    # the next one - VITE_SHOP in particular decides which brand a Vite app is.
    $prefix = ($s.Env.GetEnumerator() | ForEach-Object { "`$env:$($_.Key)='$($_.Value)';" }) -join " "
    $inner = "$prefix Set-Location '$($s.Dir)'; $($s.Cmd) *> '$log'"
    Start-Process -FilePath "powershell" `
                  -ArgumentList "-NoProfile", "-WindowStyle", "Hidden", "-Command", $inner `
                  -WindowStyle Hidden | Out-Null
}

$deadline = (Get-Date).AddSeconds(75)
$pending = [System.Collections.ArrayList]::new()
$services | ForEach-Object { [void]$pending.Add($_) }

while ($pending.Count -gt 0 -and (Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    foreach ($s in @($pending)) {
        if (Test-Service $s.Port $s.Probe) {
            Write-Host ("  {0,-16} {1}  up" -f $s.Name, $s.Port) -ForegroundColor Green
            $pending.Remove($s)
        }
    }
}

foreach ($s in $pending) {
    Write-Host ("  {0,-16} {1}  DID NOT START" -f $s.Name, $s.Port) -ForegroundColor Red
    $log = Join-Path $logDir ("{0}.log" -f $s.Port)
    if (Test-Path $log) {
        Get-Content $log -Tail 4 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    }
}

if ($pending.Count -gt 0) {
    Write-Host "`n$($pending.Count) service(s) failed. Logs: .\run_all.ps1 -Logs" -ForegroundColor Red
    exit 1
}

Write-Host @"

  FreshKart      http://localhost:5173        console  http://localhost:5173/console
  Loomcraft      http://localhost:5174        console  http://localhost:5174/console
  Buyer agent    http://localhost:5175

  Stop with  .\run_all.ps1 -Stop
"@ -ForegroundColor Cyan
