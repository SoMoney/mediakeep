"""Reported cases: titles cut at "Mr." / "vs.", ROBO·T, and old matches that came back."""

from __future__ import annotations

from pathlib import Path

import pytest

from mediakeep.agree import folder_agrees
from mediakeep.classify import resolve_title_year
from mediakeep.config import AppConfig, LibraryConfig
from mediakeep.identify.identifier import _confirmed_from_store
from mediakeep.models import Candidate, FileInfo, Identity, IdStatus, MediaKind, SourceItem, identity_to_dict
from mediakeep.parse import parse_release_name
from mediakeep.paths import sanitize_name
from mediakeep.pipeline import _path_identity_for_item
from mediakeep.store import Store


@pytest.mark.parametrize(
    "folder, title, year",
    [
        ("The Amazing Mr. Puddle (1964)", "The Amazing Mr. Puddle", 1964),
        ("Mr. Whiskers & Pip (2014)", "Mr. Whiskers & Pip", 2014),
        ("Outfoxed Too! Fox vs. Evil (2011)", "Outfoxed Too! Fox vs. Evil", 2011),
        ("Mr. Tumble's Parrots (2011)", "Mr. Tumble's Parrots", 2011),
        ("Coyote vs. Acme (2026)", "Coyote vs. Acme", 2026),
        ("The Brave Kettle Bros. Movie (2023)", "The Brave Kettle Bros. Movie", 2023),
        # Dot-separated release names still read as before.
        ("Mr.Whiskers.and.Pip.2014.1080p.BluRay.x264", "Mr Whiskers and Pip", 2014),
        ("Daydream.2010", "Daydream", 2010),
    ],
)
def test_folder_names_with_full_stops_keep_their_whole_title(folder: str, title: str, year: int) -> None:
    video = [FileInfo(path=Path(folder) / "x.mkv", size=1, mtime_ns=0, hidden=False)]
    assert resolve_title_year(folder, video, "", False)[:2] == (title, year)
    assert parse_release_name(folder + ".mkv").title == title


def test_episode_names_with_full_stops() -> None:
    from mediakeep.parse import parse_episode

    ep = parse_episode("Mr. Robot S01E02")
    assert ep is not None and (ep.season, ep.episode) == (1, 2)


def test_typographic_characters_become_plain() -> None:
    assert sanitize_name("ROBO·T (2008)") == "ROBO-T (2008)"
    assert sanitize_name("Don’t Look Sideways (2021)") == "Don't Look Sideways (2021)"
    assert sanitize_name("Spider‑Man – Far From Home") == "Spider-Man - Far From Home"


def _identity(title: str, year: int, tmdb_title: str | None = None, reasons: list[str] | None = None) -> Identity:
    cand = Candidate(
        tmdb_id=7, kind=MediaKind.MOVIE, title=tmdb_title or title, original_title="", year=year,
        original_language="", runtime_min=None, genres=[], countries=[], overview="",
        poster_url=None, imdb_id=None, score=90.0, reasons=[],
    )
    return Identity(
        item_key="k", kind=MediaKind.MOVIE, title=title, year=year, tmdb_id=7, imdb_id=None,
        status=IdStatus.AUTO, confidence=90.0, reasons=reasons or ["score 90"], candidates=[cand],
    )


def _library_setup(tmp_path: Path, folder_name: str, remembered: Identity) -> tuple[AppConfig, Store, SourceItem]:
    lib = tmp_path / "Family"
    folder = lib / folder_name
    folder.mkdir(parents=True)
    (folder / f"{folder_name}.avi").write_bytes(b"x")
    config = AppConfig(
        libraries={"family": LibraryConfig(path=lib, kind=MediaKind.MOVIE)},
        routing={"movie": "family", "show": "family"},
        state_dir=str(tmp_path / "state"),
    )
    store = Store(tmp_path / "state" / "mk.db")
    store.set_path_identity(folder, identity_to_dict(remembered), "confirmed")
    item = SourceItem(key="k", root=lib, paths=[folder], kind=MediaKind.MOVIE, title="x", year=None)
    return config, store, item


@pytest.mark.parametrize(
    "folder, remembered",
    [
        ("Mr. Whiskers & Pip (2014)", _identity("Mr", 2014, "Mr. Grumble")),
        ("Mr. Tumble's Parrots (2011)", _identity("Mr. Sparkle", 2011)),
        ("Zafir (2019)", _identity("Zafir: A Musical Spectacular", 1992)),
        ("101 Hedgehogs (1961)", _identity("101 Hedgehogs", 1996)),
    ],
)
def test_an_old_wrong_match_is_not_trusted(tmp_path: Path, folder: str, remembered: Identity) -> None:
    config, store, item = _library_setup(tmp_path, folder, remembered)
    assert _path_identity_for_item(config, store, item) is None


def test_a_remembered_match_that_fits_is_reused(tmp_path: Path) -> None:
    config, store, item = _library_setup(tmp_path, "Finding Pebble (2003)", _identity("Finding Pebble", 2003))
    got = _path_identity_for_item(config, store, item)
    assert got is not None and got.status == IdStatus.CONFIRMED


def test_a_match_you_kept_stays_even_if_the_name_differs(tmp_path: Path) -> None:
    from mediakeep.agree import KEPT

    mine = _identity("Fin Fighters", 2006, reasons=["confirmed by user", "match chosen by user", KEPT])
    config, store, item = _library_setup(tmp_path, "The Lagoon (2006)", mine)
    got = _path_identity_for_item(config, store, item)
    assert got is not None and got.title == "Fin Fighters"


