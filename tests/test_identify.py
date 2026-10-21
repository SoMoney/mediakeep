from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from mediakeep.config import AppConfig, LibraryConfig, NamingConfig, TmdbConfig
from mediakeep.identify import (
    TmdbIdentifier,
    create_identifier,
    identity_for,
    search,
)
from mediakeep.identify.identifier import _identity_to_dict
from mediakeep.identify.probe import ProbeRunner
from mediakeep.identify.route import pick_library
from mediakeep.identify.tmdb import TmdbClient, TmdbUnavailable
from mediakeep.models import (
    EpisodeRef,
    FileInfo,
    Identity,
    IdStatus,
    MediaKind,
    ProgressSink,
    SourceItem,
)
from mediakeep.pipeline import NullIdentifier, build_plan
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


class FakeResponse:
    def __init__(
        self, status: int, payload: Any, headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    async def text(self) -> str:
        return json.dumps(self._payload)

    async def json(self) -> Any:
        return self._payload

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeRequest:
    def __init__(self, session: "FakeSession", url: str, params: dict[str, Any] | None) -> None:
        self.session = session
        self.url = url
        self.params = params

    async def __aenter__(self) -> FakeResponse:
        return self.session.handler(self.url, self.params)

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeSession:
    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: Any = None,
    ) -> FakeRequest:
        self.calls.append((url, params))
        return FakeRequest(self, url, params)

    async def close(self) -> None:
        return None


class FakeProbe:
    def __init__(self, duration_seconds: float | None = None) -> None:
        self.duration_seconds = duration_seconds

    async def enrich_item(self, item: SourceItem, audio: Any = None) -> float | None:
        return self.duration_seconds


class FakeAudio:
    def __init__(self, language: str = "ja") -> None:
        self.language = language

    async def detect_language(self, video: Path) -> tuple[str, float] | None:
        return (self.language, 0.9)

    async def transcript(self, video: Path) -> str | None:
        return None


class FakeIdentifier:
    async def identify(
        self, items: list[SourceItem], progress: ProgressSink
    ) -> dict[str, Identity]:
        out: dict[str, Identity] = {}
        for item in items:
            out[item.key] = Identity(
                item_key=item.key,
                kind=MediaKind.SHOW,
                title="Show",
                year=2010,
                tmdb_id=1,
                imdb_id=None,
                status=IdStatus.AUTO,
                confidence=100.0,
                reasons=[],
                episode_titles={(1, 1): "Pilot"},
                library="tv",
            )
        return out


def make_config(tmp_path: Path, api_key: str = "test", read_token: str = "") -> AppConfig:
    return AppConfig(
        behavior={"default_destination": "file"},  # these tests file into libraries
        sources=[tmp_path / "src"],
        libraries={
            "movies": LibraryConfig(path=tmp_path / "movies", kind=MediaKind.MOVIE),
            "tv": LibraryConfig(path=tmp_path / "tv", kind=MediaKind.SHOW),
        },
        naming=NamingConfig(id_tag="tmdb"),
        tmdb=TmdbConfig(api_key=api_key, read_token=read_token),
        state_dir=str(tmp_path / "state"),
    )


def make_store(tmp_path: Path) -> Store:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    return Store(state / "mediakeep.db")


def make_client(
    config: AppConfig, store: Store, handler: Any
) -> tuple[TmdbClient, FakeSession]:
    session = FakeSession(handler)
    client = TmdbClient(config, store, session_factory=lambda: session, base_delay=0.0)
    return client, session


def make_file(name: str = "X.mkv", size: int = 1000) -> FileInfo:
    return FileInfo(path=Path(name), size=size, mtime_ns=1)


def make_item(
    key: str = "k1",
    kind: MediaKind = MediaKind.MOVIE,
    title: str = "Title",
    year: int | None = None,
    videos: list[FileInfo] | None = None,
    episodes: list[EpisodeRef] | None = None,
    hint_options: dict[str, str] | None = None,
) -> SourceItem:
    vids = videos or [make_file()]
    return SourceItem(
        key=key,
        root=Path("/"),
        paths=[vids[0].path],
        kind=kind,
        title=title,
        year=year,
        year_source="file" if year is not None else "",
        videos=vids,
        episodes=episodes or [],
        hint_options=hint_options or {},
    )


def make_identity(
    kind: MediaKind = MediaKind.MOVIE,
    title: str = "Title",
    year: int | None = None,
    genres: list[str] | None = None,
    countries: list[str] | None = None,
    original_language: str | None = None,
    library: str | None = None,
) -> Identity:
    return Identity(
        item_key="k",
        kind=kind,
        title=title,
        year=year,
        tmdb_id=None,
        imdb_id=None,
        status=IdStatus.AUTO,
        confidence=0.0,
        reasons=[],
        genres=genres or [],
        countries=countries or [],
        original_language=original_language,
        library=library,
    )


def season_payload(season: int, count: int) -> dict[str, Any]:
    return {
        "season_number": season,
        "episodes": [
            {
                "season_number": season,
                "episode_number": i,
                "name": f"S{season:02d}E{i:02d}",
            }
            for i in range(1, count + 1)
        ],
    }


def named_season_payload(season: int, names: list[str]) -> dict[str, Any]:
    return {
        "season_number": season,
        "episodes": [
            {
                "season_number": season,
                "episode_number": i,
                "name": name,
            }
            for i, name in enumerate(names, start=1)
        ],
    }


def write_file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


def offline_handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
    raise aiohttp.ClientError("offline")


def make_tmdb_setup(
    tmp_path: Path, handler: Any
) -> tuple[AppConfig, Store, TmdbIdentifier]:
    config = make_config(tmp_path)
    store = make_store(tmp_path)
    client, _session = make_client(config, store, handler)
    identifier = TmdbIdentifier(
        config, store, None, client=client, probe=FakeProbe(None)
    )
    return config, store, identifier


