#!/bin/bash
# ================================================================
# APEX TRADING BOT - VPS SETUP SCRIPT
# Run once on a fresh Ubuntu 22.04 VPS as root
# Usage: bash vps_setup.sh
# ================================================================

set -e

APEX_USER="apexbot"
APEX_DIR="/home/$APEX_USER/apex"
SERVICE="apex-bot"
PYTHON="python3"

echo ""
echo "=================================================="
echo " APEX VPS SETUP"
echo "=================================================="
echo ""

# ================================================================
# 1. SYSTEM UPDATE
# ================================================================
echo "[1/7] Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq python3 python3-pip curl wget ufw

# ================================================================
# 2. CREATE DEDICATED USER
# ================================================================
echo "[2/7] Creating apex user..."
if ! id "$APEX_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$APEX_USER"
    echo "  Created user: $APEX_USER"
else
    echo "  User $APEX_USER already exists"
fi

# ================================================================
# 3. CREATE WORKING DIRECTORY AND SET PERMISSIONS
# ================================================================
echo "[3/7] Setting up working directory: $APEX_DIR"
mkdir -p "$APEX_DIR"
chown -R "$APEX_USER:$APEX_USER" "$APEX_DIR"

# ================================================================
# 4. INSTALL PYTHON DEPENDENCIES
# ================================================================
echo "[4/7] Installing Python dependencies..."
pip3 install -q requests flask flask-cors

# ================================================================
# 5. FIREWALL - block everything except SSH
# ================================================================
echo "[5/7] Configuring firewall..."
ufw default deny incoming   > /dev/null
ufw default allow outgoing  > /dev/null
ufw allow ssh               > /dev/null
ufw --force enable          > /dev/null
echo "  Firewall active - SSH only inbound"

# ================================================================
# 6. CREATE SYSTEMD SERVICE
# ================================================================
echo "[6/7] Creating systemd service: $SERVICE..."

cat > /etc/systemd/system/$SERVICE.service << EOF
[Unit]
Description=APEX Auto-Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APEX_USER
WorkingDirectory=$APEX_DIR
ExecStart=$PYTHON $APEX_DIR/bot.py
Restart=always
RestartSec=15
StandardOutput=append:$APEX_DIR/trading_bot.log
StandardError=append:$APEX_DIR/trading_bot.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable $SERVICE

echo "  Service created and enabled on boot"

# ================================================================
# 7. INITIALISE STATE FILES (if bot.py is present)
# ================================================================
echo "[7/7] Checking for bot.py..."

if [ -f "$APEX_DIR/bot.py" ]; then
    # Create state.json if missing
    if [ ! -f "$APEX_DIR/state.json" ]; then
        echo '{"trades":{},"daily_loss":0.0,"daily_date":"","wins":0,"losses":0,"consec":0,"journal":[]}' > "$APEX_DIR/state.json"
        chown "$APEX_USER:$APEX_USER" "$APEX_DIR/state.json"
        echo "  Created state.json"
    fi

    # Create trade_log.csv if missing
    if [ ! -f "$APEX_DIR/trade_log.csv" ]; then
        echo "date,instrument,direction,timeframe,entry,tp,sl,atr,lots,strategies,score,result,pl,duration_min,balance_after,notes" > "$APEX_DIR/trade_log.csv"
        chown "$APEX_USER:$APEX_USER" "$APEX_DIR/trade_log.csv"
        echo "  Created trade_log.csv"
    fi

    systemctl start $SERVICE
    sleep 2

    echo ""
    echo "  Bot status:"
    systemctl is-active $SERVICE && echo "  STATUS: RUNNING" || echo "  STATUS: FAILED - check logs below"
else
    echo "  bot.py not found yet - upload it then run:"
    echo "    systemctl start $SERVICE"
fi

# ================================================================
# DONE
# ================================================================
echo ""
echo "=================================================="
echo " SETUP COMPLETE"
echo ""
echo " Bot files   : $APEX_DIR/"
echo " Service     : systemctl status $SERVICE"
echo " Logs        : tail -f $APEX_DIR/trading_bot.log"
echo " Start       : systemctl start $SERVICE"
echo " Stop        : systemctl stop $SERVICE"
echo " Restart     : systemctl restart $SERVICE"
echo ""
echo " The bot will auto-start on every server reboot."
echo " Telegram alerts will confirm when it comes online."
echo "=================================================="
echo ""
