"""The trend registry — every macro/industry trend a researcher has looked at,
and each stock investigated within it (bought, watched, rejected, or ruled
ineligible). Lets the owner browse past research by theme instead of by
ticker, and re-target a trend later (find more stocks in it, or point the
researcher at a specific name). Shared by dashboard.py and daily_job.py so
both write into the same list in state.
"""
from __future__ import annotations

from . import config


def find(state: dict, researcher: str, text: str) -> dict | None:
    key = text.strip().lower()
    for t in state.get("trends", []):
        if t["researcher"] == researcher and t["text"].strip().lower() == key:
            return t
    return None


def get_by_id(state: dict, trend_id: int) -> dict | None:
    for t in state.get("trends", []):
        if t["id"] == trend_id:
            return t
    return None


def get_or_create(state: dict, researcher: str, text: str, origin: str) -> dict:
    """origin is "owner" (typed into Suggest a trend) or "auto" (the researcher's own idea)."""
    t = find(state, researcher, text)
    if t:
        return t
    trends = state.setdefault("trends", [])
    t = {"id": (trends[-1]["id"] + 1) if trends else 1, "text": text,
         "researcher": researcher, "origin": origin, "created": config.today_str(),
         "stocks": []}
    trends.append(t)
    return t


def record_stock(trend: dict, rec: dict, status: str, reasoning: str = ""):
    """status is "bought" | "watch" | "rejected" | "not_tradeable"."""
    tk = (rec.get("ticker") or "").upper()
    if not tk:
        return
    for s in trend["stocks"]:
        if s["tk"] == tk:
            s.update(status=status, reasoning=reasoning, thesis=rec.get("thesis", ""),
                      conviction=rec.get("conviction", ""), date=config.today_str())
            return
    trend["stocks"].append({
        "tk": tk, "name": rec.get("name", ""), "thesis": rec.get("thesis", ""),
        "conviction": rec.get("conviction", ""), "status": status,
        "reasoning": reasoning, "date": config.today_str(),
    })


def seen_tickers(trend: dict) -> list[str]:
    return [s["tk"] for s in trend["stocks"]]
