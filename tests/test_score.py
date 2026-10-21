from __future__ import annotations

import pytest

from mediakeep.identify.score import score_candidate, title_ratio
from mediakeep.models import Candidate, MediaKind


def _candidate(
    title: str,
    original_title: str | None = None,
    year: int | None = None,
    runtime_min: int | None = None,
    kind: MediaKind = MediaKind.MOVIE,
) -> Candidate:
    return Candidate(
        tmdb_id=1,
        kind=kind,
        title=title,
        original_title=original_title or title,
        year=year,
        original_language="en",
        runtime_min=runtime_min,
        genres=[],
        countries=[],
        overview="",
        poster_url=None,
        imdb_id=None,
        score=0.0,
        reasons=[],
    )


@pytest.mark.parametrize(
    "ours,theirs,expected",
    [
        ("Bantam", "Bantam", 100),
        ("Gullwings Bay", "Gullwing's Bay", 100),
        ("Bantam", "Bantam Brawler", "<70"),
        ("Bantam", "Bantam Beak Animated Adventures", "<40"),
        ("The Grid", "Grid", 100),
        ("Amelie", "Amélie", 100),
    ],
)
def test_title_similarity_values(ours: str, theirs: str, expected: str | int) -> None:
    ratio = title_ratio(ours, _candidate(theirs))
    if expected == 100:
        assert ratio == 100
    elif expected == "<70":
        assert ratio < 70
    elif expected == "<40":
        assert ratio < 40
    else:  # pragma: no cover
        raise AssertionError("bad expectation")


def test_exact_title_runtime_gives_year_points() -> None:
    candidate = _candidate(
        "Skyport: Continuum", year=2008, runtime_min=98
    )
    score, reasons = score_candidate(
        "Skyport Continuum", None, MediaKind.MOVIE, candidate, 99.0, 630
    )
    assert any("exact title and runtime" in reason for reason in reasons)
    assert score >= 90


def test_show_year_inside_run() -> None:
    candidate = _candidate("Skyport Meridian", year=2004, kind=MediaKind.SHOW)
    score, reasons = score_candidate(
        "Skyport Meridian",
        2007,
        MediaKind.SHOW,
        candidate,
        None,
        0,
        first_year=2004,
        last_year=2009,
    )
    assert any("within the show's run" in reason for reason in reasons)
    assert any("2007 is within the show's run (2004–2009)" in reason for reason in reasons)


def test_show_year_outside_run() -> None:
    candidate = _candidate("Skyport Meridian", year=2004, kind=MediaKind.SHOW)
    score, reasons = score_candidate(
        "Skyport Meridian",
        2015,
        MediaKind.SHOW,
        candidate,
        None,
        0,
        first_year=2004,
        last_year=2009,
    )
    assert any("year mismatch" in reason for reason in reasons)
    assert score < 50
