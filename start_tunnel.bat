@echo off
REM ============================================================
REM  Optional Cloudflare Tunnel container — works with BOTH versions
REM  (voice-bot via start_bot.bat, loopback via start_loopback.bat).
REM  Starts/stops independently of either: remote friends get
REM  wss://<your-hostname>/ws whether the bot or the VB-Cable is feeding
REM  audio. start the server first, then this; stop_tunnel.bat removes
REM  just the tunnel.
REM
REM  One-time setup (dashboard-managed tunnel, ~2 minutes):
REM    1. Zero Trust dashboard -> Networks -> Tunnels -> Create tunnel
REM       -> choose "Cloudflared" -> name it -> copy the TOKEN.
REM    2. Paste the token into docker-data\config.json:
REM         "cloudflare": { "tunnel_token": "<paste>" }
REM    3. Same tunnel page -> Public Hostname -> add:
REM          subdomain: subs   domain: yourdomain.com
REM          service:   HTTP   http://server:8765
REM       ("server" is the compose service name — cloudflared reaches it
REM        over the Docker network; WebSockets pass through fine.)
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist "docker-data\config.json" (
  echo [tunnel] docker-data\config.json is missing - set up the server first.
  exit /b 1
)

set "TOKEN="
for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "(Get-Content -Raw docker-data\config.json | ConvertFrom-Json).cloudflare.tunnel_token" 2^>nul`) do set "TOKEN=%%T"

if "%TOKEN%"=="" (
  echo [tunnel] cloudflare.tunnel_token is empty in docker-data\config.json.
  echo   Get it from the Zero Trust dashboard -^> Networks -^> Tunnels -^>
  echo   Create tunnel -^> Docker, paste it into config.json, then in the
  echo   tunnel's Public Hostname settings point a hostname at
  echo   http://server:8765 and rerun this script.
  exit /b 1
)

REM compose interpolates ${CLOUDFLARE_TUNNEL_TOKEN} from .env automatically
> .env echo CLOUDFLARE_TUNNEL_TOKEN=%TOKEN%

echo [tunnel] starting cloudflared container...
docker compose --profile tunnel up -d cloudflared --no-deps || exit /b 1
timeout /t 3 /nobreak >nul
docker compose --profile tunnel logs --tail 15 cloudflared
echo.
echo [tunnel] running. If friends can't connect, check the Public Hostname
echo           service is http://server:8765 in the Zero Trust dashboard.
endlocal
