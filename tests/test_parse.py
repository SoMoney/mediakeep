from __future__ import annotations

import pytest

from mediakeep.parse import parse_episode, parse_release_name, version_label


@pytest.mark.parametrize(
    "name,title,year,res,codec",
    [
        (
            "Starfall.2014.IMAX.2160p.10bit.BluRay.AAC5.1.HEVC-x265.mkv",
            "Starfall",
            2014,
            "2160p",
            "HEVC",
        ),
        (
            "Neon.Harbor.2049.2017.1080p.WEB-DL.DDP5.1.H.264-GROUP.mp4",
            "Neon Harbor 2049",
            2017,
            "1080p",
            "H.264",
        ),
        (
            "2002.A.Moon.Voyage.1968.REMASTERED.720p.BluRay.x264.mkv",
            "2002 A Moon Voyage",
            1968,
            "720p",
            "H.264",
        ),
        (
            "1917.2019.2160p.UHD.BluRay.x265-TERMiNAL.mkv",
            "1917",
            2019,
            "2160p",
            "HEVC",
        ),
        (
            "The.Grid.(1999).[1080p].x264.avi",
            "The Grid",
            1999,
            "1080p",
            "H.264",
        ),
        (
            "Sandstorm Part Two 2024 1080p HEVC.mkv",
            "Sandstorm Part Two",
            2024,
            "1080p",
            "HEVC",
        ),
        (
            "Comet_Remus_2024_2160p_HDR_DV_AV1.mkv",
            "Comet Remus",
            2024,
            "2160p",
            "AV1",
        ),
        (
            "Some.Movie.Without.Year.DVDRip.XviD.avi",
            "Some Movie Without Year",
            None,
            None,
            "XviD",
        ),
    ],
)
def test_parse_release_name(name, title, year, res, codec):
    p = parse_release_name(name)
    assert p.title == title
    assert p.year == year
    assert p.resolution == res
    assert p.codec == codec


@pytest.mark.parametrize(
    "name,label",
    [
        ("Movie.2010.1080p.BluRay.x264.mkv", "1080p BluRay"),
        ("Movie.2010.Extended.Cut.720p.WEB-DL.mkv", "720p Extended WEB"),
        ("Movie.2010.Directors.Cut.DVDRip.avi", "Directors Cut DVD"),
        ("Movie.2010.UHD.Remastered.mkv", "2160p Remastered"),
    ],
)
def test_version_label(name, label):
    assert version_label(name) == label


@pytest.mark.parametrize(
    "name,show,season,ep,end,v,title",
    [
        ("Time.Ranger.2005.S01E01.Rose.1080p.WEB-DL.mkv", "", 1, 1, None, None, "Rose"),
        ("Time.Ranger.2005.S01E02-E03.mkv", "", 1, 2, 3, None, ""),
        ("time.ranger.2005.1x04.robots.of.paris.mkv", "", 1, 4, None, None, "Robots Of Paris"),
        ("Orbiters - 01 - Outside the Window.mkv", "Orbiters", 1, 1, None, None, "Outside the Window"),
        ("Wombat - 112a.mkv", "Wombat", 1, 23, None, None, ""),
        ("Session 07 - Comet Waltz.mkv", "", 1, 7, None, None, "Comet Waltz"),
        ("[Group] Show - 13v2 [1080p].mkv", "Show", 1, 13, None, 2, ""),
    ],
)
def test_parse_episode(name, show, season, ep, end, v, title):
    p = parse_episode(name, show)
    assert p is not None
    assert p.season == season
    assert p.episode == ep
    assert p.episode_end == end
    assert p.version_tag == v
    assert p.title == title
