#!/usr/bin/env python3
"""
Token Summary Hook — Stop event.
Reads the session log and prints savings to the UI.
"""
import json, sys, os, time

LOG_DIR = os.path.expanduser("~/.claude/token_logs")

def summarize(session_id):
    path = os.path.join(LOG_DIR, f"{session_id}.jsonl")
    if not os.path.exists(path):
        return None

    turns, chars_stripped, directive_chars = 0, 0, 0
    intents = {}

    with open(path) as f:
        for line in f:
            try:
                e = json.loads(line)
                turns += 1
                chars_stripped += e.get("chars_stripped", 0)
                directive_chars += e.get("directive_chars", 0)
                d = e.get("directive", "")
                if d:
                    key = d[:30]
                    intents[key] = intents.get(key, 0) + 1
            except Exception:
                continue

    if turns == 0:
        return None

    # ~4 chars per token estimate
    input_tokens_saved = chars_stripped // 4
    # directive adds tokens, but forces concise replies — estimate 30% output saving
    # avg output per turn ~150 tokens, 30% saving = 45 tokens saved per turn
    est_output_saved = turns * 45
    total_est = input_tokens_saved + est_output_saved

    top_intent = max(intents, key=intents.get) if intents else "—"

    lines = [
        f"📊 Token Optimizer — Session Summary",
        f"  Turns: {turns}",
        f"  Input chars stripped: {chars_stripped} (~{input_tokens_saved} tokens)",
        f"  Est. output tokens saved: {est_output_saved} (30% reduction × {turns} turns)",
        f"  Total est. saved: ~{total_est} tokens",
        f"  Most used directive: \"{top_intent}...\"",
    ]
    return "\n".join(lines)

def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    session_id = data.get('session_id', 'unknown')
    summary = summarize(session_id)

    if summary:
        print(json.dumps({"systemMessage": summary}))

if __name__ == '__main__':
    main()
