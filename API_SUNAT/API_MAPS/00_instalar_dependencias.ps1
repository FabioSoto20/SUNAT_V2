$ErrorActionPreference = "Stop"

$Python = $null
$cmd = Get-Command python -ErrorAction SilentlyContinue
if ($cmd) { $Python = $cmd.Source }

if (-not $Python) {
    $candidate = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (Test-Path $candidate) { $Python = $candidate }
}

if (-not $Python) {
    throw "No encontre Python."
}

Write-Host "Python: $Python"
& $Python -m pip install -r "$PSScriptRoot\requirements.txt"

Write-Host ""
Write-Host "No se instala Chromium de Playwright."
Write-Host "El Paso 02 intenta usar Edge o Chrome ya instalado."
