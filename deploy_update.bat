@echo off
title APEX - PUSH UPDATE TO VPS
color 0B

echo.
echo  Pushing bot.py update to VPS...
echo.

set /p VPS_IP="VPS IP (or press Enter to use saved): "
if "%VPS_IP%"=="" set VPS_IP=%APEX_VPS_IP%
if "%VPS_IP%"=="" ( echo No IP. Set APEX_VPS_IP env var or enter manually. & pause & exit )

set VPS_USER=root
set APEX_DIR=/home/apexbot/apex

echo  Uploading bot.py to %VPS_IP%...
scp -o StrictHostKeyChecking=no bot.py %VPS_USER%@%VPS_IP%:%APEX_DIR%/bot.py
if %errorlevel% neq 0 ( echo  Upload failed. & pause & exit )

echo  Restarting service...
ssh -o StrictHostKeyChecking=no %VPS_USER%@%VPS_IP% "chown apexbot:apexbot %APEX_DIR%/bot.py && systemctl restart apex-bot && sleep 2 && systemctl is-active apex-bot"

echo.
echo  Done. Bot restarted with updated bot.py.
echo  Telegram should confirm APEX V4.1 ONLINE shortly.
echo.
pause
