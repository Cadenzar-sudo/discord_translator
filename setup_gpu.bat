@echo off
REM ============================================================
REM  ONE-CLICK SETUP: Docker + GPU (CUDA) translator stack
REM  - creates docker-data\config.json from server\config.json
REM  - installs/starts Docker Desktop if needed
REM  - detects NVIDIA GPU + driver; uses the CUDA compose variant
REM  - builds and starts server + voice-bot, waits for health
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM ---- 1. base config ---------------------------------------------------
if exist "server\config.json" goto :have_base
echo [setup] server\config.json not found - run server\setup.bat first ^(it
echo         stores your bot token + access tokens^), then re-run this.
pause & exit /b 1
:have_base

if exist "docker-data\config.json" goto :have_cfg
echo [setup] creating docker-data\config.json from server\config.json ...
mkdir "docker-data" 2>nul
powershell -NoProfile -Command ^
  "$c = Get-Content 'server\config.json' -Raw | ConvertFrom-Json;" ^
  "$c.source = 'ws';" ^
  "if (-not $c.audio_shipper) { $c | Add-Member -NotePropertyName audio_shipper -NotePropertyValue ([pscustomobject]@{}) };" ^
  "$c.audio_shipper.url = 'ws://server:8765/audio';" ^
  "if (-not $c.discord_web -or $c.discord_web -is [bool]) { $c.discord_web = [pscustomobject]@{} };" ^
  "if (-not $c.discord_web.auto_join) { $c.discord_web | Add-Member -NotePropertyName auto_join -NotePropertyValue $true -Force };" ^
  "$c | ConvertTo-Json -Depth 20 | Set-Content 'docker-data\config.json' -Encoding UTF8"
if errorlevel 1 ( echo [setup] config generation failed & pause & exit /b 1 )
:have_cfg
echo [setup] config: docker-data\config.json OK

REM ---- 2. docker engine --------------------------------------------------
where docker >nul 2>&1
if errorlevel 1 (
  echo [setup] Docker not found - installing Docker Desktop via winget...
  winget install -e --id Docker.DockerDesktop --accept-source-agreements --accept-package-agreements
  if errorlevel 1 ( echo [setup] winget install failed - install Docker Desktop manually from docker.com & pause & exit /b 1 )
  echo [setup] reboot may be required for WSL2 - re-run this script after.
)
docker info >nul 2>&1
if not errorlevel 1 goto :docker_ok
echo [setup] starting Docker Desktop...
if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
set /a waits=0
:waitdocker
timeout /t 5 /nobreak >nul
docker info >nul 2>&1
if not errorlevel 1 goto :docker_ok
set /a waits+=1
if !waits! lss 36 goto :waitdocker
echo [setup] Docker engine did not come up - open Docker Desktop once manually, then re-run.
pause & exit /b 1
:docker_ok
echo [setup] Docker engine OK

REM ---- 3. GPU detection --------------------------------------------------
set "GPU=0"
nvidia-smi --query-gpu=name --format=csv,noheader >nul 2>&1
if not errorlevel 1 (
  set "GPU=1"
  for /f "usebackq delims=" %%g in (`nvidia-smi --query-gpu=name --format=csv,noheader ^| more`) do set "GPUNAME=%%g"
  echo [setup] NVIDIA GPU found: !GPUNAME!
  docker info 2>nul | findstr /C:"nvidia" >nul
  if errorlevel 1 echo [setup] note: nvidia runtime not listed yet - Docker Desktop exposes GPUs via WSL2 automatically on recent drivers.
) else (
  echo [setup] no NVIDIA GPU/driver detected - falling back to the CPU image.
)

REM ---- 4. models ---------------------------------------------------------
if defined TRANSLATOR_MODELS goto :have_models
if exist "server\models" ( set "TRANSLATOR_MODELS=%cd%\server\models" & goto :have_models )
if exist "D:\discord-translator-bot\server\models" set "TRANSLATOR_MODELS=D:\discord-translator-bot\server\models"
:have_models
if defined TRANSLATOR_MODELS ( echo [setup] models: %TRANSLATOR_MODELS% ) else ( echo [setup] models: none found - first start downloads ~3.3 GB from HuggingFace )

REM ---- 5. build + start --------------------------------------------------
if "%GPU%"=="1" (
  echo [setup] building + starting with CUDA...
  docker compose -f compose.yml -f compose.gpu.yml up -d --build
) else (
  echo [setup] building + starting (CPU)...
  docker compose up -d --build
)
if errorlevel 1 ( echo [setup] compose failed - check the output above & pause & exit /b 1 )

REM ---- 6. health ---------------------------------------------------------
echo [setup] waiting for the pipeline (model load ~30s on first run)...
set /a tries=0
:waithealth
timeout /t 4 /nobreak >nul
curl -s http://localhost:8765/health > "%TEMP%\thealth.json" 2>nul
findstr /C:"true" "%TEMP%\thealth.json" >nul 2>&1
if not errorlevel 1 goto :health_ok
set /a tries+=1
if !tries! lss 45 goto :waithealth
:health_ok
if exist "%TEMP%\thealth.json" (
  echo [setup] server status:
  type "%TEMP%\thealth.json"
  echo.
)
echo.
echo ============================================================
echo  Stack is up.  Subtitles: ws://localhost:8765/ws
echo  Health:   http://localhost:8765/health   Bot: http://localhost:8766/
echo  The voice-bot auto-joins your configured voice channel.
echo  Stop everything with:  stop_bot.bat
echo ============================================================
if "%GPU%"=="1" echo  If "device" above says cpu instead of cuda, see README ^(WSL2 GPU setup^).
pause
endlocal
