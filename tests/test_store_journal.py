from __future__ import annotations

from datetime import datetime
from pathlib import Path

from mediakeep.apply import write_undo_script
from mediakeep.models import RunRecord
from mediakeep.store import Store


def test_journal_and_undo_script(tmp_path: Path):
    store = Store(tmp_path / "state" / "mediakeep.db")
    run = RunRecord(
        id="r1",
        plan_id=None,
        kind="apply",
        started_at=datetime.now(),
        finished_at=datetime.now(),
        status="ok",
        done=1,
        failed=0,
        skipped=0,
    )
    store.create_run(run)
    store.add_journal("r1", 1, "move", str(tmp_path / "a.mkv"), str(tmp_path / "b.mkv"), None, False, "done")
    store.add_journal("r1", 2, "rmdir", str(tmp_path / "old"), None, None, False, "done")
    write_undo_script(store, "r1", store.get_journal("r1"))
    script = (tmp_path / "state" / "undo" / "undo-r1.ps1").read_text(encoding="utf-8")
    assert "b.mkv" in script
    assert "a.mkv" in script
