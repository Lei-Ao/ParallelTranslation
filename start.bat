@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating Python environment...
    python -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import fastapi,uvicorn,pymupdf,httpx,multipart" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
    if errorlevel 1 goto failed
)
start "" wscript.exe //nologo "%~dp0start.vbs"
exit /b 0
:failed
echo.
echo Parallel could not start. Check Python installation and network access.
pause
exit /b 1
