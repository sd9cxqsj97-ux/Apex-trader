@echo off
title APEX VPS DEPLOY
color 0A

echo.
echo  ================================================
echo   APEX VPS DEPLOYMENT TOOL
echo  ================================================
echo.
echo  This uploads bot.py to your VPS and starts it.
echo  Requires: SSH key set up OR password auth.
echo.
echo  COST GUIDE (cheapest Ubuntu 22.04):
echo    Vultr:        $3.50/month  (1GB RAM) - RECOMMENDED
echo    DigitalOcean: $6.00/month  (1GB RAM)
echo    Hetzner:      2.96EUR/mo   (2GB RAM) - BEST VALUE
echo.
echo  HOW TO GET A VPS:
echo    1. Create account at vultr.com or hetzner.com
echo    2. Deploy Ubuntu 22.04 LTS (smallest plan)
echo    3. Copy the server IP address
echo    4. Run this script
echo.

:: ============================================================
:: GET SERVER DETAILS
:: ============================================================
set /p VPS_IP="Enter VPS IP address: "
if "%VPS_IP%"=="" ( echo No IP entered. Exiting. & pause & exit )

set VPS_USER=root
set APEX_DIR=/home/apexbot/apex

echo.
echo  Target: %VPS_USER%@%VPS_IP%:%APEX_DIR%
echo.

:: ============================================================
:: STEP 1 - RUN SETUP SCRIPT ON VPS
:: ============================================================
echo  [1/3] Running setup script on VPS...
echo        (First time: installs Python, creates service)
echo.
scp -o StrictHostKeyChecking=no vps_setup.sh %VPS_USER%@%VPS_IP%:/tmp/vps_setup.sh
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: Could not connect to %VPS_IP%
    echo  Check: IP correct? SSH key added? Server running?
    pause & exit
)
ssh -o StrictHostKeyChecking=no %VPS_USER%@%VPS_IP% "bash /tmp/vps_setup.sh"

:: ============================================================
:: STEP 2 - UPLOAD BOT FILE
:: ============================================================
echo.
echo  [2/3] Uploading bot.py...
ssh %VPS_USER%@%VPS_IP% "mkdir -p %APEX_DIR% && chown -R apexbot:apexbot /home/apexbot"
scp -o StrictHostKeyChecking=no bot.py %VPS_USER%@%VPS_IP%:%APEX_DIR%/bot.py
if %errorlevel% neq 0 ( echo  ERROR uploading bot.py & pause & exit )
ssh %VPS_USER%@%VPS_IP% "chown apexbot:apexbot %APEX_DIR%/bot.py"

:: ============================================================
:: STEP 3 - START / RESTART SERVICE
:: ============================================================
echo.
echo  [3/3] Starting APEX bot service...
ssh %VPS_USER%@%VPS_IP% "systemctl restart apex-bot && sleep 3 && systemctl is-active apex-bot"

echo.
echo  ================================================
echo   DEPLOYMENT COMPLETE
echo.
echo   Bot is running 24/7 on %VPS_IP%
echo   Telegram alerts are active.
echo.
echo   USEFUL COMMANDS (paste into SSH session):
echo.
echo   View live logs:
echo     ssh %VPS_USER%@%VPS_IP%
echo     tail -f /home/apexbot/apex/trading_bot.log
echo.
echo   Check status:
echo     systemctl status apex-bot
echo.
echo   Stop bot:
echo     systemctl stop apex-bot
echo.
echo   Download trade log to this PC:
echo     (run in a new CMD window)
echo     scp %VPS_USER%@%VPS_IP%:/home/apexbot/apex/trade_log.csv .
echo.
echo   Update bot after changes:
echo     deploy_update.bat  (or re-run this script)
echo  ================================================
echo.
pause
