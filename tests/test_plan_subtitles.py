from __future__ import annotations

from pathlib import Path

import pytest

from mediakeep.models import FileInfo, Issue, MediaKind, SourceItem
from mediakeep.plan import _plan_subtitles, _subtitle_language_suffix


@pytest.mark.parametrize(
    "stem,expected",
    [
        ("C.2012.en", ".en"),
        ("C.2012.eng.forced", ".eng.forced"),
        ("C.2012.Spanish", ".es"),
        ("English", ".en"),
        ("2_Spanish", ".es"),
        ("C.2012", ""),
    ],
)
def test_subtitle_language_suffix(stem: str, expected: str) -> None:
    assert _subtitle_language_suffix(stem) == expected


def test_single_video_subtitle_without_language_is_renamed(tmp_path: Path) -> None:
    video = FileInfo(
        path=tmp_path / "C.2012.1080p.mkv",
        size=10,
        mtime_ns=1,
        hidden=False,
    )
    sub = FileInfo(
        path=tmp_path / "C.2012.srt",
        size=5,
        mtime_ns=1,
        hidden=False,
    )
    item = SourceItem(
        key="c",
        root=tmp_path,
        kind=MediaKind.MOVIE,
        title="C",
        year=2012,
        videos=[video],
        subtitles=[sub],
    )
    video_targets = {video.path: tmp_path / "C (2012).mkv"}
    issues: list[Issue] = []

    actions, moved = _plan_subtitles(item, video_targets, tmp_path, issues)

    assert issues == []
    assert moved == {sub.path}
    assert len(actions) == 1
    assert actions[0].dst == tmp_path / "C (2012).srt"
