# Watchdog 24/7 para Mibot.
# Corre el bot en modo headless (sin TUI) y lo reinicia solo si se cae o se corta
# la red. Pensado para dejarlo entrenando dia y noche.
#
# Uso:   .\run-forever.ps1
# Frenar: Ctrl+C
#
# Para VER el bot mientras corre asi, abrir OTRA terminal y usar el viewer:
#   venv\Scripts\python.exe bot.py            (NO: levantaria un segundo bot)
# En su lugar mira los logs:  Get-Content data\watchdog.log -Wait -Tail 20
#
# IMPORTANTE: corré UNA sola instancia (el lock data\bot.lock lo protege).

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$py   = Join-Path $root "venv\Scripts\python.exe"
$bot  = Join-Path $root "bot.py"
$log  = Join-Path $root "data\watchdog.log"
New-Item -ItemType Directory -Force -Path (Join-Path $root "data") | Out-Null

Write-Host "Watchdog Mibot iniciado. Logs en data\watchdog.log. Ctrl+C para frenar." -ForegroundColor Yellow

while ($true) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content $log "[$ts] arrancando bot (headless)..."
    Write-Host "[$ts] arrancando bot..." -ForegroundColor Green
    try {
        & $py $bot --no-tui 2>&1 | Tee-Object -FilePath $log -Append
    } catch {
        Add-Content $log "[$((Get-Date -Format 'HH:mm:ss'))] excepcion del watchdog: $_"
    }
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content $log "[$ts] el bot termino. reiniciando en 5s..."
    Write-Host "[$ts] el bot termino, reiniciando en 5s..." -ForegroundColor Red
    Start-Sleep -Seconds 5
}
