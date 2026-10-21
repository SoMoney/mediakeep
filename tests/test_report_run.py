from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Sequence

import pytest

from mediakeep import __main__, workspace
from mediakeep.config import AppConfig, LibraryConfig
from mediakeep.models import (
    ActionKind,
    Identity,
    IdStatus,
    MediaKind,
    ProgressSink,
    SourceItem,
    plan_to_json,
)
from mediakeep.pipeline import apply_plan, build_plan, undo_run
from mediakeep.store import Store


class NullProgress(ProgressSink):
    def stage(self, name: str, total: int | None = None) -> None:
        return None

    def advance(self, n: int = 1, detail: str = "") -> None:
        return None

    def log(self, message: str) -> None:
        return None

    def cancelled(self) -> bool:
        return False


def write_file(path: Path, size: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = (str(path) + str(size)).encode("utf-8")
    with open(path, "wb") as f:
        while f.tell() < size:
            f.write(seed[: min(len(seed), size - f.tell())])
            if f.tell() == 0:
                f.write(b"\0")


class FakeIdentifier:
    def __init__(self) -> None:
        self.searched = 0

    async def identify(
        self, items: Sequence[SourceItem], progress: ProgressSink
    ) -> dict[str, Identity]:
        out: dict[str, Identity] = {}
        for item in items:
            progress.advance(1)

            recorded = item.hint_options.get("_tmdb_id")
            if recorded:
                out[item.key] = Identity(
                    item_key=item.key,
                    kind=item.kind,
                    title=item.title,
                    year=item.year,
                    tmdb_id=int(recorded),
                    imdb_id=None,
                    status=IdStatus.CONFIRMED,
                    confidence=100.0,
                    reasons=["recorded id"],
                    candidates=[],
                    library="movies"
                    if item.kind != MediaKind.SHOW
                    else "tv",
                )
                continue

            self.searched += 1
            if item.kind == MediaKind.SHOW:
                out[item.key] = Identity(
                    item_key=item.key,
                    kind=MediaKind.SHOW,
                    title=item.title or "Show",
                    year=item.year or 2010,
                    tmdb_id=2000,
                    imdb_id=None,
                    status=IdStatus.AUTO,
                    confidence=100.0,
                    reasons=["fake"],
                    candidates=[],
                    library="tv",
                )
            else:
                out[item.key] = Identity(
                    item_key=item.key,
                    kind=MediaKind.MOVIE,
                    title=item.title or "Movie",
                    year=item.year or 2020,
                    tmdb_id=1000,
                    imdb_id=None,
                    status=IdStatus.AUTO,
                    confidence=100.0,
                    reasons=["fake"],
                    candidates=[],
                    library="movies",
                )
        return out


def make_tree(root: Path) -> None:
    write_file(root / "Movie A (2020).mkv", 100)
    write_file(root / "Movie B (2021).mkv", 100)
    write_file(root / "TV" / "Show (2010) S01E01.mkv", 100)
    write_file(root / "TV" / "Show (2010) S02E01.mkv", 100)


def make_config(tmp_path: Path) -> AppConfig:
    root = tmp_path / "zstaging"
    movies = tmp_path / "mk-test" / "Movies"
    tv = tmp_path / "mk-test" / "TV"
    make_tree(root)
    movies.mkdir(parents=True, exist_ok=True)
    tv.mkdir(parents=True, exist_ok=True)

    # Work in zstaging and move everything into one destination folder.
    return workspace.effective(
        AppConfig(
            workspace={"folder": root, "destination": movies},
            state_dir=str(tmp_path / "_state"),
        )
    )


def save_config(config: AppConfig, tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    config.save(cfg)
    return cfg


def targets(plan: object) -> set[str]:
    return {
        str(action.dst)
        for batch in getattr(plan, "batches", [])
        for action in getattr(batch, "actions", [])
        if getattr(action, "dst", None) is not None
    }


def test_scan_apply_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = make_config(tmp_path)
    cfg = save_config(config, tmp_path)
    fake = FakeIdentifier()
    monkeypatch.setattr("mediakeep.pipeline.create_identifier", lambda cfg, st, audio: fake)

    rc = __main__.main(["--config", str(cfg), "scan", "--apply"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "APPLYING - changing files now" in out
    assert "changing 3 items" in out
    assert "DRY RUN" not in out
    assert "would change" not in out
    assert "DONE - run " in out
    assert str(config.libraries["destination"].path) in out
    assert str(config.libraries["destination"].path) in out
    assert "Season 01:" in out
    assert "Season 02:" in out

    match = re.search(r"DONE - run (\S+):", out)
    assert match is not None
    run_id = match.group(1)
    assert f"mediakeep undo {run_id} --apply" in out


def test_apply_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = make_config(tmp_path)
    cfg = save_config(config, tmp_path)
    fake = FakeIdentifier()
    monkeypatch.setattr("mediakeep.pipeline.create_identifier", lambda cfg, st, audio: fake)

    from mediakeep import pipeline as pipeline_module

    original_apply = pipeline_module.apply_plan

    def wrapper(config_arg: AppConfig, store: Store, plan: object, item_keys: object, progress: ProgressSink):
        for batch in getattr(plan, "batches", []):
            if batch.kind != MediaKind.MOVIE:
                continue
            for action in batch.actions:
                if action.kind == ActionKind.MOVE and action.dst is not None:
                    action.dst.parent.mkdir(parents=True, exist_ok=True)
                    action.dst.write_bytes(b"collision")
                    break
            break
        return original_apply(config_arg, store, plan, item_keys, progress)

    monkeypatch.setattr("mediakeep.pipeline.apply_plan", wrapper)

    rc = __main__.main(["--config", str(cfg), "scan", "--apply"])
    out = capsys.readouterr().out

    assert rc == 3
    assert "Failed:" in out
    assert "target already exists" in out


def test_second_apply_nothing_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = make_config(tmp_path)
    cfg = save_config(config, tmp_path)
    fake = FakeIdentifier()
    monkeypatch.setattr("mediakeep.pipeline.create_identifier", lambda cfg, st, audio: fake)

    rc = __main__.main(["--config", str(cfg), "scan", "--apply"])
    assert rc == 0
    capsys.readouterr()

    rc = __main__.main(["--config", str(cfg), "scan", "--apply"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "DONE - nothing was changed." in out
    assert "mediakeep undo" not in out


def test_json_apply_and_plain_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = make_config(tmp_path)
    cfg = save_config(config, tmp_path)
    fake = FakeIdentifier()
    monkeypatch.setattr("mediakeep.pipeline.create_identifier", lambda cfg, st, audio: fake)

    rc = __main__.main(["--config", str(cfg), "scan", "--apply", "--json"])
    out = capsys.readouterr().out
    assert rc == 0

    data = json.loads(out)
    assert "plan" in data
    assert "run" in data
    assert len(data["run"]["items"]) == 3
    assert all(item["status"] == "ok" for item in data["run"]["items"])

    rc = __main__.main(["--config", str(cfg), "scan", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    plain = json.loads(out)
    assert isinstance(plain, dict)
    assert "run" not in plain


def test_runs_lists_and_details(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config = make_config(tmp_path)
    cfg = save_config(config, tmp_path)
    fake = FakeIdentifier()
    monkeypatch.setattr("mediakeep.pipeline.create_identifier", lambda cfg, st, audio: fake)

    rc = __main__.main(["--config", str(cfg), "scan", "--apply", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    run_id = json.loads(out)["run"]["id"]

    rc = __main__.main(["--config", str(cfg), "runs"])
    out = capsys.readouterr().out
    assert rc == 0
    assert run_id in out

    rc = __main__.main(["--config", str(cfg), "runs", run_id])
    out = capsys.readouterr().out
    assert rc == 0
    assert str(config.libraries["destination"].path) in out
    assert str(config.libraries["destination"].path) in out


def test_default_plan_has_no_id_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "src"
    root.mkdir(parents=True, exist_ok=True)
    write_file(root / "Solstice (2015).mkv", 100)

    config = AppConfig(
        behavior={"default_destination": "file"},  # these tests file into libraries
        sources=[root],
        libraries={
            "movies": LibraryConfig(path=tmp_path / "Movies", kind=MediaKind.MOVIE),
            "tv": LibraryConfig(path=tmp_path / "TV", kind=MediaKind.SHOW),
        },
        state_dir=str(tmp_path / "_state"),
    )
    config.libraries["movies"].path.mkdir(parents=True, exist_ok=True)
    config.libraries["tv"].path.mkdir(parents=True, exist_ok=True)

    store = Store(config.state_dir_path() / "mediakeep.db")
    fake = FakeIdentifier()
    monkeypatch.setattr("mediakeep.pipeline.create_identifier", lambda cfg, st, audio: fake)

    plan = asyncio.run(build_plan(config, store, NullProgress()))
    expected = (
        config.libraries["movies"].path
        / "Solstice (2015)"
        / "Solstice (2015).mkv"
    )
    assert str(expected) in targets(plan)
    assert "[tmdbid-" not in plan_to_json(plan)


def test_tagged_library_converges_without_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    movies = tmp_path / "Movies"
    tv = tmp_path / "TV"
    movies.mkdir(parents=True, exist_ok=True)
    tv.mkdir(parents=True, exist_ok=True)

    tagged_dir = movies / "Solstice (2015) [tmdbid-273481]"
    tagged_file = tagged_dir / "Solstice (2015) [tmdbid-273481].mkv"
    write_file(tagged_file, 100)

    config = AppConfig(
        behavior={"default_destination": "file"},  # these tests file into libraries
        sources=[],
        libraries={
            "movies": LibraryConfig(path=movies, kind=MediaKind.MOVIE),
            "tv": LibraryConfig(path=tv, kind=MediaKind.SHOW),
        },
        state_dir=str(tmp_path / "_state"),
    )
    store = Store(config.state_dir_path() / "mediakeep.db")
    fake = FakeIdentifier()
    monkeypatch.setattr("mediakeep.pipeline.create_identifier", lambda cfg, st, audio: fake)

    plan = asyncio.run(build_plan(config, store, NullProgress()))
    assert fake.searched == 0
    expected = movies / "Solstice (2015)" / "Solstice (2015).mkv"
    assert str(expected) in targets(plan)

    run = apply_plan(config, store, plan, None, NullProgress())
    assert run.failed == 0
    assert not tagged_file.exists()

    plan2 = asyncio.run(build_plan(config, store, NullProgress()))
    assert fake.searched == 0
    assert plan2.batches == []

    undo = undo_run(config, store, run.id, True, NullProgress())
    assert undo.failed == 0
    assert tagged_file.exists()


def make_tagged_show_tree(root: Path) -> Path:
    show = root / "Skyport Meridian (2004) [tmdbid-2290]"
    write_file(
        show / "Season 04" / "Skyport Meridian (2004) S04E01 - Adrift.mkv",
        100,
    )
    write_file(
        show / "Season 05" / "Skyport Meridian (2004) S05E01 - Search and Rescue.mkv",
        100,
    )
    return show


def make_tagged_show_config(tmp_path: Path) -> AppConfig:
    tv = tmp_path / "mk-test" / "TV"
    movies = tmp_path / "mk-test" / "Movies"
    tv.mkdir(parents=True, exist_ok=True)
    movies.mkdir(parents=True, exist_ok=True)
    make_tagged_show_tree(tv)

    # Tidy the TV folder where it is.
    return workspace.effective(
        AppConfig(workspace={"folder": tv}, state_dir=str(tmp_path / "_state"))
    )


def test_tagged_show_converges_with_no_leftovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_tagged_show_config(tmp_path)
    cfg = save_config(config, tmp_path)
    fake = FakeIdentifier()
    monkeypatch.setattr(
        "mediakeep.pipeline.create_identifier", lambda cfg, st, audio: fake
    )
    store = Store(config.state_dir_path() / "mediakeep.db")

    plan = asyncio.run(build_plan(config, store, NullProgress()))
    batch = next(b for b in plan.batches if b.kind == MediaKind.SHOW)
    rmdirs = [
        action.src
        for action in batch.actions
        if action.kind == ActionKind.RMDIR and action.src is not None
    ]
    show = config.libraries["destination"].path / "Skyport Meridian (2004) [tmdbid-2290]"

    assert len(rmdirs) == 3
    assert {r.name for r in rmdirs[:2]} == {"Season 04", "Season 05"}
    assert all(r.parent == show for r in rmdirs[:2])
    assert rmdirs[2] == show

    rc = __main__.main(["--config", str(cfg), "scan", "--apply"])
    out = capsys.readouterr().out

    assert rc == 0
    assert not show.exists()
    assert "Removed empty folders: 3" in out

    plan2 = asyncio.run(build_plan(config, store, NullProgress()))
    assert plan2.batches == []


def test_tagged_show_nonempty_at_apply_is_kept_and_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_tagged_show_config(tmp_path)
    cfg = save_config(config, tmp_path)
    fake = FakeIdentifier()
    monkeypatch.setattr(
        "mediakeep.pipeline.create_identifier", lambda cfg, st, audio: fake
    )

    from mediakeep import pipeline as pipeline_module

    original_apply = pipeline_module.apply_plan

    def wrapper(config_arg: AppConfig, store: Store, plan: object, item_keys: object, progress: ProgressSink):
        for batch in getattr(plan, "batches", []):
            if batch.kind != MediaKind.SHOW:
                continue
            for action in batch.actions:
                if (
                    action.kind == ActionKind.RMDIR
                    and action.src is not None
                    and action.src.name == "Season 04"
                ):
                    action.src.mkdir(parents=True, exist_ok=True)
                    (action.src / "notes.txt").write_bytes(b"note")
                    break
            break
        return original_apply(config_arg, store, plan, item_keys, progress)

    monkeypatch.setattr("mediakeep.pipeline.apply_plan", wrapper)

    rc = __main__.main(["--config", str(cfg), "scan", "--apply"])
    out = capsys.readouterr().out

    assert rc == 0

    show = config.libraries["destination"].path / "Skyport Meridian (2004) [tmdbid-2290]"
    target = config.libraries["destination"].path / "Skyport Meridian (2004)"

    assert show.exists()
    assert (show / "Season 04").exists()
    assert (show / "Season 04" / "notes.txt").exists()
    assert not (show / "Season 05").exists()

    assert (
        target / "Season 04" / "Skyport Meridian (2004) S04E01 - Adrift.mkv"
    ).exists()
    assert (
        target
        / "Season 05"
        / "Skyport Meridian (2004) S05E01 - Search and Rescue.mkv"
    ).exists()

    assert "Removed empty folders: 1" in out
    assert "Kept folders that weren't empty:" in out

    kept_section = out.split("Kept folders that weren't empty:", 1)[1]
    assert "Season 04" in kept_section
    assert "[tmdbid-2290]" in kept_section


def test_tagged_show_plan_time_notes_prevents_rmdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_tagged_show_config(tmp_path)
    show = config.libraries["destination"].path / "Skyport Meridian (2004) [tmdbid-2290]"
    write_file(show / "Season 04" / "notes.txt", 10)

    fake = FakeIdentifier()
    monkeypatch.setattr(
        "mediakeep.pipeline.create_identifier", lambda cfg, st, audio: fake
    )
    store = Store(config.state_dir_path() / "mediakeep.db")

    plan = asyncio.run(build_plan(config, store, NullProgress()))
    batch = next(b for b in plan.batches if b.kind == MediaKind.SHOW)
    rmdirs = [
        action.src
        for action in batch.actions
        if action.kind == ActionKind.RMDIR and action.src is not None
    ]

    assert all(not str(src).endswith("Season 04") for src in rmdirs)
    assert all(src != show for src in rmdirs)
    assert any(str(src).endswith("Season 05") for src in rmdirs)


def test_tagged_show_undo_restores_tagged_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_tagged_show_config(tmp_path)
    cfg = save_config(config, tmp_path)
    fake = FakeIdentifier()
    monkeypatch.setattr(
        "mediakeep.pipeline.create_identifier", lambda cfg, st, audio: fake
    )

    rc = __main__.main(["--config", str(cfg), "scan", "--apply"])
    assert rc == 0
    capsys.readouterr()

    store = Store(config.state_dir_path() / "mediakeep.db")
    runs = store.list_runs()
    assert runs

    undo = undo_run(config, store, runs[0].id, True, NullProgress())
    assert undo.failed == 0

    show = config.libraries["destination"].path / "Skyport Meridian (2004) [tmdbid-2290]"
    target = config.libraries["destination"].path / "Skyport Meridian (2004)"

    assert (
        show / "Season 04" / "Skyport Meridian (2004) S04E01 - Adrift.mkv"
    ).exists()
    assert (
        show / "Season 05" / "Skyport Meridian (2004) S05E01 - Search and Rescue.mkv"
    ).exists()
    assert not (
        target / "Season 04" / "Skyport Meridian (2004) S04E01 - Adrift.mkv"
    ).exists()


def test_scan_progress_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(tmp_path)
    cfg = save_config(config, tmp_path)
    fake = FakeIdentifier()
    monkeypatch.setattr(
        "mediakeep.pipeline.create_identifier", lambda cfg, st, audio: fake
    )

    rc = __main__.main(["--config", str(cfg), "scan"])
    out = capsys.readouterr().out

    assert rc == 0
    for root in config.root_paths:
        assert f"scan: {root}" in out
