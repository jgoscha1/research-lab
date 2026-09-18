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

# --- Universe: Robinhood-buyable small caps only ---
# Robinhood trades US stocks listed on major exchanges (Nasdaq, NYSE, NYSE
# American/AMEX, Cboe). It does NOT trade OTC / pink-sheet stocks. We only buy
# names we can verify are on an allowed exchange and inside the cap band.
ALLOWED_EXCHANGES = set(e.strip() for e in os.environ.get(
    "RESLAB_ALLOWED_EXCHANGES",
    # yfinance exchange codes: Nasdaq (NMS/NGM/NCM), NYSE (NYQ),
    # NYSE American/AMEX (ASE), Cboe (BATS/PCX)
    "NMS,NGM,NCM,NYQ,ASE,BATS,PCX").split(","))
MIN_MARKET_CAP = _f("RESLAB_MIN_MARKET_CAP", 3e8)    # $300M floor (avoid nano/illiquid)
MAX_MARKET_CAP = _f("RESLAB_MAX_MARKET_CAP", 5e9)    # $5B ceiling (small / small-mid)
MIN_PRICE = _f("RESLAB_MIN_PRICE", 2.0)              # avoid sub-$2 / penny names
REQUIRE_VERIFIED_EXCHANGE = os.environ.get("RESLAB_REQUIRE_VERIFIED", "1") == "1"

# --- Paper portfolio + sizing ---
CASH_BUDGET = _f("RESLAB_CASH_BUDGET", 20000.0)      # paper dollars per run's world
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
     "style": "contrarian value: discounted small-caps with real revenue, backlog or a policy moat; "
              "avoid pre-revenue story stocks; demand a reason the discount is temporary."},
    {"name": "Boone", "judge": "Judge Cole",
     "style": "high-conviction momentum: small-caps with real catalysts and revenue riding a strong theme; "
              "you may pay up for growth but never for pure story; size for volatility."},
]

# --- Paths ---
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.environ.get("RESLAB_STATE_DIR", os.path.join(HERE, "state"))
