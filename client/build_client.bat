@echo off
rem Build the one-file client exe + zip for sharing with friends.
rem Output: dist\TranslatorOverlay.zip  (exe + FRIENDS_README.txt)
setlocal
cd /d "%~dp0"
if not exist venv ( echo [build] venv not found - run install.bat first. & pause & exit /b 1 )

echo [build] Installing PyInstaller...
venv\Scripts\python -m pip install pyinstaller -q
if errorlevel 1 ( echo [build] PyInstaller install failed & pause & exit /b 1 )

echo [build] Building TranslatorOverlay.exe (one file, no console)...
venv\Scripts\python -m PyInstaller --onefile --noconsole --clean ^
    --name TranslatorOverlay client.py
if errorlevel 1 ( echo [build] PyInstaller failed & pause & exit /b 1 )

powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\TranslatorOverlay.exe','FRIENDS_README.txt' -DestinationPath 'dist\TranslatorOverlay.zip'"
if errorlevel 1 ( echo [build] zip failed & pause & exit /b 1 )

echo.
echo ============================================================
echo  Built: dist\TranslatorOverlay.zip  - send this to friends.
echo ============================================================
pause
