from __future__ import annotations

import html as html_lib
import os
import re
import time
from pathlib import Path
from typing import Any, Sequence

import pytest
from fastapi.testclient import TestClient

from mediakeep import workspace
from mediakeep.config import AppConfig, LibraryConfig
from mediakeep.models import Candidate, Identity, IdStatus, MediaKind, ProgressSink, SourceItem
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
        movies / "Western.In.Library.2020" / "Western.In.Library.2020.1080p.mkv",
        2100,
    )
    (tmp_path / "TV").mkdir(parents=True, exist_ok=True)
    (tmp_path / "Kids").mkdir(parents=True, exist_ok=True)


def make_single_tree(tmp_path: Path, folder_name: str) -> None:
    source = tmp_path / "source"
    write_file(source / folder_name / f"{folder_name}.mkv", 1000)
    (tmp_path / "Movies").mkdir(parents=True, exist_ok=True)
    (tmp_path / "TV").mkdir(parents=True, exist_ok=True)


def fake_abs(path: Path | str) -> str:
    s = os.path.abspath(str(path))
    if len(s) >= 2 and s[1] == ":":
        s = "Q:" + s[2:]
    else:
        s = "Q:" + s.replace("/", "\\")
    return s


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


class IronOwlIdentifier:
    async def identify(
        self, items: Sequence[SourceItem], progress: ProgressSink
    ) -> dict[str, Identity]:
        out: dict[str, Identity] = {}
        for item in items:
            kind = item.kind if item.kind != MediaKind.UNKNOWN else MediaKind.MOVIE
            title = item.title or (item.paths[0].name if item.paths else "Unknown")
            year = item.year

            if "iron owl" in title.lower():
                out[item.key] = Identity(
                    item_key=item.key,
                    kind=MediaKind.MOVIE,
                    title="Iron Owl: Vol. 1",
                    year=2003,
                    tmdb_id=1,
                    imdb_id=None,
                    status=IdStatus.AUTO,
                    confidence=100.0,
                    reasons=["fake iron owl"],
                    candidates=[
                        Candidate(
                            tmdb_id=1,
                            kind=MediaKind.MOVIE,
                            title="Iron Owl: Vol. 1",
                            original_title="",
                            year=2003,
                            original_language="",
                            runtime_min=None,
                            genres=[],
                            countries=[],
                            overview="",
                            poster_url=None,
                            imdb_id=None,
                            score=100.0,
                            reasons=["fake"],
                        )
                    ],
                )
            else:
                out[item.key] = Identity(
                    item_key=item.key,
                    kind=kind,
                    title=title,
                    year=year,
                    tmdb_id=None,
                    imdb_id=None,
                    status=IdStatus.AUTO,
                    confidence=100.0,
                    reasons=["fake"],
                )
        return out


class GigasectorReadyIdentifier:
    async def identify(
        self, items: Sequence[SourceItem], progress: ProgressSink
    ) -> dict[str, Identity]:
        out: dict[str, Identity] = {}
        for item in items:
            title = item.title or (item.paths[0].name if item.paths else "Unknown")
            year = item.year or 1989
            if "gigasector 23 iii" in title.lower() and "waking" in title.lower():
                candidate = Candidate(
                    tmdb_id=555,
                    kind=MediaKind.MOVIE,
                    title="Gigasector 23 III Part 1: The Waking of Ivy",
                    original_title="",
                    year=year,
                    original_language="",
                    runtime_min=None,
                    genres=[],
                    countries=[],
                    overview="",
                    poster_url=None,
                    imdb_id=None,
                    score=95.0,
                    reasons=["title match", "year match"],
                )
                out[item.key] = Identity(
                    item_key=item.key,
                    kind=MediaKind.MOVIE,
                    title=candidate.title,
                    year=year,
                    tmdb_id=555,
                    imdb_id=None,
                    status=IdStatus.AUTO,
                    confidence=95.0,
                    reasons=["title match", "year match"],
                    candidates=[candidate],
                )
            else:
                out[item.key] = Identity(
                    item_key=item.key,
                    kind=item.kind if item.kind != MediaKind.UNKNOWN else MediaKind.MOVIE,
                    title=title,
                    year=year,
                    tmdb_id=None,
                    imdb_id=None,
                    status=IdStatus.AUTO,
                    confidence=100.0,
                    reasons=["fake"],
                )
        return out


