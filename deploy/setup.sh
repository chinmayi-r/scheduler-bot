#!/bin/bash
# One-time setup script for Oracle Cloud Ubuntu 22.04 VM.
# Run this after SSHing in: bash setup.sh
set -e

REPO_URL="https://github.com/chinmayi-r/scheduler-bot.git"  # update if different
APP_DIR="/home/ubuntu/scheduler-bot"
VENV_DIR="/home/ubuntu/venv"

echo "==> Updating packages and installing Python 3.11..."
sudo apt-get update -q
sudo apt-get install -y python3.11 python3.11-venv python3-pip git

echo "==> Cloning repo..."
if [ -d "$APP_DIR" ]; then
  echo "    Directory already exists, pulling latest instead."
  git -C "$APP_DIR" pull origin main
else
  git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> Creating virtualenv and installing dependencies..."
python3.11 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt" -q

echo "==> Installing systemd service..."
sudo cp "$APP_DIR/deploy/schedulerbot.service" /etc/systemd/system/schedulerbot.service
sudo systemctl daemon-reload
sudo systemctl enable schedulerbot

echo ""
echo "==> Setup complete!"
echo ""
echo "Before starting the bot, create your .env file:"
echo "    nano $APP_DIR/.env"
echo ""
echo "Minimum required content:"
echo "    TELEGRAM_BOT_TOKEN=your_token_here"
echo ""
echo "Then start the bot:"
echo "    sudo systemctl start schedulerbot"
echo "    sudo systemctl status schedulerbot"
echo "    sudo journalctl -u schedulerbot -f   # live logs"
