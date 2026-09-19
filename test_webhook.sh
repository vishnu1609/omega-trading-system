#!/bin/bash
# ============================================================
# Test webhook locally — simulates a TradingView alert
# Run this AFTER main.py is running to verify the pipeline.
# ============================================================

BASE_URL="${1:-http://localhost:8000}"

echo ""
echo "Testing Omega webhook at $BASE_URL"
echo "──────────────────────────────────────"

# Health check
echo ""
echo "1. Health check..."
curl -s "$BASE_URL/health" | python3 -m json.tool
echo ""

# BUY CE signal
echo "2. Sending BUY_CE signal (Bank Nifty)..."
curl -s -X POST "$BASE_URL/tv-alert" \
  -H "Content-Type: application/json" \
  -d '{
    "strategy": "omega_complete",
    "symbol": "BANKNIFTY",
    "side": "BUY_CE",
    "strike": 50000,
    "sl": 49500,
    "ts": 1700000000
  }' | python3 -m json.tool
echo ""

sleep 2

# EXIT signal
echo "3. Sending EXIT signal..."
curl -s -X POST "$BASE_URL/tv-alert" \
  -H "Content-Type: application/json" \
  -d '{
    "strategy": "omega_complete",
    "symbol": "BANKNIFTY",
    "side": "EXIT",
    "strike": 50000,
    "ts": 1700000060
  }' | python3 -m json.tool
echo ""

# BUY PE signal
echo "4. Sending BUY_PE signal (Nifty)..."
curl -s -X POST "$BASE_URL/tv-alert" \
  -H "Content-Type: application/json" \
  -d '{
    "strategy": "omega_complete",
    "symbol": "NIFTY",
    "side": "BUY_PE",
    "strike": 23500,
    "sl": 23700,
    "ts": 1700000120
  }' | python3 -m json.tool
echo ""

echo "──────────────────────────────────────"
echo "✅ Test signals sent. Check your terminal running main.py for logs."
echo ""
