from __future__ import annotations

import pytest

from mediakeep.clean import clean_name


@pytest.mark.parametrize(
    "name,expected",
    [
        (
            "Show.Name.S01E02.Episode.Title.1080p.AMZN.WEB-DL.DDP5.1.H.264-GROUP.mkv",
            "Show Name S01E02 Episode Title.mkv",
        ),
        ("Movie Title (2019) [1080p] [x265] [10bit].mkv", "Movie Title (2019).mkv"),
        ("[SubsGroup] Wanderer - 05 (1080p) [A1B2C3D4].mkv", "Wanderer - 05.mkv"),
        ("Dustwind [2023] [tmdbid-12345].mkv", "Dustwind [2023] [tmdbid-12345].mkv"),
        ("The.Lighthouse.S02.1080p.WEB-DL.x264-GROUP", "The Lighthouse S02"),
    ],
)
def test_clean_name(name, expected):
    assert clean_name(name) == expected


def test_clean_name_folder_last_dot_chunk():
    assert (
        clean_name("The.Lighthouse.S02.1080p.WEB-DL.x264-GROUP", is_dir=True)
        == "The Lighthouse S02"
    )
    assert (
        clean_name("Movie.2010.1080p.BluRay.x265-TERMiNAL", is_dir=True)
        == "Movie 2010"
    )
    assert (
        clean_name("Movie.2010.1080p.BluRay.x265-TERMiNAL.mkv")
        == "Movie 2010.mkv"
    )
    assert (
        clean_name("Show.S01E01.720p.HDTV.x264-NTb.en.srt")
        == "Show S01E01.en.srt"
    )
