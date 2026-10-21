from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Sequence

import pytest

from mediakeep import __main__, workspace
from mediakeep.config import AppConfig, NamingConfig
from mediakeep.models import (
    ActionKind,
    Candidate,
    Identity,
    IdStatus,
    MediaKind,
    ProgressSink,
    Severity,
    SourceItem,
)
from mediakeep.parse import normalize_title
from mediakeep.pipeline import apply_plan, build_plan
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


MOVIE_NAMES = [
    "Kitchen.Witchery.2.2026.1080P.Ma.Web-Dl.Ddp5.1.Atmos.H.264-Astrid.mp4",
    "Road.Rangers.3.2026.1080P.Ma.Web-Dl.Ddp5.1.Atmos.H.264-Byndr.mp4",
    "The.Stowaway.2026.1080P.Amzn.Web-Dl.Ddp5.1.H.264-Cinepth.mp4",
    "What We Do in the Attic (2014).mp4",
]

BANTAM_TITLES = [
    "Release.The.Brown.Fat",
    "Trousers",
    "Episode.Three",
    "Episode.Four",
    "Episode.Five",
    "Episode.Six",
    "Episode.Seven",
    "Episode.Eight",
    "Episode.Nine",
    "Episode.Ten",
]

GULLWING_TITLES = [
    "Welcome.To.Gullwings.Bay",
    "Lodging",
    "Episode.Three",
    "Episode.Four",
    "Episode.Five",
    "Episode.Six",
    "Episode.Seven",
    "Episode.Eight",
    "Episode.Nine",
    "Episode.Ten",
]

MOVIE_MAP: dict[str, tuple[str, int, int]] = {
    normalize_title("Kitchen Witchery 2"): ("Kitchen Witchery 2", 2026, 1302904),
    normalize_title("Road Rangers 3"): ("Road Rangers 3", 2026, 675871),
    normalize_title("The Stowaway"): ("The Stowaway", 2026, 1554956),
    normalize_title("What We Do in the Attic"): (
        "What We Do in the Attic",
        2014,
        246741,
    ),
}

BANTAM_ID = 1001
GULLWING_ID = 1002


class FakeIdentifier:
    def __init__(self, auto_shows: bool = False) -> None:
        self.auto_shows = auto_shows
        self.searched = 0

    def _library(self, kind: MediaKind) -> str:
        return "tv" if kind == MediaKind.SHOW else "movies"

    def _candidate(
        self,
        title: str,
        year: int | None,
        tmdb_id: int,
        kind: MediaKind,
        score: float,
        reasons: list[str],
    ) -> Candidate:
        return Candidate(
            tmdb_id=tmdb_id,
            kind=kind,
            title=title,
            original_title=title,
            year=year,
            original_language="en",
            runtime_min=None,
            genres=[],
            countries=[],
            overview="",
            poster_url=None,
            imdb_id=None,
            score=score,
            reasons=reasons,
        )

    async def identify(
        self, items: Sequence[SourceItem], progress: ProgressSink
    ) -> dict[str, Identity]:
        out: dict[str, Identity] = {}
        for item in items:
            progress.advance(1)
            if progress.cancelled():
                break

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
                    library=self._library(item.kind),
                )
                continue

            self.searched += 1

            if item.kind in (MediaKind.MOVIE, MediaKind.DVD, MediaKind.BLURAY):
                data = MOVIE_MAP.get(normalize_title(item.title))
                if data:
                    title, year, tmdb_id = data
                    candidate = self._candidate(
                        title, year, tmdb_id, MediaKind.MOVIE, 100.0, ["test"]
                    )
                    out[item.key] = Identity(
                        item_key=item.key,
                        kind=MediaKind.MOVIE,
                        title=title,
                        year=year,
                        tmdb_id=tmdb_id,
                        imdb_id=None,
                        status=IdStatus.AUTO,
                        confidence=100.0,
                        reasons=["test"],
                        candidates=[candidate],
                        library="movies",
                    )
                    continue

            if item.kind == MediaKind.SHOW:
                norm = normalize_title(item.title)
                if norm == "bantam":
                    out[item.key] = self._show_identity(item, "Bantam", BANTAM_ID)
                    continue
                if norm == "gullwingsbay":
                    out[item.key] = self._show_identity(item, "Gullwings Bay", GULLWING_ID)
                    continue

            out[item.key] = Identity(
                item_key=item.key,
                kind=item.kind,
                title=item.title,
                year=item.year,
                tmdb_id=None,
                imdb_id=None,
                status=IdStatus.NEEDS_REVIEW,
                confidence=0.0,
                reasons=["unknown"],
                candidates=[],
                library=self._library(item.kind),
            )
        return out

    def _show_identity(self, item: SourceItem, title: str, tmdb_id: int) -> Identity:
        if self.auto_shows:
            candidate = self._candidate(
                title, 2026, tmdb_id, MediaKind.SHOW, 95.0, ["test"]
            )
            return Identity(
                item_key=item.key,
                kind=MediaKind.SHOW,
                title=title,
                year=2026,
                tmdb_id=tmdb_id,
                imdb_id=None,
                status=IdStatus.AUTO,
                confidence=95.0,
                reasons=["test"],
                candidates=[candidate],
                library="tv",
            )

        candidates = [
            self._candidate(
                title,
                2026,
                tmdb_id,
                MediaKind.SHOW,
                78.0,
                ["title 100%, no year to compare, kind matches"],
            ),
            self._candidate(
                f"{title} (Other)",
                2025,
                tmdb_id + 1,
                MediaKind.SHOW,
                55.0,
                ["lower score"],
            ),
        ]
        return Identity(
            item_key=item.key,
            kind=MediaKind.SHOW,
            title=item.title,
            year=None,
            tmdb_id=None,
            imdb_id=None,
            status=IdStatus.NEEDS_REVIEW,
            confidence=78.0,
            reasons=["needs review"],
            candidates=candidates,
            library="tv",
        )


