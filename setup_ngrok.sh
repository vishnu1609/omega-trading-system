#!/bin/bash
# ============================================================
# Ngrok Setup for Omega Trading System
# Exposes your local webhook (port 8000) to the internet
# so TradingView alerts can reach your bot.
# ============================================================

set -e

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║     OMEGA — NGROK WEBHOOK TUNNEL SETUP      ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ── Step 1: Check if ngrok is installed ──
if command -v ngrok &> /dev/null; then
    echo "✅ ngrok is installed: $(ngrok version)"
else
    echo "❌ ngrok not found. Installing via Homebrew..."
    if command -v brew &> /dev/null; then
        brew install ngrok/ngrok/ngrok
    else
        echo ""
        echo "   Install manually:"
        echo "   1. Go to https://ngrok.com/download"
        echo "   2. Download for macOS"
        echo "   3. Unzip and move to /usr/local/bin/"
        echo "   OR: brew install ngrok/ngrok/ngrok"
        exit 1
    fi
fi

# ── Step 2: Check auth token ──
echo ""
if ngrok config check &> /dev/null; then
    echo "✅ ngrok auth configured"
else
    echo "⚠️  ngrok needs an auth token (free account)."
    echo "   1. Sign up at https://dashboard.ngrok.com/signup"
    echo "   2. Copy your auth token from the dashboard"
    echo ""
    read -p "   Paste your ngrok auth token: " NGROK_TOKEN
    ngrok config add-authtoken "$NGROK_TOKEN"
    echo "✅ Auth token saved"
fi

# ── Step 3: Start tunnel ──
echo ""
echo "🚀 Starting ngrok tunnel on port 8000..."
echo "   Your webhook URL will appear below."
echo "   Copy it into TradingView alert settings."
echo ""
echo "   TradingView Alert Webhook URL format:"
echo "   https://xxxx-xxxx.ngrok-free.app/tv-alert"
echo ""
echo "   Press Ctrl+C to stop the tunnel."
echo "───────────────────────────────────────────────"
echo ""

ngrok http 8000
