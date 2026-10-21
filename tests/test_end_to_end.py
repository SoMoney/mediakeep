from __future__ import annotations

import asyncio
from pathlib import Path

from mediakeep.config import AppConfig, LibraryConfig
from mediakeep.models import ActionKind, MediaKind, ProgressSink, Severity
from mediakeep.pipeline import apply_plan, build_plan, undo_run
from mediakeep.store import Store


class NullProgress(ProgressSink):
    def stage(self, name: str, total: int | None = None) -> None:
        pass

    def advance(self, n: int = 1, detail: str = "") -> None:
        pass

    def log(self, message: str) -> None:
        pass

    def cancelled(self) -> bool:
        return False


HINT_TEXT = "Orbiters | Orbiters (2003) | show\n"


def write_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        # Unique content by size and path avoids identical fingerprints in tests.
        seed = (str(path) + str(size)).encode("utf-8")
        while f.tell() < size:
            f.write(seed[: min(len(seed), size - f.tell())])
            if f.tell() == 0:
                f.write(b"\0")


def make_tree(root: Path) -> None:
    files = {
        "Incoming/Starfall.2014.IMAX.2160p.10bit.BluRay.AAC5.1.HEVC-x265.mkv": 1000,
        "Incoming/Dustwind (1080p [2023]/Dustwind.2023.1080p.WEB-DL.DDP5.1.H.264.mkv": 1000,
        "Incoming/Dustwind (1080p [2023]/Dustwind.2023.1080p.WEB-DL.DDP5.1.H.264.en.srt": 1000,
        "Incoming/Neon Harbor 2049 (2017)/neon.harbor.2049.2017.2160p.mkv": 1000,
        "Incoming/1917 [2019]/1917.2019.1080p.mkv": 2000,
        "Incoming/1917 [2019]/1917.2019.720p.mkv": 1000,
        "Incoming/Iron Owl Vol 1 (2003)/iron.owl.cd1.avi": 1000,
        "Incoming/Iron Owl Vol 1 (2003)/iron.owl.cd2.avi": 1000,
        "Incoming/Ben Tor/Ben.Tor[1959]hdtv.avi": 1000,
        "Incoming/Comet (1979)/Comet.1986.mkv": 1000,
        "Incoming/Sky Duel Collection/Sky.Duel.1977.mkv": 1000,
        "Incoming/Sky Duel Collection/The.Kingdom.Fights.Back.1980.mkv": 1000,
        "Incoming/Landfall (2016)/Landfall.2016.1080p.mkv": 1000,
        "Incoming/Landfall (2016)/sample-landfall.mkv": 100,
        "Incoming/Landfall (2016)/Subs/English.srt": 1000,
        "Incoming/Landfall (2016)/Subs/Spanish.srt": 1000,
        "Incoming/The Barnhouse 2017 720p BluRay/The.Barnhouse.2017.720p.BluRay.x264.mkv": 1500,
        "Incoming/The.Barnhouse.2017.1080p.m4v": 3000,
        "Incoming/Orbiters/Orbiters - 01 - Outside the Window.mkv": 1000,
        "Incoming/Orbiters/Orbiters - 02 - Return Flight.mkv": 1000,
        "Incoming/Time.Ranger.2005.S01E01.Rose.1080p.WEB-DL.mkv": 1000,
        "Incoming/time.ranger.2005.1x04.robots.of.paris.mkv": 1000,
        "Incoming/Tangerine 1942/VIDEO_TS.IFO": 1000,
        "Incoming/Tangerine 1942/VTS_01_1.VOB": 1000,
        "Incoming/Rowan Pike/Crossfire (2004)/Crossfire.2004.1080p.mkv": 1000,
        "Incoming/Ember (1995)/Ember (1995).mkv": 1000,
    }
    for rel, size in files.items():
        write_file(root / rel, size)

    hint_path = root / "Incoming" / "jf-hints.txt"
    hint_path.parent.mkdir(parents=True, exist_ok=True)
    hint_path.write_bytes(HINT_TEXT.encode("utf-8"))

    (root / "Movies").mkdir(parents=True, exist_ok=True)
    (root / "TV").mkdir(parents=True, exist_ok=True)


def make_config(root: Path, hints_file: str = "") -> AppConfig:
    return AppConfig(
        behavior={"default_destination": "file"},  # these tests file into libraries
        sources=[root / "Incoming"],
        libraries={
            "movies": LibraryConfig(path=root / "Movies", kind=MediaKind.MOVIE),
            "tv": LibraryConfig(path=root / "TV", kind=MediaKind.SHOW),
        },
        hints_file=hints_file,
        state_dir=str(root / "_state"),
    )


