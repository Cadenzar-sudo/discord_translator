@echo off
rem Build the ADMIN overlay exe: same client, but the zip ships a config.json
rem pre-seeded with admin_token (from your client\config.json) so the owner's
rem hotkeys work: Ctrl+Alt+G = chat/game mode, Ctrl+Alt+L = cycle pinned
rem language. Friends get the regular build_admin-less TranslatorOverlay.zip.
rem Output: dist\TranslatorOverlayAdmin.zip
setlocal
cd /d "%~dp0"
if not exist venv ( echo [build] venv not found - run install.bat first. & pause & exit /b 1 )
if not exist config.json ( echo [build] client\config.json not found - run the client once, set it up, then rebuild. & pause & exit /b 1 )

rem the admin zip needs a real admin token in your own client config
findstr /C:"admin_token" config.json >nul 2>&1
if errorlevel 1 ( echo [build] your client\config.json has no admin_token - the admin exe would be pointless. & pause & exit /b 1 )

echo [build] Installing PyInstaller...
venv\Scripts\python -m pip install pyinstaller -q
if errorlevel 1 ( echo [build] PyInstaller install failed & pause & exit /b 1 )

echo [build] Building TranslatorOverlayAdmin.exe (one file, no console)...
venv\Scripts\python -m PyInstaller --onefile --noconsole --clean ^
    --name TranslatorOverlayAdmin client.py
if errorlevel 1 ( echo [build] PyInstaller failed & pause & exit /b 1 )

> "%TEMP%\admin_readme.txt" (
    echo Translator Overlay - ADMIN build
    echo ================================
    echo Same overlay as your friends use, plus the server admin token:
    echo   Ctrl+Alt+G   toggle CHAT / GAME mode on the server
    echo   Ctrl+Alt+L   cycle the pinned language (auto - ko - ja - vi)
    echo   Ctrl+Alt+P   pause subtitles    Ctrl+Alt+O show/hide
    echo   Ctrl+Alt+I   unlock click-through to reach the tray menu
    echo   Ctrl+Alt+Q   quit
    echo.
    echo Do NOT share this zip - it contains the admin token.
)
powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\TranslatorOverlayAdmin.exe','config.json','%TEMP%\admin_readme.txt' -DestinationPath 'dist\TranslatorOverlayAdmin.zip'" 2>nul
if errorlevel 1 (
    powershell -NoProfile -Command "Copy-Item '%TEMP%\admin_readme.txt' 'dist\ADMIN_README.txt'; Compress-Archive -Force -Path 'dist\TranslatorOverlayAdmin.exe','config.json','dist\ADMIN_README.txt' -DestinationPath 'dist\TranslatorOverlayAdmin.zip'"
)
if errorlevel 1 ( echo [build] zip failed & pause & exit /b 1 )

echo.
echo ============================================================
echo  Built: dist\TranslatorOverlayAdmin.zip  (keep it private!)
echo  Regular friend build is still: build_client.bat
echo ============================================================
pause