class GigasectorNeedsIdentifier:
    async def identify(
        self, items: Sequence[SourceItem], progress: ProgressSink
    ) -> dict[str, Identity]:
        out: dict[str, Identity] = {}
        for item in items:
            title = item.title or (item.paths[0].name if item.paths else "Unknown")
            year = item.year or 1989
            if "gigasector 23 iii" in title.lower() and "day of lanterns" in title.lower():
                candidate = Candidate(
                    tmdb_id=555,
                    kind=MediaKind.MOVIE,
                    title="Gigasector 23 III Part 1: The Waking of Ivy",
                    original_title="",
                    year=1989,
                    original_language="",
                    runtime_min=None,
                    genres=[],
                    countries=[],
                    overview="",
                    poster_url=None,
                    imdb_id=None,
                    score=80.0,
                    reasons=["title match"],
                )
                out[item.key] = Identity(
                    item_key=item.key,
                    kind=MediaKind.MOVIE,
                    title=candidate.title,
                    year=year,
                    tmdb_id=555,
                    imdb_id=None,
                    status=IdStatus.NEEDS_REVIEW,
                    confidence=40.0,
                    reasons=["title mismatch", "year match"],
                    candidates=[candidate],
                )
            else:
                out[item.key] = Identity(
                    item_key=item.key,
                    kind=item.kind if item.kind != MediaKind.UNKNOWN else MediaKind.MOVIE,
                    title=title,
                    year=year,
                    tmdb_id=None,
                    imdb_id=None,
                    status=IdStatus.AUTO,
                    confidence=100.0,
                    reasons=["fake"],
                )
        return out


