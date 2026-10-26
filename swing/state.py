"""Picks and positions, stored as JSON in the repo so each workflow run can read the last one's state.

Status flow: pending -> open -> closed, or pending -> cancelled.
"""

import json
import os

from . import config


def load() -> list[dict]:
    if not os.path.exists(config.STATE_FILE):
        return []
    with open(config.STATE_FILE) as f:
        return json.load(f)


def save(positions: list[dict]) -> None:
    os.makedirs(os.path.dirname(config.STATE_FILE), exist_ok=True)
    with open(config.STATE_FILE, "w") as f:
        json.dump(positions, f, indent=2, default=str)


def active(positions: list[dict]) -> list[dict]:
    return [p for p in positions if p["status"] in ("pending", "open")]


def _slots_file() -> str:
    return os.path.join(os.path.dirname(config.STATE_FILE), "runs.json")


def slot_done(day: str, slot: str) -> bool:
    try:
        with open(_slots_file()) as f:
            return slot in json.load(f).get(day, [])
    except FileNotFoundError:
        return False


def mark_slot(day: str, slot: str) -> None:
    try:
        with open(_slots_file()) as f:
            runs = json.load(f)
    except FileNotFoundError:
        runs = {}
    runs.setdefault(day, []).append(slot)
    runs = dict(sorted(runs.items())[-10:])
    with open(_slots_file(), "w") as f:
        json.dump(runs, f, indent=2)