def test_a_pick_that_does_not_fit_is_set_aside(tmp_path: Path) -> None:
    # Made with the old one-click button (year only): "Mr. Tumble's Parrots" -> "Mr. Sparkle".
    mine = _identity("Mr. Sparkle", 2011, reasons=["confirmed by user", "match chosen by user"])
    config, store, item = _library_setup(tmp_path, "Mr. Tumble's Parrots (2011)", mine)
    assert _path_identity_for_item(config, store, item) is None


def test_match_from_a_deleted_fix_is_worked_out_again(tmp_path: Path) -> None:
    store = Store(tmp_path / "mk.db")
    item = SourceItem(key="k", root=tmp_path, paths=[tmp_path / "Lucy in Paperland (2010)"], kind=MediaKind.MOVIE, title="Lucy in Paperland", year=2010)
    from_fix = _identity("Lucy in Paperland", 1951, reasons=["recorded TMDB id"])
    from_fix.status = IdStatus.CONFIRMED
    store.set_identity("k", identity_to_dict(from_fix), "confirmed")
    assert _confirmed_from_store(store, item) is None
    picked = _identity("Lucy in Paperland", 2010, reasons=["confirmed by user"])
    picked.status = IdStatus.CONFIRMED
    store.set_identity("k", identity_to_dict(picked), "confirmed")
    assert _confirmed_from_store(store, item) is not None


def test_folder_agrees_with_short_own_title() -> None:
    assert folder_agrees("The Brave Kettle Bros (2023)", _identity("The Brave Kettle Bros. Movie", 2023))
    assert folder_agrees("ROBO-T (2008)", _identity("ROBO·T", 2008))


def test_picks_the_page_keeps_and_the_ones_it_sets_aside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_web_pages import browse_rows, csrf_headers, make_app, make_config, run_scan, write_file

    config = make_config(tmp_path)
    write_file(tmp_path / "source" / "Mr. Whiskers & Pip (2014)" / "Mr. Whiskers & Pip (2014).mp4", 1000)
    client, _cfg = make_app(tmp_path, monkeypatch, config)
    run_scan(client)
    key = browse_rows(client, config.sources[0])[0]["cards"][0]["item_key"]

    async def odd_pick(cfg, store, item, tmdb_id, kind):  # type: ignore[no-untyped-def]
        ident = _identity("Mr. Grumble", 2014)
        ident.item_key = item.key
        ident.status = IdStatus.CONFIRMED
        ident.reasons = ["confirmed by user"]
        return ident

    monkeypatch.setattr("mediakeep.web.app.identity_for", odd_pick)
    headers = csrf_headers(client)

    # A one-click pick that doesn't fit the name is not kept: the item is matched again.
    r = client.post(
        f"/api/match/{key}/choose",
        json={"tmdb_id": 7, "kind": "movie", "remember": False, "source": "inbox"},
        headers=headers,
    )
    assert r.status_code == 200
    assert "Mr. Grumble" not in r.json()["card"]["label"]

    # Picked on the match page from the full list: deliberate, so it stays.
    r = client.post(f"/api/match/{key}/choose", json={"tmdb_id": 7, "kind": "movie", "source": "match"}, headers=headers)
    card = r.json()["card"]
    assert card["label"].startswith("Mr. Grumble") and card["doubtful_pick"] is False


class _TitleIdentifier:
    """Library items whose TMDB title differs from the folder name."""

    MATCHES = {
        "Goblins and Gadgets": ("Goblins & Gadgets", 2026, IdStatus.AUTO, 100.0),
    }

    async def identify(self, items, progress):  # type: ignore[no-untyped-def]
        out = {}
        for item in items:
            title, year, status, conf = self.MATCHES[item.title]
            cand = Candidate(
                tmdb_id=hash(title) % 100000, kind=MediaKind.MOVIE, title=title, original_title="",
                year=year, original_language="", runtime_min=None, genres=[], countries=[],
                overview="", poster_url=None, imdb_id=None, score=conf, reasons=[],
            )
            out[item.key] = Identity(
                item_key=item.key, kind=MediaKind.MOVIE, title=title, year=year,
                tmdb_id=cand.tmdb_id, imdb_id=None, status=status, confidence=conf,
                reasons=[f"score {conf:.0f}"], candidates=[cand],
            )
        return out


def test_choosing_tmdbs_title_when_renaming_here_goes_to_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = "Goblins and Gadgets (2026)"
    from test_web_pages import browse_rows, csrf_headers, make_app, make_config, run_scan, write_file

    config = make_config(tmp_path, destination=False)
    config = config.model_copy(update={"naming": config.naming.model_copy(update={"folder_jpg": False})})
    root = config.sources[0]
    write_file(root / folder / f"{folder}.mkv", 1000)
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=_TitleIdentifier)
    run_scan(client)

    def card_for(name: str) -> dict:  # type: ignore[type-arg]
        row = next(r for r in browse_rows(client, root) if r["name"] == name)
        return dict(row["cards"][0])

    card = card_for(folder)
    assert card["title_choice"] is not None
    headers = csrf_headers(client)

    r = client.post(f"/api/item/{card['item_key']}/title_override", json={"choice": "tmdb"}, headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["queued"] is True and data["message"].startswith("Added to Execute")
    queue = client.get("/api/queue").json()
    assert [e["item_key"] for e in queue] == [card["item_key"]]
    # The match is kept even though TMDB's title differs from the folder name.
    again = card_for(folder)
    assert again["needs"] is False and again["doubtful_pick"] is False

    # Back to "keep": nothing to rename, and the Execute entry goes.
    r = client.post(f"/api/item/{card['item_key']}/title_override", json={"choice": "keep"}, headers=headers)
    assert r.json()["queued"] is False
    assert client.get("/api/queue").json() == []
