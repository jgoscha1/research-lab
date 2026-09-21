"""Daily report + plain-English bullet summary, benchmarked against the market.

Both functions take a list of Portfolio instances — the system runs several
independent portfolios (each up to MAX_OPEN_POSITIONS names, its own cash,
its own since-inception curve), not one, so each batch of picks can be judged
against the market on its own instead of blurring together.
"""
from __future__ import annotations

from . import config


def _pct_vs_start(curve, key=None):
    if len(curve) < 1:
        return None
    if key is None:
        first, last = curve[0]["value"], curve[-1]["value"]
    else:
        pts = [c["benchmarks"].get(key) for c in curve if c["benchmarks"].get(key)]
        if len(pts) < 1:
            return None
        first, last = pts[0], pts[-1]
    if not first:
        return None
    return (last / first - 1) * 100


def _portfolio_lines(pf) -> list[str]:
    s = pf.s
    pos = s["positions"]; closed = s["closed"]; curve = s["curve"]
    invested = pf.invested_total()
    total = curve[-1]["value"] if curve else (s.get("cash", 0) + invested)
    pnl_dollar = total - config.CASH_BUDGET
    invested_ever = pf.invested_ever()
    port_ret = (pnl_dollar / invested_ever * 100) if invested_ever else None
    L = [f"**Portfolio {s['id']}** (started {s.get('created','?')}"
         f"{', closed to new names' if not pf.accepts_new() else ''}): "
         f"{len(pos)}/{config.MAX_OPEN_POSITIONS} open, {len(closed)} closed. "
         f"${invested:,.0f} invested, ${s['cash']:,.0f} cash."]
    if port_ret is not None:
        bench = " · ".join(f"{b} {(_pct_vs_start(curve, b) or 0):+.1f}%" for b in config.BENCHMARKS)
        sign = "+" if pnl_dollar >= 0 else "-"
        L.append(f"{port_ret:+.1f}% on invested capital ({sign}${abs(pnl_dollar):,.0f}) since inception vs {bench} index return.")
    if pos:
        L.append(f"Holding: {', '.join(sorted(pos.keys()))}.")
    if closed:
        wins = sum(1 for c in closed if c["pnl"] >= 0)
        L.append(f"Closed trades: {wins}/{len(closed)} profitable.")
    return L


def summary(portfolios) -> str:
    combined = sum(pf.s.get("cash", 0) + pf.invested_total() for pf in portfolios)
    L = [f"{len(portfolios)} portfolio(s), ${combined:,.0f} combined."]
    for pf in portfolios:
        L.extend(_portfolio_lines(pf))
    L.append("Any Robinhood-buyable US stock, any market cap (major US exchanges, no OTC). "
             "No real orders are placed; this is paper research. "
             "Beating the market is the only real test — give it months.")
    return "\n".join("- " + x for x in L)


def report(portfolios, events) -> str:
    md = ["# Research Lab — daily report\n"]
    md.append(summary(portfolios) + "\n")
    for pf in portfolios:
        s = pf.s
        md.append(f"## Portfolio {s['id']} — open positions")
        if not s["positions"]:
            md.append("_none_")
        for tk, p in s["positions"].items():
            legs = " + ".join(f"${l['amount']:,.0f}@${l['price']}" for l in p["legs"])
            md.append(f"- **{tk}** ({p.get('researcher')}) — invested ${p['cost_basis']:,.0f} "
                      f"[{legs}] · challenged {p['challenges']['total']}× "
                      f"(held {p['challenges']['held']}, added {p['challenges']['added']})")
        if s["closed"]:
            md.append(f"\n## Portfolio {s['id']} — closed positions")
            for c in s["closed"][-10:]:
                md.append(f"- **{c['ticker']}** {c['pnl_pct']:+.1f}% (${c['pnl']:+,.0f}) — {c.get('sell_reason','')}")
    md.append("\n## Today")
    for e in events:
        md.append(f"- {e}")
    return "\n".join(md)
