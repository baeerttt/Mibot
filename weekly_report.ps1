# weekly_report.ps1 — reporte semanal de performance + sello de auditoria.
#
# El informe de DSN pide "reportes semanales de performance". Esto corre la
# cadena completa de evidencia y publica el sello en GitHub:
#   1. track_record.py  -> recalcula el Composite Score + export CSV/JSON
#   2. audit.py         -> sella un checkpoint nuevo (cadena de hashes)
#   3. dashboard.py     -> regenera el dashboard HTML
#   4. commit + push de audit_chain.jsonl (sello con timestamp publico)
#
# Uso manual:  .\weekly_report.ps1
# Automatico:  lo dispara la tarea programada 'mibot-weekly-report' (lunes 9am).

$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
$py = Join-Path $root "venv\Scripts\python.exe"
Set-Location $root

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Host "=== Reporte semanal Mibot · $ts ===" -ForegroundColor Yellow

Write-Host "`n[1/4] track_record.py" -ForegroundColor Cyan
& $py track_record.py

Write-Host "`n[2/4] audit.py (sellar checkpoint)" -ForegroundColor Cyan
& $py audit.py

Write-Host "`n[3/4] dashboard.py" -ForegroundColor Cyan
& $py dashboard.py

Write-Host "`n[4/4] commit + push del sello de auditoria" -ForegroundColor Cyan
# Solo audit_chain.jsonl es versionable (data/ esta en .gitignore). Es el
# sello publico: deja en la historia de git la prueba inalterable del track.
git add audit_chain.jsonl
$changed = git status --porcelain audit_chain.jsonl
if ($changed) {
    git commit -m "Reporte semanal: sello de auditoria del track record ($ts)"
    git push origin master
    Write-Host "Sello publicado en GitHub." -ForegroundColor Green
} else {
    Write-Host "Sin cambios nuevos en el sello (no hay predicciones nuevas)." -ForegroundColor Gray
}

Write-Host "`n=== Listo. Dashboard en data\dashboard.html ===" -ForegroundColor Yellow
