<#
    Wipe the demo back to a clean shop (spec 15).

    Orders, carts, reservations, shopper links, proposals, chain logs and trust
    scores all accumulate while you build. By demo day they are full of test
    purchases, and a judge looking at "your previous order" sees somebody
    else's Graphic Tee. This clears the runtime state and reseeds stock from
    the catalog files. Code and catalogs are untouched.

        .\reset_demo.ps1            clear everything, keep a backup
        .\reset_demo.ps1 -NoBackup  clear without one
#>
[CmdletBinding()]
param([switch]$NoBackup)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$data = Join-Path $root "data"

if (-not (Test-Path $data)) {
    Write-Host "Nothing to reset - no data directory yet." -ForegroundColor Yellow
    return
}

# Stop the services first: a running uvicorn holds the SQLite files open, and
# half-deleting a chain log is worse than not deleting it.
Write-Host "Stopping services..." -ForegroundColor Cyan
& (Join-Path $root "run_all.ps1") -Stop | Out-Null

if (-not $NoBackup) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backup = Join-Path $root ".backups\data-$stamp"
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    Copy-Item -Path (Join-Path $data "*") -Destination $backup -Recurse -Force
    Write-Host "Backed up to $backup" -ForegroundColor DarkGray
}

Remove-Item -Path (Join-Path $data "*.sqlite") -Force -ErrorAction SilentlyContinue
Remove-Item -Path (Join-Path $data "chains") -Recurse -Force -ErrorAction SilentlyContinue

Write-Host @"

  Cleared: orders, carts, reservations, shopper links, proposals,
           trust scores, both chain logs.
  Kept:    catalogs, code, .env

  Stock reseeds from the catalog files on next start.
  Start with  .\run_all.ps1
"@ -ForegroundColor Green
