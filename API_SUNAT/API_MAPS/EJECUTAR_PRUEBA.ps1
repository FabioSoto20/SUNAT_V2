$ErrorActionPreference = "Stop"

$Python = $null
$cmd = Get-Command python -ErrorAction SilentlyContinue
if ($cmd) { $Python = $cmd.Source }

if (-not $Python) {
    $candidate = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (Test-Path $candidate) { $Python = $candidate }
}

if (-not $Python) {
    throw "No se encontro Python."
}

Set-Location $PSScriptRoot

Write-Host "=== 00 VERIFICAR ==="
& $Python ".\00_verificar_entorno.py"

Write-Host "=== 01 PREPARAR ==="
& $Python ".\01_preparar_direcciones.py"

Write-Host "=== 02 EXACTOS ==="
& $Python ".\02_buscar_ubicacion_exacta.py"

Write-Host "=== 03 REFERENCIAS ==="
& $Python ".\03_investigar_ubicacion_referencia.py"

Write-Host "=== 04 CONSOLIDAR ==="
& $Python ".\04_consolidar_resultados.py"

Write-Host "=== 05 RESUMEN ==="
& $Python ".\05_resumen_calidad.py"

Write-Host "PROCESO DE PRUEBA FINALIZADO."
