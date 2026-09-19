#!/bin/bash
# Omega Trading System — Start bot + ngrok tunnel
# Usage: ./start.sh

set -e
cd "$(dirname "$0")"

echo "╔══════════════════════════════════════════════╗"
echo "║     OMEGA TRADING SYSTEM — LAUNCHER          ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Check ngrok auth
if ! ngrok config check &>/dev/null; then
    echo "❌ ngrok not configured. Run:"
    echo "   ngrok config add-authtoken YOUR_TOKEN"
    echo ""
    echo "   Get a free token at: https://dashboard.ngrok.com/get-started/your-authtoken"
    exit 1
fi

# Start the bot in background
echo "🚀 Starting Omega bot (paper mode)..."
python3 main.py &
BOT_PID=$!
sleep 2

# Check bot is running
if ! kill -0 $BOT_PID 2>/dev/null; then
    echo "❌ Bot failed to start (likely outside market hours)"
    echo "   NSE: 09:15 - 15:15 IST | MCX: 09:00 - 23:15 IST"
    exit 1
fi

# Start ngrok tunnel
echo "🌐 Starting ngrok tunnel on port 8000..."
ngrok http 8000 --log=stdout > logs/ngrok.log 2>&1 &
NGROK_PID=$!
sleep 3

# Get public URL
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])" 2>/dev/null)

if [ -z "$NGROK_URL" ]; then
    echo "❌ ngrok failed to start"
    kill $BOT_PID 2>/dev/null
    exit 1
fi

echo ""
echo "════════════════════════════════════════════════"
echo "  ✅ Bot running (PID: $BOT_PID)"
echo "  ✅ Tunnel active"
echo ""
echo "  📡 Webhook URL for TradingView:"
echo "  ${NGROK_URL}/tv-alert"
echo ""
echo "  Paste this URL in your TradingView alert webhook field."
echo "════════════════════════════════════════════════"
echo ""
echo "Press Ctrl+C to stop everything."

# Cleanup on exit
trap "echo ''; echo 'Stopping...'; kill $BOT_PID $NGROK_PID 2>/dev/null; echo 'Done.'" EXIT

# Wait for bot
wait $BOT_PID