def write_file(path: Path, size: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = (str(path) + str(size)).encode("utf-8")
    with open(path, "wb") as f:
        while f.tell() < size:
            f.write(seed[: min(len(seed), size - f.tell())])
            if f.tell() == 0:
                f.write(b"\0")


def build_tree(root: Path) -> None:
    for name in MOVIE_NAMES:
        write_file(root / name, 100)

    for i, title in enumerate(BANTAM_TITLES, start=1):
        write_file(
            root
            / "TV"
            / f"Bantam.S01e{i:02d}.{title}.1080P.Atvp.Web-Dl.Ddp5.1.Atmos.H.264-Rawr.mp4",
            100,
        )

    for i, title in enumerate(GULLWING_TITLES, start=1):
        write_file(
            root
            / "TV"
            / f"Gullwings.Bay.S01e{i:02d}.{title}.1080P.Atvp.Web-Dl.Ddp5.1.Atmos.H.264-Rawr.mp4",
            100,
        )

    write_file(root / "TV" / "tv_merge.sh", 10)


def make_config(tmp_path: Path) -> AppConfig:
    root = tmp_path / "zstaging"
    movies = tmp_path / "mk-test" / "Movies"
    tv = tmp_path / "mk-test" / "TV"

    build_tree(root)
    movies.mkdir(parents=True, exist_ok=True)
    tv.mkdir(parents=True, exist_ok=True)

    # Work in zstaging and move everything into one destination folder.
    return workspace.effective(
        AppConfig(
            workspace={"folder": root, "destination": movies},
            naming=NamingConfig(id_tag="tmdb", year_style="parens"),
            state_dir=str(tmp_path / "_state"),
        )
    )


def build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auto_shows: bool = False):
    config = make_config(tmp_path)
    store = Store(config.state_dir_path() / "mediakeep.db")
    fake = FakeIdentifier(auto_shows=auto_shows)
    monkeypatch.setattr(
        "mediakeep.pipeline.create_identifier",
        lambda cfg, st, audio: fake,
    )
    return config, store, fake


def targets(plan: Any) -> set[str]:
    return {
        str(action.dst)
        for batch in plan.batches
        for action in batch.actions
        if action.dst is not None
    }


