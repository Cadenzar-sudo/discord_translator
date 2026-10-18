@echo off
rem Run the translator SERVER (bot + subtitle WebSocket).
rem Extra args pass to main.py, e.g.:  start.bat --test-file clip.wav
setlocal
cd /d "%~dp0"
if not exist venv ( echo [run] venv not found - run install.bat first. & pause & exit /b 1 )
venv\Scripts\python main.py %*
if errorlevel 1 pause
