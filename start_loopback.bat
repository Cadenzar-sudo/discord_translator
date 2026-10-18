@echo off
REM ============================================================
REM  VERSION A (fallback): Dockerized server + NATIVE VB-Cable shipper
REM  The server runs in a container; the web joiner + audio shipper
REM  run on this Windows PC (they need VB-Cable + the saved profile).
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist "docker-data\config.json" (
  echo [start] docker-data\config.json is missing. See start_bot.bat header
  echo         or the README "Docker setup" section.
  exit /b 1
)
if not exist "server\venv\Scripts\python.exe" (
  echo [start] server\venv missing - run: server\install.bat
  exit /b 1
)
if not exist "server\discord_web_profile" (
  echo [start] NOTE: no saved Discord Web session in server\discord_web_profile.
  echo         First run: server\venv\Scripts\python.exe server\discord_web_joiner.py --setup
)

set "TRANSLATOR_MODELS=%cd%\server\models"
if not exist "%TRANSLATOR_MODELS%" if exist "D:\discord-translator-bot\server\models" set "TRANSLATOR_MODELS=D:\discord-translator-bot\server\models"
if defined TRANSLATOR_MODELS echo [start] models: %TRANSLATOR_MODELS%

echo [start] starting server container (no voice-bot)...
docker compose up -d server || exit /b 1

echo [start] waiting for the server...
set /a tries=0
:wait
timeout /t 3 /nobreak >nul
curl -s -o nul http://localhost:8765/health
if errorlevel 1 ( set /a tries+=1 & if %tries% lss 30 goto :wait & echo [start] server did not come up & exit /b 1 )
echo [start] server is up.

echo [start] launching web joiner + VB-Cable shipper...
start "translator-joiner" cmd /c "server\venv\Scripts\python.exe -u server\discord_web_joiner.py"
start "translator-shipper" cmd /c "server\venv\Scripts\python.exe -u server\audio_shipper.py"
echo [start] running. Use stop_loopback.bat to stop everything.
endlocal