def _simple_config(tmp_path: Path) -> AppConfig:
    root = tmp_path / "src"
    root.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Movies").mkdir(parents=True, exist_ok=True)
    (tmp_path / "TV").mkdir(parents=True, exist_ok=True)
    return workspace.effective(
        AppConfig(
            workspace={"folder": root, "destination": tmp_path / "Movies"},
            state_dir=str(tmp_path / "_state"),
        )
    )


def test_movie_targets_carry_identity_and_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, store, _fake = build(tmp_path, monkeypatch, auto_shows=False)
    plan = asyncio.run(build_plan(config, store, NullProgress()))
    got = targets(plan)
    lib = config.libraries["destination"].path

    for title, tid in (
        ("Kitchen Witchery 2 (2026)", 1302904),
        ("Road Rangers 3 (2026)", 675871),
        ("The Stowaway (2026)", 1554956),
        ("What We Do in the Attic (2014)", 246741),
    ):
        folder = f"{title} [tmdbid-{tid}]"
        assert str(lib / folder / f"{folder}.mp4") in got


def test_two_show_items_not_one_tv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, store, _fake = build(tmp_path, monkeypatch, auto_shows=False)
    plan = asyncio.run(build_plan(config, store, NullProgress()))

    show_batches = [b for b in plan.batches if b.kind == MediaKind.SHOW]
    assert len(show_batches) == 2
    assert {b.label for b in show_batches} == {"Bantam", "Gullwings Bay"}
    assert all(b.needs_review for b in show_batches)


def test_auto_show_targets_without_episode_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, _fake = build(tmp_path, monkeypatch, auto_shows=True)
    plan = asyncio.run(build_plan(config, store, NullProgress()))
    got = targets(plan)
    lib = config.libraries["destination"].path

    bantam_folder = f"Bantam (2026) [tmdbid-{BANTAM_ID}]"
    bantam_target = (
        lib
        / bantam_folder
        / "Season 01"
        / "Bantam (2026) S01E01 - Release The Brown Fat.mp4"
    )
    assert str(bantam_target) in got

    gullwing_folder = f"Gullwings Bay (2026) [tmdbid-{GULLWING_ID}]"
    gullwing_target = (
        lib
        / gullwing_folder
        / "Season 01"
        / "Gullwings Bay (2026) S01E01 - Welcome To Gullwings Bay.mp4"
    )
    assert str(gullwing_target) in got

    for batch in plan.batches:
        if batch.kind != MediaKind.SHOW:
            continue
        for action in batch.actions:
            if action.dst is not None:
                assert "[tmdbid-" not in action.dst.name


def test_tv_merge_sh_is_note_and_not_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, _fake = build(tmp_path, monkeypatch, auto_shows=False)
    plan = asyncio.run(build_plan(config, store, NullProgress()))

    notes = [
        issue
        for issue in plan.issues
        if issue.severity == Severity.NOTE and issue.path.name == "tv_merge.sh"
    ]
    assert notes

    moved = [
        action
        for batch in plan.batches
        for action in batch.actions
        if action.src is not None and action.src.name == "tv_merge.sh"
    ]
    assert moved == []


def test_no_messages_mention_parts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, store, _fake = build(tmp_path, monkeypatch, auto_shows=False)
    plan = asyncio.run(build_plan(config, store, NullProgress()))

    messages: list[str] = []
    for issue in plan.issues:
        messages.append(issue.message)
        if issue.suggestion:
            messages.append(issue.suggestion)
    for batch in plan.batches:
        messages.extend(batch.reasons)
    for identity in plan.identities.values():
        messages.extend(identity.reasons)
        for candidate in identity.candidates:
            messages.extend(candidate.reasons)

    assert not any("part 2" in m.lower() for m in messages)


def test_idempotency_with_tags_does_not_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, fake = build(tmp_path, monkeypatch, auto_shows=True)
    plan = asyncio.run(build_plan(config, store, NullProgress()))
    run = apply_plan(config, store, plan, None, NullProgress())
    assert run.failed == 0

    fake.searched = 0
    plan2 = asyncio.run(build_plan(config, store, NullProgress()))
    assert plan2.batches == []
    assert fake.searched == 0


