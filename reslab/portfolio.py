"""The paper portfolio — cost basis, adds, marks, sells, and benchmark tracking.

Positions record every leg (initial buy + each add) so you always see how much
was invested, when, and at what price, plus current value and P&L. Closed
positions keep realized P&L. Nothing here places real orders.

The system runs MULTIPLE portfolios, not one. Each holds up to
MAX_OPEN_POSITIONS names and starts with its own CASH_BUDGET. Once a
portfolio fills up, the next buy opens a brand-new portfolio — its own cash,
its own positions, and (because its benchmark curve starts recording from its
own first mark()) its own since-inception return vs the market, independent
of every other portfolio. This lets you compare one batch of picks against
another instead of one number blurring them all together.
"""
from __future__ import annotations

from . import config


def _today():
    return config.today_str()


def _new_portfolio_dict(pid: int) -> dict:
    return {"id": pid, "created": _today(), "positions": {}, "closed": [],
            "cash": config.CASH_BUDGET, "curve": [], "closed_to_new": False}


class Portfolio:
    def __init__(self, state: dict):
        # state persists across days (positions, closed, benchmark curve, cash)
        state.setdefault("id", 1)
        state.setdefault("created", _today())
        state.setdefault("positions", {})     # ticker -> position
        state.setdefault("closed", [])        # list of closed positions
        state.setdefault("cash", config.CASH_BUDGET)
        state.setdefault("curve", [])         # [{date, value, benchmarks:{SPY:..}}]
        # Once a portfolio has ever reached MAX_OPEN_POSITIONS it stays closed
        # to new buys for good — even if a later sale frees a slot — so each
        # portfolio stays a clean, comparable "batch of <=N picks" rather than
        # a revolving pool. Selling still works; only new opens are blocked.
        state.setdefault("closed_to_new", False)
        self.s = state

    # --- queries ---
    def held_tickers(self):
        return list(self.s["positions"].keys())

    def position(self, tk):
        return self.s["positions"].get(tk.upper())

    def open_count(self):
        return len(self.s["positions"])

    def is_full(self):
        return self.open_count() >= config.MAX_OPEN_POSITIONS

    def accepts_new(self):
        return not self.s.get("closed_to_new", False)

    # --- trades ---
    def buy(self, tk, name, researcher, thesis, price, amount):
        tk = tk.upper()
        if price is None or price <= 0 or amount <= 0:
            return False
        pos = self.s["positions"].get(tk)
        if pos is None and not self.accepts_new():
            return False  # this portfolio is a closed batch; open new names elsewhere
        if amount > self.s["cash"]:
            amount = self.s["cash"]
        if amount <= 0:
            return False
        shares = amount / price
        if pos is None:
            pos = {"ticker": tk, "name": name, "researcher": researcher,
                   "thesis": thesis, "opened": _today(), "legs": [],
                   "shares": 0.0, "cost_basis": 0.0,
                   "challenges": {"total": 0, "held": 0, "added": 0, "trimmed": 0}}
            self.s["positions"][tk] = pos
        pos["legs"].append({"date": _today(), "kind": "initial" if not pos["legs"] else "add",
                            "price": round(price, 4), "amount": round(amount, 2),
                            "shares": round(shares, 6)})
        pos["shares"] += shares
        pos["cost_basis"] += amount
        self.s["cash"] -= amount
        if self.is_full():
            self.s["closed_to_new"] = True
        return True

    def can_add(self, tk, amount):
        pos = self.position(tk)
        return pos is not None and (pos["cost_basis"] + amount) <= config.MAX_POSITION \
            and amount <= self.s["cash"]

    def sell(self, tk, price, reason, fraction=1.0):
        """Sell all (fraction=1.0, default) or part of a position. A partial
        sell reduces shares/cost_basis in place and leaves the rest held; a
        full sell (fraction>=~1) closes the position entirely. Either way a
        realized-P&L record is appended to closed."""
        tk = tk.upper()
        pos = self.s["positions"].get(tk)
        if pos is None or price is None:
            return None
        fraction = max(0.01, min(1.0, fraction))
        full_exit = fraction >= 0.999
        shares_sold = pos["shares"] * fraction
        cost_sold = pos["cost_basis"] * fraction
        proceeds = shares_sold * price
        pnl = proceeds - cost_sold
        pnl_pct = (pnl / cost_sold * 100) if cost_sold else 0.0
        if full_exit:
            self.s["positions"].pop(tk, None)
        else:
            pos["shares"] -= shares_sold
            pos["cost_basis"] -= cost_sold
            pos["legs"].append({"date": _today(), "kind": "trim", "price": round(price, 4),
                                "amount": -round(proceeds, 2), "shares": -round(shares_sold, 6)})
        rec = {"ticker": tk, "name": pos.get("name", ""), "researcher": pos.get("researcher", ""),
               "shares": round(shares_sold, 6), "cost_basis": round(cost_sold, 2),
               "sold": _today(), "sell_price": round(price, 4),
               "proceeds": round(proceeds, 2), "pnl": round(pnl, 2),
               "pnl_pct": round(pnl_pct, 2), "sell_reason": reason, "partial": not full_exit}
        self.s["closed"].append(rec)
        self.s["cash"] += proceeds
        return rec

    # --- valuation ---
    def value_position(self, tk, price):
        pos = self.position(tk)
        if pos is None or price is None:
            return None
        val = pos["shares"] * price
        pnl = val - pos["cost_basis"]
        return {"value": val, "pnl": pnl,
                "pnl_pct": (pnl / pos["cost_basis"] * 100) if pos["cost_basis"] else 0.0}

    def mark(self, prices: dict, benchmarks: dict):
        """Record today's total value (holdings + cash) and benchmark levels."""
        total = self.s["cash"]
        for tk, pos in self.s["positions"].items():
            p = prices.get(tk)
            if p:
                total += pos["shares"] * p
        self.s["curve"].append({"date": _today(), "value": round(total, 2),
                                "benchmarks": benchmarks})
        return total

    def invested_total(self):
        return sum(p["cost_basis"] for p in self.s["positions"].values())

    def invested_ever(self):
        """Every dollar this portfolio has ever put into a buy (open
        positions' cost basis + the cost basis of everything since sold,
        whole or partial) — the right denominator for a % return that
        isn't diluted by cash still sitting on the sidelines."""
        return self.invested_total() + sum(c.get("cost_basis", 0) for c in self.s.get("closed", []))


