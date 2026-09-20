"""Durable state — everything the system is running and has learned.

This is the file that must never be lost across code changes: open positions,
cost basis, closed trades, the benchmark curve, and each researcher's memory of
what it has already proposed. Code changes and restarts reload it; a timestamped
backup is written before every run.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime

from . import config

STATE_FILE = os.path.join(config.STATE_DIR, "reslab_state.json")
BACKUP_DIR = os.path.join(config.STATE_DIR, "backups")
COST_FILE = os.path.join(config.STATE_DIR, "costs.json")


def load() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"portfolio": {}, "researchers": {}}


def backup():
    if not os.path.exists(STATE_FILE):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(STATE_FILE, os.path.join(BACKUP_DIR, f"state_{stamp}.json"))
    keep = sorted(os.listdir(BACKUP_DIR))[:-40]
    for old in keep:
        os.remove(os.path.join(BACKUP_DIR, old))


def save(state: dict):
    os.makedirs(config.STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)  # atomic


def load_costs() -> dict:
    """The AI-spend ledger: {"total": usd, "daily": {"YYYY-MM-DD": usd}}."""
    if os.path.exists(COST_FILE):
        with open(COST_FILE) as f:
            return json.load(f)
    return {"total": 0.0, "daily": {}}


def add_cost(usd: float) -> dict:
    """Record one LLM call's cost against today and the running total."""
    if not usd:
        return load_costs()
    costs = load_costs()
    key = date.today().isoformat()
    costs["total"] = costs.get("total", 0.0) + usd
    costs.setdefault("daily", {})
    costs["daily"][key] = costs["daily"].get(key, 0.0) + usd
    os.makedirs(config.STATE_DIR, exist_ok=True)
    tmp = COST_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(costs, f, indent=2)
    os.replace(tmp, COST_FILE)  # atomic
    return costs
