"""Hourly Claude review for agent/live.py.

Claude reads a summary of the market and the account and answers with a small JSON verdict:
whether to take new trades at all, and which symbols may be bought. It can only narrow what the
trading loop does; it never places orders, and symbols it names outside the universe are dropped.
"""
import json

import anthropic

MODEL = "claude-opus-5-5"

SYSTEM = """You review an automated momentum trader once an hour. The trader holds at most two \
positions in crypto and US stocks, with hard stop-losses enforced in code. You decide two things \
for the next hour:

- risk: "on" to allow new buys, "off" to block them (existing positions keep their stops).
- allow: the symbols that may be bought, chosen from the summary. Prefer clean uptrends with tight \
spreads; drop symbols that are spiking straight up (late entries), chopping sideways, or whose \
spread would eat a small gain.

Turn risk off when most symbols are falling together, when the account is in a drawdown, or when \
nothing has a clean trend. Holding cash is a good outcome. Keep the note to one sentence."""

SCHEMA = {
    "type": "object",
    "properties": {
        "risk": {"type": "string", "enum": ["on", "off"]},
        "allow": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"},
    },
    "required": ["risk", "allow", "note"],
    "additionalProperties": False,
}

_client = None


def review(summary, universe):
    global _client
    _client = _client or anthropic.Anthropic(max_retries=3, timeout=120.0)
    response = _client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(summary)}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined the review")
    text = next(b.text for b in response.content if b.type == "text")
    verdict = json.loads(text)
    verdict["allow"] = [s for s in verdict["allow"] if s in universe]
    return verdict
