@echo off
rem Run the overlay CLIENT. First start asks for the setup code.
rem Extra args pass to client.py, e.g.:  start.bat --setup
setlocal
cd /d "%~dp0"
if not exist venv ( echo [run] venv not found - run install.bat first. & pause & exit /b 1 )
venv\Scripts\python client.py %*
if errorlevel 1 pause
