from __future__ import annotations

from pathlib import Path

from mediakeep.layout import in_place_parent_path
from mediakeep.models import MediaKind, SourceItem


def touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def item(root: Path, paths: list[Path], kind: MediaKind, title: str) -> SourceItem:
    return SourceItem(key="k", root=root, paths=paths, kind=kind, title=title, year=2013)


def test_show_episodes_in_season_folder_stay_next_to_show_folder(tmp_path: Path) -> None:
    # A real case: renaming in place must not nest the new show folder inside Season 01.
    lib = tmp_path / "Anime"
    show = lib / "Siege On Colossus English (2013)"
    eps = [touch(show / "Season 01" / f"Siege.On.Colossus.S01E0{i}.mkv") for i in (1, 2)]
    assert in_place_parent_path(item(lib, eps, MediaKind.SHOW, "Siege On Colossus")) == lib


def test_show_given_as_season_folder(tmp_path: Path) -> None:
    lib = tmp_path / "Anime"
    season = lib / "Siege On Colossus English (2013)" / "Season 01"
    touch(season / "e1.mkv")
    assert in_place_parent_path(item(lib, [season], MediaKind.SHOW, "Siege On Colossus")) == lib


def test_show_spread_over_seasons(tmp_path: Path) -> None:
    lib = tmp_path / "TVlib"
    show = lib / "Some Show Folder"
    eps = [touch(show / "Season 01" / "a.S01E01.mkv"), touch(show / "Season 02" / "a.S02E01.mkv")]
    assert in_place_parent_path(item(lib, eps, MediaKind.SHOW, "Other Title")) == lib


def test_loose_episodes_in_generic_folder_stay_there(tmp_path: Path) -> None:
    tv = tmp_path / "zstaging" / "TV"
    eps = [touch(tv / "Bantam.S01E01.mkv"), touch(tv / "Bantam.S01E02.mkv")]
    assert in_place_parent_path(item(tmp_path / "zstaging", eps, MediaKind.SHOW, "Bantam")) == tv


def test_movie_folder_stays_next_to_it(tmp_path: Path) -> None:
    src = tmp_path / "Western"
    folder = src / "The Lantern 2023 1080p BluRay DD+7.1 x264-HiP"
    touch(folder / "The Lantern 2023 1080p BluRay DD+7.1 x264-HiP.mkv")
    assert in_place_parent_path(item(src, [folder], MediaKind.MOVIE, "The Lantern")) == src


def test_movie_file_in_own_folder(tmp_path: Path) -> None:
    src = tmp_path / "Western"
    f = touch(src / "The.Lantern.2023.1080p.BluRay" / "The.Lantern.2023.1080p.BluRay.mkv")
    assert in_place_parent_path(item(src, [f], MediaKind.MOVIE, "The Lantern")) == src


def test_loose_movie_in_root(tmp_path: Path) -> None:
    src = tmp_path / "zstaging"
    f = touch(src / "Solstice.2015.1080p.mkv")
    assert in_place_parent_path(item(src, [f], MediaKind.MOVIE, "Solstice")) == src
