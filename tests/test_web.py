from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import pytest
from fastapi.testclient import TestClient

from mediakeep import workspace
from mediakeep.config import AppConfig, ConfigError, LibraryConfig
from mediakeep.identify import search
from mediakeep.identify.tmdb import TmdbClient
from mediakeep.models import (
    Action,
    ActionKind,
    Batch,
    Candidate,
    FileInfo,
    Identity,
    IdStatus,
    MediaKind,
    Plan,
    ProgressSink,
    SourceItem,
)
from mediakeep.paths import unsafe_name_reason
from mediakeep.pipeline import NullIdentifier, apply_plan, build_plan
from mediakeep.store import Store
from mediakeep.web import create_app


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
    path.write_bytes(b"x" * size)


def make_config(tmp_path: Path, destination: bool = True) -> AppConfig:
    """Work in tmp/source; by default move items into tmp/Movies."""
    source = tmp_path / "source"
    movies = tmp_path / "Movies"
    saved = AppConfig(
        workspace={"folder": source, "destination": movies if destination else None},
        state_dir=str(tmp_path / "state"),
        web={"host": "127.0.0.1", "port": 8765, "access_token": "", "posters": True},
    )
    return workspace.effective(saved)


def make_settings_config(tmp_path: Path) -> AppConfig:
    source = tmp_path / "source"
    movies = tmp_path / "Movies"
    tv = tmp_path / "TV"
    kids = tmp_path / "Kids"

    return AppConfig(
        behavior={"default_destination": "file"},  # these tests file into libraries
        sources=[source],
        libraries={
            "movies": LibraryConfig(path=movies, kind=MediaKind.MOVIE),
            "tv": LibraryConfig(path=tv, kind=MediaKind.SHOW),
            "kids": LibraryConfig(path=kids, kind=MediaKind.MOVIE),
        },
        routing={"movie": "movies", "show": "tv", "rules": []},
        state_dir=str(tmp_path / "state"),
        web={"host": "127.0.0.1", "port": 8765, "access_token": "", "posters": True},
    )


def make_zstaging_config(tmp_path: Path) -> AppConfig:
    source = tmp_path / "zstaging"
    movies = tmp_path / "Movies"
    tv = tmp_path / "TV"

    return AppConfig(
        behavior={"default_destination": "file"},  # these tests file into libraries
        sources=[source],
        libraries={
            "movies": LibraryConfig(path=movies, kind=MediaKind.MOVIE),
            "tv": LibraryConfig(path=tv, kind=MediaKind.SHOW),
        },
        routing={"movie": "movies", "show": "tv", "rules": []},
        state_dir=str(tmp_path / "state"),
        web={"host": "127.0.0.1", "port": 8765, "access_token": "", "posters": True},
    )


