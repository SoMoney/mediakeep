from __future__ import annotations

from mediakeep.clean import clean_name
from mediakeep.parse import parse_episode, parse_release_name, version_label


def run() -> int:
    checks: list[tuple[str, bool]] = []

    def check(label: str, value: bool) -> None:
        checks.append((label, value))

    releases = [
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
            "1944.2019.2160p.UHD.BluRay.x265-TERMiNAL.mkv",
            "1944",
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
    ]
    for name, title, year, res, codec in releases:
        rel = parse_release_name(name)
        check(
            f"release {name}",
            rel.title == title and rel.year == year and rel.resolution == res and rel.codec == codec,
        )

    versions = [
        ("Movie.2010.1080p.BluRay.x264.mkv", "1080p BluRay"),
        ("Movie.2010.Extended.Cut.720p.WEB-DL.mkv", "720p Extended WEB"),
        ("Movie.2010.Directors.Cut.DVDRip.avi", "Directors Cut DVD"),
        ("Movie.2010.UHD.Remastered.mkv", "2160p Remastered"),
    ]
    for name, label in versions:
        check(f"version {name}", version_label(name) == label)

    eps = [
        ("Time.Ranger.2005.S01E01.Rose.1080p.WEB-DL.mkv", "", 1, 1, None, None, "Rose"),
        ("Time.Ranger.2005.S01E02-E03.mkv", "", 1, 2, 3, None, ""),
        ("time.ranger.2005.1x04.robots.of.paris.mkv", "", 1, 4, None, None, "Robots Of Paris"),
        ("Orbiters - 01 - Outside the Window.mkv", "Orbiters", 1, 1, None, None, "Outside the Window"),
        ("Wombat - 112a.mkv", "Wombat", 1, 23, None, None, ""),
        ("Session 07 - Comet Waltz.mkv", "", 1, 7, None, None, "Comet Waltz"),
        ("[Group] Show - 13v2 [1080p].mkv", "Show", 1, 13, None, 2, ""),
    ]
    for name, show, season, ep, end, v, title in eps:
        ep_parse = parse_episode(name, show)
        ok = (
            ep_parse is not None
            and ep_parse.season == season
            and ep_parse.episode == ep
            and ep_parse.episode_end == end
            and ep_parse.version_tag == v
            and ep_parse.title == title
        )
        check(f"episode {name}", ok)

    cleans = [
        (
            "Show.Name.S01E02.Episode.Title.1080p.AMZN.WEB-DL.DDP5.1.H.264-GROUP.mkv",
            "Show Name S01E02 Episode Title.mkv",
        ),
        ("Movie Title (2019) [1080p] [x265] [10bit].mkv", "Movie Title (2019).mkv"),
        ("[SubsGroup] Wanderer - 05 (1080p) [A1B2C3D4].mkv", "Wanderer - 05.mkv"),
        ("Dustwind [2023] [tmdbid-12345].mkv", "Dustwind [2023] [tmdbid-12345].mkv"),
        ("The.Lighthouse.S02.1080p.WEB-DL.x264-GROUP", "The Lighthouse S02"),
    ]
    for name, expected in cleans:
        check(f"clean {name}", clean_name(name) == expected)

    failed = [label for label, ok in checks if not ok]
    if failed:
        print("selftest failed:")
        for label in failed:
            print(f"  {label}")
        return 1
    print(f"selftest ok ({len(checks)} checks)")
    return 0
