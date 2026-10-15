@echo off
rem Run the Discord voice translator.
rem Extra args are passed to main.py, e.g.:  start.bat --list-devices
setlocal
cd /d "%~dp0"

if not exist venv (
    echo [run] venv not found - run install.bat first.
    pause
    exit /b 1
)

venv\Scripts\python main.py %*
if errorlevel 1 pause
