@echo off
title APEX TRADING SYSTEM
color 0A
echo.
echo  ================================================
echo   APEX TRADING SYSTEM V3 - STARTING UP
echo  ================================================
echo.

cd /d C:\Users\LANOVO\Desktop\APEX_TRADER

echo  [1/3] Starting Proxy Server (Gold/Oil/Forex)...
start "APEX PROXY" cmd /k "cd /d C:\Users\LANOVO\Desktop\APEX_TRADER && python proxy.py"
timeout /t 3 /nobreak >nul

echo  [2/3] Starting Auto-Trader Bot V3...
start "APEX BOT V3" cmd /k "cd /d C:\Users\LANOVO\Desktop\APEX_TRADER && python bot.py"
timeout /t 3 /nobreak >nul

echo  [3/3] Opening APEX Ultimate...
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --disable-web-security --user-data-dir="C:\chrome-dev" --allow-file-access-from-files "C:\Users\LANOVO\Desktop\APEX_TRADER\apex-ultimate.html"

echo.
echo  ================================================
echo   ALL SYSTEMS RUNNING
echo.
echo   PROXY window  = Keep open (Gold/Oil/Forex)
echo   BOT window    = Keep open (auto-trading)
echo   Chrome        = APEX Ultimate loaded
echo.
echo   Do NOT close any Command Prompt windows!
echo  ================================================
echo.
echo  Press any key to close this launcher...
pause >nul
