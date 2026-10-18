@echo off
rem One-time setup for the translator SERVER (the PC with the NVIDIA GPU).
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
venv\Scripts\python -m py_compile main.py config.py segmentation.py transcriber.py translator.py pipeline.py speakers.py voice_receiver.py sub_server.py bot.py setup_wizard.py predownload_models.py
if errorlevel 1 ( echo [setup] Code check failed & pause & exit /b 1 )

echo.
echo [setup] Models (~3.4 GB total):
if exist "D:\Translation Discord\models" (
    set /p MODELS="  [c]opy from D:\Translation Discord, [d]ownload, or [s]kip? [c] "
) else (
    set /p MODELS="  [d]ownload now or [s]kip (downloads on first run)? [d] "
)
if /i "%MODELS%"=="" set MODELS=d
if /i "%MODELS%"=="c" (
    venv\Scripts\python predownload_models.py --copy-from "D:\Translation Discord"
) else if /i "%MODELS%"=="s" (
    echo   Skipped - models download automatically on first start.
) else (
    venv\Scripts\python predownload_models.py
)

echo.
echo ============================================================
echo  Setup complete!  Next: run  setup.bat  (bot token + codes)
echo ============================================================
pause
