@echo off
rem ============================================================
rem  Rebuild the console-less Windows .exe
rem  Result: dist\RobloxMultiInstance.exe  (single file, no console)
rem
rem  Requires PyInstaller (one-time):
rem      python -m pip install pyinstaller
rem ============================================================
cd /d "%~dp0"
python -m PyInstaller --noconsole --onefile ^
    --name RobloxMultiInstance ^
    --hidden-import cookie_login ^
    --hidden-import bootstrapper ^
    --clean roblox_launcher.py
echo.
echo Done: dist\RobloxMultiInstance.exe
pause