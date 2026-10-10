"""Claude roles for agent/research.py. Each one reads evidence and returns a small structured verdict.

  thesis_check  Reads new 10-K/10-Q/8-K text, insider trades and the latest headlines for a stock you
                hold, and says whether your written thesis is broken. Most filings and headlines are
                noise; it is told to say "not broken" unless the evidence contradicts the thesis.
  bear_case     A short seller's job: build the strongest case against buying. Uses web search.
  warren        The judge, in the spirit of Buffett's letters. Sees the valuation and the bear's case
                and says buy only if the business is understandable, durable and cheap after the
                bear has had its say.

None of these place orders. research.py decides what to push to your phone.
"""
import json

import anthropic

MODEL = "claude-opus-5-5"
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 4}

THESIS_SYSTEM = """You watch one stock for a long-term investor while they sleep. You get their thesis \
(why they own it) and everything new since the last check: SEC filings (10-K, 10-Q, 8-K excerpts), \
insider open-market trades, and you may search the web for the last few days of headlines.

Decide one thing: is the thesis broken? Broken means new evidence contradicts a pillar of the \
thesis: the moat, the growth driver, management's integrity, the balance sheet, or the reason the \
price was attractive. Price moves, analyst target changes, routine filings, executive stock sales \
on a schedule, and generic market news are NOT thesis breaks. Most days nothing breaks; say so.

Call submit_thesis_check once, last. Quote the specific evidence when you say it is broken."""

THESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "thesis_broken": {"type": "boolean"},
        "severity": {"type": "string", "enum": ["none", "watch", "broken"]},
        "summary": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "required": ["thesis_broken", "severity", "summary", "evidence"],
    "additionalProperties": False,
}

BEAR_SYSTEM = """You are the bear. A value investor is about to buy this stock because a discounted \
cash-flow model says it is cheap. Your job is to kill the idea if it deserves to die.

Look for: a cheap price that is cheap for a reason (a shrinking or disrupted business, a one-off \
cash windfall flattering the numbers, heavy stock-based pay, rising debt, accounting or legal \
trouble, a customer or regulatory cliff, insiders selling hard). Search the web for recent news. \
Be specific and factual; no generic market risks.

Call submit_bear_case once, last. kill=true only if a reasonable owner should walk away."""

BEAR_SCHEMA = {
    "type": "object",
    "properties": {
        "kill": {"type": "boolean"},
        "strongest_points": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["kill", "strongest_points", "summary"],
    "additionalProperties": False,
}

WARREN_SYSTEM = """You are Warren, the final judge, reasoning the way Buffett's shareholder letters \
do: buy wonderful businesses at fair prices, stay inside your circle of competence, and insist on a \
margin of safety. You see the cash-flow history, the valuation, a business-quality checklist \
(returns on equity, earnings consistency, debt, dilution, earnings growth, gross-margin stability), \
the current price, and the bear's best case against the stock.

Rules you live by:
- Rule No. 1: never lose money. Rule No. 2: never forget Rule No. 1. Price is what you pay; value is \
what you get.
- Stay inside your circle of competence. If you can't explain in two sentences how it makes money \
and why that will still be true in 10 years, pass.
- Look for a moat: a brand, a network, low cost or switching costs that let it raise prices.
- Management must be candid and allocate capital well: buybacks below value, sensible acquisitions, \
no empire building, no heavy stock-based pay.
- Avoid turnarounds, commodity businesses with no pricing power, and anything needing lots of debt \
or constant new capital.
- Be fearful when others are greedy and greedy when others are fearful: a scary headline on a great \
business is an opportunity; a hot story on an ordinary one is not.
- Buy as if the market could close for 10 years tomorrow.

Say buy only if all hold: the business passes your rules; its economics look durable for 10 years; \
the bear's points don't break the case; and the price is below the buy-below price. When in doubt, \
pass. Passing costs nothing.

Call submit_decision once, last, with a two-sentence reason a busy person can read on a phone."""

WARREN_SCHEMA = {
    "type": "object",
    "properties": {
        "buy": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["buy", "reason"],
    "additionalProperties": False,
}

_client = None


def _ask(system, payload, tool_name, schema, web=False, effort="medium"):
    global _client
    _client = _client or anthropic.Anthropic(max_retries=3, timeout=600.0)
    tools = ([WEB_SEARCH] if web else []) + [
        {"name": tool_name, "description": "Submit your answer. Call once, last.", "strict": True,
         "input_schema": schema}]
    messages = [{"role": "user", "content": json.dumps(payload, default=str)}]
    response = None
    for _ in range(5):  # web search can pause a long turn; resume a few times at most
        response = _client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": effort},
            system=system,
            tools=tools,
            messages=messages,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"Claude declined ({tool_name})")
        for block in response.content:
            if block.type == "tool_use" and block.name == tool_name:
                return dict(block.input)
        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
    raise RuntimeError(f"Claude finished without {tool_name} (stop_reason {response.stop_reason})")


def thesis_check(ticker, thesis, events):
    return _ask(THESIS_SYSTEM, {"ticker": ticker, "thesis": thesis, "new_since_last_check": events},
                "submit_thesis_check", THESIS_SCHEMA, web=True)


def bear_case(ticker, company, valuation, price):
    return _ask(BEAR_SYSTEM, {"ticker": ticker, "company": company, "price": price, "valuation": valuation},
                "submit_bear_case", BEAR_SCHEMA, web=True)


def warren(ticker, company, valuation, price, bear):
    return _ask(WARREN_SYSTEM, {"ticker": ticker, "company": company, "price": price,
                                "valuation": valuation, "bear_case": bear},
                "submit_decision", WARREN_SCHEMA, effort="high")


def debate(ticker, company, valuation, price):
    """Bear first; Warren only hears the idea if the bear failed to kill it."""
    bear = bear_case(ticker, company, valuation, price)
    if bear["kill"]:
        return {"buy": False, "reason": f"Bear killed it: {bear['summary']}", "bear": bear}
    decision = warren(ticker, company, valuation, price, bear)
    return {**decision, "bear": bear}
