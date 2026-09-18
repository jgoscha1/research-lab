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
from datetime import datetime

from . import config

STATE_FILE = os.path.join(config.STATE_DIR, "reslab_state.json")
BACKUP_DIR = os.path.join(config.STATE_DIR, "backups")


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
