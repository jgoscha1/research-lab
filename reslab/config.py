"""Configuration — everything tunable in one place, overridable by environment.

Copy .env.example to .env (or set these in your shell / systemd) and edit.
"""
from __future__ import annotations

import os


def _f(name, default): 
    try: return float(os.environ.get(name, default))
    except Exception: return float(default)

def _i(name, default):
    try: return int(os.environ.get(name, default))
    except Exception: return int(default)


# --- LLM (the researchers + judges run on your Anthropic API key) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Set to a current model. Capable models suit research; a smaller one is cheaper.
MODEL = os.environ.get("RESLAB_MODEL", "claude-opus-4-8")
# Web-search tool version. 20250305 is the broadly-available stable one.
WEB_SEARCH_TOOL = os.environ.get("RESLAB_WEB_SEARCH_TOOL", "web_search_20250305")
MAX_SEARCHES = _i("RESLAB_MAX_SEARCHES", 6)

# --- Universe: any Robinhood-buyable US stock ---
# Robinhood trades US stocks listed on major exchanges (Nasdaq, NYSE, NYSE
# American/AMEX, Cboe). It does NOT trade OTC / pink-sheet stocks. We only buy
# names we can verify are on an allowed exchange — no market-cap band.
ALLOWED_EXCHANGES = set(e.strip() for e in os.environ.get(
    "RESLAB_ALLOWED_EXCHANGES",
    # yfinance exchange codes: Nasdaq (NMS/NGM/NCM), NYSE (NYQ),
    # NYSE American/AMEX (ASE), Cboe (BATS/PCX)
    "NMS,NGM,NCM,NYQ,ASE,BATS,PCX").split(","))
MIN_PRICE = _f("RESLAB_MIN_PRICE", 2.0)              # avoid sub-$2 / penny names
REQUIRE_VERIFIED_EXCHANGE = os.environ.get("RESLAB_REQUIRE_VERIFIED", "1") == "1"

# --- Paper portfolio + sizing ---
CASH_BUDGET = _f("RESLAB_CASH_BUDGET", 100000.0)     # paper dollars per run's world
INITIAL_POSITION = _f("RESLAB_INITIAL_POSITION", 1000.0)
ADD_SIZE = _f("RESLAB_ADD_SIZE", 600.0)
MAX_POSITION = _f("RESLAB_MAX_POSITION", 3000.0)     # hard cap per name
MAX_OPEN_POSITIONS = _i("RESLAB_MAX_OPEN", 12)
NEW_IDEAS_PER_DAY = _i("RESLAB_NEW_IDEAS_PER_DAY", 2)  # per researcher

# --- Benchmarks (what "beating the market" means) ---
BENCHMARKS = [b.strip() for b in os.environ.get("RESLAB_BENCHMARKS", "SPY,IWM").split(",")]

# --- The two researchers (each with its own judge persona) ---
RESEARCHERS = [
    {"name": "Ada",   "judge": "Judge Vera",
     "style": "contrarian value: discounted stocks with real revenue, backlog or a policy moat; "
              "avoid pre-revenue story stocks; demand a reason the discount is temporary."},
    {"name": "Boone", "judge": "Judge Cole",
     "style": "high-conviction momentum: stocks with real catalysts and revenue riding a strong theme; "
              "you may pay up for growth but never for pure story; size for volatility."},
]

# --- Paths ---
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.environ.get("RESLAB_STATE_DIR", os.path.join(HERE, "state"))

# --- Pricing, for the AI-cost meter ($ per 1M tokens). Source: Anthropic's
# published API pricing (platform.claude.com/docs/en/about-claude/pricing) as
# of 2026-09-20 — update here if rates change. Unrecognized models fall back
# to a same-family estimate rather than $0.
MODEL_PRICING = {
    "claude-opus-5":       (5.00, 25.00),
    "claude-opus-4-8":     (5.00, 25.00),
    "claude-opus-4-7":     (5.00, 25.00),
    "claude-opus-4-6":     (5.00, 25.00),
    "claude-opus-4-5":     (5.00, 25.00),
    "claude-sonnet-5":     (2.00, 10.00),
    "claude-sonnet-4-6":   (3.00, 15.00),
    "claude-sonnet-4-5":   (3.00, 15.00),
    "claude-haiku-4-5":    (1.00, 5.00),
}
WEB_SEARCH_COST_PER_SEARCH = 0.01  # $10 / 1,000 searches


def price_for_model(model_id: str) -> tuple[float, float]:
    """(input $/MTok, output $/MTok) for a model id, with a same-family fallback."""
    if model_id in MODEL_PRICING:
        return MODEL_PRICING[model_id]
    m = model_id.lower()
    if "opus" in m:
        return MODEL_PRICING["claude-opus-5"]
    if "haiku" in m:
        return MODEL_PRICING["claude-haiku-4-5"]
    return MODEL_PRICING["claude-sonnet-4-6"]  # sonnet or unknown
