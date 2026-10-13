#!/usr/bin/env python3
"""
Token Optimizer Hook — UserPromptSubmit
Intent-aware directive injection + per-session savings log.
"""
import json, sys, re, os, time

LOG_DIR = os.path.expanduser("~/.claude/token_logs")
os.makedirs(LOG_DIR, exist_ok=True)

FILLERS = [
    r'(?i)\bplease\b[\s,]*',
    r'(?i)\bkindly\b[\s,]*',
    r'(?i)\bcan you\s+',
    r'(?i)\bcould you\s+',
    r'(?i)\bi would like you to\s+',
    r'(?i)\bi want you to\s+',
    r'(?i)\bi need you to\s+',
    r'(?i)\bwould you please\s+',
    r'(?i)\bdo you think you could\s+',
    r'(?i)\bthe following\b\s*:',
    r'(?i)\bjust\s+',
    r'(?i)\bbasically\s+',
    r'(?i)\bactually\s+',
    r'(?i)\bsimply\s+',
]

INTENT_RULES = [
    (r'(?i)^(is|are|does|do|can|will|should|has|have|was|were)\b',
     "Answer in one sentence or less."),
    (r'(?i)\b(fix|bug|error|crash|broken|issue|problem|failing|fail)\b',
     "Show only changed lines + one-line reason. No preamble."),
    (r'(?i)\b(write|create|build|make|implement|add|generate)\b.{0,40}\b(function|class|code|script|method|api|route|component)\b',
     "Code only. Inline comments for non-obvious parts only. No prose."),
    (r'(?i)^(explain|how|what|why|when|where)\b',
     "Max 3 bullet points. Code example if it clarifies. No filler."),
    (r'(?i)\b(refactor|improve|optimis|optimiz|clean|simplif)\b',
     "Show diff or changed code only. One-line justification max."),
    (r'(?i)^(list|show|give me|tell me)\b',
     "Bullet list only. No intro sentence."),
]

FALLBACK = "Dense answer only. No preamble or trailing summary."

def compress(text):
    original = len(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    for p in FILLERS:
        text = re.sub(p, '', text)
    return text.strip(), original - len(text.strip())

def detect_directive(prompt):
    if len(prompt) < 12:
        return None
    for pattern, directive in INTENT_RULES:
        if re.search(pattern, prompt.strip()):
            return directive
    return FALLBACK

def log_turn(session_id, chars_stripped, directive):
    path = os.path.join(LOG_DIR, f"{session_id}.jsonl")
    entry = {
        "ts": time.time(),
        "chars_stripped": chars_stripped,
        "directive_chars": len(directive) if directive else 0,
        "directive": directive or "",
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")

def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    prompt = data.get('prompt', '')
    session_id = data.get('session_id', 'unknown')
    compressed, saved = compress(prompt)
    directive = detect_directive(compressed)

    log_turn(session_id, saved, directive)

    if saved > 0:
        sys.stderr.write(f"[TokenOpt] -{saved} chars stripped\n")

    if directive is None:
        sys.exit(0)

    sys.stderr.write(f"[TokenOpt] → {directive[:50]}\n")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": directive
        }
    }))

if __name__ == '__main__':
    main()
