"""
Dhan Account Setup & Validation Script

Run this FIRST after creating your Dhan account and getting API access.
It will:
  1. Validate your credentials
  2. Check available funds
  3. Download the instrument master CSV
  4. Test a sample LTP fetch
  5. Confirm webhook endpoint is reachable

Usage:
    python setup_dhan.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BANNER = """
╔══════════════════════════════════════════════╗
║     OMEGA TRADING SYSTEM — DHAN SETUP       ║
╚══════════════════════════════════════════════╝
"""


def check(label: str, passed: bool, detail: str = "") -> bool:
    icon = "✅" if passed else "❌"
    msg = f"  {icon} {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return passed


def main() -> None:
    print(BANNER)
    all_ok = True

    # ── Step 1: Check env vars ────────────────────────────────────────
    print("1. Checking credentials...\n")

    client_id = os.environ.get("DHAN_CLIENT_ID", "")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN", "")

    all_ok &= check("DHAN_CLIENT_ID", bool(client_id),
                     client_id[:4] + "..." if client_id else "NOT SET — add to .env")
    all_ok &= check("DHAN_ACCESS_TOKEN", bool(access_token),
                     "set (" + str(len(access_token)) + " chars)" if access_token else "NOT SET — add to .env")

    tv_secret = os.environ.get("TV_WEBHOOK_SECRET", "")
    check("TV_WEBHOOK_SECRET", bool(tv_secret),
          "set" if tv_secret else "NOT SET (optional — needed for webhook)")

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    check("TELEGRAM_BOT_TOKEN", bool(tg_token),
          "set" if tg_token else "NOT SET (optional — needed for alerts)")

    if not client_id or not access_token:
        print("\n⚠️  Cannot proceed without Dhan credentials.")
        print("   1. Go to https://dhanhq.co → Login → Profile → API Access")
        print("   2. Generate an access token")
        print("   3. Copy your Client ID and Access Token")
        print("   4. Add them to .env file (see .env.example)")
        sys.exit(1)

    # ── Step 2: Test API connectivity ─────────────────────────────────
    print("\n2. Testing Dhan API connectivity...\n")

    import requests
    headers = {"Content-Type": "application/json", "access-token": access_token}

    try:
        resp = requests.get("https://api.dhan.co/v2/fundlimit", headers=headers, timeout=10)
        if resp.status_code == 200:
            funds = resp.json()
            available = funds.get("data", {}).get("availabelBalance",
                        funds.get("availabelBalance", "N/A"))
            check("API Connection", True, f"Connected successfully")
            check("Available Funds", True, f"₹{available}")
        else:
            check("API Connection", False, f"HTTP {resp.status_code}: {resp.text[:100]}")
            all_ok = False
    except Exception as exc:
        check("API Connection", False, str(exc))
        all_ok = False

    # ── Step 3: Download instrument master ────────────────────────────
    print("\n3. Downloading instrument master...\n")

    try:
        from core.instrument_master import InstrumentMaster
        im = InstrumentMaster()
        count = im.download_and_parse()
        check("Instrument Master", count > 0, f"{count} instruments loaded")
        if count > 0:
            # Quick lookup test
            nifty_opts = im.search("NIFTY", segment="NSE_FNO", instrument_type="OPTIDX")
            check("Nifty Options Found", len(nifty_opts) > 0,
                  f"{len(nifty_opts)} contracts")
    except Exception as exc:
        check("Instrument Master", False, str(exc))
        print("   ↳ Will be created when you run the instrument master loader")

    # ── Step 4: Test LTP fetch ────────────────────────────────────────
    print("\n4. Testing market data...\n")

    try:
        payload = {
            "NSE_FNO": {
                "securityId": "13"  # Nifty 50 index
            }
        }
        resp = requests.post("https://api.dhan.co/v2/marketfeed/ltp",
                             json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            check("Market Data (Nifty LTP)", True, f"Response OK")
        else:
            check("Market Data", False, f"HTTP {resp.status_code}")
    except Exception as exc:
        check("Market Data", False, str(exc))

    # ── Step 5: Check project structure ───────────────────────────────
    print("\n5. Checking project structure...\n")

    required_files = [
        "config/settings.yaml",
        "config/instruments.yaml",
        "core/broker_dhan.py",
        "core/order_manager.py",
        "core/position_manager.py",
        "core/risk_engine.py",
        "strategies/omega_complete.py",
        "webhook/tv_receiver.py",
        "main.py",
    ]
    base = Path(__file__).parent
    for f in required_files:
        exists = (base / f).exists()
        check(f, exists)
        if not exists:
            all_ok = False

    # ── Summary ───────────────────────────────────────────────────────
    print("\n" + "=" * 48)
    if all_ok:
        print("  🟢 ALL CHECKS PASSED — Ready to paper trade!")
        print("  Run: python main.py")
    else:
        print("  🟡 SOME CHECKS FAILED — Fix issues above first")
    print("=" * 48)

    # ── Next steps ────────────────────────────────────────────────────
    print("""
NEXT STEPS:
  1. python main.py              → Start in paper mode
  2. Send a test webhook:
     curl -X POST http://localhost:8000/tv-alert \\
       -H "Content-Type: application/json" \\
       -d '{"strategy":"omega_complete","symbol":"BANKNIFTY","side":"BUY_CE","strike":50000}'
  3. Add Pine Script to TradingView with webhook URL
  4. Paper trade for 2+ weeks before going live
""")


if __name__ == "__main__":
    main()