def make_probed_tmdb_setup(
    tmp_path: Path, handler: Any, duration_seconds: float | None
) -> tuple[AppConfig, Store, TmdbIdentifier]:
    config = make_config(tmp_path)
    store = make_store(tmp_path)
    client, _session = make_client(config, store, handler)
    identifier = TmdbIdentifier(
        config, store, None, client=client, probe=FakeProbe(duration_seconds)
    )
    return config, store, identifier


BANTAM_NAMES = [
    "Release the Brown Fat",
    "Trousers",
    "White Whale",
    "Angry Like an Angry Person",
    "Mr. Razzles",
    "Cop Hawk",
    "All the Dog's Names",
    "Nobody Spook It",
    "Ludlow's Fourth Hottest Professor",
    "Songs for Raisa",
]

GULLWING_NAMES = [
    "Welcome to Gullwing's Bay",
    "Lodging",
    "Episode Three",
    "Episode Four",
    "Episode Five",
    "Episode Six",
    "Episode Seven",
    "Episode Eight",
    "Episode Nine",
    "Episode Ten",
]

S04_NAMES = [
    "Adrift Part 2",
    "Home",
    "The Defiant Ones",
    "Duet",
    "The Lost Tribe",
    "Hidden",
    "The Pegasus Project",
    "Inferno",
    "Critical Mass",
    "The Long Goodbye",
    "Remembered",
    "Trinity",
    "The Tower",
    "The Gift",
    "The Return",
    "The Siege",
    "The Last Man",
    "The Last Man Part 2",
    "The Last Man Part 3",
    "The Last Man Part 4",
]

S05_NAMES = [
    "Search and Rescue",
    "Infection",
    "Uncharted",
    "The Lost and the Found",
    "The Gift",
    "The Return",
    "The Siege",
    "The Last Man",
    "The Last Man Part 2",
    "The Last Man Part 3",
    "The Last Man Part 4",
    "Ep 12",
    "Ep 13",
    "Ep 14",
    "Ep 15",
    "Ep 16",
    "Ep 17",
    "Ep 18",
    "Ep 19",
    "Enemy at the Gate",
]


@pytest.mark.asyncio
async def test_starfall_auto(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = make_store(tmp_path)

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/movie"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 157336,
                            "title": "Starfall",
                            "original_title": "Starfall",
                            "release_date": "2014-11-05",
                            "original_language": "en",
                            "vote_count": 10000,
                        }
                    ]
                },
            )
        if url.endswith("/movie/157336"):
            return FakeResponse(
                200,
                {
                    "title": "Starfall",
                    "original_title": "Starfall",
                    "release_date": "2014-11-05",
                    "runtime": 169,
                    "original_language": "en",
                    "genres": [{"name": "Science Fiction"}],
                    "production_countries": [
                        {"iso_3166_1": "US", "name": "United States of America"}
                    ],
                    "external_ids": {"imdb_id": "tt0816692"},
                },
            )
        return FakeResponse(404, {})

    client, _session = make_client(config, store, handler)
    item = make_item(kind=MediaKind.MOVIE, title="Starfall", year=2014)
    identifier = TmdbIdentifier(
        config, store, None, client=client, probe=FakeProbe(169 * 60)
    )
    result = await identifier.identify([item], NullProgress())
    identity = result[item.key]

    assert identity.status == IdStatus.AUTO
    assert identity.tmdb_id == 157336
    assert identity.runtime_min == 169
    assert any("runtime" in reason.lower() for reason in identity.reasons)
    assert identity.candidates[0].score >= 90


@pytest.mark.asyncio
async def test_the_thing_with_year_picks_right(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = make_store(tmp_path)

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/movie"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 1,
                            "title": "The Thing",
                            "original_title": "The Thing",
                            "release_date": "1982-06-25",
                            "original_language": "en",
                            "vote_count": 100,
                        },
                        {
                            "id": 2,
                            "title": "The Thing",
                            "original_title": "The Thing",
                            "release_date": "2011-10-12",
                            "original_language": "en",
                            "vote_count": 100,
                        },
                    ]
                },
            )
        if url.endswith("/movie/1"):
            return FakeResponse(
                200,
                {
                    "title": "The Thing",
                    "original_title": "The Thing",
                    "release_date": "1982-06-25",
                    "runtime": 109,
                    "original_language": "en",
                },
            )
        if url.endswith("/movie/2"):
            return FakeResponse(
                200,
                {
                    "title": "The Thing",
                    "original_title": "The Thing",
                    "release_date": "2011-10-12",
                    "runtime": 102,
                    "original_language": "en",
                },
            )
        return FakeResponse(404, {})

    client, _session = make_client(config, store, handler)
    item = make_item(kind=MediaKind.MOVIE, title="The Thing", year=1982)
    identifier = TmdbIdentifier(
        config, store, None, client=client, probe=FakeProbe(109 * 60)
    )
    result = await identifier.identify([item], NullProgress())
    assert result[item.key].tmdb_id == 1


@pytest.mark.asyncio
async def test_the_thing_without_year_needs_review(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = make_store(tmp_path)

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/movie"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 1,
                            "title": "The Thing",
                            "original_title": "The Thing",
                            "release_date": "1982-06-25",
                            "original_language": "en",
                            "vote_count": 0,
                        },
                        {
                            "id": 2,
                            "title": "The Thing",
                            "original_title": "The Thing",
                            "release_date": "2011-10-12",
                            "original_language": "en",
                            "vote_count": 0,
                        },
                    ]
                },
            )
        if url.endswith("/movie/1"):
            return FakeResponse(
                200,
                {
                    "title": "The Thing",
                    "original_title": "The Thing",
                    "release_date": "1982-06-25",
                    "original_language": "en",
                },
            )
        if url.endswith("/movie/2"):
            return FakeResponse(
                200,
                {
                    "title": "The Thing",
                    "original_title": "The Thing",
                    "release_date": "2011-10-12",
                    "original_language": "en",
                },
            )
        return FakeResponse(404, {})

    client, _session = make_client(config, store, handler)
    item = make_item(kind=MediaKind.MOVIE, title="The Thing", year=None)
    identifier = TmdbIdentifier(config, store, None, client=client, probe=FakeProbe(None))
    result = await identifier.identify([item], NullProgress())
    identity = result[item.key]
    assert identity.status == IdStatus.NEEDS_REVIEW
    assert len(identity.candidates) == 2


