@echo off
rem Start the whole translator stack on this PC:
rem   1. server  (loopback capture + Whisper + Azure/NLLB + subtitle websocket)
rem   2. Discord Web auto-joiner (spawned by the server ~15s after start)
rem   3. local overlay client (only if client\venv exists; pass "noclient" to skip)
rem
rem Usage:  start_all.bat [noclient]
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"
set "SYS=%SystemRoot%\System32"

if not exist "%ROOT%server\venv" (
    echo [start] server\venv missing - run server\install.bat first.
    pause
    exit /b 1
)

echo [start] launching server...
start "translator-server" cmd /c ""%ROOT%server\venv\Scripts\python.exe" -u "%ROOT%server\main.py""

echo [start] waiting ~25s for the pipeline and the Discord Web joiner...
%SYS%\ping.exe -n 26 127.0.0.1 >nul

if /i "%1"=="noclient" goto :done
if exist "%ROOT%client\venv" (
    echo [start] launching local overlay client...
    start "overlay-client" cmd /c ""%ROOT%client\venv\Scripts\python.exe" -u "%ROOT%client\client.py""
)
:done
echo.
echo [start] all running. The Chrome window is the translator's Discord
echo [start] client - do not close it. Run stop_all.bat to stop everything.
%SYS%\ping.exe -n 6 127.0.0.1 >nul