class WonderCloseIdentifier:
    async def identify(
        self, items: Sequence[SourceItem], progress: ProgressSink
    ) -> dict[str, Identity]:
        out: dict[str, Identity] = {}
        for item in items:
            title = item.title or (item.paths[0].name if item.paths else "Unknown")
            year = item.year or 2009
            if "steel maiden" in title.lower():
                candidates = [
                    Candidate(
                        tmdb_id=2009,
                        kind=MediaKind.MOVIE,
                        title="Steel Maiden",
                        original_title="",
                        year=2009,
                        original_language="",
                        runtime_min=None,
                        genres=[],
                        countries=[],
                        overview="",
                        poster_url=None,
                        imdb_id=None,
                        score=70.0,
                        reasons=["title match"],
                    ),
                    Candidate(
                        tmdb_id=2011,
                        kind=MediaKind.MOVIE,
                        title="Steel Maiden",
                        original_title="",
                        year=2011,
                        original_language="",
                        runtime_min=None,
                        genres=[],
                        countries=[],
                        overview="",
                        poster_url=None,
                        imdb_id=None,
                        score=66.0,
                        reasons=["title match"],
                    ),
                ]
                out[item.key] = Identity(
                    item_key=item.key,
                    kind=MediaKind.MOVIE,
                    title="Steel Maiden",
                    year=year,
                    tmdb_id=2009,
                    imdb_id=None,
                    status=IdStatus.NEEDS_REVIEW,
                    confidence=68.0,
                    reasons=["title match"],
                    candidates=candidates,
                )
            else:
                out[item.key] = Identity(
                    item_key=item.key,
                    kind=item.kind if item.kind != MediaKind.UNKNOWN else MediaKind.MOVIE,
                    title=title,
                    year=year,
                    tmdb_id=None,
                    imdb_id=None,
                    status=IdStatus.AUTO,
                    confidence=100.0,
                    reasons=["fake"],
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


def make_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: AppConfig | None = None,
    identifier: Any | None = None,
) -> tuple[TestClient, Path]:
    if config is None:
        config = make_config(tmp_path)
    cfg_path = save_config(config, tmp_path)

    monkeypatch.setenv("MEDIAKEEP_CONFIG", str(cfg_path))
    ident_cls = identifier or FakeIdentifier
    monkeypatch.setattr(
        "mediakeep.pipeline.create_identifier",
        lambda cfg, store, audio: ident_cls(),
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


def browse_rows(client: TestClient, path: Path) -> list[dict[str, Any]]:
    r = client.get("/api/browse", params={"path": str(path)})
    assert r.status_code == 200
    return list(r.json()["rows"])


def sidebar_html(html: str) -> str:
    match = re.search(r'<nav class="sidebar".*?</nav>', html, re.S)
    assert match is not None
    return match.group(0)


def assert_sidebar_order(html: str, active: str) -> None:
    sb = sidebar_html(html)
    labels = [
        "Folder",
        "Remembered fixes",
        "Settings",
        "Execute",
        "History & undo",
    ]
    positions = []
    for label in labels:
        pos = sb.find(">" + label)
        assert pos >= 0, f"missing sidebar label {label}"
        positions.append(pos)
    assert positions == sorted(positions), "sidebar order is wrong"
    assert 'aria-current="page"' in sb


def visible_text(html: str) -> str:
    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    return html_lib.unescape(text)


def visible_text_without_tmdb(html: str) -> str:
    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
    html = re.sub(r"<style\b.*?</style>", "", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]*data-tmdb-title[^>]*>.*?</[^>]+>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    return html_lib.unescape(text)


def assert_plain_words(html: str) -> None:
    text = visible_text(html)
    for word in ["plan", "batch", "hint", "routing", "override", "identity"]:
        assert not re.search(rf"\b{word}\b", text, re.I), word


def assert_full_paths(html: str) -> None:
    assert ">Movies\\" not in html
    assert ">TV\\" not in html
    for match in re.finditer(r'data-path-cell="true"[^>]*>([^<]+)', html):
        path = html_lib.unescape(match.group(1)).strip()
        if not path:
            continue
        assert (
            re.match(r"^[A-Za-z]:\\", path)
            or path.startswith("\\\\")
            or path.startswith("/")
        ), path


def assert_select_all(html: str) -> None:
    assert "data-select-all" in html
    assert "data-select-row" in html


def make_kill_config(tmp_path: Path) -> AppConfig:
    source = tmp_path / "source"
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


def make_kill_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    write_file(source / "Iron Owl Vol 1 2003" / "Iron.Owl.Vol.1.2003.1080p.mkv", 1000)
    (tmp_path / "Movies").mkdir(parents=True, exist_ok=True)
    (tmp_path / "TV").mkdir(parents=True, exist_ok=True)


def make_used_config(tmp_path: Path) -> AppConfig:
    source = tmp_path / "source"
    movies = tmp_path / "Movies"
    western = tmp_path / "Western"
    tv = tmp_path / "TV"
    return AppConfig(
        behavior={"default_destination": "file"},  # these tests file into libraries
        sources=[source],
        libraries={
            "movies": LibraryConfig(path=movies, kind=MediaKind.MOVIE),
            "western": LibraryConfig(path=western, kind=MediaKind.MOVIE),
            "tv": LibraryConfig(path=tv, kind=MediaKind.SHOW),
        },
        routing={
            "movie": "western",
            "show": "tv",
            "rules": [
                {"when": {"kind": "movie"}, "to": "movies"},
                {"when": {"kind": "movie"}, "to": "western"},
            ],
        },
        state_dir=str(tmp_path / "state"),
        web={"host": "127.0.0.1", "port": 8765, "access_token": "", "posters": True},
    )


def test_pages_render_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    make_tree(tmp_path)
    client, _cfg = make_app(tmp_path, monkeypatch, config)
    run_scan(client)
    headers = csrf_headers(client)

    assert client.post(
        "/api/fixes",
        json={
            "pattern": "The Lighthouse US",
            "name": "The Lighthouse US",
            "options": {"kind": "show"},
            "scan": False,
        },
        headers=headers,
    ).status_code == 200

    rows = browse_rows(client, config.sources[0])
    ready_row = next(row for row in rows if row["status"] == "ready" and row["item_keys"])
    assert client.post(
        "/api/queue",
        json={"kind": "item", "item_key": ready_row["item_keys"][0], "source": "inbox"},
        headers=headers,
    ).status_code == 200

    review_row = next((row for row in rows if row["status"] == "needs" and row.get("match_key")), None)
    match_key = review_row["match_key"] if review_row is not None else ready_row["item_keys"][0]

    pages = {
        "/inbox": ("Folder", "inbox"),
        f"/match/{match_key}": ("Which movie is this?", "inbox"),
        "/fixes": ("Remembered fixes", "fixes"),
        "/execute": ("Execute", "execute"),
        "/settings": ("Settings", "settings"),
        "/history": ("History & undo", "history"),
    }

    for path, (heading, active) in pages.items():
        r = client.get(path)
        assert r.status_code == 200, path
        assert heading in r.text
        assert_sidebar_order(r.text, active)
        assert_plain_words(r.text)
        assert_full_paths(r.text)
        if path in {"/inbox", "/fixes", "/execute"}:
            assert_select_all(r.text)

    inbox = client.get("/inbox").text
    assert "to Execute" in inbox
    assert "Nothing moves yet" in inbox

    css = client.get("/static/style.css")
    js = client.get("/static/app.js")
    assert css.status_code == 200
    assert js.status_code == 200
    for text in (css.text, js.text):
        for match in re.finditer(r"https?://[^\s\"'`)]+", text):
            assert match.group(0).startswith("https://image.tmdb.org"), match.group(0)


def test_names_are_written_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_kill_config(tmp_path)
    make_kill_tree(tmp_path)
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=IronOwlIdentifier)
    run_scan(client)
    headers = csrf_headers(client)

    rows = browse_rows(client, config.sources[0])
    ready_row = next(row for row in rows if row["status"] == "ready" and row["item_keys"])
    item_key = ready_row["item_keys"][0]

    assert client.post(
        "/api/queue",
        json={"kind": "item", "item_key": item_key, "source": "inbox"},
        headers=headers,
    ).status_code == 200

    pages = [
        "/inbox",
        f"/match/{item_key}",
        "/execute",
        "/settings",
        "/history",
        "/fixes",
    ]
    for path in pages:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "Iron Owl: Vol" not in visible_text_without_tmdb(r.text)

    assert "Iron Owl - Vol. 1 (2003)" in visible_text(client.get("/inbox").text)
    assert "Iron Owl - Vol. 1 (2003)" in visible_text(client.get("/execute").text)