# --------------------------------------------------------------------------- #
# Multi-portfolio bookkeeping. These operate on the top-level state dict
# (state["portfolios"] is a list of the plain dicts Portfolio wraps).

def load_portfolios(state: dict) -> list[Portfolio]:
    """All portfolios, oldest first. One-time-migrates a legacy single
    state["portfolio"] dict (from before multi-portfolio support, including
    the earlier "rounds" scheme) into portfolios[0]."""
    if "portfolios" not in state:
        legacy = state.pop("portfolio", None)
        if legacy:
            legacy.pop("rounds", None)  # obsolete same-portfolio-expansion scheme
            legacy.setdefault("id", 1)
            legacy.setdefault("created", _today())
            legacy.setdefault("closed_to_new", len(legacy.get("positions", {})) >= config.MAX_OPEN_POSITIONS)
            state["portfolios"] = [legacy]
        else:
            state["portfolios"] = [_new_portfolio_dict(1)]
    return [Portfolio(p) for p in state["portfolios"]]


def active_portfolio(state: dict) -> Portfolio:
    """The portfolio new names go into: the most recent one still accepting
    new buys. Creates a fresh one (its own id, cash, empty curve) once the
    newest has permanently closed to new buys (see Portfolio.accepts_new)."""
    pfs = load_portfolios(state)
    if pfs and pfs[-1].accepts_new():
        return pfs[-1]
    new_id = (pfs[-1].s["id"] + 1) if pfs else 1
    new_dict = _new_portfolio_dict(new_id)
    state["portfolios"].append(new_dict)
    return Portfolio(new_dict)


def all_held_tickers(state: dict) -> list[str]:
    return [tk for p in load_portfolios(state) for tk in p.held_tickers()]


def find_holder(state: dict, tk: str) -> Portfolio | None:
    """The Portfolio currently holding tk, if any."""
    tk = tk.upper()
    for p in load_portfolios(state):
        if p.position(tk):
            return p
    return None