def test_scan_json_stdout_is_pure_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, _store, _fake = build(tmp_path, monkeypatch, auto_shows=False)
    cfg_path = tmp_path / "config.yaml"
    config.save(cfg_path)

    rc = __main__.main(["--config", str(cfg_path), "scan", "--json"])
    captured = capsys.readouterr()

    assert rc == 0
    data = json.loads(captured.out)
    assert isinstance(data, dict)
    assert "config:" in captured.err
    assert "config:" not in captured.out.split("\n")[0]

    target = (
        config.libraries["destination"].path
        / "Kitchen Witchery 2 (2026) [tmdbid-1302904]"
        / "Kitchen Witchery 2 (2026) [tmdbid-1302904].mp4"
    )
    dsts = {
        str(action["dst"])
        for batch in data.get("batches", [])
        for action in batch.get("actions", [])
        if action.get("dst")
    }
    assert str(target) in dsts


def test_text_report_shows_moves_and_review_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, _store, _fake = build(tmp_path, monkeypatch, auto_shows=False)
    cfg_path = tmp_path / "config.yaml"
    config.save(cfg_path)

    rc = __main__.main(["--config", str(cfg_path), "scan"])
    captured = capsys.readouterr()

    assert rc == 0
    out = captured.out
    assert "DRY RUN" in out
    assert "Waiting for your review" in out
    assert "Bantam" in out
    assert "score 78" in out
    assert "Kitchen.Witchery.2.2026" in out
    assert "[tmdbid-1302904]" in out


def test_nested_config_error_is_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    zstaging = tmp_path / "zstaging"
    zstaging.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
workspace:
  folder: '{zstaging.as_posix()}'
  destination: '{zstaging.as_posix()}/Movies'
""",
        encoding="utf-8",
    )

    rc = __main__.main(["--config", str(cfg), "scan"])
    captured = capsys.readouterr()

    assert rc == 2
    combined = captured.out + captured.err
    assert "config error in" in combined
    assert "can't be inside the working folder" in combined
    assert "Traceback" not in combined


def test_only_outside_roots_and_trailing_quote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _store, _fake = build(tmp_path, monkeypatch, auto_shows=False)
    cfg_path = tmp_path / "config.yaml"
    config.save(cfg_path)

    rc = __main__.main(
        ["--config", str(cfg_path), "scan", "--only", str(tmp_path / "outside")]
    )
    captured = capsys.readouterr()
    assert rc == 2
    combined = captured.out + captured.err
    assert "is not inside the working folder or its destination" in combined
    assert str(config.sources[0]) in combined

    rc = __main__.main(
        ["--config", str(cfg_path), "scan", "--only", str(config.sources[0]) + '"']
    )
    assert rc == 0


def test_loose_sample_next_to_movie_is_left_alone(tmp_path: Path) -> None:
    config = _simple_config(tmp_path)
    root = config.sources[0]
    movie = root / "Movie.2020.1080p.mkv"
    sample = root / "!Sample Movie.2020.1080p.mkv"
    write_file(movie, 100)
    write_file(sample, 50)

    store = Store(config.state_dir_path() / "mediakeep.db")
    plan = asyncio.run(build_plan(config, store, NullProgress()))

    movies = [item for item in plan.items.values() if item.kind == MediaKind.MOVIE]
    assert len(movies) == 1
    assert sample in plan.samples

    moved = [
        action
        for batch in plan.batches
        for action in batch.actions
        if action.src == sample
    ]
    assert moved == []
    assert not any(
        "Sample" in str(action.dst)
        for batch in plan.batches
        for action in batch.actions
        if action.dst is not None
    )


def test_multi_version_movie_versions_not_parts(tmp_path: Path) -> None:
    config = _simple_config(tmp_path)
    root = config.sources[0]
    write_file(root / "A (2010)" / "A.2010.1080p.mkv", 100)
    write_file(root / "A (2010)" / "A.2010.720p.mkv", 50)
    write_file(root / "B (2011)" / "B.2011.part1.mkv", 100)
    write_file(root / "B (2011)" / "B.2011.part2.mkv", 100)

    store = Store(config.state_dir_path() / "mediakeep.db")
    plan = asyncio.run(build_plan(config, store, NullProgress()))

    assert any("A (2010)" in label for label in plan.multi_version)
    assert all("B (2011)" not in label for label in plan.multi_version)


def test_multi_version_show_only_duplicate_episode(tmp_path: Path) -> None:
    config = _simple_config(tmp_path)
    root = config.sources[0] / "TV"
    write_file(root / "Show.S01E01.a.mkv", 100)
    write_file(root / "Show.S01E01.b.mkv", 80)
    write_file(root / "Show.S01E02.mkv", 100)

    store = Store(config.state_dir_path() / "mediakeep.db")
    plan = asyncio.run(build_plan(config, store, NullProgress()))

    assert any("Show S01E01 (2 versions)" in label for label in plan.multi_version)
    assert not any("S01E02" in label for label in plan.multi_version)


def test_loose_sample_is_listed_and_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fix 7 item 2: a loose sample is never planned and is always listed."""
    config = _simple_config(tmp_path)
    root = config.sources[0]
    movie = root / "Dr.Moonbeam.1964.1080P.Bluray.Ac3.X264-Hdc.mp4"
    sample = root / "!Sample Dr.Moonbeam.1964.1080P.Bluray.Ac3.X264-Hdc.mp4"
    write_file(movie, 100)
    write_file(sample, 50)

    store = Store(config.state_dir_path() / "mediakeep.db")
    plan = asyncio.run(build_plan(config, store, NullProgress()))

    assert sample in plan.samples
    assert not [
        action
        for batch in plan.batches
        for action in batch.actions
        if action.src == sample
    ]

    cfg_path = tmp_path / "config.yaml"
    config.save(cfg_path)
    __main__.main(["--config", str(cfg_path), "scan"])
    out = capsys.readouterr().out

    assert "Samples (left alone)" in out
    assert "!Sample" in out


