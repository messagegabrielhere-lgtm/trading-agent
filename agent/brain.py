"""Hourly Claude review for agent/live.py.

Claude reads a summary of the market and the account, checks the news and social sentiment for the
candidates with web search, and submits a small verdict: whether to take new trades at all, and
which symbols may be bought. It can only narrow what the trading loop does; it never places orders,
and symbols it names outside the universe are dropped.
"""
import json

import anthropic

MODEL = "claude-opus-5-5"

SYSTEM = """You review an automated momentum trader once an hour. It holds at most two positions in \
crypto and US stocks, with stop-losses enforced in code. You decide two things for the next hour:

- risk: "on" to allow new buys, "off" to block them (existing positions keep their stops).
- allow: the symbols that may be bought, chosen from the summary.

How to decide:
- Start from the numbers: prefer clean uptrends with tight spreads; drop symbols spiking straight \
up (late entries), chopping sideways, or whose spread would eat a small gain.
- Then check the news and social sentiment with web search, briefly (a search or two, aimed at the \
symbols that look tradable). Drop a symbol with fresh bad news (hack, exploit, delisting, lawsuit, \
offering, guidance cut, exchange trouble) or a sentiment spike that looks like a pump. A big \
scheduled event in the next few hours (Fed decision, CPI, token unlock, earnings) is a reason to \
wait.
- Timing: hour_utc is the current hour. During thin_hours_utc liquidity is low and moves fade more \
often, so be stricter then. Weekends are thinner for crypto too.
- Turn risk off when most symbols fall together, when the account is in a drawdown, or when nothing \
has a clean trend. Holding cash is a good outcome.

Finish by calling submit_verdict once. Keep the note to one sentence, naming any news you acted on."""

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

TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 3},
    {"name": "submit_verdict", "description": "Submit the verdict for the next hour. Call once, last.",
     "strict": True, "input_schema": SCHEMA},
]

_client = None


def review(summary, universe):
    global _client
    _client = _client or anthropic.Anthropic(max_retries=3, timeout=300.0)
    messages = [{"role": "user", "content": json.dumps(summary)}]
    for _ in range(4):  # web search can pause a long turn; resume it a few times at most
        response = _client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined the review")
        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_verdict":
                verdict = dict(block.input)
                verdict["allow"] = [s for s in verdict["allow"] if s in universe]
                return verdict
        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
    raise RuntimeError(f"Claude finished without a verdict (stop_reason {response.stop_reason})")