def test_settings_folder_picker_markup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _cfg = make_app(tmp_path, monkeypatch)
    html = client.get("/settings").text
    assert "<dialog" in html
    assert "data-folder-picker" in html


def test_static_app_js_contains_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _cfg = make_app(tmp_path, monkeypatch)
    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "/api/folders" in js.text
    assert "/in_place" in js.text


def test_inbox_ready_row_explains_rename_and_same_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    folder = "Gigasector 23 III The Waking of Ivy (1989)"
    make_single_tree(tmp_path, folder)
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=GigasectorReadyIdentifier)
    run_scan(client)
    headers = csrf_headers(client)

    rows = browse_rows(client, config.sources[0])
    row = next(r for r in rows if r.get("item_keys"))
    key = row["item_keys"][0]

    assert client.post(
        f"/api/item/{key}/in_place", json={"on": True}, headers=headers
    ).status_code == 200

    html = client.get("/inbox").text

    assert re.search(r'<ins data-diff-added>[^<]*Part 1[^<]*</ins>', html)
    assert 'data-outcome="in_place"' in html
    assert "Rename here" in html
    # Filing into the library would give a different folder, so the row offers it.
    assert 'data-row-mode="file"' in html

    assert "Keep your title:" in html
    assert "Use TMDB's:" in html
    assert folder in html
    assert "Gigasector 23 III Part 1 - The Waking of Ivy (1989)" in html

    assert client.post(
        f"/api/item/{key}/title_override", json={"choice": "keep"}, headers=headers
    ).status_code == 200

    html = client.get("/inbox").text
    assert "Already tidy" in html


def test_inbox_needs_row_explains_why_and_best_guess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    folder = "Gigasector 23 III The Day of Lanterns (1989)"
    make_single_tree(tmp_path, folder)
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=GigasectorNeedsIdentifier)
    run_scan(client)

    html = client.get("/inbox").text
    assert "data-why-needs" in html

    why_match = re.search(r'data-why-needs[^>]*>([^<]+)', html)
    assert why_match is not None
    why = html_lib.unescape(why_match.group(1))
    assert "The Day of Lanterns" in why
    assert "The Waking of Ivy" in why

    assert "If you confirm the top match:" in html
    path_match = re.search(r'data-best-guess[^>]*>([^<]+)', html)
    assert path_match is not None
    path = html_lib.unescape(path_match.group(1)).strip()
    assert path.startswith(str(tmp_path)) or re.match(r"^[A-Za-z]:\\", path) or path.startswith("\\\\")

    assert "stays here" not in html


def test_inbox_why_needs_two_close_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    make_single_tree(tmp_path, "Steel Maiden (2009)")
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=WonderCloseIdentifier)
    run_scan(client)

    html = client.get("/inbox").text
    why_match = re.search(r'data-why-needs[^>]*>([^<]+)', html)
    assert why_match is not None
    why = html_lib.unescape(why_match.group(1))
    assert why.startswith("Two close matches")


