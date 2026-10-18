@echo off
rem One-time setup for the overlay CLIENT (any player's PC, no GPU needed).
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo [setup] Python launcher "py" not found.
    echo         Install Python 3.10+ from https://www.python.org/downloads/
    echo         or run:  winget install Python.Python.3.12
    echo         IMPORTANT: tick "Add python.exe to PATH" in the installer.
    pause
    exit /b 1
)

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [setup] Python 3.10 or newer is required.
    pause
    exit /b 1
)

if not exist venv (
    echo [setup] Creating virtual environment...
    py -3 -m venv venv
    if errorlevel 1 ( echo [setup] venv creation failed & pause & exit /b 1 )
)

echo [setup] Installing dependencies (small - no models, no CUDA)...
venv\Scripts\python -m pip install --upgrade pip >nul
venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 ( echo [setup] Dependency install failed & pause & exit /b 1 )

venv\Scripts\python -m py_compile client.py config.py overlay.py
if errorlevel 1 ( echo [setup] Code check failed & pause & exit /b 1 )

echo.
echo ============================================================
echo  Setup complete!  Run  start.bat  - it will ask ONCE for
echo  the setup code from the server owner, then never again.
echo ============================================================
pause
