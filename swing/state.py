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
