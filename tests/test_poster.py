"""The matched TMDB poster is saved as folder.jpg in the item's folder when a change is applied."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Sequence

import pytest

from mediakeep.models import Candidate, Identity, IdStatus, MediaKind, ProgressSink, SourceItem

from test_web_pages import (
    browse_rows,
    csrf_headers,
    make_app,
    make_config,
    run_scan,
    wait_job,
    write_file,
)

POSTER = "https://image.tmdb.org/t/p/w342/lantern.jpg"
JPEG = b"\xff\xd8\xff\xe0" + b"poster-bytes" * 10


class PosterIdentifier:
    async def identify(
        self, items: Sequence[SourceItem], progress: ProgressSink
    ) -> dict[str, Identity]:
        out: dict[str, Identity] = {}
        for item in items:
            cand = Candidate(
                tmdb_id=704239, kind=MediaKind.MOVIE, title="The Lantern", original_title="",
                year=2023, original_language="en", runtime_min=None, genres=[], countries=[],
                overview="", poster_url=POSTER, imdb_id=None, score=96.0, reasons=["title match"],
            )
            out[item.key] = Identity(
                item_key=item.key, kind=MediaKind.MOVIE, title="The Lantern", year=2023,
                tmdb_id=704239, imdb_id=None, status=IdStatus.AUTO, confidence=96.0,
                reasons=["title match"], candidates=[cand],
            )
        return out


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **naming: Any) -> tuple[Any, Any, str]:
    config = make_config(tmp_path)
    if naming:
        config = config.model_copy(update={"naming": config.naming.model_copy(update=naming)})
    write_file(tmp_path / "source" / "The.Lantern.2023.1080p" / "The.Lantern.2023.1080p.mkv", 1000)
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=PosterIdentifier)
    run_scan(client)
    row = next(r for r in browse_rows(client, config.sources[0]) if r.get("item_keys"))
    return client, config, row["item_keys"][0]


def _execute(client: Any, key: str) -> dict[str, Any]:
    headers = csrf_headers(client)
    assert client.post(
        "/api/queue", json={"kind": "item", "item_key": key, "source": "inbox"}, headers=headers
    ).status_code == 200
    ids = [e["id"] for e in client.get("/api/queue").json()]
    assert client.post(
        "/api/jobs/execute", json={"ids": ids, "confirmed": True}, headers=headers
    ).status_code == 200
    job = wait_job(client)
    assert job["state"] == "done", job
    return job


def test_poster_is_planned_shown_saved_and_undone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetched: list[str] = []

    def fake_download(url: str, timeout: float = 30.0) -> bytes:
        fetched.append(url)
        return JPEG

    monkeypatch.setattr("mediakeep.apply._download", fake_download)
    client, config, key = _setup(tmp_path, monkeypatch)
    folder = tmp_path / "Movies" / "The Lantern (2023)"

    # Execute says so before anything happens.
    headers = csrf_headers(client)
    client.post("/api/queue", json={"kind": "item", "item_key": key, "source": "inbox"}, headers=headers)
    assert "saves the poster as" in client.get("/execute").text
    client.request("DELETE", "/api/queue", json={"ids": [e["id"] for e in client.get("/api/queue").json()]}, headers=headers)

    job = _execute(client, key)
    assert (folder / "The Lantern (2023).mkv").exists()
    assert (folder / "folder.jpg").read_bytes() == JPEG
    # A bigger size than the page thumbnails.
    assert fetched == [re.sub("/w342/", "/w780/", POSTER)]

    run_id = job["result"]["run_id"]
    script = (config.state_dir_path() / "undo" / f"undo-{run_id}.ps1").read_text(encoding="utf-8")
    assert "folder.jpg" in script

    assert client.post(
        "/api/jobs/undo", json={"run_id": run_id, "apply": True}, headers=csrf_headers(client)
    ).status_code == 200
    wait_job(client)
    assert not (folder / "folder.jpg").exists()
    assert (tmp_path / "source" / "The.Lantern.2023.1080p" / "The.Lantern.2023.1080p.mkv").exists()


def test_failed_download_does_not_stop_the_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(url: str, timeout: float = 30.0) -> bytes:
        raise OSError("network is down")

    monkeypatch.setattr("mediakeep.apply._download", broken)
    client, _config, key = _setup(tmp_path, monkeypatch)
    _execute(client, key)
    folder = tmp_path / "Movies" / "The Lantern (2023)"
    assert (folder / "The Lantern (2023).mkv").exists()
    assert not (folder / "folder.jpg").exists()


def test_existing_poster_is_kept_and_setting_turns_it_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mediakeep.apply._download", lambda url, timeout=30.0: JPEG)
    # Off in Settings: no poster step at all.
    client, _config, key = _setup(tmp_path / "off", monkeypatch, folder_jpg=False)
    headers = csrf_headers(client)
    client.post("/api/queue", json={"kind": "item", "item_key": key, "source": "inbox"}, headers=headers)
    assert "saves the poster as" not in client.get("/execute").text

    # The destination folder already has a folder.jpg: it is never replaced.
    existing = tmp_path / "on" / "Movies" / "The Lantern (2023)" / "folder.jpg"
    write_file(existing, 10)
    before = existing.read_bytes()
    client, _config, key = _setup(tmp_path / "on", monkeypatch)
    headers = csrf_headers(client)
    client.post("/api/queue", json={"kind": "item", "item_key": key, "source": "inbox"}, headers=headers)
    assert "saves the poster as" not in client.get("/execute").text
    client.request("DELETE", "/api/queue", json={"ids": [e["id"] for e in client.get("/api/queue").json()]}, headers=headers)
    _execute(client, key)
    assert existing.read_bytes() == before


class SparkIdentifier:
    """A reported case: "Spark (2020)", two close matches, already named right."""

    async def identify(
        self, items: Sequence[SourceItem], progress: ProgressSink
    ) -> dict[str, Identity]:
        out: dict[str, Identity] = {}
        for item in items:
            c1 = Candidate(
                tmdb_id=508442, kind=MediaKind.MOVIE, title="Spark", original_title="", year=2020,
                original_language="en", runtime_min=None, genres=[], countries=[], overview="",
                poster_url="https://image.tmdb.org/t/p/w342/spark.jpg", imdb_id=None, score=72.0,
                reasons=["title match"],
            )
            c2 = Candidate(
                tmdb_id=1, kind=MediaKind.MOVIE, title="Spark", original_title="", year=2016,
                original_language="en", runtime_min=None, genres=[], countries=[], overview="",
                poster_url=None, imdb_id=None, score=66.0, reasons=["title match"],
            )
            out[item.key] = Identity(
                item_key=item.key, kind=MediaKind.MOVIE, title="Spark", year=2020, tmdb_id=508442,
                imdb_id=None, status=IdStatus.NEEDS_REVIEW, confidence=60.0, reasons=["close"],
                candidates=[c1, c2],
            )
        return out


def _spark(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, str, Path]:
    config = make_config(tmp_path, destination=False)  # rename here
    folder = tmp_path / "source" / "Spark (2020)"
    write_file(folder / "Spark (2020).mkv", 1000)
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=SparkIdentifier)
    run_scan(client)
    row = browse_rows(client, config.sources[0])[0]
    assert row["status"] == "needs"
    # A plain scan never adds a poster on its own.
    assert row["cards"][0]["poster_only"] is False
    return client, config, row["cards"][0]["item_key"], folder


def _choose(client: Any, key: str) -> dict[str, Any]:
    r = client.post(
        f"/api/match/{key}/choose",
        json={"tmdb_id": 508442, "kind": "movie", "remember": False, "source": "inbox"},
        headers=csrf_headers(client),
    )
    assert r.status_code == 200, r.text
    data: dict[str, Any] = r.json()
    return data


def test_confirming_a_tidy_item_queues_its_poster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mediakeep.apply._download", lambda url, timeout=30.0: JPEG)
    client, config, key, folder = _spark(tmp_path, monkeypatch)

    data = _choose(client, key)
    assert data["queued"] is True and data["message"].startswith("Added to Execute")
    assert data["card"]["poster_only"] is True
    # It stays on the Execute list (it used to vanish: "nothing to change any more").
    queue = client.get("/api/queue").json()
    assert [e["item_key"] for e in queue] == [key]
    html = client.get("/execute").text
    assert "Save poster" in html and re.search(r'id="execute-badge">1<', html)

    job_ids = [e["id"] for e in queue]
    r = client.post("/api/jobs/execute", json={"ids": job_ids, "confirmed": True}, headers=csrf_headers(client))
    assert r.status_code == 200
    job = wait_job(client)
    assert job["state"] == "done", job
    assert (folder / "folder.jpg").read_bytes() == JPEG
    assert (folder / "Spark (2020).mkv").exists()

    client.post("/api/jobs/undo", json={"run_id": job["result"]["run_id"], "apply": True}, headers=csrf_headers(client))
    wait_job(client)
    assert not (folder / "folder.jpg").exists()
    assert (folder / "Spark (2020).mkv").exists()


def test_confirming_a_tidy_item_with_a_poster_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, config, key, folder = _spark(tmp_path, monkeypatch)
    write_file(folder / "folder.jpg", 10)
    data = _choose(client, key)
    assert data["queued"] is False
    assert "already named right" in data["message"]
    assert client.get("/api/queue").json() == []
    row = browse_rows(client, config.sources[0])[0]
    assert row["status"] == "tidy"


def test_right_folder_name_with_wrong_file_name_is_not_tidy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    config = config.model_copy(
        update={
            "behavior": config.behavior.model_copy(update={"default_destination": "in_place"}),
            "naming": config.naming.model_copy(update={"folder_jpg": False}),
        }
    )
    write_file(tmp_path / "source" / "The Lantern (2023)" / "lantern.1080p.mkv", 1000)
    client, _cfg = make_app(tmp_path, monkeypatch, config, identifier=PosterIdentifier)
    run_scan(client)
    row = browse_rows(client, config.sources[0])[0]
    assert row["status"] == "ready", row["status"]