@pytest.mark.asyncio
async def test_fargo_movie_folder_gets_movie(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = make_store(tmp_path)

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/movie"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 1,
                            "title": "Fargo",
                            "original_title": "Fargo",
                            "release_date": "1996-03-08",
                            "original_language": "en",
                            "vote_count": 100,
                        }
                    ]
                },
            )
        if url.endswith("/search/tv"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 2,
                            "name": "Fargo",
                            "original_name": "Fargo",
                            "first_air_date": "2014-04-15",
                            "original_language": "en",
                            "vote_count": 100,
                        }
                    ]
                },
            )
        if url.endswith("/movie/1"):
            return FakeResponse(
                200,
                {
                    "title": "Fargo",
                    "original_title": "Fargo",
                    "release_date": "1996-03-08",
                    "runtime": 98,
                    "original_language": "en",
                },
            )
        if url.endswith("/tv/2"):
            return FakeResponse(
                200,
                {
                    "name": "Fargo",
                    "original_name": "Fargo",
                    "first_air_date": "2014-04-15",
                    "episode_run_time": [53],
                    "original_language": "en",
                },
            )
        return FakeResponse(404, {})

    client, _session = make_client(config, store, handler)
    item = make_item(kind=MediaKind.MOVIE, title="Fargo", year=1996)
    identifier = TmdbIdentifier(config, store, None, client=client, probe=FakeProbe(98 * 60))
    result = await identifier.identify([item], NullProgress())
    assert result[item.key].kind == MediaKind.MOVIE
    assert result[item.key].tmdb_id == 1


@pytest.mark.asyncio
async def test_fargo_show_folder_gets_show(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = make_store(tmp_path)
    video = make_file("Fargo.S01E01.mkv")
    episode = EpisodeRef(
        file=video,
        season=1,
        episode=1,
        episode_end=None,
        title="",
        version_tag=None,
        absolute=False,
    )

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/tv"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 2,
                            "name": "Fargo",
                            "original_name": "Fargo",
                            "first_air_date": "2014-04-15",
                            "original_language": "en",
                            "vote_count": 100,
                        }
                    ]
                },
            )
        if "/season/" in url:
            return FakeResponse(200, season_payload(1, 10))
        if url.endswith("/tv/2"):
            return FakeResponse(
                200,
                {
                    "name": "Fargo",
                    "original_name": "Fargo",
                    "first_air_date": "2014-04-15",
                    "episode_run_time": [53],
                    "original_language": "en",
                },
            )
        return FakeResponse(404, {})

    client, _session = make_client(config, store, handler)
    item = make_item(
        kind=MediaKind.SHOW,
        title="Fargo",
        year=2014,
        videos=[video],
        episodes=[episode],
    )
    identifier = TmdbIdentifier(config, store, None, client=client, probe=FakeProbe(53 * 60))
    result = await identifier.identify([item], NullProgress())
    assert result[item.key].kind == MediaKind.SHOW
    assert result[item.key].tmdb_id == 2


@pytest.mark.asyncio
async def test_loose_file_kind_disagreement_needs_review(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = make_store(tmp_path)

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/movie"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 1,
                            "title": "Fargo",
                            "original_title": "Fargo",
                            "release_date": "1996-03-08",
                            "original_language": "en",
                            "vote_count": 10,
                        }
                    ]
                },
            )
        if url.endswith("/search/tv"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 2,
                            "name": "Fargo",
                            "original_name": "Fargo",
                            "first_air_date": "2014-04-15",
                            "original_language": "en",
                            "vote_count": 10,
                        }
                    ]
                },
            )
        if url.endswith("/movie/1"):
            return FakeResponse(
                200,
                {
                    "title": "Fargo",
                    "original_title": "Fargo",
                    "release_date": "1996-03-08",
                    "original_language": "en",
                },
            )
        if url.endswith("/tv/2"):
            return FakeResponse(
                200,
                {
                    "name": "Fargo",
                    "original_name": "Fargo",
                    "first_air_date": "2014-04-15",
                    "original_language": "en",
                },
            )
        return FakeResponse(404, {})

    client, _session = make_client(config, store, handler)
    item = make_item(
        kind=MediaKind.MOVIE,
        title="Fargo",
        year=2014,
        videos=[make_file("Fargo - 05.mkv")],
    )
    identifier = TmdbIdentifier(config, store, None, client=client, probe=FakeProbe(None))
    result = await identifier.identify([item], NullProgress())
    identity = result[item.key]
    assert identity.status == IdStatus.NEEDS_REVIEW
    assert identity.kind == MediaKind.SHOW
    assert any("differs from classification" in reason for reason in identity.reasons)


