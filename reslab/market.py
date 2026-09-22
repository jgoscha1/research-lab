"""Real market data + the "can I actually buy this on Robinhood?" gate.

Data via yfinance (any US-listed ticker: Nasdaq, NYSE, NYSE American) with a
Stooq no-key fallback for prices. Eligibility uses yfinance's exchange field —
any market cap is eligible. Runs on your machine (needs internet); it will not
run inside the Claude sandbox.

Swap in a paid provider later by reimplementing get_daily() / get_quote() —
nothing else changes.
"""
from __future__ import annotations

import io
import time

import pandas as pd

from . import config


def _yf():
    import yfinance as yf  # lazy; only needed at runtime on your box
    return yf


def get_daily(ticker: str, period: str = "13mo") -> pd.DataFrame:
    """Daily OHLCV history. Returns a date-indexed frame with a 'close' column."""
    try:
        yf = _yf()
        df = yf.download(ticker, period=period, interval="1d",
                         auto_adjust=True, progress=False)
        if len(df):
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.rename(columns=str.lower)
            df.index.name = "date"
            return df[["close"]].dropna()
    except Exception:
        pass
    # Stooq fallback (no key)
    try:
        import urllib.request
        url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
        raw = urllib.request.urlopen(url, timeout=30).read().decode()
        df = pd.read_csv(io.StringIO(raw))
        df.columns = [c.strip().lower() for c in df.columns]
        if "close" in df and "date" in df:
            df["date"] = pd.to_datetime(df["date"])
            return df.set_index("date")[["close"]].dropna()
    except Exception:
        pass
    return pd.DataFrame(columns=["close"])


def _live_quote(ticker: str) -> float | None:
    """A live/near-live quote, when yfinance has one — this can be far
    fresher than the daily-bar download, which has been observed to lag
    the actual trading session by a day or more (e.g. Friday's close still
    showing up as "current" on Monday evening)."""
    try:
        fi = _yf().Ticker(ticker).fast_info
        p = fi.get("lastPrice")
        if p:
            return float(p)
    except Exception:
        pass
    try:
        info = _yf().Ticker(ticker).info or {}
        p = info.get("currentPrice") or info.get("regularMarketPrice")
        if p:
            return float(p)
    except Exception:
        pass
    return None


def last_price(ticker: str) -> float | None:
    p = _live_quote(ticker)
    if p is not None:
        return p
    df = get_daily(ticker, period="5d")
    return float(df["close"].iloc[-1]) if len(df) else None


def info(ticker: str) -> dict:
    try:
        return _yf().Ticker(ticker).info or {}
    except Exception:
        return {}


def eligibility(ticker: str) -> dict:
    """Is this a Robinhood-buyable stock? Returns a verdict + the facts.

    Robinhood trades US stocks on major exchanges (Nasdaq / NYSE / NYSE American
    / Cboe) — NOT OTC/pink sheets. We only clear names we can verify sit on an
    allowed exchange, are common equity, and are priced above the penny-stock
    floor. No market-cap band — any size is eligible.
    """
    d = info(ticker)
    exch = d.get("exchange")
    qtype = (d.get("quoteType") or "").upper()
    cap = d.get("marketCap")
    price = d.get("currentPrice") or d.get("regularMarketPrice") or last_price(ticker)
    reasons = []
    ok = True

    if config.REQUIRE_VERIFIED_EXCHANGE:
        if exch is None:
            ok = False; reasons.append("exchange unverified")
        elif exch not in config.ALLOWED_EXCHANGES:
            ok = False; reasons.append(f"exchange {exch} not Robinhood-eligible (OTC/other)")
    if qtype and qtype not in ("EQUITY", "ETF"):
        ok = False; reasons.append(f"not common equity ({qtype})")
    if price is not None and price < config.MIN_PRICE:
        ok = False; reasons.append(f"price ${price:.2f} below floor")

    return {"ticker": ticker.upper(), "tradeable": ok, "exchange": exch,
            "market_cap": cap, "price": price, "reasons": reasons}


def benchmark_levels() -> dict:
    """Latest close for each benchmark (e.g. SPY, IWM) for relative tracking."""
    out = {}
    for b in config.BENCHMARKS:
        p = last_price(b)
        if p is not None:
            out[b] = p
    return out
