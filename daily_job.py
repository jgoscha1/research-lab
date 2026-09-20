"""daily_job.py — the once-a-day cron entry point.

Each run:
  1. Back up state.
  2. Mark holdings with fresh prices; each Judge reviews hold / add / sell.
  3. Each researcher hunts new Robinhood-buyable stocks of any size; each
     survivor is gated (real exchange) and interrogated by its Judge; accepted
     names are bought on paper.
  4. Record the portfolio value vs benchmarks; write report + summary; save.

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

from reslab import config, market, llm, store, report
from reslab.portfolio import Portfolio


def run():
    store.backup()
    state = store.load()
    state.setdefault("portfolio", {})
    state.setdefault("researchers", {})
    pf = Portfolio(state["portfolio"])
    events = []

    # ---- prices for holdings + benchmarks ----
    prices = {}
    for tk in pf.held_tickers():
        p = market.last_price(tk)
        if p:
            prices[tk] = p
    benchmarks = market.benchmark_levels()

    # ---- Judge reviews each holding ----
    for r in config.RESEARCHERS:
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
                if rec: events.append(f"SOLD {tk} {rec['pnl_pct']:+.1f}% (${rec['pnl']:+,.0f}) — {rec['sell_reason']}")
            elif d["action"] == "add" and pf.can_add(tk, config.ADD_SIZE):
                if pf.buy(tk, pos["name"], r["name"], pos["thesis"], price, config.ADD_SIZE):
                    pos["challenges"]["added"] += 1
                    events.append(f"ADDED ${config.ADD_SIZE:,.0f} to {tk} — {d.get('reasoning','')}")
            else:
                pos["challenges"]["held"] += 1

    # ---- researchers hunt new ideas ----
    for r in config.RESEARCHERS:
        rstate = state["researchers"].setdefault(r["name"], {"proposed": []})
        if pf.open_count() >= config.MAX_OPEN_POSITIONS:
            break
        avoid = pf.held_tickers() + rstate["proposed"][-40:]
        for _ in range(config.NEW_IDEAS_PER_DAY):
            rec = llm.research(r, avoid)
            if not rec or rec.get("_offline"):
                events.append(f"{r['name']}: live research unavailable (set ANTHROPIC_API_KEY).")
                break
            tk = (rec.get("ticker") or "").upper()
            if not tk or tk in avoid:
                continue
            rstate["proposed"].append(tk); avoid.append(tk)
            elig = market.eligibility(tk)
            if not elig["tradeable"]:
                events.append(f"{r['name']} → {tk}: skipped (not Robinhood-buyable: {', '.join(elig['reasons'])}).")
                continue
            verdict = llm.judge_new(r, rec)
            if verdict["decision"] == "accept":
                price = elig["price"] or market.last_price(tk)
                if pf.buy(tk, rec.get("name", tk), r["name"], rec.get("thesis", ""), price, config.INITIAL_POSITION):
                    pf.position(tk)["verdict"] = verdict
                    events.append(f"BOUGHT {tk} @ ${price:.2f} (${config.INITIAL_POSITION:,.0f}) — {r['judge']}: {verdict.get('reasoning','')[:160]}")
            else:
                events.append(f"{r['name']} → {tk}: {verdict['decision']} — {r['judge']}: {verdict.get('reasoning','')[:160]}")

    # ---- mark, report, save ----
    for tk in pf.held_tickers():
        if tk not in prices:
            p = market.last_price(tk)
            if p: prices[tk] = p
    total = pf.mark(prices, benchmarks)
    store.save(state)

    md = report.report(pf, events)
    summ = report.summary(pf)
    _write("daily_report.md", "# Research Lab — daily report\n\n" + md)
    _write("summary.md", "# Research Lab — summary\n\n" + summ + "\n")
    print(summ)
    print(f"\n[daily job] total paper value ${total:,.0f} · state saved · report written")


def _today():
    from datetime import date
    return str(date.today())


def _write(name, text):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), "w") as f:
        f.write(text)


if __name__ == "__main__":
    run()