def test_inbox_plain_words_and_singular_file_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    make_single_tree(tmp_path, "One.Movie.2020")
    client, _cfg = make_app(tmp_path, monkeypatch, config)
    run_scan(client)

    html = client.get("/inbox").text
    text = visible_text(html)
    lowered = text.lower()

    assert "1 file" in text
    assert "1 files" not in text
    assert "needs review" not in lowered
    assert "identity" not in lowered
    assert "rename in place" not in lowered


def test_inbox_matched_item_with_nothing_to_do_is_tidy_not_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A confident match whose name is already right plans nothing. That is "Already tidy",
    # never "Needs you" (a reported "Mechafront (1985) — stays here — Needs you" rows).
    config = make_config(tmp_path)
    make_single_tree(tmp_path, "Mechafront (1985)")
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=GigasectorReadyIdentifier)
    run_scan(client)
    headers = csrf_headers(client)
    row = next(r for r in browse_rows(client, config.sources[0]) if r.get("item_keys"))
    key = row["item_keys"][0]
    assert client.post(
        f"/api/item/{key}/in_place", json={"on": True}, headers=headers
    ).status_code == 200

    rows = browse_rows(client, config.sources[0])
    mechafront = next(r for r in rows if r["name"] == "Mechafront (1985)")
    assert mechafront["status"] == "tidy"
    html = client.get("/inbox").text
    assert "Needs you</span>" not in html.split("<tbody>", 1)[1]


def test_inbox_needs_row_best_guess_is_the_full_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    make_single_tree(tmp_path, "Gigasector 23 III The Day of Lanterns (1989)")
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=GigasectorNeedsIdentifier)
    run_scan(client)

    html = client.get("/inbox").text
    path_match = re.search(r"data-best-guess[^>]*>([^<]+)", html)
    assert path_match is not None
    path = html_lib.unescape(path_match.group(1)).strip()
    assert path.endswith("Gigasector 23 III Part 1 - The Waking of Ivy (1989)")
    # Settle the match first: no title choice on a Needs-you row.
    assert "data-title-choice-row" not in html


def test_inbox_shows_working_folder_and_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    make_tree(tmp_path)
    client, _cfg = make_app(tmp_path, monkeypatch, config)
    run_scan(client)
    html = client.get("/inbox").text
    source = str(config.sources[0])

    assert "Working folder" in html and "Destination" in html
    m = re.search(r"data-working-folder>([^<]+)<", html)
    assert m is not None and html_lib.unescape(m.group(1)).strip() == source
    # The destination is named in full.
    assert str(config.libraries["destination"].path) in html
    assert "data-pick-workspace" in html and "<dialog" in html and "data-folder-picker" in html

    # The breadcrumb starts at the source and never offers a folder above it.
    crumbs = re.search(r'<nav aria-label="Folder path" class="breadcrumb">(.*?)</nav>', html, re.S)
    assert crumbs is not None
    for target in re.findall(r'data-browse="([^"]+)"', crumbs.group(1)):
        assert html_lib.unescape(target).startswith(source)
    assert str(tmp_path) + "<" not in crumbs.group(1)


def test_app_js_parses() -> None:
    # A JavaScript syntax error silently disables every button on every page.
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    js = Path(__file__).resolve().parents[1] / "mediakeep" / "web" / "static" / "app.js"
    result = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_history_can_be_cleared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime

    from mediakeep.models import RunRecord

    client, _cfg = make_app(tmp_path, monkeypatch)
    store = client.app.state.store  # type: ignore[attr-defined]
    for i in range(3):
        store.create_run(
            RunRecord(
                id=f"run{i}", plan_id=None, kind="apply", started_at=datetime.now(),
                finished_at=None, status="done", done=1, failed=0, skipped=0,
            )
        )
    html = client.get("/history").text
    assert "Clear all history" in html and "Remove from history" in html

    headers = csrf_headers(client)
    r = client.request("DELETE", "/api/runs", json={"ids": ["run0"]}, headers=headers)
    assert r.status_code == 200 and r.json()["removed"] == 1
    assert store.count_runs() == 2
    r = client.request("DELETE", "/api/runs", json={}, headers=headers)
    assert r.json()["removed"] == 2
    assert store.count_runs() == 0
    assert "Clear all history" not in client.get("/history").text