def test_sample_folder_in_a_movie_folder_is_never_planned(tmp_path: Path) -> None:
    """Fix 7 item 2: the movie moves, the sample and its folders stay."""
    config = _simple_config(tmp_path)
    root = config.sources[0]
    movie = root / "Solstice.2015.1080p" / "Solstice.2015.1080p.mkv"
    sample = root / "Solstice.2015.1080p" / "Sample" / "solstice-sample.mkv"
    write_file(movie, 100)
    write_file(sample, 40)

    store = Store(config.state_dir_path() / "mediakeep.db")
    plan = asyncio.run(build_plan(config, store, NullProgress()))

    batch = next(b for b in plan.batches if b.kind == MediaKind.MOVIE)
    assert any(
        action.kind == ActionKind.MOVE and action.src == movie
        for action in batch.actions
    )

    assert sample in plan.samples
    assert not [
        action
        for b in plan.batches
        for action in b.actions
        if action.src == sample
    ]
    assert not [
        action
        for b in plan.batches
        for action in b.actions
        if action.kind == ActionKind.RMDIR
        and action.src is not None
        and "Solstice" in str(action.src)
    ]


def test_text_report_is_ascii_and_aligned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fix 7 items 5 and 6: plain ASCII, aligned headers, confirm preview."""
    config, _store, _fake = build(tmp_path, monkeypatch, auto_shows=False)
    cfg_path = tmp_path / "config.yaml"
    config.save(cfg_path)

    __main__.main(["--config", str(cfg_path), "scan"])
    out = capsys.readouterr().out

    out.encode("ascii")

    assert "if you confirm 1:" in out
    after = out.split("if you confirm 1:", 1)[1][:2000]
    assert "[tmdbid-" in after
    assert "   move  " in after
    assert "      -> " in after

    columns = {
        line.index(" -> ")
        for line in out.splitlines()
        if line.startswith("== ") and " -> " in line
    }
    assert columns
    assert len(columns) == 1
