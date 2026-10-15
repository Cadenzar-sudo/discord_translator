@echo off
rem One-time setup for the Discord voice translator.
rem Safe to re-run; skips what is already done.
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
) else (
    echo [setup] venv already exists.
)

echo [setup] Installing dependencies (first run downloads ~1 GB of CUDA libs)...
venv\Scripts\python -m pip install --upgrade pip >nul
venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 ( echo [setup] Dependency install failed & pause & exit /b 1 )

echo [setup] Checking code...
venv\Scripts\python -m py_compile main.py config.py audio_capture.py segmentation.py transcriber.py translator.py overlay.py
if errorlevel 1 ( echo [setup] Code check failed & pause & exit /b 1 )

echo.
echo ============================================================
echo  Setup complete!
echo.
echo  Next steps on this PC (one-time):
echo   1. Install VB-Cable: https://vb-audio.com/Cable/  (reboot after)
echo   2. Discord Settings ^> Voice ^& Video ^> Output Device: CABLE Input
echo   3. Windows Sound ^> Recording ^> CABLE Output ^> Properties ^>
echo      Listen tab ^> tick "Listen to this device"
echo   4. Run:  start.bat
echo.
echo  Models (~2.9 GB) download automatically on first run.
echo ============================================================
pause
