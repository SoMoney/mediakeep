from __future__ import annotations

from pathlib import Path

from mediakeep.hints import Hint, match_hint, parse_hint_line


def _hint(pattern: str, name: str = "", **options) -> Hint:
    return Hint(id=1, pattern=pattern, name=name, options=options, origin="ui")


def test_hint_longest_wins():
    hints = [_hint("Movie"), _hint("Movie/Part")]
    m = match_hint(Path("D:/x"), Path("Movie/Part/file.mkv"), hints, folder=False)
    assert m is not None
    assert m.pattern == "Movie/Part"


def test_hint_wildcard():
    hints = [_hint("Movie*.mkv")]
    m = match_hint(Path("D:/x"), Path("Movie1.mkv"), hints, folder=False)
    assert m is not None


def test_hint_folder_prefix():
    hints = [_hint("Orbiters")]
    m = match_hint(Path("D:/x"), Path("Orbiters/Orbiters - 01.mkv"), hints, folder=False)
    assert m is not None


def test_hint_options_parse():
    parsed = parse_hint_line("Orbiters | Orbiters (2003) | show;season=1;offset=2")
    assert parsed is not None
    pattern, name, options = parsed
    assert pattern == "Orbiters"
    assert name == "Orbiters (2003)"
    assert options["show"] == "true"
    assert options["season"] == "1"
    assert options["offset"] == "2"
