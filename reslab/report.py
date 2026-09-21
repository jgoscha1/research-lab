"""Daily report + plain-English bullet summary, benchmarked against the market."""
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


def summary(pf) -> str:
    s = pf.s
    pos = s["positions"]; closed = s["closed"]; curve = s["curve"]
    invested = pf.invested_total()
    port_ret = _pct_vs_start(curve)
    L = []
    rounds = s.get("rounds", 1)
    L.append(f"{len(pos)}/{pf.max_open()} open position(s) (round {rounds}), {len(closed)} closed. "
             f"${invested:,.0f} invested, ${s['cash']:,.0f} cash.")
    if port_ret is not None:
        bench = " · ".join(f"{b} {(_pct_vs_start(curve, b) or 0):+.1f}%" for b in config.BENCHMARKS)
        L.append(f"Portfolio {port_ret:+.1f}% since inception vs {bench}. "
                 f"Beating the market is the only real test — give it months.")
    if pos:
        names = ", ".join(sorted(pos.keys()))
        L.append(f"Holding: {names}.")
    if closed:
        wins = sum(1 for c in closed if c["pnl"] >= 0)
        L.append(f"Closed trades: {wins}/{len(closed)} profitable.")
    L.append("Any Robinhood-buyable US stock, any market cap (major US exchanges, no OTC). "
             "No real orders are placed; this is paper research.")
    return "\n".join("- " + x for x in L)


def report(pf, events) -> str:
    s = pf.s
    md = ["# Research Lab — daily report\n"]
    md.append(summary(pf) + "\n")
    md.append("## Open positions")
    if not s["positions"]:
        md.append("_none_")
    for tk, p in s["positions"].items():
        legs = " + ".join(f"${l['amount']:,.0f}@${l['price']}" for l in p["legs"])
        md.append(f"- **{tk}** ({p.get('researcher')}) — invested ${p['cost_basis']:,.0f} "
                  f"[{legs}] · challenged {p['challenges']['total']}× "
                  f"(held {p['challenges']['held']}, added {p['challenges']['added']})")
    md.append("\n## Today")
    for e in events:
        md.append(f"- {e}")
    md.append("\n## Closed positions")
    for c in s["closed"][-10:]:
        md.append(f"- **{c['ticker']}** {c['pnl_pct']:+.1f}% (${c['pnl']:+,.0f}) — {c.get('sell_reason','')}")
    return "\n".join(md)
