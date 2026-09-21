"""dashboard.py — the live Research Lab app you control from a browser.

A private URL (no login — the address itself is the secret). Press Go and the
two researchers run continuously (each cycle: research a Robinhood-buyable stock
of any size, or — once per day per holding — its Judge reviews an existing
position for buy-more/hold/sell), streaming their conversation live. Stop
anytime, or it auto-stops after 6 hours. Buys go into whichever portfolio is
still accepting new names; once one fills to MAX_OPEN_POSITIONS it's closed
for good and the next buy starts a fresh portfolio with its own cash, so each
batch's return vs SPY/QQQ can be judged on its own. Shows every portfolio,
positions, price charts, 1-page reports, closed trades, and AI spend.

Continuous running spends your Anthropic credit — watch the cycle counter and
rely on your Console spend cap. Paper only; no real orders.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from reslab import config, market, llm, store, trends, portfolio  # noqa

PORT = int(os.environ.get("DASH_PORT", "8080"))
INTERVAL = int(os.environ.get("RUN_INTERVAL_SEC", "90"))
MAX_HOURS = float(os.environ.get("RUN_MAX_HOURS", "6"))


def _token():
    t = os.environ.get("DASH_TOKEN")
    if t:
        return t
    f = os.path.join(HERE, ".dash_token")
    if os.path.exists(f):
        return open(f).read().strip()
    t = secrets.token_urlsafe(9)
    open(f, "w").write(t)
    return t
TOKEN = _token()

RUN = {"running": False, "started": 0.0, "until": 0.0, "cycles": 0}
EVENTS = []
_ev_i = 0
_price = {}
LOCK = threading.Lock()


def log(who, pair, text, kind="msg"):
    global _ev_i
    EVENTS.append({"i": _ev_i, "who": who, "pair": pair, "text": text, "kind": kind})
    _ev_i += 1
    del EVENTS[:-600]


def price(tk):
    p, ts = _price.get(tk, (None, 0))
    if time.time() - ts < 300:
        return p
    try:
        p = market.last_price(tk)
    except Exception:
        p = None
    _price[tk] = (p, time.time())
    return p


def _state():
    st = store.load()
    portfolio.load_portfolios(st)  # migrates legacy state["portfolio"] if present
    st.setdefault("researchers", {})
    st.setdefault("trends", [])
    return st


STATUS_MAP = {"reject": "rejected", "watch": "watch"}  # judge decision -> trend-stock status


def _judge_and_place(st, r, rec, trend_text, trend, bought_note=""):
    """Judge one pick and, if accepted, buy it into the current active portfolio
    — spawning a brand-new one (its own cash, its own curve) if every existing
    portfolio is already full. Logs the outcome and records it on the trend
    either way. Returns True if bought."""
    tk = (rec.get("ticker") or "").upper()
    elig = market.eligibility(tk)
    if not elig["tradeable"]:
        log("s", r["name"], f"{tk}: skipped — not Robinhood-buyable ({', '.join(elig['reasons'])}).", "sys")
        trends.record_stock(trend, rec, "not_tradeable")
        return False
    verdict = llm.judge_new(r, rec, trend=trend_text)
    log("j", r["name"], f"{r['judge']}: {verdict.get('reasoning','')}", "verdict")
    if verdict["decision"] != "accept":
        log("s", r["name"], f"{tk}: {verdict['decision']} — not bought.", "sys")
        trends.record_stock(trend, rec, STATUS_MAP.get(verdict["decision"], verdict["decision"]), verdict.get("reasoning", ""))
        return False
    pf = portfolio.active_portfolio(st)
    was_new = pf.open_count() == 0
    px = elig["price"] or price(tk)
    if px and pf.buy(tk, rec.get("name", tk), r["name"], rec.get("thesis", ""), px, config.INITIAL_POSITION):
        if was_new:
            log("s", "", f"\U0001f4c8 Starting portfolio {pf.s['id']} with ${config.CASH_BUDGET:,.0f}.", "sys")
        p = pf.position(tk); p["rec"] = rec; p["verdict"] = verdict; p["trend"] = trend_text
        log("s", r["name"], f"BOUGHT {tk} @ ${px:.2f} (${config.INITIAL_POSITION:,.0f}){bought_note} — portfolio {pf.s['id']}.", "sys")
        trends.record_stock(trend, rec, "bought", verdict.get("reasoning", ""))
        return True
    log("s", r["name"], f"{tk}: accepted, but no price available — not bought.", "sys")
    trends.record_stock(trend, rec, "watch", verdict.get("reasoning", ""))
    return False


def one_cycle(cyc):
    st = _state()
    r = config.RESEARCHERS[cyc % len(config.RESEARCHERS)]
    today = str(date.today())
    pfs = portfolio.load_portfolios(st)
    # Each holding gets at most one buy/hold/sell review per day — once a
    # position has been reviewed today, leave it alone until tomorrow. Scans
    # every portfolio, since a researcher's holdings can span several of them.
    due = None
    for p in pfs:
        for tk, pos in p.s["positions"].items():
            if pos["researcher"] == r["name"] and pos.get("last_challenge", {}).get("date") != today:
                due = (p, tk)
                break
        if due:
            break

    if due:
        p, tk = due
        pos = p.position(tk); px = price(tk)
        v = p.value_position(tk, px) or {"value": pos["cost_basis"], "pnl_pct": 0}
        log("r", r["name"], f"Reviewing {tk} (portfolio {p.s['id']}) — ${v['value']:,.0f} ({v['pnl_pct']:+.1f}%).")
        d = llm.judge_hold(r, pos, v["value"], v["pnl_pct"])
        pos["challenges"]["total"] += 1
        pos["last_challenge"] = {"date": today, "action": d["action"], "reasoning": d.get("reasoning", "")}
        log("j", r["name"], f"{r['judge']}: {d.get('reasoning','')}", "verdict")
        if d["action"] == "sell":
            rec = p.sell(tk, px, d.get("reasoning", "sell"))
            if rec: log("s", r["name"], f"SOLD {tk} {rec['pnl_pct']:+.1f}% (${rec['pnl']:+,.0f}).", "sys")
        elif d["action"] == "trim" and px:
            frac = max(1, min(90, int(d.get("trim_pct") or 50))) / 100.0
            rec = p.sell(tk, px, d.get("reasoning", "trim"), fraction=frac)
            if rec:
                pos["challenges"]["trimmed"] = pos["challenges"].get("trimmed", 0) + 1
                log("s", r["name"], f"TRIMMED {int(frac*100)}% of {tk} {rec['pnl_pct']:+.1f}% (${rec['pnl']:+,.0f}) — still holding the rest.", "sys")
        elif d["action"] == "add" and p.can_add(tk, config.ADD_SIZE) and px:
            if p.buy(tk, pos["name"], r["name"], pos["thesis"], px, config.ADD_SIZE):
                pos["challenges"]["added"] += 1
                log("s", r["name"], f"ADDED ${config.ADD_SIZE:,.0f} to {tk}.", "sys")
        else:
            pos["challenges"]["held"] += 1
    else:
        rstate = st["researchers"].setdefault(r["name"], {"proposed": []})
        avoid = portfolio.all_held_tickers(st) + rstate["proposed"][-40:]
        result = llm.research(r, avoid)
        if result.get("_offline"):
            log("s", r["name"], "Live research unavailable (set ANTHROPIC_API_KEY).", "sys")
            store.save(st); return
        trend_text = result["trend"]
        trend = trends.get_or_create(st, r["name"], trend_text, origin="auto")
        log("r", r["name"], f"Trend: {trend_text}")
        for rec in result["picks"][:3]:
            tk = (rec.get("ticker") or "").upper()
            if not tk or tk in avoid:
                continue
            rstate["proposed"].append(tk); avoid.append(tk)
            log("r", r["name"], f"{tk} — {rec.get('thesis','')}")
            _judge_and_place(st, r, rec, trend_text, trend)

    bench = {b: price(b) for b in config.BENCHMARKS}
    bench = {b: p for b, p in bench.items() if p}
    for p in portfolio.load_portfolios(st):
        prices = {t: price(t) for t in p.held_tickers()}
        prices = {t: pv for t, pv in prices.items() if pv}
        curve = p.s.setdefault("curve", [])
        if not curve or curve[-1]["date"] != today or (cyc % 8 == 0):
            p.mark(prices, bench)
            del p.s["curve"][:-3000]
    store.save(st)


def worker():
    RUN.update(running=True, started=time.time(), until=time.time() + MAX_HOURS * 3600, cycles=0)
    log("s", "", "\u25b6 Started. Researchers running\u2026", "sys")
    cyc = 0
    while RUN["running"] and time.time() < RUN["until"]:
        try:
            one_cycle(cyc)
        except Exception as e:
            log("s", "", f"cycle error: {e}", "sys")
        cyc += 1; RUN["cycles"] = cyc
        for _ in range(INTERVAL):
            if not RUN["running"] or time.time() >= RUN["until"]:
                break
            time.sleep(1)
    RUN["running"] = False
    log("s", "", "\u25a0 Stopped.", "sys")


def start():
    with LOCK:
        if not RUN["running"]:
            threading.Thread(target=worker, daemon=True).start()


def stop():
    RUN["running"] = False


def _pct(curve, key=None):
    if not curve: return None
    if key is None: a, b = curve[0]["value"], curve[-1]["value"]
    else:
        pts = [c["benchmarks"].get(key) for c in curve if c["benchmarks"].get(key)]
        if not pts: return None
        a, b = pts[0], pts[-1]
    return (b / a - 1) * 100 if a else None


def _invested_pct(pf, total=None):
    """% return based on capital actually invested, not the whole starting
    budget — so a portfolio that's still slowly deploying cash isn't
    shown as flat just because most of its $ is still sitting idle."""
    if total is None:
        curve = pf.s.get("curve", [])
        total = curve[-1]["value"] if curve else (pf.s.get("cash", 0) + pf.invested_total())
    invested_ever = pf.invested_ever()
    if not invested_ever:
        return None
    return (total - config.CASH_BUDGET) / invested_ever * 100


def _bench_dollar_series(curve, key, base_value):
    """Forward-filled benchmark index level for `key`, rescaled so it starts
    at base_value — the same starting dollar amount as the portfolio curve —
    so the two can be overlaid on one chart as an apples-to-apples comparison."""
    out = []
    last = None
    base_level = None
    for c in curve:
        lvl = c.get("benchmarks", {}).get(key)
        if lvl:
            last = lvl
            if base_level is None:
                base_level = lvl
        out.append(last)
    if not base_level or base_value is None:
        return [None] * len(curve)
    return [round(base_value * (v / base_level), 2) if v else None for v in out]


def state_json():
    st = _state()
    pfs = portfolio.load_portfolios(st)
    portfolios = []
    positions = []
    closed = []
    combined_total = 0.0
    for pf in pfs:
        holdings_val = 0.0
        for tk, p in pf.s["positions"].items():
            px = price(tk); val = p["shares"] * px if px else None
            if val: holdings_val += val
            pnl = (val - p["cost_basis"]) if val is not None else None
            positions.append({
                "tk": tk, "name": p.get("name", ""), "researcher": p.get("researcher", ""),
                "cost_basis": p["cost_basis"], "buy_price": p["legs"][0]["price"] if p["legs"] else None,
                "cur": px, "value": val, "pnl": pnl,
                "pnl_pct": (pnl / p["cost_basis"] * 100) if (pnl is not None and p["cost_basis"]) else None,
                "legs": p["legs"], "challenges": p.get("challenges", {}),
                "last_challenge": p.get("last_challenge"), "rec": p.get("rec"),
                "verdict": p.get("verdict"), "thesis": p.get("thesis", ""),
                "trend": p.get("trend", ""), "portfolio_id": pf.s["id"],
            })
        closed.extend({"tk": c["ticker"], "name": c.get("name", ""), "by": c.get("researcher", ""),
                        "pnl": c["pnl"], "pnl_pct": c["pnl_pct"], "sell_reason": c.get("sell_reason", ""),
                        "partial": c.get("partial", False), "portfolio_id": pf.s["id"]}
                       for c in pf.s.get("closed", []))
        cash = pf.s.get("cash", 0.0); curve = pf.s.get("curve", [])
        total = cash + holdings_val
        combined_total += total
        window = [c["value"] for c in curve][-120:]
        base_value = window[0] if window else None
        bench_curves = {b: _bench_dollar_series(curve, b, base_value)[-120:] for b in config.BENCHMARKS}
        portfolios.append({
            "id": pf.s["id"], "created": pf.s.get("created", ""),
            "total": total, "invested": pf.invested_total(), "cash": cash,
            "pnl_dollar": total - config.CASH_BUDGET,
            "port_pct": _invested_pct(pf, total), "benchmarks": {b: _pct(curve, b) for b in config.BENCHMARKS},
            "curve": window, "bench_curves": bench_curves,
            "open_count": pf.open_count(), "max_open": config.MAX_OPEN_POSITIONS,
            "accepts_new": pf.accepts_new(),
        })
    closed.sort(key=lambda c: c.get("portfolio_id", 0))
    closed = closed[-40:]
    remaining = max(0, RUN["until"] - time.time()) if RUN["running"] else 0
    costs = store.load_costs()
    return {
        "run": {"running": RUN["running"], "cycles": RUN["cycles"],
                "elapsed": int(time.time() - RUN["started"]) if RUN["started"] else 0,
                "remaining": int(remaining), "interval": INTERVAL},
        "portfolios": portfolios,
        "combined_total": combined_total,
        "positions": positions,
        "closed": closed,
        "events": EVENTS[-400:],
        "researchers": [{"name": r["name"], "judge": r["judge"]} for r in config.RESEARCHERS],
        "costs": {"today": costs.get("daily", {}).get(str(date.today()), 0.0),
                  "total": costs.get("total", 0.0)},
        "trends": list(reversed(st.get("trends", [])))[-60:],
    }


def price_history(tk):
    try:
        df = market.get_daily(tk, "13mo")
    except Exception:
        df = None
    if df is None or not len(df):
        return {"labels": [], "prices": [], "dates": []}
    s = df["close"]; step = max(1, len(s) // 60); s = s.iloc[::step]
    return {"labels": [d.strftime("%b %y") for d in s.index],
            "dates": [d.strftime("%Y-%m-%d") for d in s.index],
            "prices": [round(float(x), 2) for x in s.values]}


def ask_book(q, history=None):
    st = _state()
    rows = []
    for pf in portfolio.load_portfolios(st):
        rows += [f"[portfolio {pf.s['id']}] {tk} ({p.get('researcher')}): invested ${p['cost_basis']:,.0f}. {p.get('thesis','')}"
                 for tk, p in pf.s["positions"].items()]
        rows += [f"[portfolio {pf.s['id']}] SOLD {c['ticker']}: {c['pnl_pct']:+.1f}%. {c.get('sell_reason','')}"
                 for c in pf.s.get("closed", [])]
    book = "\n".join(rows) or "No positions yet."
    convo = ""
    if history:
        turns = "\n".join(f"{'Owner' if h.get('role') == 'user' else 'Assistant'}: {h.get('text','')}"
                           for h in history[-10:] if h.get("text"))
        if turns:
            convo = f"\n\nConversation so far:\n{turns}\n"
    txt = llm._ask(f"You are the Research Lab assistant. Answer the owner's question about the portfolio, "
                   f"grounded ONLY in this book; concise, honest, not investment advice.\nBOOK:\n{book}{convo}\n\nQUESTION: {q}")
    return txt or "Live AI unavailable \u2014 check ANTHROPIC_API_KEY."


def research_trend(trend_text, rname):
    """Owner-suggested trend: find up to 3 stocks for it, judge and place each."""
    r = next((x for x in config.RESEARCHERS if x["name"].lower() == rname.lower()), config.RESEARCHERS[0])
    st = _state()
    rstate = st["researchers"].setdefault(r["name"], {"proposed": []})
    avoid = portfolio.all_held_tickers(st) + rstate["proposed"][-40:]
    result = llm.research(r, avoid, trend_focus=trend_text)
    if result.get("_offline"):
        log("s", r["name"], f"Your trend '{trend_text}': live research unavailable.", "sys")
        return "Live AI unavailable."
    trend = trends.get_or_create(st, r["name"], trend_text, origin="owner")
    picks = [p for p in result["picks"] if (p.get("ticker") or "").upper() not in avoid][:3]
    if not picks:
        log("s", r["name"], f"Your trend '{trend_text}': nothing new to report.", "sys")
        store.save(st)
        return "No new picks \u2014 try a more specific trend."
    bought = []
    for rec in picks:
        tk = (rec.get("ticker") or "").upper()
        rstate["proposed"].append(tk); avoid.append(tk)
        log("r", r["name"], f"[your trend: {trend_text}] {tk} \u2014 {rec.get('thesis','')}")
        if _judge_and_place(st, r, rec, trend_text, trend, bought_note=" from your trend"):
            bought.append(tk)
    store.save(st)
    return f"{len(picks)} pick(s) reviewed" + (f", bought {', '.join(bought)}" if bought else "") + "."


def expand_trend(trend_id):
    """Find more stocks in a trend already on file — the 'find others' button."""
    st = _state()
    trend = trends.get_by_id(st, trend_id)
    if trend is None:
        return "Trend not found."
    r = next((x for x in config.RESEARCHERS if x["name"] == trend["researcher"]), config.RESEARCHERS[0])
    rstate = st["researchers"].setdefault(r["name"], {"proposed": []})
    avoid = portfolio.all_held_tickers(st) + rstate["proposed"][-40:] + trends.seen_tickers(trend)
    result = llm.research(r, avoid, trend_focus=trend["text"])
    if result.get("_offline"):
        log("s", r["name"], f"Your trend '{trend['text']}': live research unavailable.", "sys")
        return "Live AI unavailable."
    picks = [p for p in result["picks"] if (p.get("ticker") or "").upper() not in avoid][:3]
    if not picks:
        log("s", r["name"], f"'{trend['text']}': no new names to add right now.", "sys")
        return "No new picks in this trend right now."
    bought = []
    for rec in picks:
        tk = (rec.get("ticker") or "").upper()
        rstate["proposed"].append(tk); avoid.append(tk)
        log("r", r["name"], f"[more on: {trend['text']}] {tk} — {rec.get('thesis','')}")
        if _judge_and_place(st, r, rec, trend["text"], trend, bought_note=" from this trend"):
            bought.append(tk)
    store.save(st)
    return f"{len(picks)} new pick(s) reviewed" + (f", bought {', '.join(bought)}" if bought else "") + "."


def analyze_trend_stock(trend_id, ticker):
    """Owner points at a specific ticker to analyze under an existing trend."""
    st = _state()
    trend = trends.get_by_id(st, trend_id)
    if trend is None:
        return "Trend not found."
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return "Enter a ticker."
    r = next((x for x in config.RESEARCHERS if x["name"] == trend["researcher"]), config.RESEARCHERS[0])
    rec = llm.research_stock(r, ticker, trend["text"])
    if rec is None:
        log("s", r["name"], f"{ticker}: live research unavailable.", "sys")
        return "Live AI unavailable."
    log("r", r["name"], f"[your pick for: {trend['text']}] {ticker} — {rec.get('thesis','')}")
    rstate = st["researchers"].setdefault(r["name"], {"proposed": []})
    rstate["proposed"].append(ticker)
    bought = _judge_and_place(st, r, rec, trend["text"], trend, bought_note=" from your pick")
    store.save(st)
    return f"{ticker}: reviewed" + (", bought" if bought else "") + "."


def system_review():
    """Ask a fresh Claude call to audit this whole setup and suggest changes
    that could improve risk-adjusted returns — not just the current holdings."""
    st = _state()
    pfs = portfolio.load_portfolios(st)
    combined_total = 0.0
    portfolio_lines = []
    for pf in pfs:
        curve = pf.s.get("curve", [])
        closed = pf.s.get("closed", [])
        wins = sum(1 for c in closed if c["pnl"] >= 0)
        bench = ", ".join(f"{b} {(_pct(curve, b) or 0):+.1f}%" for b in config.BENCHMARKS)
        holds = "; ".join(f"{tk} {p.get('researcher')} {p['challenges'].get('total',0)}x reviewed"
                           for tk, p in pf.s["positions"].items()) or "none"
        trades = "; ".join(f"{c['ticker']} {c['pnl_pct']:+.1f}% ({(c.get('sell_reason') or '')[:80]})"
                            for c in closed[-15:]) or "none yet"
        total = curve[-1]["value"] if curve else (pf.s.get("cash", 0) + pf.invested_total())
        combined_total += total
        port_pct = _invested_pct(pf, total)
        portfolio_lines.append(
            f"Portfolio {pf.s['id']} (started {pf.s.get('created','?')}"
            f"{', closed to new names' if not pf.accepts_new() else ', still accepting new names'}): "
            f"${total:,.0f} ({(port_pct or 0):+.1f}% on invested capital vs {bench} index return). "
            f"{pf.open_count()}/{config.MAX_OPEN_POSITIONS} open, {len(closed)} closed ({wins}/{len(closed)} profitable).\n"
            f"  Open positions: {holds}\n  Recent closed trades: {trades}"
        )
    summary = (
        f"{len(pfs)} portfolio(s), ${combined_total:,.0f} combined.\n"
        + "\n".join(portfolio_lines) + "\n"
        f"Config: initial position ${config.INITIAL_POSITION:,.0f}, add size ${config.ADD_SIZE:,.0f}, "
        f"max position ${config.MAX_POSITION:,.0f}, {config.MAX_OPEN_POSITIONS} names per portfolio "
        f"(a new ${config.CASH_BUDGET:,.0f} portfolio starts once the current one fills up), "
        f"each holding reviewed at most once/day.\n"
        f"Researchers: " + "; ".join(f"{r['name']} ({r['judge']}): {r['style']}" for r in config.RESEARCHERS)
    )
    prompt = (
        "You are an outside quant/portfolio-strategy consultant auditing this automated "
        "paper-trading research system (two LLM researchers, each with a skeptical LLM judge, "
        "picking Robinhood-buyable US stocks of any market cap, split across multiple independent "
        "~30-name portfolios so each batch's performance can be judged on its own). "
        "Here is its current state:\n\n"
        + summary +
        "\n\nGive concrete, prioritized suggestions to improve risk-adjusted returns going "
        "forward: sizing/diversification, review cadence, what in this exact setup is "
        "structurally likely helping or hurting, and specific parameter changes worth trying. "
        "Be honest if the sample size is too small to conclude much yet. Not investment advice."
    )
    log("s", "", "\U0001f50d System review requested…", "sys")
    txt = llm._ask(prompt, max_tokens=2000)
    if not txt:
        return "Live AI unavailable — check ANTHROPIC_API_KEY."
    log("s", "", "\U0001f50d System review ready — see the button output.", "sys")
    return txt


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype); self.end_headers(); self.wfile.write(b)

    def _path(self):
        p = urlparse(self.path).path
        if p in ("/" + TOKEN, "/" + TOKEN + "/"):
            return "/"
        if p.startswith("/" + TOKEN + "/"):
            return p[len("/" + TOKEN):]
        return None

    def do_GET(self):
        rp = self._path()
        if rp is None: return self._send(404, "not found", "text/plain")
        if rp == "/": return self._send(200, PAGE, "text/html; charset=utf-8")
        if rp == "/api/state": return self._send(200, json.dumps(state_json()))
        if rp == "/api/go": start(); return self._send(200, json.dumps({"ok": True}))
        if rp == "/api/stop": stop(); return self._send(200, json.dumps({"ok": True}))
        if rp.startswith("/api/history"):
            tk = parse_qs(urlparse(self.path).query).get("tk", [""])[0]
            return self._send(200, json.dumps(price_history(tk)))
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        rp = self._path()
        if rp is None: return self._send(404, "not found", "text/plain")
        ln = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(ln) or "{}")
        if rp == "/api/ask": return self._send(200, json.dumps({"answer": ask_book(data.get("q", ""), data.get("history"))}))
        if rp == "/api/trend": return self._send(200, json.dumps({"result": research_trend(data.get("trend", ""), data.get("researcher", "Ada"))}))
        if rp == "/api/trend/expand": return self._send(200, json.dumps({"result": expand_trend(data.get("trend_id"))}))
        if rp == "/api/trend/stock": return self._send(200, json.dumps({"result": analyze_trend_stock(data.get("trend_id"), data.get("ticker", ""))}))
        if rp == "/api/review": return self._send(200, json.dumps({"review": system_review()}))
        return self._send(404, "not found", "text/plain")

    def log_message(self, *a):
        pass


PAGE = """<!DOCTYPE html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>Research Lab</title>
<style>
:root{--bg:#0f1115;--pan:#171a21;--ln:#272b33;--mut:#9aa1ab;--ink:#e8eaed;--up:#4ade80;--dn:#f87171;--ac:#e8eaed}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,Segoe UI,Roboto,Arial,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:16px 14px 60px}
h1{font-size:19px;margin:0} h3{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 8px}
.card{background:var(--pan);border:1px solid var(--ln);border-radius:12px;padding:14px;margin-bottom:14px}
button{font:inherit;font-weight:600;font-size:13px;color:var(--ac);background:var(--pan);border:1px solid var(--ln);border-radius:9px;padding:8px 14px;cursor:pointer}
button:hover{border-color:var(--mut)} .go{background:var(--up);color:#06210f;border-color:var(--up)} .stop{background:var(--dn);color:#2a0a0a;border-color:var(--dn)}
.mini{font-size:12px;padding:4px 9px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
.on{background:var(--up)} .off{background:var(--mut)}
.stat{display:inline-block;margin-right:20px} .big{font-size:21px;font-weight:700} .mut{color:var(--mut);font-size:12px}
.up{color:var(--up)} .dn{color:var(--dn)}
.feed{max-height:340px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;padding-right:4px}
.feed.collapsed,#trendsBody.collapsed{display:none}
.card-head{display:flex;align-items:center;justify-content:space-between;cursor:pointer;gap:8px}
.card-head h3{margin:0} .chev{color:var(--mut);font-size:11px;transition:transform .15s} .chev.collapsed{transform:rotate(-90deg)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
.tab{font-size:11px;padding:4px 10px;opacity:.55} .tab.active{opacity:1;border-color:var(--mut);background:#1c2029}
.m{max-width:90%;padding:7px 10px;border-radius:11px;font-size:13px;white-space:pre-wrap}
.m.r{align-self:flex-start;background:#12203a} .m.j{align-self:flex-end;background:#2a2010} .m.s{align-self:center;color:var(--mut);font-size:12px;background:transparent}
.m.u{align-self:flex-end;background:#1c2029} .m.a{align-self:flex-start;background:#12203a}
.who{font-size:10px;font-weight:700;text-transform:uppercase;color:var(--mut);margin-bottom:2px}
table{width:100%;border-collapse:collapse;font-size:13px} td{padding:7px 5px;border-bottom:1px solid var(--ln);vertical-align:top}
input{width:100%;font:inherit;font-size:13px;color:var(--ink);background:var(--bg);border:1px solid var(--ln);border-radius:9px;padding:8px 10px;margin-bottom:8px}
.warn{background:#2a2010;color:#fbbf24;border-radius:9px;padding:9px 11px;font-size:12.5px;margin-bottom:14px}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;padding:14px;z-index:9}
.overlay.open{display:flex}.modal{background:var(--pan);border:1px solid var(--ln);border-radius:14px;max-width:600px;width:100%;max-height:86vh;overflow:auto;padding:18px}
h4{font-size:11px;text-transform:uppercase;color:var(--mut);margin:13px 0 3px} svg{width:100%;height:auto}
.pos{border:1px solid var(--ln);border-radius:10px;padding:10px;margin-bottom:8px}
</style></head><body><div class='wrap'>
<div style='display:flex;align-items:center;gap:12px;flex-wrap:wrap'><h1>\U0001f52c Research Lab \u2014 live</h1>
<span id='status' class='mut'></span><span style='margin-left:auto'></span>
<button id='go' class='go'>\u25b6 Go</button><button id='stop' class='stop'>\u25a0 Stop</button></div>
<div class='mut' style='margin:4px 0 12px' id='runline'></div>
<div class='warn'>\u26a0\ufe0f Running spends your Anthropic credit (each cycle = AI + web search). Watch the cycle count; your Console spend cap is the backstop. Paper only \u2014 no real orders, not investment advice.</div>

<div class='card'><h3>Portfolios \u00b7 paper</h3>
<p class='mut' style='margin-top:0' id='p_summary'>\u2014</p>
<div id='portfolios'></div></div>

<div class='card'><div class='card-head' onclick='toggleFeed()'><h3>Live conversation</h3><span class='chev' id='feedChev'>▼</span></div>
<div class='tabs' id='feedTabs'></div><div class='feed' id='feed'></div></div>
<div class='card'><h3>Open positions</h3><div id='positions'></div></div>
<div class='card'><h3>Closed positions</h3><div id='closed'></div></div>

<div class='card'><div class='card-head' onclick='toggleTrendsCard()'><h3>Trends</h3><span class='chev' id='trendsChev'>▼</span></div>
<div id='trendsBody'><p class='mut' style='margin-top:0'>Every macro/industry trend a researcher has looked at, and each stock reviewed within it. Click one to expand.</p>
<div id='trends'></div></div></div>

<div class='card'><h3>Ask the lab</h3>
<div class='feed' id='askchat' style='max-height:280px;margin-bottom:10px'></div>
<input id='askin' placeholder='e.g. which holding is up the most, and why?' onkeydown='if(event.key==="Enter")ask()'>
<button class='mini' onclick='ask()'>Ask</button></div>
<div class='card'><h3>Suggest a trend</h3><p class='mut' style='margin-top:0'>The researcher will find 1-3 stocks that benefit from it.</p>
<input id='trendin' placeholder='e.g. undersea cables, water scarcity\u2026'>
<button class='mini' onclick='trend("Ada")'>Send to Ada</button> <button class='mini' onclick='trend("Boone")'>Send to Boone</button>
<div id='trendout' class='mut' style='margin-top:8px'></div></div>
<div class='card'><h3>System review</h3><p class='mut' style='margin-top:0'>Ask a fresh agent to audit this whole setup — sizing, cadence, diversification — for ways to improve returns.</p>
<button class='mini' onclick='review()'>\U0001f50d Review my system</button><div id='reviewout' class='mut' style='margin-top:8px'></div></div>

<div class='mut'>To change the system itself, use Claude Code on the server (<code>cd research-lab &amp;&amp; claude</code>). Your positions live safely in state and survive changes.</div>
</div>
<div class='overlay' id='ov'><div class='modal' id='ovb'></div></div>
<script>
let STATE=null, FEED_FILTER='all', tabsBuilt=false; const $=id=>document.getElementById(id);
const BASE=location.pathname.replace(/\\/$/,'');
function toggleFeed(force){let c;try{c=force!==undefined?force:localStorage.getItem('feedCollapsed')!=='1';}catch(e){c=force!==undefined?force:!$('feed').classList.contains('collapsed');}
 $('feed').classList.toggle('collapsed',c);$('feedChev').classList.toggle('collapsed',c);
 try{localStorage.setItem('feedCollapsed',c?'1':'0');}catch(e){}}
try{toggleFeed(localStorage.getItem('feedCollapsed')==='1');}catch(e){}
function toggleTrendsCard(force){let c;try{c=force!==undefined?force:localStorage.getItem('trendsCollapsed')!=='1';}catch(e){c=force!==undefined?force:!$('trendsBody').classList.contains('collapsed');}
 $('trendsBody').classList.toggle('collapsed',c);$('trendsChev').classList.toggle('collapsed',c);
 try{localStorage.setItem('trendsCollapsed',c?'1':'0');}catch(e){}}
try{toggleTrendsCard(localStorage.getItem('trendsCollapsed')==='1');}catch(e){}
function buildFeedTabs(researchers){if(tabsBuilt||!researchers)return;tabsBuilt=true;
 const el=$('feedTabs'); const mk=(val,label)=>{const b=document.createElement('button');b.className='mini tab'+(val===FEED_FILTER?' active':'');
   b.textContent=label;b.onclick=(e)=>{e.stopPropagation();FEED_FILTER=val;document.querySelectorAll('#feedTabs .tab').forEach(x=>x.classList.toggle('active',x===b));renderFeed();};
   return b;};
 el.appendChild(mk('all','All'));
 researchers.forEach(r=>el.appendChild(mk(r.name,r.name+' & '+r.judge)));}
function renderFeed(){if(!STATE)return;const f=$('feed'); const atBottom=f.scrollHeight-f.scrollTop-f.clientHeight<60;
 const evs=STATE.events.filter(e=>FEED_FILTER==='all'||e.pair===FEED_FILTER||!e.pair);
 f.innerHTML='';
 evs.forEach(e=>{const d=document.createElement('div');d.className='m '+e.who;
   if(e.who!=='s'){const w=document.createElement('div');w.className='who';w.textContent=(e.who==='r'?'Researcher':'Judge')+(e.pair?' \u00b7 '+e.pair:'');d.appendChild(w);}
   const t=document.createElement('div');t.textContent=e.text;d.appendChild(t);f.appendChild(d);});
 if(atBottom)f.scrollTop=f.scrollHeight;}
function fmt(n){return n==null?'\u2014':'$'+Math.round(n).toLocaleString()}
function fmtSigned(n){if(n==null)return '\u2014';return (n>=0?'+':'-')+'$'+Math.abs(Math.round(n)).toLocaleString();}
function pc(n){return n==null?'\u2014':(n>=0?'+':'')+n.toFixed(1)+'%'}
function trunc(s,n){if(!s)return '';return s.length>n?s.slice(0,n).trim()+'\u2026':s;}
function chart(vals){if(!vals||vals.length<2)return"<p class='mut'>chart builds as it runs</p>";
 const lo=Math.min(...vals),hi=Math.max(...vals),W=640,H=120,p=6,x=i=>p+i*(W-2*p)/(vals.length-1),y=v=>H-p-(v-lo)/((hi-lo)||1)*(H-2*p);
 return "<svg viewBox='0 0 "+W+" "+H+"'><path d='"+vals.map((v,i)=>(i?'L':'M')+x(i).toFixed(1)+' '+y(v).toFixed(1)).join(' ')+"' fill='none' stroke='#60a5fa' stroke-width='2'/></svg>";}
const BENCH_COLORS={SPY:'#f59e0b',QQQ:'#c084fc'};
const BENCH_PALETTE=['#34d399','#f472b6','#38bdf8'];
function multiChart(series){
 const all=[]; series.forEach(s=>(s.vals||[]).forEach(v=>{if(v!=null)all.push(v);}));
 if(all.length<2)return "<p class='mut'>chart builds as it runs</p>";
 const lo=Math.min(...all),hi=Math.max(...all),W=640,H=120,p=6;
 const n=Math.max(...series.map(s=>(s.vals||[]).length));
 if(n<2)return "<p class='mut'>chart builds as it runs</p>";
 const x=i=>p+i*(W-2*p)/(n-1),y=v=>H-p-(v-lo)/((hi-lo)||1)*(H-2*p);
 const paths=series.map(s=>{let d='',started=false;
   (s.vals||[]).forEach((v,i)=>{if(v==null)return;d+=(started?'L':'M')+x(i).toFixed(1)+' '+y(v).toFixed(1)+' ';started=true;});
   return d?"<path d='"+d.trim()+"' fill='none' stroke='"+s.color+"' stroke-width='2'/>":'';
 }).join('');
 const legend=series.map(s=>"<span style='display:inline-flex;align-items:center;gap:4px;margin-right:12px'>"+
   "<span style='width:8px;height:8px;border-radius:50%;background:"+s.color+";display:inline-block'></span>"+
   "<span class='mut' style='font-size:11px'>"+s.label+"</span></span>").join('');
 return "<svg viewBox='0 0 "+W+" "+H+"'>"+paths+"</svg><div style='margin-top:4px'>"+legend+"</div>";}
async function poll(){try{STATE=await (await fetch(BASE+'/api/state')).json();render(STATE);}catch(e){}}
function render(s){const r=s.run;
 $('status').innerHTML="<span class='dot "+(r.running?'on':'off')+"'></span>"+(r.running?'running':'stopped');
 $('runline').textContent=r.running?('cycle '+r.cycles+' \u00b7 running '+Math.floor(r.elapsed/60)+'m \u00b7 auto-stops in '+Math.floor(r.remaining/3600)+'h'+Math.floor(r.remaining%3600/60)+'m'):(r.cycles?('stopped after '+r.cycles+' cycles'):'idle \u2014 press Go');
 const costs=s.costs||{today:0,total:0}; const pfs=s.portfolios||[];
 $('p_summary').textContent=pfs.length+' portfolio'+(pfs.length===1?'':'s')+' \u00b7 combined '+fmt(s.combined_total)+' \u00b7 AI cost today $'+costs.today.toFixed(2)+' \u00b7 lifetime $'+costs.total.toFixed(2);
 $('portfolios').innerHTML=pfs.length?pfs.map(p=>{
   const badge=p.accepts_new?"<span class='up' style='font-size:11px'>accepting new</span>":"<span class='mut' style='font-size:11px'>closed to new</span>";
   return "<div class='pos' style='margin-bottom:10px'>"+
     "<div style='display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:6px'>"+
       "<b>Portfolio "+p.id+"</b><span class='mut'>started "+p.created+"</span>"+badge+"</div>"+
     "<span class='stat'><span class='mut'>Total</span><br><span class='big'>"+fmt(p.total)+"</span></span>"+
     "<span class='stat'><span class='mut'>Invested</span><br><span class='big'>"+fmt(p.invested)+"</span></span>"+
     "<span class='stat'><span class='mut'>Cash</span><br><span class='big'>"+fmt(p.cash)+"</span></span>"+
     "<span class='stat'><span class='mut'>P&amp;L</span><br><span class='big "+((p.pnl_dollar||0)>=0?'up':'dn')+"'>"+fmtSigned(p.pnl_dollar)+"</span><br><span class='mut'>"+pc(p.port_pct)+" on invested</span></span>"+
     "<span class='stat'><span class='mut'>Positions</span><br><span class='big'>"+p.open_count+"/"+p.max_open+"</span></span>"+
     "<div class='mut' style='margin-top:6px'>since inception, vs "+Object.entries(p.benchmarks).map(([k,v])=>k+' '+pc(v)).join(' \u00b7 ')+"</div>"+
     "<div style='margin-top:8px'>"+multiChart([{vals:p.curve,color:'#60a5fa',label:'Portfolio'}].concat(
       Object.entries(p.bench_curves||{}).map(([k,v],idx)=>({vals:v,color:BENCH_COLORS[k]||BENCH_PALETTE[idx%BENCH_PALETTE.length],label:k}))))+"</div></div>";
 }).join(''):"<span class='mut'>No portfolio yet \u2014 press Go and one starts automatically.</span>";
 buildFeedTabs(s.researchers); renderFeed();
 $('positions').innerHTML=s.positions.length?s.positions.map((x,i)=>{
   const legs=x.legs.map(l=>'$'+Math.round(l.amount).toLocaleString()+'@$'+l.price).join(' + ');const ch=x.challenges||{};
   return "<div class='pos'><b>"+x.tk+"</b> <span class='mut'>P"+x.portfolio_id+" \u00b7 "+x.name+" \u00b7 "+x.researcher+"</span>"+
     "<div class='mut'>Bought $"+(x.buy_price||'\u2014')+" \u2192 $"+(x.cur?x.cur.toFixed(2):'\u2014')+" <span class='"+((x.pnl_pct||0)>=0?'up':'dn')+"'>"+pc(x.pnl_pct)+" ("+fmtSigned(x.pnl)+")</span></div>"+
     "<div class='mut'>Invested "+fmt(x.cost_basis)+" ["+legs+"] \u2192 value "+fmt(x.value)+" \u00b7 challenged "+(ch.total||0)+"\u00d7 (held "+(ch.held||0)+", added "+(ch.added||0)+", trimmed "+(ch.trimmed||0)+")</div>"+
     "<div style='margin-top:6px'>"+((x.rec||x.verdict)?"<button class='mini' onclick='rep("+i+")'>report \u25b8</button> ":"")+"<button class='mini' onclick='chartOf("+i+")'>chart \u25b8</button></div></div>";
 }).join(''):"<span class='mut'>No open positions yet \u2014 press Go and watch them appear.</span>";
 $('closed').innerHTML=s.closed.length?"<table>"+s.closed.slice().reverse().map(c=>
   "<tr><td><b>"+c.tk+"</b>"+(c.partial?" <span class='mut' style='font-size:11px'>(trim)</span>":"")+" <span class='mut'>P"+c.portfolio_id+" \u00b7 "+c.by+"</span></td><td class='"+(c.pnl>=0?'up':'dn')+"'>"+pc(c.pnl_pct)+" ($"+Math.round(c.pnl).toLocaleString()+")</td><td class='mut'>"+(c.sell_reason||'').slice(0,90)+"</td></tr>").join('')+"</table>":"<span class='mut'>none yet</span>";
 renderTrends(s.trends);
}
let TRENDS_OPEN=new Set(), TREND_MSG={};
function statusBadge(st){const map={bought:['up','bought'],watch:['mut','watching'],rejected:['dn','rejected'],not_tradeable:['mut','not tradeable']};
 const pair=map[st]||['mut',st]; return "<span class='"+pair[0]+"' style='font-size:11px'>"+pair[1]+"</span>";}
function renderTrends(list){const el=$('trends'); if(!list){return;}
 if(!list.length){el.innerHTML="<span class='mut'>No trends explored yet — press Go, or suggest one above.</span>";return;}
 el.innerHTML=list.map(t=>{
   const open=TRENDS_OPEN.has(t.id);
   const counts={}; (t.stocks||[]).forEach(s=>counts[s.status]=(counts[s.status]||0)+1);
   const summary=Object.entries(counts).map(([k,v])=>v+' '+k).join(', ')||'no stocks yet';
   const stocksHtml=(t.stocks||[]).length?(t.stocks||[]).map(s=>
     "<div class='pos'><b>"+s.tk+"</b> "+statusBadge(s.status)+" <span class='mut'>"+(s.name||'')+"</span>"+
     (s.thesis?"<div class='mut' style='margin-top:4px'>"+s.thesis+"</div>":"")+
     (s.reasoning?"<div class='mut' style='margin-top:4px'>⚖︎ "+s.reasoning+"</div>":"")+
     "</div>").join(''):"<span class='mut'>no stocks yet</span>";
   return "<div class='pos' style='margin-bottom:8px'>"+
     "<div style='cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:8px' onclick='toggleTrend("+t.id+")'>"+
       "<div><b>"+trunc(t.text,64)+"</b><div class='mut' style='margin-top:2px'>"+t.researcher+" · "+t.origin+" · "+summary+"</div></div>"+
       "<span class='chev"+(open?'':' collapsed')+"'>▼</span></div>"+
     (open?("<div style='margin-top:8px'><p style='margin:0 0 8px'>"+t.text+"</p>"+stocksHtml+
       "<div style='margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center'>"+
         "<button class='mini' onclick='event.stopPropagation();findOthers("+t.id+")'>find others ▸</button>"+
         "<input id='ti_"+t.id+"' placeholder='suggest a ticker, e.g. NVDA' style='flex:1;min-width:140px;margin:0' onclick='event.stopPropagation()'>"+
         "<button class='mini' onclick='event.stopPropagation();suggestStock("+t.id+")'>analyze ▸</button></div>"+
       "<div class='mut' style='margin-top:6px'>"+(TREND_MSG[t.id]||'')+"</div></div>"):"")+
     "</div>";
 }).join('');
}
function toggleTrend(id){if(TRENDS_OPEN.has(id))TRENDS_OPEN.delete(id);else TRENDS_OPEN.add(id); renderTrends(STATE&&STATE.trends);}
async function findOthers(id){TREND_MSG[id]='looking for more… this can take a bit (live web research)…';renderTrends(STATE.trends);
 const r=await (await fetch(BASE+'/api/trend/expand',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trend_id:id})})).json();
 TREND_MSG[id]=r.result+' (see the conversation feed)'; renderTrends(STATE.trends);}
async function suggestStock(id){const inp=$('ti_'+id); const tk=inp?inp.value.trim():''; if(!tk)return;
 TREND_MSG[id]='analyzing '+tk+'…'; renderTrends(STATE.trends);
 const r=await (await fetch(BASE+'/api/trend/stock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trend_id:id,ticker:tk})})).json();
 TREND_MSG[id]=r.result+' (see the conversation feed)'; renderTrends(STATE.trends);}
function rep(i){const x=STATE.positions[i];const r=x.rec||{};const v=x.verdict||{};
 open2("<h3>"+x.tk+" \u00b7 "+x.name+"</h3>"+(x.trend?"<h4>Trend</h4><p>"+x.trend+"</p>":"")+
  "<h4>Thesis</h4><p>"+(r.thesis||x.thesis||'')+"</p>"+(r.valuation?"<h4>Valuation</h4><p>"+r.valuation+"</p>":"")+
  (r.catalyst?"<h4>Catalyst</h4><p>"+r.catalyst+"</p>":"")+(r.risks?"<h4>Risks</h4><p>"+r.risks+"</p>":"")+
  "<h4>\u2696\ufe0e Judge</h4><p>"+(v.reasoning||'')+"</p><p class='mut' style='margin-top:12px'>Not investment advice.</p>");}
async function chartOf(i){const x=STATE.positions[i]; const tk=x.tk;
 open2("<h3>"+tk+" \u00b7 12-month price</h3><p class='mut'>loading\u2026</p>");
 const h=await (await fetch(BASE+'/api/history?tk='+tk)).json();
 if(!h.prices.length){$('ovb').innerHTML="<h3>"+tk+"</h3><p class='mut'>no price history available</p>";return;}
 const lo=Math.min(...h.prices),hi=Math.max(...h.prices),W=560,H=200,p=8,xf=i2=>p+i2*(W-2*p)/(h.prices.length-1),yf=v=>H-p-(v-lo)/((hi-lo)||1)*(H-2*p);
 let markers='';
 (x.legs||[]).forEach(leg=>{
   if(!h.dates||!h.dates.length)return;
   let best=0,bd=Infinity; const ld=new Date(leg.date).getTime();
   h.dates.forEach((d,di)=>{const diff=Math.abs(new Date(d).getTime()-ld); if(diff<bd){bd=diff;best=di;}});
   const cy=yf(Math.max(lo,Math.min(hi,leg.price)));
   const color=leg.kind==='initial'?'#4ade80':(leg.kind==='trim'?'#f87171':'#38bdf8');
   const verb=leg.kind==='initial'?'Bought':(leg.kind==='trim'?'Trimmed':'Added');
   markers+="<circle cx='"+xf(best).toFixed(1)+"' cy='"+cy.toFixed(1)+"' r='4' fill='"+color+"' stroke='#0f1115' stroke-width='1.5'><title>"+verb+" $"+Math.abs(Math.round(leg.amount)).toLocaleString()+" @ $"+leg.price+" on "+leg.date+"</title></circle>";
 });
 open2("<h3>"+tk+" \u00b7 12-month price</h3><svg viewBox='0 0 "+W+" "+H+"'><text x='2' y='12' font-size='10' fill='#9aa1ab'>$"+hi.toFixed(0)+"</text><text x='2' y='"+(H-2)+"' font-size='10' fill='#9aa1ab'>$"+lo.toFixed(0)+"</text><path d='"+h.prices.map((v,i2)=>(i2?'L':'M')+xf(i2).toFixed(1)+' '+yf(v).toFixed(1)).join(' ')+"' fill='none' stroke='#60a5fa' stroke-width='2'/>"+markers+"</svg><p class='mut'>via yfinance \u00b7 dots mark buys (green), adds (blue), trims (red)</p>");}
function open2(html){$('ovb').innerHTML=html+"<div style='margin-top:12px'><button class='mini' onclick='cls()'>close</button></div>";$('ov').classList.add('open');}
function cls(){$('ov').classList.remove('open');}
$('ov').addEventListener('click',e=>{if(e.target.id==='ov')cls();});
$('go').onclick=()=>{fetch(BASE+'/api/go');$('runline').textContent='starting\u2026';};
$('stop').onclick=()=>{fetch(BASE+'/api/stop');};
let ASK_HISTORY=[];
function renderAskChat(){const el=$('askchat'); if(!el)return;
 el.innerHTML=ASK_HISTORY.map(h=>"<div class='m "+(h.role==='user'?'u':'a')+"'>"+
   h.text.replace(/&/g,'&amp;').replace(/</g,'&lt;')+"</div>").join('');
 el.scrollTop=el.scrollHeight;}
async function ask(){const q=$('askin').value.trim();if(!q)return;$('askin').value='';
 ASK_HISTORY.push({role:'user',text:q}); renderAskChat();
 const hist=ASK_HISTORY.slice(0,-1);
 const pending={role:'assistant',text:'thinking\u2026'}; ASK_HISTORY.push(pending); renderAskChat();
 const r=await (await fetch(BASE+'/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({q,history:hist})})).json();
 pending.text=r.answer; renderAskChat();}
async function trend(who){const t=$('trendin').value.trim();if(!t)return;$('trendout').textContent=who+' is researching "'+t+'"\u2026';
 const r=await (await fetch(BASE+'/api/trend',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({trend:t,researcher:who})})).json();$('trendout').textContent=r.result+' (see the conversation feed)';}
async function review(){$('reviewout').textContent='auditing the system\u2026 this can take a minute (live web research)\u2026';
 const r=await (await fetch(BASE+'/api/review',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
 $('reviewout').textContent='done \u2014 see the report.';
 open2("<h3>\U0001f50d System review</h3><p style='white-space:pre-wrap'>"+(r.review||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')+"</p><p class='mut' style='margin-top:12px'>Not investment advice.</p>");}
poll();setInterval(poll,4000);
</script></body></html>"""


if __name__ == "__main__":
    print("=" * 60)
    print("Research Lab dashboard is live.")
    print("Open this PRIVATE URL in your browser (keep it secret):")
    print(f"    http://YOUR_SERVER_IP:{PORT}/{TOKEN}/")
    print("=" * 60)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