@pytest.mark.asyncio
async def test_retry_after_then_success(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = make_store(tmp_path)
    state = {"n": 0}

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/search/movie"):
            state["n"] += 1
            if state["n"] == 1:
                return FakeResponse(429, {}, {"Retry-After": "0"})
            return FakeResponse(200, {"results": []})
        return FakeResponse(404, {})

    client, _session = make_client(config, store, handler)
    results = await client.search_movie("X")
    assert results == []
    assert state["n"] == 2


@pytest.mark.asyncio
async def test_offline_stale_cache(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = make_store(tmp_path)
    client = TmdbClient(
        config,
        store,
        session_factory=lambda: FakeSession(offline_handler),
        base_delay=0.0,
    )
    client._ensure_cache()
    key = "/search/movie?language=en-US&query=X"
    body = json.dumps({"results": [{"id": 1, "title": "X", "vote_count": 0}]})
    fetched = (datetime.now() - timedelta(days=2)).isoformat()
    conn = store._conn()
    conn.execute(
        "INSERT OR REPLACE INTO api_cache (key, body, fetched_at, ttl_s) VALUES (?, ?, ?, ?)",
        (key, body, fetched, 1),
    )
    conn.commit()

    results = await client.search_movie("X")
    assert results[0]["id"] == 1


@pytest.mark.asyncio
async def test_offline_no_cache(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = make_store(tmp_path)
    client = TmdbClient(
        config,
        store,
        session_factory=lambda: FakeSession(offline_handler),
        base_delay=0.0,
    )
    with pytest.raises(TmdbUnavailable):
        await client.search_movie("Nope")


@pytest.mark.asyncio
async def test_absolute_show_mapping(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = make_store(tmp_path)
    video = make_file("Show - 27.mkv")
    episode = EpisodeRef(
        file=video,
        season=1,
        episode=27,
        episode_end=None,
        title="",
        version_tag=None,
        absolute=True,
    )

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/tv"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 1,
                            "name": "Show",
                            "original_name": "Show",
                            "first_air_date": "2010-01-01",
                            "original_language": "en",
                            "vote_count": 100,
                        }
                    ]
                },
            )
        if "/tv/1/season/1" in url:
            return FakeResponse(200, season_payload(1, 26))
        if "/tv/1/season/2" in url:
            return FakeResponse(200, season_payload(2, 26))
        if url.endswith("/tv/1"):
            return FakeResponse(
                200,
                {
                    "name": "Show",
                    "original_name": "Show",
                    "first_air_date": "2010-01-01",
                    "episode_run_time": [24],
                    "original_language": "en",
                },
            )
        return FakeResponse(404, {})

    client, _session = make_client(config, store, handler)
    item = make_item(
        kind=MediaKind.SHOW,
        title="Show",
        year=2010,
        videos=[video],
        episodes=[episode],
    )
    identifier = TmdbIdentifier(config, store, None, client=client, probe=FakeProbe(24 * 60))
    result = await identifier.identify([item], NullProgress())
    identity = result[item.key]

    assert item.episodes[0].season == 2
    assert item.episodes[0].episode == 1
    assert item.episodes[0].absolute is False
    assert identity.episode_titles.get((2, 1)) == "S02E01"
    assert any("absolute 27 -> S02E01" in reason for reason in identity.reasons)


@pytest.mark.asyncio
async def test_episode_titles_used_in_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    store = make_store(tmp_path)
    write_file(tmp_path / "src" / "Show (2010)" / "Season 01" / "Show (2010) S01E01.mkv", 1000)

    monkeypatch.setattr(
        "mediakeep.identify.create_identifier",
        lambda config, store, audio: FakeIdentifier(),
    )
    plan = await build_plan(config, store, NullProgress())
    assert any(
        "Pilot" in str(action.dst)
        for batch in plan.batches
        for action in batch.actions
        if action.dst is not None
    )


def test_routing_genre_country_and_order(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    libs = dict(config.libraries)
    libs["anime"] = LibraryConfig(path=tmp_path / "anime", kind=MediaKind.MOVIE)
    config.libraries = libs
    config.routing.rules = [
        {"when": {"kind": "show"}, "to": "tv"},
        {
            "when": {
                "kind": "movie",
                "genres_any": ["Animation"],
                "countries_any": ["JP"],
            },
            "to": "anime",
        },
        {"when": {"kind": "movie"}, "to": "movies"},
    ]

    identity = make_identity(
        kind=MediaKind.MOVIE,
        genres=["Animation"],
        countries=["JP", "Japan"],
        original_language="ja",
    )
    assert pick_library(config, identity) == "anime"

    identity2 = make_identity(kind=MediaKind.SHOW)
    assert pick_library(config, identity2) == "tv"

    identity3 = make_identity(kind=MediaKind.MOVIE, genres=["Drama"], countries=["US"])
    assert pick_library(config, identity3) == "movies"


def test_routing_language_and_regex(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.routing.rules = [
        {
            "when": {
                "kind": "movie",
                "original_language": "ja",
                "title_regex": r"^The",
            },
            "to": "movies",
        }
    ]
    identity = make_identity(
        kind=MediaKind.MOVIE,
        title="The Movie",
        original_language="ja",
    )
    assert pick_library(config, identity) == "movies"


@pytest.mark.asyncio
async def test_confirmed_identity_reused_without_search(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = make_store(tmp_path)
    identity = make_identity(kind=MediaKind.MOVIE, title="Starfall")
    identity.tmdb_id = 157336
    identity.year = 2014
    identity.status = IdStatus.CONFIRMED
    identity.confidence = 100.0
    identity.reasons = ["confirmed by user"]  # a match you picked
    store.set_identity("k1", _identity_to_dict(identity), identity.status.value)

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if "/search" in url:
            return FakeResponse(200, {"results": [{"id": 999, "title": "Wrong"}]})
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/movie/157336"):
            return FakeResponse(
                200,
                {
                    "title": "Starfall",
                    "original_title": "Starfall",
                    "release_date": "2014-11-05",
                    "runtime": 169,
                    "original_language": "en",
                    "genres": [{"name": "Science Fiction"}],
                    "production_countries": [{"iso_3166_1": "US", "name": "United States"}],
                },
            )
        return FakeResponse(404, {})

    client, session = make_client(config, store, handler)
    item = make_item(videos=[make_file("Starfall (2014).mkv")], key="k1", kind=MediaKind.MOVIE, title="Starfall", year=2014)
    identifier = TmdbIdentifier(config, store, None, client=client, probe=FakeProbe())
    result = await identifier.identify([item], NullProgress())

    assert result["k1"].tmdb_id == 157336
    assert not any("/search" in url for url, _params in session.calls)


@pytest.mark.asyncio
async def test_audio_tiebreak_prefers_matching_language(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = make_store(tmp_path)

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/movie"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 1,
                            "title": "Show",
                            "original_title": "Show",
                            "release_date": "2014-01-01",
                            "original_language": "en",
                            "vote_count": 10,
                        },
                        {
                            "id": 2,
                            "title": "Show",
                            "original_title": "Show",
                            "release_date": "2014-01-01",
                            "original_language": "ja",
                            "vote_count": 10,
                        },
                    ]
                },
            )
        if url.endswith("/movie/1"):
            return FakeResponse(
                200,
                {
                    "title": "Show",
                    "original_title": "Show",
                    "release_date": "2014-01-01",
                    "original_language": "en",
                },
            )
        if url.endswith("/movie/2"):
            return FakeResponse(
                200,
                {
                    "title": "Show",
                    "original_title": "Show",
                    "release_date": "2014-01-01",
                    "original_language": "ja",
                },
            )
        return FakeResponse(404, {})

    client, _session = make_client(config, store, handler)
    item = make_item(kind=MediaKind.MOVIE, title="Show", year=2014, videos=[make_file("Show.mkv")])
    identifier = TmdbIdentifier(
        config, store, FakeAudio("ja"), client=client, probe=FakeProbe(None)
    )
    result = await identifier.identify([item], NullProgress())
    best = result[item.key].candidates[0]
    assert best.original_language == "ja"
    assert any("audio language ja" in reason for reason in best.reasons)