def tree(root: Path, skip: set[str] | None = None) -> list[str]:
    skip = skip or {"_state"}
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root)
            if rel.parts and rel.parts[0] in skip:
                continue
            out.append(rel.as_posix())
    return out


def tree_sizes(root: Path, skip: set[str] | None = None) -> dict[str, int]:
    skip = skip or {"_state"}
    out: dict[str, int] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root)
            if rel.parts and rel.parts[0] in skip:
                continue
            out[rel.as_posix()] = p.stat().st_size
    return out


def make_simple_tree(root: Path, files: dict[str, int]) -> None:
    for rel, size in files.items():
        write_file(root / rel, size)
    (root / "Movies").mkdir(parents=True, exist_ok=True)
    (root / "TV").mkdir(parents=True, exist_ok=True)


def test_end_to_end(tmp_path: Path):
    make_tree(tmp_path)
    config = make_config(tmp_path, str(tmp_path / "Incoming" / "jf-hints.txt"))
    original = tree_sizes(tmp_path)

    store = Store(config.state_dir_path() / "mediakeep.db")
    progress = NullProgress()

    plan = asyncio.run(build_plan(config, store, progress))

    attention = [i for i in plan.issues if i.severity == Severity.ATTENTION]
    assert len(attention) == 2

    comet = [i for i in attention if "Comet" in str(i.path) or "Comet" in i.message]
    assert len(comet) == 1
    assert "year" in comet[0].message.lower()

    sky_duel = [
        i
        for i in attention
        if i.suggestion
        and "Sky Duel (1977)" in i.suggestion
        and "The Kingdom Fights Back (1980)" in i.suggestion
    ]
    assert len(sky_duel) == 1

    run = apply_plan(config, store, plan, None, progress)
    assert run.failed == 0

    expected = {
        "Incoming/jf-hints.txt": len(HINT_TEXT.encode("utf-8")),
        "Incoming/Comet (1979)/Comet.1986.mkv": 1000,
        "Incoming/Sky Duel Collection/Sky.Duel.1977.mkv": 1000,
        "Incoming/Sky Duel Collection/The.Kingdom.Fights.Back.1980.mkv": 1000,
        "Movies/Starfall (2014)/Starfall (2014).mkv": 1000,
        "Movies/Dustwind (2023)/Dustwind (2023).mkv": 1000,
        "Movies/Dustwind (2023)/Dustwind (2023).en.srt": 1000,
        "Movies/Neon Harbor 2049 (2017)/Neon Harbor 2049 (2017).mkv": 1000,
        "Movies/1917 (2019)/1917 (2019).mkv": 2000,
        "Movies/1917 (2019)/1917 (2019) - LoRes.mkv": 1000,
        "Movies/Iron Owl Vol 1 (2003)/Iron Owl Vol 1 (2003) - part1.avi": 1000,
        "Movies/Iron Owl Vol 1 (2003)/Iron Owl Vol 1 (2003) - part2.avi": 1000,
        "Movies/Ben Tor (1959)/Ben Tor (1959).avi": 1000,
        "Movies/Landfall (2016)/Landfall (2016).mkv": 1000,
        "Movies/Landfall (2016)/Landfall (2016).en.srt": 1000,
        "Movies/Landfall (2016)/Landfall (2016).es.srt": 1000,
        "Incoming/Landfall (2016)/sample-landfall.mkv": 100,  # samples are left alone (fix 7)
        "Movies/The Barnhouse (2017)/The Barnhouse (2017).m4v": 3000,
        "Movies/The Barnhouse (2017)/The Barnhouse (2017) - LoRes.mkv": 1500,
        "Movies/Tangerine (1942)/VIDEO_TS/VIDEO_TS.IFO": 1000,
        "Movies/Tangerine (1942)/VIDEO_TS/VTS_01_1.VOB": 1000,
        "Movies/Crossfire (2004)/Crossfire (2004).mkv": 1000,
        "Movies/Ember (1995)/Ember (1995).mkv": 1000,
        "TV/Orbiters (2003)/Season 01/Orbiters (2003) S01E01 - Outside the Window.mkv": 1000,
        "TV/Orbiters (2003)/Season 01/Orbiters (2003) S01E02 - Return Flight.mkv": 1000,
        "TV/Time Ranger (2005)/Season 01/Time Ranger (2005) S01E01 - Rose.mkv": 1000,
        "TV/Time Ranger (2005)/Season 01/Time Ranger (2005) S01E04 - Robots Of Paris.mkv": 1000,
    }
    assert tree_sizes(tmp_path) == expected

    script = config.state_dir_path() / "undo" / f"undo-{run.id}.ps1"
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    move_journal = [
        e
        for e in store.get_journal(run.id)
        if e.get("kind") == "move" and e.get("status") == "done"
    ]
    assert move_journal
    for entry in move_journal:
        assert entry["src"] in text
        assert entry["dst"] in text
    assert not any(str(e["src"]).endswith("sample-landfall.mkv") for e in move_journal)  # samples stay (fix 7)

    plan2 = asyncio.run(build_plan(config, store, progress))
    assert plan2.batches == []
    attention2 = [i for i in plan2.issues if i.severity == Severity.ATTENTION]
    assert len(attention2) == 2

    undo = undo_run(config, store, run.id, True, progress)
    assert undo.failed == 0
    assert tree_sizes(tmp_path) == original
    assert not any(
        p.name.startswith("~mk-") and p.suffix == ".tmp"
        for p in tmp_path.rglob("*")
        if p.is_file()
    )


