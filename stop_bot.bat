@echo off
REM Stop the bot version (server + voice-bot containers).
cd /d "%~dp0"
docker compose down