def save_config(config: AppConfig, tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    config.save(path)
    return path


def make_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_file(source / "Daydream.2010" / "Daydream.2010.1080p.mkv", 1000)
    write_file(source / "Sandstorm.2021" / "Sandstorm.2021.1080p.mkv", 1100)
    write_file(source / "Starfall.2014" / "Starfall.2014.1080p.mkv", 1200)
    write_file(source / "The.Lantern.2023" / "The.Lantern.2023.1080p.mkv", 1300)
    write_file(source / "Iron.Owl.Vol.1.2003" / "Iron.Owl.Vol.1.2003.1080p.mkv", 1350)
    write_file(source / "Review.Me.2020" / "Review.Me.2020.1080p.mkv", 1400)
    write_file(source / "Western.Movie.2020" / "Western.Movie.2020.1080p.mkv", 1500)
    write_file(source / "Kids.Movie.2020" / "Kids.Movie.2020.1080p.mkv", 1600)
    write_file(source / "Ignore.Folder" / "Ignore.Me.2020.mkv", 1700)
    write_file(source / "Parent" / "Path.Fix.2020" / "Path.Fix.2020.1080p.mkv", 1800)
    for i in range(1, 4):
        write_file(source / "The.Lighthouse.US" / f"the.lighthouse.us.s01e0{i}.720p.mkv", 1900 + i)
    write_file(source / "notes.txt", 10)

    movies = tmp_path / "Movies"
    write_file(movies / "Solstice.2015.1080p.mkv", 2000)
    write_file(
        movies / "Iron.Owl.Vol.1.2003" / "Iron.Owl.Vol.1.2003.1080p.mkv",
        2050,
    )
    write_file(
        movies / "Western.In.Library.2020" / "Western.In.Library.2020.1080p.mkv",
        2100,
    )


def tree_snapshot(roots: dict[str, Path]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for name, root in roots.items():
        files: dict[str, int] = {}
        if root.exists():
            for p in root.rglob("*"):
                if p.is_file():
                    files[str(p.relative_to(root))] = p.stat().st_size
        out[name] = files
    return out


class FakeIdentifier:
    async def identify(
        self, items: Sequence[SourceItem], progress: ProgressSink
    ) -> dict[str, Identity]:
        out: dict[str, Identity] = {}
        for item in items:
            kind = item.kind if item.kind != MediaKind.UNKNOWN else MediaKind.MOVIE
            title = item.title or (item.paths[0].name if item.paths else "Unknown")
            year = item.year
            genres: list[str] = []
            if "western" in title.lower():
                genres = ["Western"]

            tmdb_id: int | None = None
            candidates: list[Candidate] = []
            status = IdStatus.AUTO
            confidence = 100.0
            reasons = ["fake"]
            identity_title = title

            if title.lower().startswith("review me"):
                status = IdStatus.NEEDS_REVIEW
                confidence = 40.0
                reasons = ["fake review"]
                candidates = [
                    Candidate(
                        tmdb_id=999,
                        kind=kind,
                        title="Review Me",
                        original_title="",
                        year=year,
                        original_language="",
                        runtime_min=None,
                        genres=[],
                        countries=[],
                        overview="",
                        poster_url=None,
                        imdb_id=None,
                        score=50.0,
                        reasons=["fake"],
                    )
                ]

            if title.lower().startswith("the lantern"):
                tmdb_id = 777
                identity_title = "Ada Finch's The Lantern"
                candidates = [
                    Candidate(
                        tmdb_id=777,
                        kind=MediaKind.MOVIE,
                        title="Ada Finch's The Lantern",
                        original_title="",
                        year=2023,
                        original_language="",
                        runtime_min=130,
                        genres=["Action"],
                        countries=[],
                        overview="",
                        poster_url=None,
                        imdb_id=None,
                        score=80.0,
                        reasons=["fake"],
                    )
                ]

            if "iron owl" in title.lower():
                tmdb_id = 888
                identity_title = "Iron Owl: Vol. 1"
                year = 2003
                candidates = [
                    Candidate(
                        tmdb_id=888,
                        kind=MediaKind.MOVIE,
                        title="Iron Owl: Vol. 1",
                        original_title="",
                        year=2003,
                        original_language="",
                        runtime_min=111,
                        genres=["Action"],
                        countries=[],
                        overview="",
                        poster_url=None,
                        imdb_id=None,
                        score=90.0,
                        reasons=["fake"],
                    )
                ]

            out[item.key] = Identity(
                item_key=item.key,
                kind=kind,
                title=identity_title,
                year=year,
                tmdb_id=tmdb_id,
                imdb_id=None,
                status=status,
                confidence=confidence,
                reasons=reasons,
                candidates=candidates,
                genres=genres,
            )
        return out


async def fake_identity_for(
    config: AppConfig, store: Store, item: SourceItem, tmdb_id: int, kind: MediaKind
) -> Identity:
    return Identity(
        item_key=item.key,
        kind=kind,
        title=item.title or "Chosen",
        year=item.year,
        tmdb_id=tmdb_id,
        imdb_id=None,
        status=IdStatus.CONFIRMED,
        confidence=100.0,
        reasons=["fake choose"],
    )


async def fake_search(
    config: AppConfig,
    store: Store,
    query: str,
    year: int | None,
    kind: MediaKind | None,
) -> list[Candidate]:
    return []


def make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: AppConfig | None = None) -> tuple[TestClient, Path]:
    if config is None:
        config = make_config(tmp_path)
    cfg_path = save_config(config, tmp_path)

    monkeypatch.setenv("MEDIAKEEP_CONFIG", str(cfg_path))
    monkeypatch.setattr(
        "mediakeep.pipeline.create_identifier",
        lambda cfg, store, audio: FakeIdentifier(),
    )
    monkeypatch.setattr("mediakeep.web.app.identity_for", fake_identity_for)
    monkeypatch.setattr("mediakeep.web.app.search", fake_search)

    app = create_app(config)
    return TestClient(app), cfg_path


def csrf_headers(client: TestClient) -> dict[str, str]:
    client.get("/api/health")
    token = client.cookies.get("mediakeep_csrf")
    return {"X-CSRF-Token": token or ""}


def wait_job(client: TestClient, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get("/api/jobs/current").json()
        if not job:
            return {"state": "none"}
        if job.get("state") in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job timeout")


def run_scan(client: TestClient) -> dict[str, Any]:
    r = client.post("/api/jobs/scan", json={}, headers=csrf_headers(client))
    assert r.status_code == 200
    job = wait_job(client)
    assert job["state"] == "done", job
    return job


def roots(config: AppConfig) -> dict[str, Path]:
    out = {f"source:{p}": p for p in config.sources}
    for name, lib in config.libraries.items():
        out[f"library:{name}"] = lib.path
    return out


def browse_rows(client: TestClient, path: Path) -> list[dict[str, Any]]:
    r = client.get("/api/browse", params={"path": str(path)})
    assert r.status_code == 200
    return list(r.json()["rows"])


def queue_entries(client: TestClient) -> list[dict[str, Any]]:
    return list(client.get("/api/queue").json())


def execute_rows(client: TestClient) -> list[dict[str, Any]]:
    data = client.get("/api/execute").json()
    return list(data["rows"])


def assert_no_raw(obj: Any) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "tmdb_title":
                continue
            assert_no_raw(value)
    elif isinstance(obj, list):
        for value in obj:
            assert_no_raw(value)
    elif isinstance(obj, str):
        assert "Iron Owl: Vol" not in obj


def test_nothing_changes_before_execute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    make_tree(tmp_path)
    client, cfg_path = make_app(tmp_path, monkeypatch, config)
    before = tree_snapshot(roots(config))
    initial_config = cfg_path.read_text(encoding="utf-8")

    run_scan(client)

    source = config.sources[0]
    rows = browse_rows(client, source)
    ready_keys: list[str] = []
    for row in rows:
        if row["status"] == "ready" and row["selectable"]:
            ready_keys.extend(row["item_keys"])
    assert len(ready_keys) >= 3
    inbox_keys = ready_keys[:3]

    headers = csrf_headers(client)
    for key in inbox_keys:
        r = client.post(
            "/api/queue",
            json={"kind": "item", "item_key": key, "source": "inbox"},
            headers=headers,
        )
        assert r.status_code == 200

    review_row = next(row for row in rows if row["status"] == "needs" and row.get("match_key"))
    r = client.post(
        f"/api/match/{review_row['match_key']}/choose",
        json={"tmdb_id": 999, "kind": "movie", "remember": False, "scan": False},
        headers=headers,
    )
    assert r.status_code == 200

    after = tree_snapshot(roots(config))
    assert after == before
    assert cfg_path.read_text(encoding="utf-8") == initial_config
    assert not cfg_path.with_name(cfg_path.name + ".bak").exists()

    exec_data = client.get("/api/execute").json()
    rows = exec_data["rows"]
    item_rows = [r for r in rows if r["entry_kind"] == "item"]
    assert len(item_rows) == 4


def test_execute_applies_ticked_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    make_tree(tmp_path)
    client, cfg_path = make_app(tmp_path, monkeypatch, config)
    before = tree_snapshot(roots(config))

    run_scan(client)
    headers = csrf_headers(client)

    source = config.sources[0]
    rows = browse_rows(client, source)
    inbox_keys: list[str] = []
    for row in rows:
        if row["status"] == "ready" and row["selectable"] and "Western" not in row["name"]:
            inbox_keys.extend(row["item_keys"])
    assert len(inbox_keys) >= 3
    for key in inbox_keys[:3]:
        assert client.post(
            "/api/queue",
            json={"kind": "item", "item_key": key, "source": "inbox"},
            headers=headers,
        ).status_code == 200

    review_row = next(row for row in rows if row["status"] == "needs" and row.get("match_key"))
    assert client.post(
        f"/api/match/{review_row['match_key']}/choose",
        json={"tmdb_id": 999, "kind": "movie", "remember": False, "scan": False},
        headers=headers,
    ).status_code == 200

    exec_data = client.get("/api/execute").json()
    all_rows = exec_data["rows"]
    inbox_rows = [r for r in all_rows if r["source"] == "inbox"]
    assert inbox_rows

    untick = inbox_rows[0]
    ticked = [r for r in all_rows if r["queue_id"] != untick["queue_id"]]
    ticked_targets = {r["queue_id"]: r["to_paths"] for r in ticked if r["entry_kind"] == "item"}

    r = client.post(
        "/api/jobs/execute",
        json={"ids": [r["queue_id"] for r in ticked]},
        headers=headers,
    )
    assert r.status_code == 200
    job = wait_job(client)
    assert job["state"] == "done", job
    result = job["result"]
    assert result["run_id"]
    assert isinstance(result["run"], dict)

    for paths in ticked_targets.values():
        assert paths
        assert Path(paths[0]).exists()

    assert not Path(untick["to_paths"][0]).exists()
    q = queue_entries(client)
    assert any(int(e["id"]) == int(untick["queue_id"]) for e in q)

    r = client.post(
        "/api/jobs/undo",
        json={"run_id": result["run_id"], "apply": True},
        headers=headers,
    )
    assert r.status_code == 200
    wait_job(client)
    assert tree_snapshot(roots(config)) == before


def test_title_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    make_tree(tmp_path)
    client, _cfg = make_app(tmp_path, monkeypatch, config)
    run_scan(client)
    headers = csrf_headers(client)

    # New downloads (Inbox) get TMDB's title by default (fix 7); "keep" switches to the
    # title read from the file, and "tmdb" switches back. Keeping your own title by default
    # applies only to items already in a library (renamed in place).
    rows = browse_rows(client, config.sources[0])
    row = next(r for r in rows if r["name"] == "The.Lantern.2023")
    key = row["item_keys"][0]
    assert "Ada Finch's The Lantern (2023)" in row["target"]

    r = client.post(
        f"/api/item/{key}/title_override",
        json={"choice": "keep"},
        headers=headers,
    )
    assert r.status_code == 200
    rows = browse_rows(client, config.sources[0])
    row = next(r for r in rows if r["name"] == "The.Lantern.2023")
    assert "The Lantern (2023)" in row["target"]
    assert "Ada Finch" not in row["target"]

    r = client.post(
        f"/api/item/{key}/title_override",
        json={"choice": "tmdb"},
        headers=headers,
    )
    assert r.status_code == 200

    rows = browse_rows(client, config.sources[0])
    row = next(r for r in rows if r["name"] == "The.Lantern.2023")
    assert "Ada Finch's The Lantern (2023)" in row["target"]

    match = client.get(f"/api/match/{key}").json()
    assert match["selected"]["tmdb_id"] == 777


def test_fixes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    make_tree(tmp_path)
    client, _cfg = make_app(tmp_path, monkeypatch, config)
    headers = csrf_headers(client)

    fixes = [
        {
            "pattern": "The Lighthouse US",
            "name": "The Lighthouse US",
            "options": {"kind": "show"},
            "scan": False,
        },
        {
            "pattern": "Ignore.Folder",
            "name": "",
            "options": {"ignore": "true"},
            "scan": False,
        },
        {
            "pattern": "Parent/Path.Fix.2020",
            "name": "Path Fix",
            "options": {"kind": "movie"},
            "scan": False,
        },
    ]
    for fix in fixes:
        r = client.post("/api/fixes", json=fix, headers=headers)
        assert r.status_code == 200

    run_scan(client)

    browse = client.get("/api/browse", params={"path": str(config.sources[0])}).json()
    rows = {r["name"]: r for r in browse["rows"]}

    assert rows["The.Lighthouse.US"]["status"] != "left"
    assert config.sources[0].name in browse["folder"]
    assert any(p.endswith("Ignore.Folder") for p in browse["left_paths"])

    parent_rows = browse_rows(client, config.sources[0] / "Parent")
    assert any(r["name"] == "Path.Fix.2020" and r["status"] == "ready" for r in parent_rows)


def test_browse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    make_tree(tmp_path)
    client, _cfg = make_app(tmp_path, monkeypatch, config)
    run_scan(client)

    r = client.get("/api/browse", params={"path": str(config.sources[0])})
    assert r.status_code == 200
    data = r.json()
    assert data["rows"]

    outside = tmp_path / "outside"
    outside.mkdir()
    r = client.get("/api/browse", params={"path": str(outside)})
    assert r.status_code == 403

    for row in data["rows"]:
        target = row.get("target")
        if target:
            assert os.path.isabs(target) or target[1:3] == ":\\\\" or target[1] == ":"


@pytest.mark.asyncio
async def test_poster_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    config.tmdb.api_key = "test"
    store = Store(tmp_path / "state" / "mediakeep.db")

    def handler(path: str, params: dict[str, Any] | None) -> dict[str, Any]:
        if path.endswith("/configuration"):
            return {
                "images": {
                    "base_url": "http://image.tmdb.org/t/p/",
                    "secure_base_url": "https://image.tmdb.org/t/p/",
                    "poster_sizes": ["w92", "w185", "w342"],
                }
            }
        if "/search/movie" in path:
            return {
                "results": [
                    {
                        "id": 1,
                        "title": "Test",
                        "original_title": "Test",
                        "release_date": "2020-01-01",
                        "poster_path": "abc.jpg",
                        "original_language": "en",
                    }
                ]
            }
        if "/movie/" in path:
            return {
                "id": 1,
                "title": "Test",
                "original_title": "Test",
                "release_date": "2020-01-01",
                "poster_path": "abc.jpg",
                "original_language": "en",
                "runtime": 120,
                "genres": [{"name": "Action"}],
                "production_countries": [{"iso_3166_1": "US"}],
            }
        return {"results": []}

    async def fake_request(
        self: TmdbClient,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        cache: bool = True,
    ) -> Any:
        return handler(path, params)

    monkeypatch.setattr(TmdbClient, "_request", fake_request)

    candidates = await search(config, store, "Test", None, MediaKind.MOVIE)
    assert candidates
    assert candidates[0].poster_url == "https://image.tmdb.org/t/p/w342/abc.jpg"


def test_non_loopback_refuses_without_token(tmp_path: Path) -> None:
    config = AppConfig(web={"host": "0.0.0.0", "access_token": ""})
    store = Store(tmp_path / "state" / "mediakeep.db")
    with pytest.raises(ConfigError):
        create_app(config, store=store)


def test_login_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    config.web.access_token = "secret"
    cfg_path = save_config(config, tmp_path)
    monkeypatch.setenv("MEDIAKEEP_CONFIG", str(cfg_path))
    app = create_app(config)
    client = TestClient(app)

    r = client.get("/api/queue")
    assert r.status_code == 401

    r = client.post("/api/login", json={"token": "secret"})
    assert r.status_code == 200
    assert client.get("/api/queue").status_code == 200


def test_post_without_csrf_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    client, _cfg = make_app(tmp_path, monkeypatch, config)
    r = client.post("/api/queue", json={"kind": "setting", "payload": {"path": "naming.year_style", "op": "set", "value": "brackets"}})
    assert r.status_code == 403


def test_sse_emits_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    make_tree(tmp_path)
    client, _cfg = make_app(tmp_path, monkeypatch, config)
    run_scan(client)

    r = client.get("/api/jobs/events")
    assert r.status_code == 200
    assert "data:" in r.text


def test_display_names_are_on_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    make_tree(tmp_path)
    client, _cfg = make_app(tmp_path, monkeypatch, config)
    run_scan(client)
    headers = csrf_headers(client)

    rows = browse_rows(client, config.sources[0])
    row = next(r for r in rows if r["name"].lower().startswith("iron.owl"))
    assert "Iron Owl - Vol. 1 (2003)" in str(row["target"])
    assert_no_raw(row)

    key = row["item_keys"][0]
    match = client.get(f"/api/match/{key}").json()
    candidate = next(c for c in match["candidates"] if c.get("tmdb_id") == 888)
    assert candidate["tmdb_title"] == "Iron Owl: Vol. 1"

    assert client.post(
        "/api/queue",
        json={"kind": "item", "item_key": key, "source": "inbox"},
        headers=headers,
    ).status_code == 200

    exec_row = next(r for r in execute_rows(client) if r["item_key"] == key)
    assert "Iron Owl - Vol. 1 (2003)" in exec_row["label"]
    assert_no_raw(exec_row)


def test_unsafe_name_reason() -> None:
    for name in ["a:b", "a?", "a.", "a ", "CON", "con.mkv", "LPT1.srt", "x" * 256]:
        assert unsafe_name_reason(name) is not None

    for name in ["Iron Owl - Vol. 1 (2003)", "What If... (2021)"]:
        assert unsafe_name_reason(name) is None


def test_plan_check_drops_unsafe_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    movies = tmp_path / "Movies"
    write_file(source / "Bad.Movie.2020" / "Bad.Movie.2020.mkv", 100)

    config = AppConfig(
        behavior={"default_destination": "file"},  # these tests file into libraries
        sources=[source],
        libraries={"movies": LibraryConfig(path=movies, kind=MediaKind.MOVIE)},
        routing={"movie": "movies", "show": "tv", "rules": []},
        state_dir=str(tmp_path / "state"),
    )
    store = Store(tmp_path / "state" / "mediakeep.db")

    monkeypatch.setattr(
        "mediakeep.pipeline.create_identifier",
        lambda cfg, st, audio: NullIdentifier(),
    )
    # The folder name comes from layout.folder_base (used by layout.target_dir); patch both
    # places so the unsafe name really reaches the planned paths.
    monkeypatch.setattr(
        "mediakeep.plan.folder_base",
        lambda item, identity, cfg: "Bad:Name",
    )
    monkeypatch.setattr(
        "mediakeep.layout.folder_base",
        lambda item, identity, cfg: "Bad:Name",
    )
    # plan.py cleans every folder name again with sanitize_name (which would turn "Bad:Name"
    # into "Bad - Name"); bypass that so the final unsafe-name check is what gets tested.
    monkeypatch.setattr("mediakeep.plan.sanitize_name", lambda name: name)

    plan = asyncio.run(build_plan(config, store, NullProgress(), None, None))

    assert plan.items
    assert not plan.batches
    assert any("Bad:Name" in issue.message for issue in plan.issues)


def test_apply_check_blocks_unsafe_batch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir(parents=True)

    config = AppConfig(
        behavior={"default_destination": "file"},  # these tests file into libraries
        sources=[source],
        libraries={"movies": LibraryConfig(path=tmp_path / "Movies", kind=MediaKind.MOVIE)},
        routing={"movie": "movies", "show": "tv", "rules": []},
        state_dir=str(tmp_path / "state"),
    )
    store = Store(tmp_path / "state" / "mediakeep.db")

    bad_src = source / "BadSrc" / "bad.mkv"
    good_src = source / "Good.Movie.2020" / "good.mkv"
    write_file(bad_src, 100)
    write_file(good_src, 100)

    bad_dst = source / "Bad?Name" / "bad.mkv"
    good_dst = source / "Good Movie (2020)" / "Good Movie (2020).mkv"

    def file_info(path: Path) -> FileInfo:
        st = path.stat()
        return FileInfo(path=path, size=st.st_size, mtime_ns=st.st_mtime_ns, hidden=False)

    def identity(key: str, title: str, year: int | None) -> Identity:
        return Identity(
            item_key=key,
            kind=MediaKind.MOVIE,
            title=title,
            year=year,
            tmdb_id=None,
            imdb_id=None,
            status=IdStatus.AUTO,
            confidence=100.0,
            reasons=["test"],
        )

    def item(key: str, folder: Path, title: str, year: int | None, video: Path) -> SourceItem:
        return SourceItem(
            key=key,
            root=source,
            paths=[folder],
            kind=MediaKind.MOVIE,
            title=title,
            year=year,
            videos=[file_info(video)],
        )

    bad_batch = Batch(
        item_key="bad",
        label="Bad",
        kind=MediaKind.MOVIE,
        actions=[
            Action(kind=ActionKind.MKDIR, src=None, dst=bad_dst.parent),
            Action(kind=ActionKind.MOVE, src=bad_src, dst=bad_dst),
        ],
        reasons=[f"target: {bad_dst.parent}"],
        preconditions=[file_info(bad_src)],
        needs_review=False,
    )

    good_batch = Batch(
        item_key="good",
        label="Good",
        kind=MediaKind.MOVIE,
        actions=[
            Action(kind=ActionKind.MKDIR, src=None, dst=good_dst.parent),
            Action(kind=ActionKind.MOVE, src=good_src, dst=good_dst),
        ],
        reasons=[f"target: {good_dst.parent}"],
        preconditions=[file_info(good_src)],
        needs_review=False,
    )

    plan = Plan(
        id="unsafe-test",
        created_at=datetime.now(),
        config_hash="",
        items={
            "bad": item("bad", bad_src.parent, "Bad", 2020, bad_src),
            "good": item("good", good_src.parent, "Good Movie", 2020, good_src),
        },
        identities={
            "bad": identity("bad", "Bad", 2020),
            "good": identity("good", "Good Movie", 2020),
        },
        batches=[bad_batch, good_batch],
        issues=[],
        duplicates=[],
        multi_version=[],
        samples=[],
        unchanged=0,
    )

    run = apply_plan(config, store, plan, ["bad", "good"], NullProgress())

    assert run.failed >= 1
    assert any("Bad?Name" in issue.message for issue in run.issues)
    assert bad_src.exists()
    assert not bad_dst.exists()
    assert good_dst.exists()


def test_rename_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_zstaging_config(tmp_path)
    source = config.sources[0]
    original_folder = source / "The.Lantern.2023.1080p.BluRay"
    write_file(original_folder / "The.Lantern.2023.1080p.BluRay.mkv", 1300)

    client, _cfg = make_app(tmp_path, monkeypatch, config)
    run_scan(client)
    headers = csrf_headers(client)

    rows = browse_rows(client, source)
    row = next(r for r in rows if r["name"] == original_folder.name)
    key = row["item_keys"][0]

    assert client.post(
        f"/api/item/{key}/title_override",
        json={"choice": "keep"},
        headers=headers,
    ).status_code == 200

    r = client.post(f"/api/item/{key}/in_place", json={"on": True}, headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["in_place"] is True
    expected_folder = source / "The Lantern (2023)"
    assert Path(data["in_place_target"]) == expected_folder

    assert client.post(
        "/api/queue",
        json={"kind": "item", "item_key": key, "source": "inbox"},
        headers=headers,
    ).status_code == 200

    exec_row = next(r for r in execute_rows(client) if r["item_key"] == key)
    assert exec_row["source"].startswith("in_place:")
    assert exec_row["renamed_in_place"] is True

    r = client.post(
        "/api/jobs/execute",
        json={"ids": [exec_row["queue_id"]], "confirmed": True},
        headers=headers,
    )
    assert r.status_code == 200
    job = wait_job(client)
    assert job["state"] == "done", job
    run_id = job["result"]["run_id"]

    assert (expected_folder / "The Lantern (2023).mkv").exists()
    assert not (original_folder / "The.Lantern.2023.1080p.BluRay.mkv").exists()

    r = client.post(
        "/api/jobs/undo",
        json={"run_id": run_id, "apply": True},
        headers=headers,
    )
    assert r.status_code == 200
    wait_job(client)
    assert (original_folder / "The.Lantern.2023.1080p.BluRay.mkv").exists()
    assert not expected_folder.exists()

    r = client.post(f"/api/item/{key}/in_place", json={"on": False}, headers=headers)
    assert r.status_code == 200
    rows = browse_rows(client, source)
    row = next(r for r in rows if r["name"] == original_folder.name)
    assert str(config.libraries["movies"].path) in row["target"]


def test_add_library_nested_in_source_returns_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_settings_config(tmp_path)
    nested = config.sources[0] / "nested"
    nested.mkdir(parents=True)

    client, _cfg = make_app(tmp_path, monkeypatch, config)
    headers = csrf_headers(client)

    r = client.post(
        "/api/queue",
        json={
            "kind": "setting",
            "payload": {
                "path": "libraries.nested",
                "op": "set",
                "value": {"path": str(nested), "kind": "movie"},
            },
            "source": "settings",
        },
        headers=headers,
    )
    assert r.status_code == 422
    assert queue_entries(client) == []


def test_folder_picker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_settings_config(tmp_path)
    client, _cfg = make_app(tmp_path, monkeypatch, config)

    (tmp_path / "sub1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sub1" / "inner").mkdir(parents=True, exist_ok=True)
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")

    r = client.get("/api/folders", params={"path": str(tmp_path)})
    assert r.status_code == 200
    data = r.json()
    names = {f["name"] for f in data["folders"]}
    assert "sub1" in names
    assert "file.txt" not in names
    assert all(Path(f["path"]).is_dir() for f in data["folders"])

    r = client.get("/api/folders")
    assert r.status_code == 200
    assert r.json()["roots"]

    r = client.get("/api/folders", params={"path": str(tmp_path / "does-not-exist")})
    assert r.status_code == 404


@pytest.mark.parametrize("file_name", ["Facing.the.Ridge.(2006).mkv", "ftg-720p.mkv"])
def test_rename_in_place_never_nests_in_the_old_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_name: str
) -> None:
    # A reported bug: D:\Videos\Sports\Facing.the.Ridge.(2006) was renamed to
    # D:\Videos\Sports\Facing.the.Ridge.(2006)\Facing the Ridge (2006).
    config = make_zstaging_config(tmp_path)
    source = config.sources[0]
    folder = source / "Facing.the.Ridge.(2006)"
    write_file(folder / file_name, 1300)

    client, _cfg = make_app(tmp_path, monkeypatch, config)
    run_scan(client)
    headers = csrf_headers(client)
    row = next(r for r in browse_rows(client, source) if r["name"] == folder.name)
    key = (row["item_keys"] or [c["item_key"] for c in row["cards"]])[0]

    r = client.post(f"/api/item/{key}/in_place", json={"on": True}, headers=headers)
    assert r.status_code == 200
    target = Path(r.json()["in_place_target"])
    assert target.parent == source, target
    # A second change re-plans the item again; still next to the old folder, not inside it.
    client.post(f"/api/item/{key}/title_override", json={"choice": "keep"}, headers=headers)
    r = client.post(f"/api/item/{key}/in_place", json={"on": True}, headers=headers)
    assert Path(r.json()["in_place_target"]).parent == source