def test_create_identifier_no_credentials(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.tmdb.api_key = ""
    config.tmdb.read_token = ""
    store = make_store(tmp_path)
    assert isinstance(create_identifier(config, store, None), NullIdentifier)


@pytest.mark.asyncio
async def test_search_no_credentials(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.tmdb.api_key = ""
    config.tmdb.read_token = ""
    store = make_store(tmp_path)
    assert await search(config, store, "x", None, None) == []


@pytest.mark.asyncio
async def test_identity_for_no_credentials_saves(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.tmdb.api_key = ""
    config.tmdb.read_token = ""
    store = make_store(tmp_path)
    item = make_item(key="k", title="Title", year=2014)
    identity = await identity_for(config, store, item, 123, MediaKind.MOVIE)
    assert identity.status == IdStatus.CONFIRMED
    assert store.get_identity("k") is not None


@pytest.mark.asyncio
async def test_probe_missing(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.tools.ffprobe = "definitely-not-ffprobe"
    store = make_store(tmp_path)
    runner = ProbeRunner(config, store)
    item = make_item(videos=[make_file("X.mkv")])
    result = await runner.enrich_item(item, None)
    assert result is None
    assert not item.video_heights
    assert not item.audio_languages


@pytest.mark.asyncio
async def test_bantam_episode_evidence_auto_and_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src" / "TV"
    for i, name in enumerate(BANTAM_NAMES, start=1):
        file_title = name.replace(" ", ".")
        write_file(
            source
            / f"Bantam.S01e{i:02d}.{file_title}.1080P.Atvp.Web-Dl.Ddp5.1.Atmos.H.264-Rawr.mp4",
            100,
        )

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/tv"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 1001,
                            "name": "Bantam",
                            "original_name": "Bantam",
                            "first_air_date": "2026-01-01",
                            "last_air_date": "2026-01-01",
                            "original_language": "en",
                            "vote_count": 137,
                        },
                        {
                            "id": 1002,
                            "name": "Bantam Brawler",
                            "original_name": "Bantam Brawler",
                            "first_air_date": "2026-01-01",
                            "last_air_date": "2026-01-01",
                            "original_language": "en",
                            "vote_count": 87,
                        },
                        {
                            "id": 1003,
                            "name": "Bantam Beak Animated Adventures",
                            "original_name": "Bantam Beak Animated Adventures",
                            "first_air_date": "2011-01-01",
                            "last_air_date": "2011-01-01",
                            "original_language": "en",
                            "vote_count": 7,
                        },
                    ]
                },
            )
        if url.endswith("/tv/1001/season/1"):
            return FakeResponse(200, named_season_payload(1, BANTAM_NAMES))
        if url.endswith("/tv/1002/season/1"):
            return FakeResponse(
                200,
                named_season_payload(
                    1,
                    [
                        "One",
                        "Two",
                        "Three",
                        "Four",
                        "Five",
                        "Six",
                        "Seven",
                        "Eight",
                        "Nine",
                        "Ten",
                        "Eleven",
                        "Twelve",
                    ],
                ),
            )
        if url.endswith("/tv/1001"):
            return FakeResponse(
                200,
                {
                    "name": "Bantam",
                    "original_name": "Bantam",
                    "first_air_date": "2026-01-01",
                    "last_air_date": "2026-01-01",
                    "original_language": "en",
                },
            )
        if url.endswith("/tv/1002"):
            return FakeResponse(
                200,
                {
                    "name": "Bantam Brawler",
                    "original_name": "Bantam Brawler",
                    "first_air_date": "2026-01-01",
                    "last_air_date": "2026-01-01",
                    "original_language": "en",
                },
            )
        if url.endswith("/tv/1003"):
            return FakeResponse(
                200,
                {
                    "name": "Bantam Beak Animated Adventures",
                    "original_name": "Bantam Beak Animated Adventures",
                    "first_air_date": "2011-01-01",
                    "last_air_date": "2011-01-01",
                    "original_language": "en",
                },
            )
        return FakeResponse(404, {})

    config, store, identifier = make_tmdb_setup(tmp_path, handler)
    monkeypatch.setattr(
        "mediakeep.pipeline.create_identifier",
        lambda cfg, st, audio: identifier,
    )
    plan = await build_plan(config, store, NullProgress())

    identity = next(i for i in plan.identities.values() if i.kind == MediaKind.SHOW)
    assert identity.status == IdStatus.AUTO
    assert identity.tmdb_id == 1001
    assert any("10 of 10 episode titles match" in reason for reason in identity.reasons)
    assert any(
        "episodes 1–10 all exist in season 1" in reason for reason in identity.reasons
    )

    scores = {candidate.tmdb_id: candidate.score for candidate in identity.candidates}
    assert scores[1001] - scores[1002] >= 15

    dsts = {
        str(action.dst)
        for batch in plan.batches
        for action in batch.actions
        if action.dst is not None
    }
    folder = "Bantam (2026) [tmdbid-1001]"
    target = (
        config.libraries["tv"].path
        / folder
        / "Season 01"
        / "Bantam (2026) S01E05 - Mr. Razzles.mp4"
    )
    assert str(target) in dsts


