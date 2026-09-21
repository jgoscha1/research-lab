"""daily_job.py — the once-a-day cron entry point.

Each run:
  1. Back up state.
  2. Mark holdings with fresh prices; each Judge reviews hold / add / sell,
     for every portfolio (a researcher's holdings can span several).
  3. Each researcher hunts new Robinhood-buyable stocks of any size, one
     trend + up to 3 picks per call; each survivor is gated (real exchange)
     and interrogated by its Judge; accepted names are bought into whichever
     portfolio is still accepting new names — starting a brand-new portfolio
     (its own cash, its own since-inception curve) once the current one fills
     up, so each batch of picks can be judged against the market on its own.
  4. Record every portfolio's value vs benchmarks; write report + summary;
     save.

Runs on YOUR machine with YOUR Anthropic API key (for research) and internet
(for prices). No real orders are placed.

    30 17 * * 1-5   cd /path/to/research-lab && python daily_job.py
"""
from __future__ import annotations

import os
import sys
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reslab import config, market, llm, store, report, trends, portfolio


def run():
    store.backup()
    state = store.load()
    pfs = portfolio.load_portfolios(state)
    state.setdefault("researchers", {})
    events = []

    # ---- prices for holdings + benchmarks ----
    prices = {}
    for tk in portfolio.all_held_tickers(state):
        p = market.last_price(tk)
        if p:
            prices[tk] = p
    benchmarks = market.benchmark_levels()

    # ---- Judge reviews each holding, across every portfolio ----
    for r in config.RESEARCHERS:
        for pf in pfs:
            for tk in [t for t, p in pf.s["positions"].items() if p["researcher"] == r["name"]]:
                pos = pf.position(tk); price = prices.get(tk)
                v = pf.value_position(tk, price)
                if v is None:
                    continue
                d = llm.judge_hold(r, pos, v["value"], v["pnl_pct"])
                pos["challenges"]["total"] += 1
                pos["last_challenge"] = {"date": _today(), "action": d["action"], "reasoning": d.get("reasoning", "")}
                if d["action"] == "sell":
                    rec = pf.sell(tk, price, d.get("reasoning", "Judge: sell"))
                    if rec: events.append(f"SOLD {tk} (portfolio {pf.s['id']}) {rec['pnl_pct']:+.1f}% (${rec['pnl']:+,.0f}) — {rec['sell_reason']}")
                elif d["action"] == "add" and pf.can_add(tk, config.ADD_SIZE):
                    if pf.buy(tk, pos["name"], r["name"], pos["thesis"], price, config.ADD_SIZE):
                        pos["challenges"]["added"] += 1
                        events.append(f"ADDED ${config.ADD_SIZE:,.0f} to {tk} (portfolio {pf.s['id']}) — {d.get('reasoning','')}")
                else:
                    pos["challenges"]["held"] += 1

    # ---- researchers hunt new ideas: each call finds a trend + up to 3 stocks ----
    status_map = {"reject": "rejected", "watch": "watch"}
    for r in config.RESEARCHERS:
        rstate = state["researchers"].setdefault(r["name"], {"proposed": []})
        avoid = portfolio.all_held_tickers(state) + rstate["proposed"][-40:]
        for _ in range(config.NEW_IDEAS_PER_DAY):
            result = llm.research(r, avoid)
            if result.get("_offline"):
                events.append(f"{r['name']}: live research unavailable (set ANTHROPIC_API_KEY).")
                break
            trend_text = result["trend"]
            trend = trends.get_or_create(state, r["name"], trend_text, origin="auto")
            events.append(f"{r['name']} trend: {trend_text}")
            for rec in result["picks"][:3]:
                tk = (rec.get("ticker") or "").upper()
                if not tk or tk in avoid:
                    continue
                rstate["proposed"].append(tk); avoid.append(tk)
                elig = market.eligibility(tk)
                if not elig["tradeable"]:
                    events.append(f"{r['name']} → {tk}: skipped (not Robinhood-buyable: {', '.join(elig['reasons'])}).")
                    trends.record_stock(trend, rec, "not_tradeable")
                    continue
                verdict = llm.judge_new(r, rec, trend=trend_text)
                if verdict["decision"] != "accept":
                    events.append(f"{r['name']} → {tk}: {verdict['decision']} — {r['judge']}: {verdict.get('reasoning','')[:160]}")
                    trends.record_stock(trend, rec, status_map.get(verdict["decision"], verdict["decision"]), verdict.get("reasoning", ""))
                else:
                    pf = portfolio.active_portfolio(state)
                    was_new = pf.open_count() == 0
                    price = elig["price"] or market.last_price(tk)
                    if price and pf.buy(tk, rec.get("name", tk), r["name"], rec.get("thesis", ""), price, config.INITIAL_POSITION):
                        if was_new:
                            events.append(f"Starting portfolio {pf.s['id']} with ${config.CASH_BUDGET:,.0f}.")
                        p = pf.position(tk); p["verdict"] = verdict; p["trend"] = trend_text
                        events.append(f"BOUGHT {tk} @ ${price:.2f} (${config.INITIAL_POSITION:,.0f}, portfolio {pf.s['id']}) — {r['judge']}: {verdict.get('reasoning','')[:160]}")
                        trends.record_stock(trend, rec, "bought", verdict.get("reasoning", ""))
                    else:
                        events.append(f"{r['name']} → {tk}: accepted, but no price available — not bought.")
                        trends.record_stock(trend, rec, "watch", verdict.get("reasoning", ""))

    # ---- mark every portfolio, report, save ----
    for tk in portfolio.all_held_tickers(state):
        if tk not in prices:
            p = market.last_price(tk)
            if p: prices[tk] = p
    pfs = portfolio.load_portfolios(state)  # pick up any portfolio created above
    combined_total = 0.0
    for pf in pfs:
        combined_total += pf.mark(prices, benchmarks)
    store.save(state)

    md = report.report(pfs, events)
    summ = report.summary(pfs)
    _write("daily_report.md", "# Research Lab — daily report\n\n" + md)
    _write("summary.md", "# Research Lab — summary\n\n" + summ + "\n")
    print(summ)
    print(f"\n[daily job] {len(pfs)} portfolio(s), ${combined_total:,.0f} combined · state saved · report written")


def _today():
    from datetime import date
    return str(date.today())


def _write(name, text):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), "w") as f:
        f.write(text)


if __name__ == "__main__":
    run()
