"""Configuration: resolve environment overrides and validate the DB exists.

Pure resolution (`load_config`) is separated from the filesystem check
(`validate_db_exists`) so the former is usable in tests with no DB present.
"""

from __future__ import annotations

import os
import plistlib
from dataclasses import dataclass
from pathlib import Path

from .models import WeightUnit

DEFAULT_DB_PATH = (
    "~/Library/Containers/com.smartgymapp.smartgym/Data/Documents/GymModel.sqlite"
)
DEFAULT_APP_BUNDLE = "/Applications/SmartGym.app"
DEFAULT_BACKUP_DIR = "~/.smartgym-mcp/backups"
# SmartGym's own preferences; only ever read (plistlib), never written.
DEFAULT_PREFS_PATH = (
    "~/Library/Containers/com.smartgymapp.smartgym/Data/Library/Preferences/"
    "com.smartgymapp.smartgym.plist"
)
_PREFS_WEIGHT_UNIT_KEY = "currentWeightUnitKey"
_PREFS_WEIGHT_UNIT_LB = 2  # observed: stored kg converts to the whole lb the app shows

_TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    db_path: Path
    app_bundle: Path
    backup_dir: Path
    allow_write_while_running: bool
    weight_unit: WeightUnit = "kg"


def _resolve(value: str) -> Path:
    return Path(value).expanduser().resolve()


def weight_unit_from_prefs(prefs_path: Path) -> WeightUnit:
    """SmartGym's display unit from its prefs plist: 2 → "lb"; anything else or
    unreadable → "kg". Read-only."""
    try:
        with prefs_path.open("rb") as f:
            prefs = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return "kg"
    if not isinstance(prefs, dict):
        return "kg"
    return "lb" if prefs.get(_PREFS_WEIGHT_UNIT_KEY) == _PREFS_WEIGHT_UNIT_LB else "kg"


def resolve_weight_unit(prefs_path: Path | None = None) -> WeightUnit:
    """SMARTGYM_WEIGHT_UNIT (lb|kg) wins; else the app's preference; else kg."""
    env = os.environ.get("SMARTGYM_WEIGHT_UNIT", "").strip().lower()
    if env == "lb":
        return "lb"
    if env == "kg":
        return "kg"
    if env:
        raise ValueError(f"SMARTGYM_WEIGHT_UNIT must be 'lb' or 'kg', got {env!r}.")
    return weight_unit_from_prefs(prefs_path or _resolve(DEFAULT_PREFS_PATH))


def load_config() -> Config:
    """Read env overrides with spec defaults. No filesystem writes; the only read is
    SmartGym's prefs plist for the weight unit (skipped when SMARTGYM_WEIGHT_UNIT is set)."""
    return Config(
        db_path=_resolve(os.environ.get("SMARTGYM_DB_PATH", DEFAULT_DB_PATH)),
        app_bundle=_resolve(os.environ.get("SMARTGYM_APP_BUNDLE", DEFAULT_APP_BUNDLE)),
        backup_dir=_resolve(os.environ.get("SMARTGYM_BACKUP_DIR", DEFAULT_BACKUP_DIR)),
        allow_write_while_running=os.environ.get("SMARTGYM_ALLOW_WRITE_WHILE_RUNNING", "")
        .strip()
        .lower()
        in _TRUTHY,
        weight_unit=resolve_weight_unit(),
    )


def validate_db_exists(cfg: Config) -> None:
    """Loud, actionable startup check that the SmartGym DB is reachable."""
    if not cfg.db_path.exists():
        raise FileNotFoundError(
            f"SmartGym DB not found at {cfg.db_path}. "
            "Is SmartGym installed? Override the location with SMARTGYM_DB_PATH."
        )
