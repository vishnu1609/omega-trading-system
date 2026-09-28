#!/bin/bash
# Omega Trading System — VPS Deploy Script
# Usage: ./deploy.sh [setup|start|stop|logs|status]

set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="omega-trading"

case "${1:-help}" in
  setup)
    echo "=== Installing dependencies ==="
    sudo apt-get update
    sudo apt-get install -y python3 python3-pip python3-venv

    echo "=== Creating virtual environment ==="
    cd "$APP_DIR"
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt

    echo "=== Creating systemd service ==="
    sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null <<EOF
[Unit]
Description=Omega Trading System
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/python3 main.py
Restart=on-failure
RestartSec=30
StandardOutput=append:$APP_DIR/logs/omega_stdout.log
StandardError=append:$APP_DIR/logs/omega_stdout.log

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable $SERVICE_NAME
    mkdir -p "$APP_DIR/data" "$APP_DIR/logs/daily_reports"

    echo ""
    echo "=== Setup complete ==="
    echo "1. Copy your .env file:  cp .env.example .env && nano .env"
    echo "2. Start the bot:        ./deploy.sh start"
    echo "3. Dashboard at:         http://YOUR_SERVER_IP:8000/dashboard"
    ;;

  start)
    sudo systemctl start $SERVICE_NAME
    echo "Omega started. Check: ./deploy.sh status"
    ;;

  stop)
    sudo systemctl stop $SERVICE_NAME
    echo "Omega stopped."
    ;;

  restart)
    sudo systemctl restart $SERVICE_NAME
    echo "Omega restarted."
    ;;

  logs)
    tail -f "$APP_DIR/logs/omega.log"
    ;;

  status)
    sudo systemctl status $SERVICE_NAME --no-pager
    echo ""
    echo "--- Last 10 log lines ---"
    tail -10 "$APP_DIR/logs/omega.log" 2>/dev/null
    ;;

  docker)
    cd "$APP_DIR"
    docker compose up -d --build
    echo "Omega running in Docker. Dashboard: http://localhost:8000/dashboard"
    ;;

  *)
    echo "Usage: ./deploy.sh [setup|start|stop|restart|logs|status|docker]"
    echo ""
    echo "  setup   - Install deps, create venv, configure systemd"
    echo "  start   - Start the bot"
    echo "  stop    - Stop the bot"
    echo "  restart - Restart the bot"
    echo "  logs    - Tail live logs"
    echo "  status  - Show bot status"
    echo "  docker  - Run with Docker Compose"
    ;;
esac
