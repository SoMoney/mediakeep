"""One folder at a time: work in one folder, renamed where it is by default or moved into one
destination folder."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from mediakeep import __main__, workspace
from mediakeep.config import AppConfig, load_config

from test_web_pages import (
    browse_rows,
    csrf_headers,
    make_app,
    run_scan,
    wait_job,
    write_file,
)


def _config(tmp_path: Path, folder: Path | None, destination: Path | None = None) -> AppConfig:
    return AppConfig(
        workspace={"folder": folder, "destination": destination, "recent": [folder] if folder else []},
        naming={"folder_jpg": False},
        state_dir=str(tmp_path / "state"),
        web={"host": "127.0.0.1", "port": 8765, "access_token": "", "posters": True},
    )


def test_effective_settings_are_one_folder_and_one_destination(tmp_path: Path) -> None:
    folder, dest = tmp_path / "Family", tmp_path / "Movies"
    eff = workspace.effective(_config(tmp_path, folder))
    assert eff.sources == [folder]
    assert [lib.path for lib in eff.libraries.values()] == [folder]
    assert eff.routing.rules == []
    assert eff.behavior.default_destination == "in_place"

    eff = workspace.effective(_config(tmp_path, folder, dest))
    assert [lib.path for lib in eff.libraries.values()] == [dest]
    assert eff.behavior.default_destination == "file"

    # No folder chosen yet: nothing to scan.
    eff = workspace.effective(_config(tmp_path, None))
    assert eff.sources == [] and eff.libraries == {}


def test_settings_file_holds_only_the_working_folder(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    workspace.effective(_config(tmp_path, tmp_path / "Family")).save(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "sources" not in data and "libraries" not in data and "routing" not in data
    assert data["workspace"]["folder"] == str(tmp_path / "Family")

    # Older files with sources and libraries load; those keys are ignored.
    path.write_text(
        f"sources: ['{(tmp_path / 'Old').as_posix()}']\nlibraries: {{}}\nwhisper: {{enabled: true}}\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.sources == [] and cfg.workspace.folder is None


def test_first_run_writes_settings_and_carries_on(tmp_path: Path) -> None:
    path = tmp_path / "new" / "config.yaml"
    cfg = load_config(path)
    assert path.exists() and cfg.workspace.folder is None
    assert "Q:" not in path.read_text(encoding="utf-8")


def test_destination_inside_the_folder_is_refused_in_the_file(tmp_path: Path) -> None:
    with pytest.raises(Exception):
        AppConfig(workspace={"folder": tmp_path / "A", "destination": tmp_path / "A" / "B"})
    with pytest.raises(Exception):
        AppConfig(workspace={"folder": tmp_path / "A" / "B", "destination": tmp_path / "A"})


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destination: bool = False) -> tuple[Any, Path]:
    folder = tmp_path / "Family"
    write_file(folder / "The.Lantern.Keepers.2023.1080p" / "The.Lantern.Keepers.2023.1080p.mkv", 1000)
    dest = tmp_path / "Movies"
    dest.mkdir()
    client, cfg_path = make_app(tmp_path, monkeypatch, _config(tmp_path, folder, dest if destination else None))
    run_scan(client)
    return client, cfg_path


def test_start_page_without_a_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = make_app(tmp_path, monkeypatch, _config(tmp_path, None))
    html = client.get("/inbox").text
    assert "No folder chosen yet" in html and "data-pick-workspace" in html
    assert "Add your TMDB key first" in html
    assert client.get("/settings").status_code == 200
    assert client.get("/execute").status_code == 200


def test_folder_page_and_removed_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _app(tmp_path, monkeypatch)
    html = client.get("/inbox").text
    assert "<h1>Folder</h1>" in html
    assert "data-workspace" in html and "Rename here" in html and "Move to another folder" in html
    for gone in ("/library/movies", "/where"):
        assert f'href="{gone}"' not in html
        assert client.get(gone, follow_redirects=False).status_code == 404
    settings = client.get("/settings").text
    assert "Folders" in settings and "Recent folders" in settings
    assert 'id="sources"' not in settings and "Jellyfin" not in settings


def test_rename_here_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _app(tmp_path, monkeypatch)
    folder = tmp_path / "Family"
    row = next(r for r in browse_rows(client, folder) if r["status"] == "ready")
    card = row["cards"][0]
    assert card["in_place"] is True
    assert Path(card["new_path"]).parent == folder


def test_move_to_a_destination_and_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _app(tmp_path, monkeypatch, destination=True)
    folder, dest = tmp_path / "Family", tmp_path / "Movies"
    row = next(r for r in browse_rows(client, folder) if r["status"] == "ready")
    card = row["cards"][0]
    assert card["in_place"] is False
    assert Path(card["new_path"]).parent == dest
    headers = csrf_headers(client)
    client.post("/api/queue", json={"kind": "item", "item_key": card["item_key"], "source": "inbox"}, headers=headers)
    assert f"Move into {dest}" in client.get("/execute").text
    ids = [e["id"] for e in client.get("/api/queue").json()]
    client.post("/api/jobs/execute", json={"ids": ids}, headers=headers)
    assert wait_job(client)["state"] == "done"
    moved = list(dest.rglob("*.mkv"))
    assert len(moved) == 1 and moved[0].parent.parent == dest


def test_switching_folder_and_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, cfg_path = _app(tmp_path, monkeypatch)
    headers = csrf_headers(client)
    sports, dest = tmp_path / "Sports", tmp_path / "Movies"
    sports.mkdir()

    r = client.post("/api/workspace", json={"destination": str(dest)}, headers=headers)
    assert r.status_code == 200 and r.json()["destination"] == str(dest)
    saved = load_config(cfg_path)
    assert saved.workspace.destination == dest

    # Another folder starts on "Rename here" again.
    r = client.post("/api/workspace", json={"folder": str(sports)}, headers=headers)
    assert r.status_code == 200 and r.json()["destination"] == ""
    saved = load_config(cfg_path)
    assert saved.workspace.folder == sports and saved.workspace.destination is None
    assert saved.workspace.recent[:2] == [sports, tmp_path / "Family"]

    # Destinations that would tangle the two folders are refused.
    inside = sports / "sub"
    inside.mkdir()
    assert client.post("/api/workspace", json={"destination": str(inside)}, headers=headers).status_code == 422
    assert client.post("/api/workspace", json={"destination": str(tmp_path)}, headers=headers).status_code == 422
    assert client.post("/api/workspace", json={"folder": str(tmp_path / "nope")}, headers=headers).status_code == 422

    # Forgetting a recent folder only edits the list.
    r = client.post("/api/workspace/forget", json={"path": str(tmp_path / "Family")}, headers=headers)
    assert str(tmp_path / "Family") not in r.json()["recent"]
    assert (tmp_path / "Family").exists()


def test_settings_page_changes_only_its_own_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, cfg_path = _app(tmp_path, monkeypatch)
    headers = csrf_headers(client)

    def apply(path: str, value: Any) -> Any:
        return client.post("/api/settings/apply", json={"payload": {"path": path, "value": value}}, headers=headers)

    r = apply("naming.year_style", "brackets")
    assert r.status_code == 200, r.text
    assert r.json()["rescan"] is True
    saved = load_config(cfg_path)
    assert saved.naming.year_style == "brackets"
    assert saved.workspace.folder == tmp_path / "Family"
    assert cfg_path.with_name("config.yaml.bak").exists()

    assert apply("behavior.remember_choices", True).json()["rescan"] is False
    assert apply("behavior.remember_choices", "yes").status_code == 422
    assert apply("state_dir", "C:/elsewhere").status_code == 422
    assert apply("workspace.folder", str(tmp_path)).status_code == 422


def test_tmdb_test_without_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _app(tmp_path, monkeypatch)
    r = client.post("/api/tmdb/test", json={}, headers=csrf_headers(client))
    assert r.json()["ok"] is False and "No TMDB key" in r.json()["message"]


def test_add_many_items_to_execute_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "Family"
    for i in range(12):
        write_file(folder / f"Movie.Number.{i}.2010" / f"Movie.Number.{i}.2010.mkv", 100)
    client, _ = make_app(tmp_path, monkeypatch, _config(tmp_path, folder))
    run_scan(client)
    headers = csrf_headers(client)
    keys = [c["item_key"] for r in browse_rows(client, folder) for c in r["cards"]]
    r = client.post(
        "/api/queue", json={"kind": "items", "item_keys": keys + ["gone"], "source": "inbox"}, headers=headers
    )
    assert r.json() == {"ok": True, "added": len(keys), "missing": 1}
    assert len(client.get("/api/queue").json()) == len(keys)
    html = client.get("/execute").text
    assert f"Show all {len(keys)}" in html


def test_cli_folder_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "config.yaml"
    family, movies = tmp_path / "Family", tmp_path / "Movies"
    family.mkdir()
    movies.mkdir()

    assert __main__.main(["--config", str(cfg), "scan"]) == 2
    assert "No working folder yet" in capsys.readouterr().out

    assert __main__.main(["--config", str(cfg), "folder", str(family), "--move-to", str(movies)]) == 0
    saved = load_config(cfg)
    assert saved.workspace.folder == family and saved.workspace.destination == movies

    assert __main__.main(["--config", str(cfg), "folder", str(family)]) == 0
    assert load_config(cfg).workspace.destination is None

    assert __main__.main(["--config", str(cfg), "folder", str(family), "--move-to", str(family / "x")]) == 2
    capsys.readouterr()
    assert __main__.main(["--config", str(cfg), "folder"]) == 0
    assert str(family) in capsys.readouterr().out
