@echo off
REM ============================================================
REM  VERSION B (primary): Dockerized server + DAVE voice-bot sidecar
REM  Everything runs in containers; overlays connect as usual.
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist "docker-data\config.json" (
  echo [start] docker-data\config.json is missing.
  echo         Copy server\config.json to docker-data\config.json and set:
  echo           "source": "ws"
  echo           "audio_shipper": { "url": "ws://server:8765/audio" }
  echo         Your bot token ^& channel IDs carry over unchanged.
  exit /b 1
)

REM reuse an existing model dir instead of re-downloading 3.3 GB
if exist "%TRANSLATOR_MODELS%" goto :have_models
if exist "server\models" ( set "TRANSLATOR_MODELS=%cd%\server\models" & goto :have_models )
if exist "D:\discord-translator-bot\server\models" set "TRANSLATOR_MODELS=D:\discord-translator-bot\server\models"
:have_models
if defined TRANSLATOR_MODELS echo [start] models: %TRANSLATOR_MODELS%

echo [start] building + starting server and voice-bot...
docker compose up -d --build || exit /b 1

echo [start] waiting for the server to come up...
set /a tries=0
:wait
timeout /t 3 /nobreak >nul
curl -s -o nul http://localhost:8765/health
if errorlevel 1 ( set /a tries+=1 & if %tries% lss 30 goto :wait & echo [start] server did not come up - check: docker compose logs server & exit /b 1 )
echo [start] server is up:  ws://localhost:8765/ws  (health: http://localhost:8765/health)
echo [start] voice-bot health: http://localhost:8766/  (join a voice channel or let auto-join run)
endlocal