def test_history_details_show_the_undo_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime

    from mediakeep.models import RunRecord

    config = make_config(tmp_path)
    client, _cfg = make_app(tmp_path, monkeypatch, config)
    store = client.app.state.store  # type: ignore[attr-defined]
    for rid, n in (("short", 3), ("long", 150)):
        store.create_run(
            RunRecord(
                id=rid, plan_id=None, kind="apply", started_at=datetime.now(),
                finished_at=None, status="done", done=1, failed=0, skipped=0,
            )
        )
        script = config.state_dir_path() / "undo" / f"undo-{rid}.ps1"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "\n".join(f"Move-Item -LiteralPath 'Q:\\new\\{i}' -Destination 'Q:\\old\\{i}'" for i in range(n)),
            encoding="utf-8",
        )

    html = client.get("/history").text
    previews = re.findall(r"<pre[^>]*data-undo-script[^>]*>(.*?)</pre>", html, re.S)
    assert len(previews) == 2
    texts = sorted((html_lib.unescape(p) for p in previews), key=len)
    assert texts[0].count("\n") == 2  # all 3 lines
    assert texts[1].count("\n") == 99  # the first 100 lines only
    assert "Q:\\new\\99'" in texts[1] and "Q:\\new\\100'" not in texts[1]
    assert "first 100 of 150 lines" in html


def test_execute_badge_matches_the_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A reported bug: the sidebar said Execute 7, the page said "Apply 0 changes".
    config = make_config(tmp_path)
    make_single_tree(tmp_path, "Mechafront (1985)")
    make_single_tree(tmp_path, "Gigasector 23 III The Waking of Ivy (1989)")
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=GigasectorReadyIdentifier)
    run_scan(client)
    headers = csrf_headers(client)
    rows = {r["name"]: r for r in browse_rows(client, config.sources[0]) if r.get("item_keys")}
    mechafront = rows["Mechafront (1985)"]["item_keys"][0]
    gigasector = rows["Gigasector 23 III The Waking of Ivy (1989)"]["item_keys"][0]
    for key in (mechafront, gigasector):
        assert client.post(
            "/api/queue", json={"kind": "item", "item_key": key, "source": "inbox"}, headers=headers
        ).status_code == 200
    assert len(client.get("/api/queue").json()) == 2

    # Renamed here, Mechafront (1985) is already right: nothing left for its entry to do.
    client.post(f"/api/item/{mechafront}/in_place", json={"on": True}, headers=headers)

    html = client.get("/execute").text
    badge = re.search(r'id="execute-badge">(\d+)<', html)
    assert badge is not None and badge.group(1) == "1"
    assert re.search(r'Apply <span data-count-scope="execute">1</span> changes', html)
    assert "data-dropped" in html and "Mechafront (1985)" in html and "nothing to change any more" in html
    assert [e["item_key"] for e in client.get("/api/queue").json()] == [gigasector]


def test_inbox_filter_survives_check_the_match() -> None:
    js = (Path(__file__).resolve().parents[1] / "mediakeep" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert 'searchParams.set("show"' in js
    assert "restoreInboxFilter" in js
    assert "mk-return" in js and 'returnUrl("/execute")' in js


class YearOffIdentifier:
    """Needs review, and the top suggestion's year differs from the name's."""

    async def identify(
        self, items: Sequence[SourceItem], progress: ProgressSink
    ) -> dict[str, Identity]:
        out: dict[str, Identity] = {}
        for item in items:
            cand = Candidate(
                tmdb_id=777, kind=MediaKind.SHOW, title="Mechafront", original_title="",
                year=(item.year or 1985) + 1, original_language="", runtime_min=None, genres=[],
                countries=[], overview="", poster_url="https://image.tmdb.org/t/p/w342/r.jpg",
                imdb_id=None, score=70.0, reasons=["title match"],
            )
            out[item.key] = Identity(
                item_key=item.key, kind=MediaKind.SHOW, title="Mechafront", year=cand.year,
                tmdb_id=777, imdb_id=None, status=IdStatus.NEEDS_REVIEW, confidence=50.0,
                reasons=["year differs"], candidates=[cand],
            )
        return out


def test_needs_row_offers_the_top_match_when_the_year_agrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_poster import SparkIdentifier

    config = make_config(tmp_path)
    make_single_tree(tmp_path, "Spark (2020)")
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=SparkIdentifier)
    run_scan(client)

    html = client.get("/inbox").text
    row = re.search(r'<tr[^>]*data-status="needs"[^>]*>.*?</tr>', html, re.S)
    assert row is not None
    assert "data-quick-match" in row.group(0)
    assert "Spark (2020)" in row.group(0)
    assert re.search(r'class="poster(-box)? thumb"', row.group(0))
    key = re.search(r'data-quick-match data-item-key="([^"]+)"', row.group(0))
    tmdb = re.search(r'data-tmdb-id="(\d+)"', row.group(0))
    assert key is not None and tmdb is not None

    # One click: confirmed and no longer needs you.
    r = client.post(
        f"/api/match/{key.group(1)}/choose",
        json={"tmdb_id": int(tmdb.group(1)), "kind": "movie", "remember": False, "source": "inbox"},
        headers=csrf_headers(client),
    )
    assert r.status_code == 200, r.text
    html = client.get("/inbox").text
    assert not re.search(r'<tr[^>]*data-status="needs"', html)