@pytest.mark.asyncio
async def test_gullwing_bay_title_similarity_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src" / "TV"
    for i, name in enumerate(GULLWING_NAMES, start=1):
        file_title = name.replace(" ", ".").replace("’", "")
        write_file(
            source
            / f"Gullwings.Bay.S01e{i:02d}.{file_title}.1080P.Atvp.Web-Dl.Ddp5.1.Atmos.H.264-Rawr.mp4",
            100,
        )

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/tv"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 2001,
                            "name": "Gullwing's Bay",
                            "original_name": "Gullwing's Bay",
                            "first_air_date": "2026-01-01",
                            "last_air_date": "2026-01-01",
                            "original_language": "en",
                            "vote_count": 555,
                        }
                    ]
                },
            )
        if url.endswith("/tv/2001/season/1"):
            return FakeResponse(200, named_season_payload(1, GULLWING_NAMES))
        if url.endswith("/tv/2001"):
            return FakeResponse(
                200,
                {
                    "name": "Gullwing's Bay",
                    "original_name": "Gullwing's Bay",
                    "first_air_date": "2026-01-01",
                    "last_air_date": "2026-01-01",
                    "original_language": "en",
                },
            )
        return FakeResponse(404, {})

    config, store, identifier = make_tmdb_setup(tmp_path, handler)
    monkeypatch.setattr(
        "mediakeep.pipeline.create_identifier",
        lambda cfg, st, audio: identifier,
    )
    plan = await build_plan(config, store, NullProgress())

    identity = next(i for i in plan.identities.values() if i.kind == MediaKind.SHOW)
    assert identity.status == IdStatus.AUTO
    assert identity.tmdb_id == 2001
    assert any("title similarity 100%" in reason for reason in identity.reasons)
    assert any("title matches exactly" in reason for reason in identity.reasons)


@pytest.mark.asyncio
async def test_show_year_in_run_and_all_seasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src" / "TV"
    s04_file_titles = list(S04_NAMES)
    s04_file_titles[10] = "Rememberd"
    for i, title in enumerate(s04_file_titles, start=1):
        file_title = title.replace(" ", ".")
        write_file(
            source
            / f"Skyport.Meridian.2007.S04e{i:02d}.{file_title}.1080P.Web-Dl.Ddp5.1.H.264-Rawr.mp4",
            100,
        )

    for i in range(1, 21):
        if i <= 5:
            name = f"Skyport.Meridian.S05e{i:02d}.mp4"
        elif i == 20:
            name = f"Skyport.Meridian.S05e{i:02d}.Extended.1080P.Web-Dl.Ddp5.1.H.264-Rawr.mp4"
        else:
            name = f"Skyport.Meridian.S05e{i:02d}.1080P.Web-Dl.Ddp5.1.H.264-Rawr.mp4"
        write_file(source / name, 100)

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/tv"):
            if (params or {}).get("first_air_date_year") == "2007":
                return FakeResponse(200, {"results": []})
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 3001,
                            "name": "Skyport Meridian",
                            "original_name": "Skyport Meridian",
                            "first_air_date": "2004-07-16",
                            "last_air_date": "2009-12-19",
                            "original_language": "en",
                            "vote_count": 300,
                        }
                    ]
                },
            )
        if url.endswith("/tv/3001/season/4"):
            return FakeResponse(200, named_season_payload(4, S04_NAMES))
        if url.endswith("/tv/3001/season/5"):
            return FakeResponse(200, named_season_payload(5, S05_NAMES))
        if url.endswith("/tv/3001"):
            return FakeResponse(
                200,
                {
                    "name": "Skyport Meridian",
                    "original_name": "Skyport Meridian",
                    "first_air_date": "2004-07-16",
                    "last_air_date": "2009-12-19",
                    "original_language": "en",
                },
            )
        return FakeResponse(404, {})

    config, store, identifier = make_tmdb_setup(tmp_path, handler)
    monkeypatch.setattr(
        "mediakeep.pipeline.create_identifier",
        lambda cfg, st, audio: identifier,
    )
    plan = await build_plan(config, store, NullProgress())

    identity = next(i for i in plan.identities.values() if i.kind == MediaKind.SHOW)
    assert identity.status == IdStatus.AUTO
    assert identity.year == 2004
    assert any("within the show's run" in reason for reason in identity.reasons)

    dsts = [
        Path(str(action.dst))
        for batch in plan.batches
        for action in batch.actions
        if action.dst is not None
    ]
    assert len(dsts) == 40
    assert {p.parent.name for p in dsts} == {"Season 04", "Season 05"}
    folder = "Skyport Meridian (2004) [tmdbid-3001]"
    assert all(folder in str(p) for p in dsts)

    s04e11 = [p for p in dsts if p.name.startswith("Skyport Meridian (2004) S04E11")]
    assert s04e11 and "Remembered" in s04e11[0].name

    s05e20 = [p for p in dsts if p.name.startswith("Skyport Meridian (2004) S05E20")]
    assert s05e20 and "Extended" not in s05e20[0].name

    assert not any("Skyport" in label for label in plan.multi_version)


@pytest.mark.asyncio
async def test_show_episode_count_minus10_does_not_win(tmp_path: Path) -> None:
    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/tv"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 1001,
                            "name": "Show",
                            "original_name": "Show",
                            "first_air_date": "2010-01-01",
                            "original_language": "en",
                            "vote_count": 0,
                        },
                        {
                            "id": 1002,
                            "name": "Show",
                            "original_name": "Show",
                            "first_air_date": "2010-01-01",
                            "original_language": "en",
                            "vote_count": 0,
                        },
                    ]
                },
            )
        if url.endswith("/tv/1001/season/1"):
            return FakeResponse(
                200, named_season_payload(1, [f"Ep {i}" for i in range(1, 11)])
            )
        if url.endswith("/tv/1002/season/1"):
            return FakeResponse(
                200, named_season_payload(1, [f"Ep {i}" for i in range(1, 9)])
            )
        if url.endswith("/tv/1001") or url.endswith("/tv/1002"):
            return FakeResponse(
                200,
                {
                    "name": "Show",
                    "original_name": "Show",
                    "first_air_date": "2010-01-01",
                    "original_language": "en",
                },
            )
        return FakeResponse(404, {})

    _config, _store, identifier = make_tmdb_setup(tmp_path, handler)
    episodes = [
        EpisodeRef(
            file=make_file(f"Show.S01e{i:02d}.mkv"),
            season=1,
            episode=i,
            episode_end=None,
            title=f"Ep {i}",
            version_tag=None,
            absolute=False,
        )
        for i in range(1, 11)
    ]
    item = make_item(
        kind=MediaKind.SHOW,
        title="Show",
        year=None,
        videos=[ep.file for ep in episodes],
        episodes=episodes,
    )
    result = await identifier.identify([item], NullProgress())
    identity = result[item.key]

    assert identity.tmdb_id == 1001
    loser = next(c for c in identity.candidates if c.tmdb_id == 1002)
    assert any("go beyond season 1" in reason for reason in loser.reasons)


