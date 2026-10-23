$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment.' }
}
& '.\.venv\Scripts\python.exe' -c "import importlib.util,sys; sys.exit(0 if all(importlib.util.find_spec(m) for m in ['fastapi','uvicorn','pymupdf','httpx','multipart']) else 1)"
if ($LASTEXITCODE -ne 0) {
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements-lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Could not install dependencies.' }
}
Write-Host 'Parallel is starting at http://127.0.0.1:8765. Press Ctrl+C to stop.'
& '.\.venv\Scripts\python.exe' launch.py
