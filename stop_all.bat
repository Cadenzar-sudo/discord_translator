@echo off
rem Stop the whole translator stack: server, Discord Web joiner, overlay
rem client, and the joiner's Chromium (your own Chrome is NOT touched -
rem only the Playwright browser from ms-playwright is closed).
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object {$_.CommandLine -match 'main.py|discord_web_joiner|client.py'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Get-Process chrome -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*ms-playwright*' } | Stop-Process -Force -ErrorAction SilentlyContinue"
echo [stop] translator stack stopped.
%SystemRoot%\System32\ping.exe -n 4 127.0.0.1 >nul
