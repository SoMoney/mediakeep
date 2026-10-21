from __future__ import annotations

from pathlib import Path

from mediakeep.models import Action, ActionKind
from mediakeep.plan import validate_batch


def test_case_only_rename_allowed(tmp_path: Path):
    src = tmp_path / "File.mkv"
    dst = tmp_path / "file.mkv"
    src.write_text("x", encoding="utf-8")
    actions = [Action(kind=ActionKind.MOVE, src=src, dst=dst)]
    ok, err = validate_batch(actions, set())
    assert ok, err


def test_swap_allowed(tmp_path: Path):
    a = tmp_path / "a.mkv"
    b = tmp_path / "b.mkv"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")
    actions = [
        Action(kind=ActionKind.MOVE, src=a, dst=b),
        Action(kind=ActionKind.MOVE, src=b, dst=a),
    ]
    ok, err = validate_batch(actions, set())
    assert ok, err


def test_claimed_target_rejected(tmp_path: Path):
    src = tmp_path / "a.mkv"
    dst = tmp_path / "b.mkv"
    src.write_text("a", encoding="utf-8")
    actions = [Action(kind=ActionKind.MOVE, src=src, dst=dst)]
    claimed = {str(dst).lower()}
    ok, err = validate_batch(actions, claimed)
    assert not ok
    assert "claimed" in (err or "")
