"""Tiny key/value settings store (JSON file). Used for runtime toggles like
'registration_open' that admins flip without code change.
"""
from __future__ import annotations
import json
from pathlib import Path

from .db import DATA_ROOT

SETTINGS_PATH = DATA_ROOT / "settings.json"
DEFAULTS = {
    "registration_open": True,
    "min_password_length": 8,
}


def _load() -> dict:
    if not SETTINGS_PATH.exists():
        return dict(DEFAULTS)
    try:
        d = json.loads(SETTINGS_PATH.read_text())
    except Exception:
        return dict(DEFAULTS)
    out = dict(DEFAULTS)
    out.update(d or {})
    return out


def _save(d: dict):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(d, indent=2))


def get_setting(key: str, default=None):
    d = _load()
    return d.get(key, DEFAULTS.get(key, default))


def set_setting(key: str, value):
    d = _load()
    d[key] = value
    _save(d)
    return d
