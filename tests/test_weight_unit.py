"""Weight-unit resolution: env override, SmartGym's prefs plist, and the kg fallback."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from smartgym_mcp import config


def _plist(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "prefs.plist"
    path.write_bytes(plistlib.dumps(payload))
    return path


def test_prefs_key_2_is_lb(tmp_path: Path) -> None:
    assert config.weight_unit_from_prefs(_plist(tmp_path, {"currentWeightUnitKey": 2})) == "lb"


@pytest.mark.parametrize("value", [0, 1, 3, "2"])
def test_other_prefs_values_fall_back_to_kg(tmp_path: Path, value: object) -> None:
    prefs = _plist(tmp_path, {"currentWeightUnitKey": value})
    assert config.weight_unit_from_prefs(prefs) == "kg"


def test_missing_key_missing_file_and_garbage_fall_back_to_kg(tmp_path: Path) -> None:
    assert config.weight_unit_from_prefs(_plist(tmp_path, {"other": 1})) == "kg"
    assert config.weight_unit_from_prefs(tmp_path / "nope.plist") == "kg"
    garbage = tmp_path / "garbage.plist"
    garbage.write_bytes(b"not a plist")
    assert config.weight_unit_from_prefs(garbage) == "kg"
    assert config.weight_unit_from_prefs(_plist(tmp_path, [2])) == "kg"


@pytest.mark.parametrize(("env", "expected"), [("lb", "lb"), ("KG", "kg"), (" Lb ", "lb")])
def test_env_override_wins_over_prefs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str, expected: str
) -> None:
    monkeypatch.setenv("SMARTGYM_WEIGHT_UNIT", env)
    prefs = _plist(tmp_path, {"currentWeightUnitKey": 2 if expected == "kg" else 0})
    assert config.resolve_weight_unit(prefs) == expected


def test_invalid_env_value_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMARTGYM_WEIGHT_UNIT", "pounds")
    with pytest.raises(ValueError, match="SMARTGYM_WEIGHT_UNIT"):
        config.resolve_weight_unit()


def test_no_env_uses_prefs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMARTGYM_WEIGHT_UNIT", raising=False)
    assert config.resolve_weight_unit(_plist(tmp_path, {"currentWeightUnitKey": 2})) == "lb"
