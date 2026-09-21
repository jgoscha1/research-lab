"""The researchers and judges — LLM calls with live web search.

Each call asks Claude (with the web_search tool) to do real research and return
structured JSON. Runs on YOUR Anthropic API key. If the key or the anthropic
package is missing, functions return a clearly-marked offline stub so the rest
of the pipeline still runs for testing.

Honesty rails baked into the prompts: focus only on Robinhood-buyable US
stocks (major exchanges, no OTC); reject pre-revenue story stocks and ideas
that are already priced in; the Judge cannot be argued past the evidence.

Every call's token + web-search usage is priced (config.MODEL_PRICING) and
recorded via store.add_cost() so the dashboard can show what research is
actually costing.
"""
from __future__ import annotations

import json

from . import config, store


def _client():
    if not config.ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    except Exception:
        return None


def _cost(usage) -> float:
    """USD cost of one response, from its usage block (tokens + web searches)."""
    if usage is None:
        return 0.0
    in_rate, out_rate = config.price_for_model(config.MODEL)
    cost = (getattr(usage, "input_tokens", 0) or 0) / 1e6 * in_rate
    cost += (getattr(usage, "output_tokens", 0) or 0) / 1e6 * out_rate
    cost += (getattr(usage, "cache_read_input_tokens", 0) or 0) / 1e6 * in_rate * 0.1
    cost += (getattr(usage, "cache_creation_input_tokens", 0) or 0) / 1e6 * in_rate * 1.25
    stu = getattr(usage, "server_tool_use", None)
    searches = getattr(stu, "web_search_requests", 0) if stu else 0
    cost += (searches or 0) * config.WEB_SEARCH_COST_PER_SEARCH
    return cost


def _ask(prompt: str, max_tokens: int = 1500) -> str | None:
    """One web-search-enabled call; returns the concatenated final text."""
    client = _client()
    if client is None:
        return None
    try:
        msg = client.messages.create(
            model=config.MODEL, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            tools=[{"type": config.WEB_SEARCH_TOOL, "name": "web_search",
                    "max_uses": config.MAX_SEARCHES}],
        )
        store.add_cost(_cost(getattr(msg, "usage", None)))
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    except Exception as e:
        print(f"[llm] call failed: {e}")
        return None


def _json(text: str | None):
    if not text:
        return None
    t = text.strip().replace("```json", "").replace("```", "").strip()
    # grab the first {...} or [...] block
    for open_c, close_c in (("{", "}"), ("[", "[")):
        i, j = t.find(open_c), t.rfind(close_c if close_c != "[" else "]")
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    return None


# --------------------------------------------------------------------------- #
RESEARCH_PROMPT = """You are {name}, a buy-side equity researcher. Your style: {style}

Using web search for current information, identify ONE fresh macro or industry \
trend, then find 1 to 3 specific US-listed stocks that would genuinely benefit \
from it — any market cap, from small-cap to mega-cap. Hard constraints on every pick:
- It must be buyable on Robinhood: listed on a MAJOR US exchange (Nasdaq, NYSE, \
or NYSE American). NO OTC / pink-sheet stocks.
- It must have real revenue, backlog, contracts, or a concrete policy/structural \
moat tied to the trend — NOT a pre-revenue story stock.
- Only include a pick you can honestly defend as not already fully priced in.
- Do not recommend any of these already-seen names: {avoid}.

Respond ONLY as JSON:
{{"trend":"one clear sentence naming the trend and why it matters now",
"picks":[
 {{"ticker":"...","name":"...","thesis":"2-3 sentence case tying this company to the trend",
  "valuation":"cheap|fair|rich + one line why","catalyst":"what could re-rate it",
  "risks":"the main risks","conviction":"low|medium|high"}}
]}}"""

STOCK_IN_TREND_PROMPT = """You are {name}, a buy-side equity researcher. Your style: {style}

The owner wants your honest take on one specific stock as a play on this trend: \
"{trend}"

Stock: {ticker}

Using web search for current, real information on {ticker}: does the trend \
driver actually apply to this company, is there real revenue/backlog/moat tied \
to it, and is the valuation defensible? If the link is weak or the fundamentals \
don't support it, say so plainly rather than forcing a bull case.

Respond ONLY as JSON:
{{"ticker":"{ticker}","name":"company name","thesis":"2-3 sentence case, or why it \
doesn't fit the trend","valuation":"cheap|fair|rich + one line why",
"catalyst":"what could re-rate it (or 'none' if the case is weak)",
"risks":"the main risks","conviction":"low|medium|high"}}"""

