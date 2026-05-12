#!/bin/bash
# Manual redeploy script. Run from the VM after pushing code changes.
# Usage: bash /home/ubuntu/scheduler-bot/deploy/redeploy.sh
set -e

APP_DIR="/home/ubuntu/scheduler-bot"
VENV_DIR="/home/ubuntu/venv"

echo "==> Pulling latest code..."
git -C "$APP_DIR" pull origin main

echo "==> Installing any new dependencies..."
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt" -q

echo "==> Restarting bot..."
sudo systemctl restart schedulerbot
sleep 3
sudo systemctl status schedulerbot --no-pager
