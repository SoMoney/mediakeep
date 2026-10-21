"""The year in a name is evidence: searches use it, and remembered fixes respect it."""

from __future__ import annotations

from pathlib import Path

import pytest

from mediakeep.config import AppConfig
from mediakeep.hints import Hint, match_hint, path_matches_hint
from mediakeep.identify.identifier import _query_year
from mediakeep.models import FileInfo, MediaKind, SourceItem

from test_web_pages import (
    browse_rows,
    csrf_headers,
    make_app,
    make_config,
    make_tree,
    run_scan,
)


def _item(folder: str, files: list[str], year: int, source: str) -> SourceItem:
    root = Path("/media/Family")
    return SourceItem(
        key="k", root=root, paths=[root / folder], kind=MediaKind.MOVIE, title="x",
        year=year, year_source=source,
        videos=[FileInfo(path=root / folder / f, size=1, mtime_ns=0, hidden=False) for f in files],
    )


def test_folder_year_is_used_when_the_files_agree() -> None:
    config = AppConfig()
    assert config.naming.trust_folder_year is False
    item = _item("Duchess and the Bear (2017)", ["Duchess and the Bear (2017).mkv"], 2017, "folder")
    assert _query_year(config, item) == 2017
    # No year in the file name: the folder's year still counts.
    item = _item("101 Hedgehogs (1961)", ["101 Hedgehogs.mkv"], 1961, "folder")
    assert _query_year(config, item) == 1961


def test_folder_year_is_dropped_when_a_file_disagrees() -> None:
    item = _item("Movies (2019)", ["Some Film (2004).mkv"], 2019, "folder")
    assert _query_year(AppConfig(), item) is None


def _fix(**options: str) -> Hint:
    return Hint(id=1, pattern="Lucy in Paperland", name="Lucy in Paperland", options=options, origin="ui")


def test_title_fix_does_not_catch_another_years_film() -> None:
    fix = _fix(kind="movie", year="1951", tmdb="12092")
    root = Path("/media/Family")
    assert match_hint(root, Path("Lucy in Paperland (2010)"), [fix], folder=True) is None
    assert match_hint(root, Path("Lucy in Paperland (1951)"), [fix], folder=True) is fix
    assert match_hint(root, Path("Lucy in Paperland"), [fix], folder=True) is fix
    assert not path_matches_hint(fix, root / "Lucy in Paperland (2010)")
    assert path_matches_hint(fix, root / "Lucy.in.Paperland.1951.720p")


def test_fix_made_to_correct_a_wrong_year_still_applies_to_its_folder() -> None:
    # The name said 2009, the user picked the 1951 film: the fix remembers the name's year.
    fix = _fix(kind="movie", year="1951", tmdb="12092", name_year="2009")
    root = Path("/media/Family")
    assert match_hint(root, Path("Lucy in Paperland (2009)"), [fix], folder=True) is fix
    assert match_hint(root, Path("Lucy in Paperland (2010)"), [fix], folder=True) is None


def test_inbox_comes_back_to_the_whole_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    make_tree(tmp_path)
    client, _cfg = make_app(tmp_path, monkeypatch, config)
    run_scan(client)
    sub = config.sources[0] / "Daydream.2010"
    html = client.get("/inbox", params={"folder": str(sub)}).text
    assert "Show the whole folder" in html
    # Opening the Folder page again shows the whole folder, not the subfolder.
    html = client.get("/inbox").text
    assert "Show the whole folder" not in html
    assert len([r for r in browse_rows(client, config.sources[0]) if r.get("cards")]) > 1
    assert csrf_headers(client)


def test_why_needs_names_the_clash_first() -> None:
    from mediakeep.models import Candidate, Identity, IdStatus, Issue, Severity
    from mediakeep.web.planner import _why_needs

    item = _item("Lucy in Paperland (2010)", ["Lucy in Paperland (2010).mkv"], 2010, "folder")
    item.title = "Lucy in Paperland"
    cand = Candidate(
        tmdb_id=12092, kind=MediaKind.MOVIE, title="Lucy in Paperland", original_title="",
        year=1951, original_language="", runtime_min=None, genres=[], countries=[], overview="",
        poster_url=None, imdb_id=None, score=100.0, reasons=["confirmed by user"],
    )
    identity = Identity(
        item_key="k", kind=MediaKind.MOVIE, title="Lucy in Paperland", year=1951, tmdb_id=12092,
        imdb_id=None, status=IdStatus.CONFIRMED, confidence=100.0,
        reasons=["confirmed by user", "match chosen by user"], candidates=[cand],
    )
    clash = Issue(
        item_key="k", path=Path("/media/Family/x"), severity=Severity.ATTENTION,
        message="target already exists: /media/Family/Lucy in Paperland (1951)/Lucy in Paperland (1951).mkv",
    )
    why = _why_needs(AppConfig(), item, identity, [clash])
    assert why.startswith('Matched as "Lucy in Paperland (1951)", but that name is already taken')
    assert "100% sure" not in why
    # Without the clash, the year difference is the reason.
    why = _why_needs(AppConfig(), item, identity, [])
    assert "the name says 2010" in why


def _needs_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remember_default: bool):  # type: ignore[no-untyped-def]
    from test_web_pages import GigasectorNeedsIdentifier, make_single_tree

    config = make_config(tmp_path)
    config = config.model_copy(
        update={"behavior": config.behavior.model_copy(update={"remember_choices": remember_default})}
    )
    make_single_tree(tmp_path, "Gigasector 23 III The Day of Lanterns (1989)")
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=GigasectorNeedsIdentifier)
    run_scan(client)
    key = next(r for r in browse_rows(client, config.sources[0]) if r["status"] == "needs")["cards"][0]["item_key"]
    return client, key


def _fix_count(client) -> int:  # type: ignore[no-untyped-def]
    store = client.app.state.store
    return len(store.list_hints())


def test_choices_are_not_remembered_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert AppConfig().behavior.remember_choices is False
    client, key = _needs_client(tmp_path, monkeypatch, remember_default=False)
    html = client.get(f"/match/{key}").text
    assert 'id="remember-choice" >' in html or 'id="remember-choice">' in html  # unticked
    r = client.post(f"/api/match/{key}/choose", json={"tmdb_id": 555, "kind": "movie"}, headers=csrf_headers(client))
    assert r.status_code == 200
    assert _fix_count(client) == 0
    # Ticking the box still saves one.
    r = client.post(
        f"/api/match/{key}/choose", json={"tmdb_id": 555, "kind": "movie", "remember": True}, headers=csrf_headers(client)
    )
    assert _fix_count(client) == 1
    settings = client.get("/settings").text
    assert 'data-setting-path="behavior.remember_choices"' in settings


def test_remember_by_default_when_turned_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, key = _needs_client(tmp_path, monkeypatch, remember_default=True)
    html = client.get(f"/match/{key}").text
    assert 'id="remember-choice" checked' in html
    client.post(f"/api/match/{key}/choose", json={"tmdb_id": 555, "kind": "movie"}, headers=csrf_headers(client))
    assert _fix_count(client) == 1
    # The one-click button on the Needs-you list never saves a fix.
    client2, key2 = _needs_client(tmp_path / "b", monkeypatch, remember_default=True)
    client2.post(
        f"/api/match/{key2}/choose", json={"tmdb_id": 555, "kind": "movie", "remember": False},
        headers=csrf_headers(client2),
    )
    assert _fix_count(client2) == 0
