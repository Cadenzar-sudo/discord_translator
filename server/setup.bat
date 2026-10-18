@echo off
rem One-time: bot token, access token, setup code for friends.
setlocal
cd /d "%~dp0"
if not exist venv ( echo [setup] venv not found - run install.bat first. & pause & exit /b 1 )
venv\Scripts\python setup_wizard.py
pause
