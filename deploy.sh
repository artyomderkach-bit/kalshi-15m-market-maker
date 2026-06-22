#!/bin/bash
# Deploy to a small Ubuntu VPS via rsync + systemd.
#   KALSHI_SSH_KEY   path to your SSH private key
#   KALSHI_SSH_HOST  user@host of the server
set -e

KEY="${KALSHI_SSH_KEY:-$HOME/.ssh/id_rsa}"
SERVER="${KALSHI_SSH_HOST:-ubuntu@your.server.ip}"
REMOTE_DIR='~/kalshi-15m-bot'

echo "==> rsync project"
rsync -az --delete \
  --exclude .venv --exclude venv --exclude data --exclude .git \
  --exclude __pycache__ --exclude .pytest_cache --exclude '*.log' \
  -e "ssh -i $KEY" ./ "$SERVER:$REMOTE_DIR/"

echo "==> install"
ssh -i "$KEY" "$SERVER" bash -s <<'EOF'
cd ~/kalshi-15m-bot
mkdir -p data
[ -d .venv ] || python3 -m venv .venv
# install sequentially with --no-cache-dir: the instance is small and a bulk
# install gets OOM-killed
for p in requests cryptography python-dotenv pandas plotly streamlit; do
  .venv/bin/pip install -q --no-cache-dir "$p"
done
EOF

echo "==> restart via systemd (survives reboots)"
ssh -i "$KEY" "$SERVER" bash -s <<'EOF'
# one-time setup if units missing
if [ ! -f /etc/systemd/system/kalshi-15m-bot.service ]; then
  sudo tee /etc/systemd/system/kalshi-15m-bot.service > /dev/null << 'UNIT'
[Unit]
Description=Kalshi 15m Crypto Trading Bot
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/kalshi-15m-bot
ExecStartPre=/home/ubuntu/kalshi-15m-bot/scripts/ensure_deps.sh
ExecStart=/home/ubuntu/kalshi-15m-bot/.venv/bin/python -m bot.run
Restart=always
RestartSec=10
StandardOutput=append:/home/ubuntu/kalshi-15m-bot/bot.log
StandardError=append:/home/ubuntu/kalshi-15m-bot/bot.log
[Install]
WantedBy=multi-user.target
UNIT
  sudo tee /etc/systemd/system/kalshi-15m-dash.service > /dev/null << 'UNIT'
[Unit]
Description=Kalshi 15m Crypto Bot Dashboard
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/kalshi-15m-bot
ExecStart=/home/ubuntu/kalshi-15m-bot/.venv/bin/streamlit run app.py
Restart=always
RestartSec=10
StandardOutput=append:/home/ubuntu/kalshi-15m-bot/dash.log
StandardError=append:/home/ubuntu/kalshi-15m-bot/dash.log
[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable kalshi-15m-bot kalshi-15m-dash
fi
# stop legacy screen sessions if any
screen -S k15m-bot -X quit 2>/dev/null || true
screen -S k15m-dash -X quit 2>/dev/null || true
sudo systemctl restart kalshi-15m-bot kalshi-15m-dash
sleep 3
systemctl is-active kalshi-15m-bot kalshi-15m-dash

# Watchdog cron: health check every 2h; daily summary + email at 9:30 AM Central.
MARKER="# kalshi-watchdog"
if ! crontab -l 2>/dev/null | grep -q "$MARKER"; then
  (crontab -l 2>/dev/null; echo "0 */2 * * * cd ~/kalshi-15m-bot && .venv/bin/python scripts/watchdog.py $MARKER") | crontab -
fi
SUMMARY_MARKER="# kalshi-daily-summary"
if ! crontab -l 2>/dev/null | grep -q "$SUMMARY_MARKER"; then
  (crontab -l 2>/dev/null; echo "CRON_TZ=America/Chicago"; echo "30 9 * * * cd ~/kalshi-15m-bot && .venv/bin/python scripts/watchdog.py summary $SUMMARY_MARKER") | crontab -
fi
CALIB_MARKER="# kalshi-weekly-calibration"
if ! crontab -l 2>/dev/null | grep -q "$CALIB_MARKER"; then
  (crontab -l 2>/dev/null; echo "CRON_TZ=America/Chicago"; echo "0 10 * * 1 cd ~/kalshi-15m-bot && .venv/bin/python scripts/calibrate.py $CALIB_MARKER") | crontab -
fi
EOF

echo "==> done. dashboard: http://<your-server>:8501"
echo "logs:  ssh -i $KEY $SERVER 'sudo journalctl -u kalshi-15m-bot -n 50 --no-pager'"