JUDGE_NEW_PROMPT = """You are {judge}, a rigorous, skeptical gatekeeper who cannot be \
argued past the evidence. Researcher {name} proposes this stock as a beneficiary of \
a trend they identified:
{rec}

Using web search to verify, interrogate it: does it genuinely tie to the trend, is \
the thesis sound, is it ALREADY priced in, is there real revenue/evidence (reject \
pure story stocks), and is the valuation defensible? Decide honestly.

Respond ONLY as JSON:
{{"decision":"accept|watch|reject","priced_in":true|false,
"reasoning":"2-4 sentences, first person, citing what you checked",
"position_note":"sizing / what to watch if accepted"}}"""

JUDGE_HOLD_PROMPT = """You are {judge}. You hold {ticker} ({name}) in the paper \
portfolio. Cost basis ${cost:.0f}, current value ${value:.0f} ({pnl:+.1f}%). \
Original thesis: {thesis}

Using web search for any news since, decide today's action. Add only if the \
thesis strengthened AND it isn't already stretched; sell the WHOLE position if \
the thesis broke or it now lags a simple index; trim PART of it if you'd rather \
lock in some gains or cut risk without fully exiting (set "trim_pct" to the \
percent of current shares to sell, e.g. 25, 33, 50); otherwise hold.

Respond ONLY as JSON:
{{"action":"hold|add|trim|sell","trim_pct":0,
"reasoning":"2-3 sentences, first person, what changed"}}"""


def research(researcher: dict, avoid: list[str], trend_focus: str | None = None) -> dict:
    """Find a trend and 1-3 stocks that benefit from it.

    Returns {"trend": str, "picks": [rec, ...]}; picks is empty (with
    "_offline": True) if the API key/package is missing or the call failed.
    trend_focus, when given, steers the researcher toward that specific trend
    instead of letting them pick their own (used for owner-suggested trends).
    """
    style = researcher["style"]
    if trend_focus:
        style += f" Focus specifically on this trend the owner flagged: {trend_focus}"
    txt = _ask(RESEARCH_PROMPT.format(
        name=researcher["name"], style=style, avoid=", ".join(avoid) or "none"))
    data = _json(txt)
    picks = (data or {}).get("picks") or []
    if not picks:
        return {"trend": trend_focus or (data or {}).get("trend", ""), "picks": [], "_offline": True}
    for p in picks:
        p["by"] = researcher["name"]
    return {"trend": data.get("trend") or trend_focus or "", "picks": picks}


def research_stock(researcher: dict, ticker: str, trend: str) -> dict | None:
    """Build (or debunk) the case for one owner-suggested stock under a given trend."""
    txt = _ask(STOCK_IN_TREND_PROMPT.format(
        name=researcher["name"], style=researcher["style"],
        ticker=ticker.upper(), trend=trend))
    rec = _json(txt)
    if rec is None:
        return None
    rec["by"] = researcher["name"]
    return rec


def judge_new(researcher: dict, rec: dict, trend: str | None = None) -> dict:
    payload = {k: rec.get(k) for k in ("ticker", "name", "thesis", "valuation", "catalyst", "risks")}
    if trend:
        payload["trend"] = trend
    txt = _ask(JUDGE_NEW_PROMPT.format(
        judge=researcher["judge"], name=researcher["name"], rec=json.dumps(payload)))
    v = _json(txt)
    if v is None:
        return {"decision": "reject", "priced_in": None,
                "reasoning": "Offline — set ANTHROPIC_API_KEY to enable the Judge.",
                "position_note": ""}
    return v


def judge_hold(researcher: dict, pos: dict, cur_value: float, pnl_pct: float) -> dict:
    txt = _ask(JUDGE_HOLD_PROMPT.format(
        judge=researcher["judge"], ticker=pos["ticker"], name=pos.get("name", ""),
        cost=pos["cost_basis"], value=cur_value, pnl=pnl_pct, thesis=pos.get("thesis", "")))
    v = _json(txt)
    if v is None:
        return {"action": "hold", "reasoning": "Offline — defaulting to hold."}
    return v
