@echo off
REM Stop the loopback version: shipper + joiner processes + server container.
cd /d "%~dp0"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'audio_shipper|discord_web_joiner' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
docker compose stop server
echo [stop] loopback version stopped.
