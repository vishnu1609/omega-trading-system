#!/usr/bin/env python3
"""
Token Leaderboard — reads all session logs and prints ranked savings.
Run directly: python3 ~/.claude/hooks/token_leaderboard.py
"""
import json, os, time
from datetime import datetime

LOG_DIR = os.path.expanduser("~/.claude/token_logs")

def parse_session(path):
    turns, chars_stripped, directive_chars = 0, 0, 0
    first_ts, last_ts = None, None
    intents = {}
    with open(path) as f:
        for line in f:
            try:
                e = json.loads(line)
                turns += 1
                chars_stripped += e.get("chars_stripped", 0)
                directive_chars += e.get("directive_chars", 0)
                ts = e.get("ts", 0)
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts
                d = e.get("directive", "")[:30]
                if d:
                    intents[d] = intents.get(d, 0) + 1
            except Exception:
                continue
    if turns == 0:
        return None
    input_saved = chars_stripped // 4
    output_saved = turns * 45
    total = input_saved + output_saved
    top_intent = max(intents, key=intents.get) if intents else "—"
    date = datetime.fromtimestamp(first_ts).strftime("%b %d %H:%M") if first_ts else "?"
    return {
        "session_id": os.path.basename(path).replace(".jsonl", ""),
        "turns": turns,
        "input_saved": input_saved,
        "output_saved": output_saved,
        "total": total,
        "top_intent": top_intent,
        "date": date,
    }

def leaderboard():
    if not os.path.exists(LOG_DIR):
        print("No session logs found.")
        return

    sessions = []
    for fname in os.listdir(LOG_DIR):
        if not fname.endswith(".jsonl"):
            continue
        try:
            s = parse_session(os.path.join(LOG_DIR, fname))
            if s:
                sessions.append(s)
        except Exception:
            continue

    if not sessions:
        print("No data yet — run some sessions first.")
        return

    sessions.sort(key=lambda x: x["total"], reverse=True)

    total_all = sum(s["total"] for s in sessions)
    total_turns = sum(s["turns"] for s in sessions)

    print("=" * 65)
    print("  OMEGA TOKEN OPTIMIZER — ALL-TIME LEADERBOARD")
    print("=" * 65)
    print(f"  {'#':<3} {'Date':<14} {'Turns':<6} {'Input':<8} {'Output':<9} {'TOTAL':<8} Top Intent")
    print("-" * 65)
    for i, s in enumerate(sessions[:20], 1):
        sid_short = s["session_id"][-8:]
        print(
            f"  {i:<3} {s['date']:<14} {s['turns']:<6} "
            f"~{s['input_saved']:<6} ~{s['output_saved']:<8} "
            f"~{s['total']:<7} {s['top_intent'][:20]}"
        )
    print("=" * 65)
    print(f"  TOTAL ACROSS {len(sessions)} SESSIONS: ~{total_all:,} tokens saved ({total_turns} turns)")
    print("=" * 65)

if __name__ == "__main__":
    leaderboard()