def test_needs_row_hides_quick_match_when_the_title_does_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same year, different film: "The Day of Lanterns" is not "The Waking of Ivy".
    config = make_config(tmp_path)
    make_single_tree(tmp_path, "Gigasector 23 III The Day of Lanterns (1989)")
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=GigasectorNeedsIdentifier)
    run_scan(client)
    html = client.get("/inbox").text
    assert re.search(r'<tr[^>]*data-status="needs"', html)
    assert "data-quick-match" not in html


def test_needs_row_hides_quick_match_when_the_year_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    make_single_tree(tmp_path, "Mechafront (1985)")
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=YearOffIdentifier)
    run_scan(client)
    html = client.get("/inbox").text
    assert re.search(r'<tr[^>]*data-status="needs"', html)
    assert "data-quick-match" not in html
    assert "Check the match" in html


def test_inbox_page_barely_touches_the_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # On a network drive every disk check is slow; a page must not check each item again
    # and again (the sidebar count used to re-check every item in every source).
    config = make_config(tmp_path)
    for i in range(40):
        write_file(tmp_path / "source" / f"Some.Movie.{i}.(19{50 + i})" / f"some movie {i}.mkv", 1000)
    client, _cfg = make_app(tmp_path, monkeypatch, config)
    run_scan(client)
    client.get("/inbox")

    calls = {"n": 0}
    real_stat = os.stat

    def counting_stat(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return real_stat(*args, **kwargs)

    monkeypatch.setattr(os, "stat", counting_stat)
    assert client.get("/inbox").status_code == 200
    monkeypatch.setattr(os, "stat", real_stat)
    assert calls["n"] < 40, calls["n"]


def test_confirmed_match_is_not_looked_up_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    class Counting(GigasectorNeedsIdentifier):
        async def identify(self, items: Sequence[SourceItem], progress: ProgressSink) -> dict[str, Identity]:
            calls["n"] += 1
            return await super().identify(items, progress)

    config = make_config(tmp_path)
    make_single_tree(tmp_path, "Gigasector 23 III The Day of Lanterns (1989)")
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=Counting)
    run_scan(client)
    key = next(r for r in browse_rows(client, config.sources[0]) if r["status"] == "needs")["cards"][0]["item_key"]
    before = calls["n"]
    r = client.post(
        f"/api/match/{key}/choose",
        json={"tmdb_id": 555, "kind": "movie", "remember": False, "source": "inbox"},
        headers=csrf_headers(client),
    )
    assert r.status_code == 200 and r.json()["card"]["needs"] is False
    assert calls["n"] == before


def test_scan_progress_lists_what_it_is_looking_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    make_tree(tmp_path)
    client, _cfg = make_app(tmp_path, monkeypatch, config)
    job = run_scan(client)
    recent = job["progress"].get("recent") or []
    texts = [r["text"] for r in recent]
    seqs = [r["seq"] for r in recent]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # Folders read, items found and matches made all show up.
    assert any("Daydream.2010" in t for t in texts)
    assert any(t.startswith("movie: ") for t in texts)
    assert 'id="job-panel"' in client.get("/inbox").text


def test_restart_banner_when_code_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _cfg = make_app(tmp_path, monkeypatch)
    assert "data-restart-needed" not in client.get("/inbox").text
    app = client.app  # type: ignore[attr-defined]
    app.state.code_mtime -= 3600  # as if the code on disk is an hour newer than the app
    app.state.code_checked = (float("-inf"), False)
    assert "data-restart-needed" in client.get("/inbox").text
