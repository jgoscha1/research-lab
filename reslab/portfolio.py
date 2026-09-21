"""The paper portfolio — cost basis, adds, marks, sells, and benchmark tracking.

Positions record every leg (initial buy + each add) so you always see how much
was invested, when, and at what price, plus current value and P&L. Closed
positions keep realized P&L. Nothing here places real orders.
"""
from __future__ import annotations

from datetime import date

from . import config


def _today():
    return str(date.today())


class Portfolio:
    def __init__(self, state: dict):
        # state persists across days (positions, closed, benchmark curve, cash)
        state.setdefault("positions", {})     # ticker -> position
        state.setdefault("closed", [])        # list of closed positions
        state.setdefault("cash", config.CASH_BUDGET)
        state.setdefault("curve", [])         # [{date, value, benchmarks:{SPY:..}}]
        state.setdefault("rounds", 1)         # see max_open()/start_new_round()
        self.s = state

    # --- queries ---
    def held_tickers(self):
        return list(self.s["positions"].keys())

    def position(self, tk):
        return self.s["positions"].get(tk.upper())

    def open_count(self):
        return len(self.s["positions"])

    def max_open(self):
        """Position-count ceiling for the current round (see start_new_round)."""
        return config.MAX_OPEN_POSITIONS * self.s.get("rounds", 1)

    def at_cap(self):
        return self.open_count() >= self.max_open()

    def start_new_round(self):
        """Called when at_cap(): raise the position ceiling by another
        MAX_OPEN_POSITIONS and inject a fresh CASH_BUDGET of paper capital,
        so the system keeps researching and buying instead of stalling once
        it fills its slots. Existing positions are untouched."""
        self.s["rounds"] = self.s.get("rounds", 1) + 1
        self.s["cash"] = self.s.get("cash", 0.0) + config.CASH_BUDGET

    # --- trades ---
    def buy(self, tk, name, researcher, thesis, price, amount):
        tk = tk.upper()
        if price is None or price <= 0 or amount <= 0:
            return False
        if amount > self.s["cash"]:
            amount = self.s["cash"]
        if amount <= 0:
            return False
        shares = amount / price
        pos = self.s["positions"].get(tk)
        if pos is None:
            pos = {"ticker": tk, "name": name, "researcher": researcher,
                   "thesis": thesis, "opened": _today(), "legs": [],
                   "shares": 0.0, "cost_basis": 0.0,
                   "challenges": {"total": 0, "held": 0, "added": 0}}
            self.s["positions"][tk] = pos
        pos["legs"].append({"date": _today(), "kind": "initial" if not pos["legs"] else "add",
                            "price": round(price, 4), "amount": round(amount, 2),
                            "shares": round(shares, 6)})
        pos["shares"] += shares
        pos["cost_basis"] += amount
        self.s["cash"] -= amount
        return True

    def can_add(self, tk, amount):
        pos = self.position(tk)
        return pos is not None and (pos["cost_basis"] + amount) <= config.MAX_POSITION \
            and amount <= self.s["cash"]

    def sell(self, tk, price, reason):
        tk = tk.upper()
        pos = self.s["positions"].pop(tk, None)
        if pos is None or price is None:
            return None
        proceeds = pos["shares"] * price
        pnl = proceeds - pos["cost_basis"]
        pnl_pct = (pnl / pos["cost_basis"] * 100) if pos["cost_basis"] else 0.0
        rec = {**pos, "sold": _today(), "sell_price": round(price, 4),
               "proceeds": round(proceeds, 2), "pnl": round(pnl, 2),
               "pnl_pct": round(pnl_pct, 2), "sell_reason": reason}
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
