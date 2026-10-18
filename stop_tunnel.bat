@echo off
REM Stop and remove the Cloudflare tunnel container. The server (and the
REM voice-bot, if running) keep running — only internet exposure stops.
cd /d "%~dp0"
docker compose --profile tunnel rm -sf cloudflared
echo [tunnel] stopped. LAN connections and the bot are unaffected.
