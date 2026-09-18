"""Real market data + the "can I actually buy this on Robinhood, and is it a
small cap?" gate.

Data via yfinance (any US-listed ticker: Nasdaq, NYSE, NYSE American) with a
Stooq no-key fallback for prices. Eligibility uses yfinance's exchange +
market-cap fields. Runs on your machine (needs internet); it will not run inside
the Claude sandbox.

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


def last_price(ticker: str) -> float | None:
    df = get_daily(ticker, period="5d")
    return float(df["close"].iloc[-1]) if len(df) else None


def info(ticker: str) -> dict:
    try:
        return _yf().Ticker(ticker).info or {}
    except Exception:
        return {}


def eligibility(ticker: str) -> dict:
    """Is this a Robinhood-buyable small cap? Returns a verdict + the facts.

    Robinhood trades US stocks on major exchanges (Nasdaq / NYSE / NYSE American
    / Cboe) — NOT OTC/pink sheets. We only clear names we can verify sit on an
    allowed exchange, are common equity, priced above the floor, and inside the
    market-cap band.
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
    if cap is not None:
        if cap < config.MIN_MARKET_CAP: ok = False; reasons.append(f"market cap ${cap/1e6:.0f}M below floor")
        if cap > config.MAX_MARKET_CAP: ok = False; reasons.append(f"market cap ${cap/1e9:.1f}B above small-cap ceiling")
    else:
        reasons.append("market cap unknown")
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