DR_MOONBEAM = "Dr. Moonbeam or: How I Learned to Stop Fretting and Love the Comet"


@pytest.mark.asyncio
async def test_main_title_is_exact_and_names_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix 7 item 1: "Dr Moonbeam" matches the full "or:" title exactly."""
    write_file(tmp_path / "src" / "Dr.Moonbeam.1964.1080P.Bluray.Ac3.X264-Hdc.mp4", 100)

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/movie"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 935,
                            "title": DR_MOONBEAM,
                            "original_title": DR_MOONBEAM,
                            "release_date": "1964-01-29",
                            "original_language": "en",
                            "vote_count": 6415,
                        }
                    ]
                },
            )
        if url.endswith("/movie/935"):
            return FakeResponse(
                200,
                {
                    "title": DR_MOONBEAM,
                    "original_title": DR_MOONBEAM,
                    "release_date": "1964-01-29",
                    "runtime": 95,
                    "original_language": "en",
                },
            )
        return FakeResponse(404, {})

    config, store, identifier = make_probed_tmdb_setup(tmp_path, handler, 95 * 60)
    monkeypatch.setattr(
        "mediakeep.pipeline.create_identifier",
        lambda cfg, st, audio: identifier,
    )
    plan = await build_plan(config, store, NullProgress())

    identity = next(iter(plan.identities.values()))
    assert identity.status == IdStatus.AUTO
    assert identity.tmdb_id == 935
    assert any("title similarity 100%" in reason for reason in identity.reasons)
    assert any(
        'title matches main title "Dr. Moonbeam"' in reason
        for reason in identity.reasons
    )

    folder = (
        "Dr. Moonbeam or How I Learned to Stop Fretting and Love the Comet "
        "(1964) [tmdbid-935]"
    )
    expected = config.libraries["movies"].path / folder / f"{folder}.mp4"
    dsts = {
        str(action.dst)
        for batch in plan.batches
        for action in batch.actions
        if action.dst is not None
    }
    assert str(expected) in dsts


@pytest.mark.asyncio
async def test_main_title_does_not_overmatch(tmp_path: Path) -> None:
    """"Mission" alone may match, a long query may not fall back to it."""
    config = make_config(tmp_path)
    store = make_store(tmp_path)

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/movie"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 1,
                            "title": "Venture: Unbound",
                            "original_title": "Venture: Unbound",
                            "release_date": "1996-05-22",
                            "original_language": "en",
                            "vote_count": 100,
                        }
                    ]
                },
            )
        if url.endswith("/movie/1"):
            return FakeResponse(
                200,
                {
                    "title": "Venture: Unbound",
                    "original_title": "Venture: Unbound",
                    "release_date": "1996-05-22",
                    "runtime": 110,
                    "original_language": "en",
                },
            )
        return FakeResponse(404, {})

    client, _session = make_client(config, store, handler)
    identifier = TmdbIdentifier(config, store, None, client=client, probe=FakeProbe(None))
    item = make_item(
        kind=MediaKind.MOVIE,
        title="Venture Unbound Final Reckoning",
        year=None,
        videos=[make_file("Venture.Unbound.Final.Reckoning.2023.mkv")],
    )
    result = await identifier.identify([item], NullProgress())
    identity = result[item.key]

    assert identity.status == IdStatus.NEEDS_REVIEW
    assert identity.confidence < 90
    assert not any("title matches" in reason for reason in identity.reasons)
    assert not any("title similarity 100%" in reason for reason in identity.reasons)


@pytest.mark.asyncio
async def test_short_main_title_is_exact(tmp_path: Path) -> None:
    """"Iron Owl" is an exact main-title match for "Iron Owl: Vol. 1"."""
    config = make_config(tmp_path)
    store = make_store(tmp_path)

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/movie"):
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 393,
                            "title": "Iron Owl: Vol. 1",
                            "original_title": "Iron Owl: Vol. 1",
                            "release_date": "2003-10-10",
                            "original_language": "en",
                            "vote_count": 300000,
                        }
                    ]
                },
            )
        if url.endswith("/movie/393"):
            return FakeResponse(
                200,
                {
                    "title": "Iron Owl: Vol. 1",
                    "original_title": "Iron Owl: Vol. 1",
                    "release_date": "2003-10-10",
                    "runtime": 111,
                    "original_language": "en",
                },
            )
        return FakeResponse(404, {})

    client, _session = make_client(config, store, handler)
    identifier = TmdbIdentifier(
        config, store, None, client=client, probe=FakeProbe(111 * 60)
    )
    item = make_item(
        kind=MediaKind.MOVIE,
        title="Iron Owl",
        year=2003,
        videos=[make_file("Iron.Owl.2003.1080p.mkv")],
    )
    result = await identifier.identify([item], NullProgress())
    identity = result[item.key]

    assert any("title similarity 100%" in reason for reason in identity.reasons)
    assert any(
        'title matches main title "Iron Owl"' in reason for reason in identity.reasons
    )
    assert identity.status == IdStatus.AUTO


@pytest.mark.asyncio
async def test_yearless_retry_keeps_the_scoring_year(tmp_path: Path) -> None:
    """Fix 7 item 3: the retry drops the year from the search only."""

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/tv"):
            if (params or {}).get("first_air_date_year") == "2007":
                return FakeResponse(200, {"results": []})
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": 3001,
                            "name": "Skyport Meridian",
                            "original_name": "Skyport Meridian",
                            "first_air_date": "2004-07-16",
                            "last_air_date": "2009-12-19",
                            "original_language": "en",
                            "vote_count": 300,
                        }
                    ]
                },
            )
        if url.endswith("/tv/3001/season/4"):
            return FakeResponse(
                200, named_season_payload(4, ["Adrift Part 2", "Home", "The Defiant Ones"])
            )
        if url.endswith("/tv/3001"):
            return FakeResponse(
                200,
                {
                    "name": "Skyport Meridian",
                    "original_name": "Skyport Meridian",
                    "first_air_date": "2004-07-16",
                    "last_air_date": "2009-12-19",
                    "original_language": "en",
                },
            )
        return FakeResponse(404, {})

    _config, _store, identifier = make_tmdb_setup(tmp_path, handler)
    episodes = [
        EpisodeRef(
            file=make_file(f"Skyport.Meridian.2007.S04e{i:02d}.mkv"),
            season=4,
            episode=i,
            episode_end=None,
            title="",
            version_tag=None,
            absolute=False,
        )
        for i in (1, 2, 3)
    ]
    item = make_item(
        kind=MediaKind.SHOW,
        title="Skyport Meridian",
        year=2007,
        videos=[ep.file for ep in episodes],
        episodes=episodes,
    )
    result = await identifier.identify([item], NullProgress())
    identity = result[item.key]

    assert identity.year == 2004
    assert any(
        "2007 is within the show's run (2004–2009)" in reason
        for reason in identity.reasons
    )
    assert any(
        "2007 is within the show's run (2004–2009)" in reason
        for reason in identity.candidates[0].reasons
    )


@pytest.mark.asyncio
async def test_year_in_the_name_beats_an_exact_title_from_another_year(tmp_path: Path) -> None:
    # A reported case: "101 Hedgehogs (1961)". TMDB calls the 1961 film "One Hundred and One
    # Hedgehogs"; the 1996 remake is titled exactly "101 Hedgehogs".
    config = make_config(tmp_path)
    store = make_store(tmp_path)

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/movie"):
            return FakeResponse(200, {"results": [
                {"id": 11674, "title": "101 Hedgehogs", "original_title": "101 Hedgehogs",
                 "release_date": "1996-11-18", "original_language": "en", "vote_count": 3000},
                {"id": 12230, "title": "One Hundred and One Hedgehogs",
                 "original_title": "One Hundred and One Hedgehogs",
                 "release_date": "1961-01-25", "original_language": "en", "vote_count": 6000},
            ]})
        if url.endswith("/movie/11674"):
            return FakeResponse(200, {"title": "101 Hedgehogs", "release_date": "1996-11-18", "runtime": 103})
        if url.endswith("/movie/12230"):
            return FakeResponse(200, {"title": "One Hundred and One Hedgehogs", "release_date": "1961-01-25", "runtime": 79})
        return FakeResponse(404, {})

    client, _session = make_client(config, store, handler)
    folder = Path("/media/Family/101 Hedgehogs (1961)")
    video = FileInfo(path=folder / "101 Hedgehogs (1961).mkv", size=1, mtime_ns=0, hidden=False)
    item = make_item(kind=MediaKind.MOVIE, title="101 Hedgehogs", year=1961, videos=[video])
    item.year_source = "folder"
    item.paths = [folder]
    identifier = TmdbIdentifier(config, store, None, client=client, probe=FakeProbe(None))
    identity = (await identifier.identify([item], NullProgress()))[item.key]
    assert identity.tmdb_id == 12230
    assert identity.year == 1961


@pytest.mark.asyncio
async def test_a_match_from_another_year_is_never_auto_approved(tmp_path: Path) -> None:
    # Even when the name's year can't be used for the search (a file names another year),
    # a match whose year disagrees with the name goes to Needs you.
    config = make_config(tmp_path)
    store = make_store(tmp_path)

    def handler(url: str, params: dict[str, Any] | None) -> FakeResponse:
        if url.endswith("/configuration"):
            return FakeResponse(200, {"images": {"base_url": "https://image.tmdb.org/t/p/w500"}})
        if url.endswith("/search/movie"):
            return FakeResponse(200, {"results": [
                {"id": 11674, "title": "101 Hedgehogs", "original_title": "101 Hedgehogs",
                 "release_date": "1996-11-18", "original_language": "en", "vote_count": 3000},
            ]})
        if url.endswith("/movie/11674"):
            return FakeResponse(200, {"title": "101 Hedgehogs", "release_date": "1996-11-18", "runtime": 103})
        return FakeResponse(404, {})

    client, _session = make_client(config, store, handler)
    folder = Path("/media/Family/101 Hedgehogs (1961)")
    video = FileInfo(path=folder / "101 Hedgehogs (2008 remux).mkv", size=1, mtime_ns=0, hidden=False)
    item = make_item(kind=MediaKind.MOVIE, title="101 Hedgehogs", year=1961, videos=[video])
    item.year_source = "folder"
    item.paths = [folder]
    identifier = TmdbIdentifier(config, store, None, client=client, probe=FakeProbe(103 * 60))
    identity = (await identifier.identify([item], NullProgress()))[item.key]
    assert identity.year == 1996
    assert identity.status == IdStatus.NEEDS_REVIEW
    assert any("the name says 1961" in r for r in identity.reasons)


def test_identify_progress_line_names_the_match() -> None:
    from mediakeep.identify.identifier import _identify_detail

    item = make_item(title="Spark", year=2020, videos=[make_file("Spark (2020).mkv")])
    ident = make_identity(title="Spark", year=2020)
    assert _identify_detail(item, ident) == "Spark (2020).mkv  →  Spark (2020)"
    assert _identify_detail(item, None).endswith("→  no match")