def test_drift_skips_only_changed_batch(tmp_path: Path):
    root = tmp_path
    files = {
        "Incoming/A (2010)/A.2010.1080p.mkv": 100,
        "Incoming/A (2010)/A.2010.720p.mkv": 50,
        "Incoming/B (2011)/B.2011.mkv": 70,
    }
    make_simple_tree(root, files)
    config = make_config(root)
    store = Store(config.state_dir_path() / "mediakeep.db")
    progress = NullProgress()

    plan = asyncio.run(build_plan(config, store, progress))
    assert len(plan.batches) >= 2

    a_batch = next(
        b
        for b in plan.batches
        if any("A (2010)" in str(p.path) for p in b.preconditions)
    )
    changed_pre = a_batch.preconditions[0]
    old_size = changed_pre.size

    with open(changed_pre.path, "ab") as f:
        f.write(b"x")

    run = apply_plan(config, store, plan, None, progress)

    assert run.skipped == 1
    assert any("changed since it was planned" in i.message for i in run.issues)

    a_dir = root / "Incoming/A (2010)"
    assert a_dir.is_dir()
    assert (a_dir / "A.2010.1080p.mkv").exists()
    assert (a_dir / "A.2010.720p.mkv").exists()
    assert changed_pre.path.stat().st_size == old_size + 1

    a_target = root / "Movies/A (2010)"
    if a_target.exists():
        assert not any(p.is_file() for p in a_target.rglob("*"))

    assert (root / "Movies/B (2011)/B (2011).mkv").exists()


def test_collision_fails_batch_and_rolls_back(tmp_path: Path):
    root = tmp_path
    files = {
        "Incoming/C (2012)/C.2012.1080p.mkv": 100,
        "Incoming/C (2012)/C.2012.en.srt": 50,
        "Incoming/D (2013)/D.2013.mkv": 70,
    }
    make_simple_tree(root, files)
    config = make_config(root)
    store = Store(config.state_dir_path() / "mediakeep.db")
    progress = NullProgress()

    plan = asyncio.run(build_plan(config, store, progress))

    c_batch = next(
        b
        for b in plan.batches
        if any(
            a.kind == ActionKind.MOVE and str(a.src).endswith("C.2012.en.srt")
            for a in b.actions
        )
    )
    assert not c_batch.needs_review
    collision_action = next(
        a
        for a in c_batch.actions
        if a.kind == ActionKind.MOVE and str(a.src).endswith("C.2012.en.srt")
    )
    target = collision_action.dst
    assert target is not None

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"do not overwrite")

    run = apply_plan(config, store, plan, None, progress)

    assert run.failed == 1
    assert any("target already exists" in i.message for i in run.issues)
    assert target.read_bytes() == b"do not overwrite"

    assert (root / "Incoming/C (2012)/C.2012.1080p.mkv").exists()
    assert (root / "Incoming/C (2012)/C.2012.en.srt").exists()

    c_target = root / "Movies/C (2012)"
    if c_target.exists():
        files_in = [p for p in c_target.rglob("*") if p.is_file()]
        assert len(files_in) == 1
        assert files_in[0] == target
        assert files_in[0].read_bytes() == b"do not overwrite"

    assert (root / "Movies/D (2013)/D (2013).mkv").exists()
    assert not any(
        p.name.startswith("~mk-") and p.suffix == ".tmp"
        for p in tmp_path.rglob("*")
        if p.is_file()
    )
