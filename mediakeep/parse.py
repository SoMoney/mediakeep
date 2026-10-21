from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from mediakeep.paths import sanitize_name

TITLE_YEARS: set[int] = {1917, 2012, 1984, 2049}


RELEASE_JUNK_WORDS = {
    "2160p",
    "1080p",
    "1080i",
    "720p",
    "576p",
    "480p",
    "4k",
    "uhd",
    "hdr",
    "bluray",
    "blurau",
    "bdrip",
    "brrip",
    "dvd",
    "dvdrip",
    "dvdscr",
    "web-dl",
    "webrip",
    "hdtv",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "xvid",
    "divx",
    "ac3",
    "dts",
    "aac",
    "extended",
    "unrated",
    "uncut",
    "remastered",
    "repack",
    "proper",
    "limited",
    "theatrical",
    "imax",
    "director",
    "director's",
    "directors",
    "cut",
}


@dataclass(slots=True)
class ParsedRelease:
    title: str
    year: int | None
    resolution: str | None
    codec: str | None


@dataclass(slots=True)
class EpisodeParse:
    season: int
    episode: int
    episode_end: int | None
    version_tag: int | None
    title: str
    absolute: bool


def normalize_title(text: str) -> str:
    s = text.lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    if s.startswith("the"):
        s = s[3:]
    return s


_EPISODE_STRICT_RE = re.compile(
    r"(?<![a-z0-9])s\d{1,2}e\d{1,3}(?![0-9])|(?<!\d)\d{1,2}x\d{1,3}(?!\d)",
    re.I,
)

_ID_TAG_RE = re.compile(r"\[(?:tmdbid-(\d+)|imdbid-(tt\d+))\]", re.I)


def episode_prefix(name: str) -> str | None:
    """Return the show-title prefix before a strict episode number."""
    stem = _strip_extension(name)
    m = _EPISODE_STRICT_RE.search(stem)
    if not m:
        return None
    prefix = stem[: m.start()]
    prefix = re.sub(r"[._-]+", " ", prefix)
    prefix = re.sub(r"\s+", " ", prefix).strip(" -_")
    return prefix or None


def parse_id_tag(name: str) -> tuple[str, int | None, str | None]:
    """Split ``[tmdbid-N]`` / ``[imdbid-ttN]`` from a file or folder name."""
    s = Path(name).name
    m = _ID_TAG_RE.search(s)
    if not m:
        return s, None, None
    tmdb_id = int(m.group(1)) if m.group(1) else None
    imdb_id = m.group(2) if m.group(2) else None
    clean = s[: m.start()] + s[m.end() :]
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean, tmdb_id, imdb_id


def id_tag_options(name: str) -> tuple[str, dict[str, str]]:
    """Return ``(clean_name, hint_options)`` with recorded ids, if present."""
    clean, tmdb_id, imdb_id = parse_id_tag(name)
    options: dict[str, str] = {}
    if tmdb_id is not None:
        options["_tmdb_id"] = str(tmdb_id)
    if imdb_id is not None:
        options["_imdb_id"] = imdb_id
    return clean, options


def _detect_resolution(base: str) -> str | None:
    low = base.lower()
    if "2160p" in low or "uhd" in low or "4k" in low:
        return "2160p"
    for r in ("1080p", "720p", "576p", "480p"):
        if r in low:
            return r
    return None


def _detect_codec(base: str) -> str | None:
    low = base.lower()
    if any(x in low for x in ("x265", "hevc", "h265", "h.265")):
        return "HEVC"
    if any(x in low for x in ("x264", "h264", "h.264", "avc")):
        return "H.264"
    if "av1" in low:
        return "AV1"
    if "xvid" in low:
        return "XviD"
    if "divx" in low:
        return "DivX"
    return None


def _year_is_bracketed(base: str, year: int) -> bool:
    return bool(re.search(rf"[(\[][^()\[\]]*{year}[^()\[\]]*[\])]", base))


# Only these are file extensions. A folder name like "The Amazing Mr. Puddle (1964)" has
# no extension: Path.stem would cut it at "Mr" and lose the rest of the title and the year.
_KNOWN_EXTS = {
    # video
    "mkv", "mp4", "m4v", "avi", "mov", "wmv", "mpg", "mpeg", "m2ts", "mts", "ts", "vob",
    "iso", "webm", "flv", "ogm", "ogv", "divx", "xvid", "3gp", "rm", "rmvb", "asf", "m2v",
    "img", "bdmv",
    # subtitles and sidecars
    "srt", "sub", "idx", "ass", "ssa", "vtt", "sup", "smi", "nfo", "txt", "jpg", "jpeg",
    "png", "tbn", "xml", "sfv", "md5", "nzb", "url", "par2", "rar", "zip", "7z", "part",
    "ifo", "bup",
    # the placeholder callers add to a folder name
    "x",
}


def _strip_extension(name: str) -> str:
    p = Path(name)
    suffix = p.suffix
    if suffix and suffix[1:].lower() in _KNOWN_EXTS:
        return p.stem
    return p.name


def parse_release_name(name: str) -> ParsedRelease:
    base = _strip_extension(name)
    s_year = base.replace(".", " ").replace("_", " ")
    years = [
        int(m.group())
        for m in re.finditer(r"(?<!\d)(?:19|20)\d{2}(?!\d)", s_year)
    ]
    year: int | None = None
    if years:
        if (
            len(years) == 1
            and years[0] in TITLE_YEARS
            and not _year_is_bracketed(base, years[0])
        ):
            year = None
        else:
            year = years[-1]

    t = base
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"\(.*$", " ", t)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(r"\[.*$", " ", t)

    if year is not None:
        last = None
        for m in re.finditer(rf"(?<!\d){year}(?!\d)", t):
            last = m
        if last is not None:
            t = t[: last.start()]

    t = re.sub(r"\bextended cut\b", " ", t, flags=re.I)
    t = re.sub(r"\bdirector'?s cut\b", " ", t, flags=re.I)
    # "Mr.Whiskers.and.Pip.2014" uses dots as spaces; "The Amazing Mr. Puddle (1964)"
    # is written with spaces, and its dots belong to the title ("Mr.", "vs.", "Bros.").
    written_with_spaces = base.count(" ") >= base.count(".")
    if written_with_spaces:
        t = t.replace("_", " ")
    else:
        t = t.replace(".", " ").replace("_", " ")

    clean_tokens: list[str] = []
    for tok in t.split():
        low = tok.lower().strip("-.")
        if low in RELEASE_JUNK_WORDS:
            continue
        clean_tokens.append(tok)

    title = re.sub(r"\s+", " ", " ".join(clean_tokens)).strip(" .-_")
    if not title:
        title = base.replace(".", " ").replace("_", " ").strip(" .-_")
    return ParsedRelease(
        title=title,
        year=year,
        resolution=_detect_resolution(base),
        codec=_detect_codec(base),
    )


def version_label(name: str) -> str:
    base = _strip_extension(name)
    low = base.lower()
    parts: list[str] = []

    res = parse_release_name(name).resolution
    if res:
        parts.append(res)

    def has(word: str) -> bool:
        return re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", low) is not None

    if has("extended"):
        parts.append("Extended")
    elif has("unrated"):
        parts.append("Unrated")
    elif has("uncut"):
        parts.append("Uncut")
    elif has("theatrical"):
        parts.append("Theatrical")
    elif has("remastered"):
        parts.append("Remastered")
    elif has("imax"):
        parts.append("IMAX")

    if has("directors") or has("director") or has("director's"):
        if "Directors Cut" not in parts:
            parts.append("Directors Cut")
    else:
        m = re.search(r"\b([a-z]+)[ _]cut\b", low)
        if m and m.group(1) not in {
            "extended",
            "unrated",
            "uncut",
            "theatrical",
            "remastered",
            "imax",
            "directors",
            "director",
        }:
            parts.append(f"{m.group(1).capitalize()} Cut")

    source = ""
    if re.search(r"bluray|bdrip|brrip", low):
        source = "BluRay"
    elif re.search(r"web-dl|webrip|web", low):
        source = "WEB"
    elif re.search(r"hdtv|hdrip", low):
        source = "HDTV"
    elif re.search(r"screener|dvdscr", low):
        source = "Screener"
    elif re.search(r"dvdrip|dvd5|dvd9", low):
        source = "DVD"
    if source:
        parts.append(source)

    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return " ".join(out)


def _is_release_junk_token(tok: str) -> bool:
    low = tok.lower().strip("[]()")
    if not low:
        return True
    if re.fullmatch(r"(19|20)\d{2}", low):
        return False
    if re.fullmatch(r"s\d{1,2}e\d{1,3}(?:-e\d{1,3})?", low):
        return False
    if low.startswith(("tmdbid-", "imdbid-")):
        return False
    junk = {
        "4k",
        "uhd",
        "hdr",
        "dv",
        "sdr",
        "bluray",
        "bdrip",
        "brrip",
        "bdremux",
        "remux",
        "web-dl",
        "webrip",
        "hdtv",
        "hdrip",
        "dvdrip",
        "dvdscr",
        "dvd5",
        "dvd9",
        "hddvd",
        "x264",
        "x265",
        "h.264",
        "h264",
        "hevc",
        "avc",
        "xvid",
        "divx",
        "mpeg-2",
        "hi10p",
        "8bit",
        "10bit",
        "aac",
        "ac3",
        "eac3",
        "dts",
        "dd",
        "ddp",
        "truehd",
        "atmos",
        "flac",
        "opus",
        "lpcm",
        "mp3",
        "amzn",
        "nf",
        "dsnp",
        "hmax",
        "atvp",
        "hulu",
        "pcok",
        "repack",
        "proper",
        "internal",
        "rs2",
        "hybrid",
        "2in1",
        "korsub",
        "hc",
        "nordic",
        "extended",
        "unrated",
        "uncut",
        "theatrical",
        "remastered",
        "imax",
        "directors",
        "director",
        "director's",
    }
    if low in junk:
        return True
    if re.fullmatch(r"\d{3,4}p", low):
        return True
    if re.fullmatch(r"\d{3,4}x\d{3,4}", low):
        return True
    if low.startswith("dts-") and low.split("-", 1)[1] in {"hd", "ma", "x", "es"}:
        return True
    if any(
        low.startswith(x)
        for x in (
            "aac",
            "ac3",
            "eac3",
            "ddp",
            "dd",
            "truehd",
            "atmos",
            "flac",
            "opus",
            "lpcm",
            "mp3",
        )
    ):
        return True
    if "5.1" in low or "7.1" in low:
        return True
    base = re.sub(r"-group$", "", low)
    if base in junk:
        return True
    return False


def _has_release_junk(text: str) -> bool:
    return any(_is_release_junk_token(t) for t in text.split())


def _first_junk_prefix(token: str) -> tuple[str, bool]:
    """Return the text before the first release-junk token in a whitespace token."""
    if re.search(r"[-_.]", token):
        parts = re.split(r"[-_.]", token)
        for i, part in enumerate(parts):
            if part and _is_release_junk_token(part):
                return " ".join(p for p in parts[:i] if p), True

    if _is_release_junk_token(token):
        return "", True

    return token, False


def _clean_episode_title(text: str, show_title: str) -> str:
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\[[^\]]*$", " ", text)

    def paren(m: re.Match[str]) -> str:
        inner = m.group(1)
        if _has_release_junk(inner):
            return " "
        return m.group(0)

    text = re.sub(r"\(([^)]*)\)", paren, text)
    text = re.sub(r"\([^)]*$", " ", text)

    text = text.replace("_", " ")
    text = text.lstrip(" .-_")

    if " " not in text:
        text = text.replace(".", " ")

    text = re.sub(r"\s+", " ", text).strip()

    kept: list[str] = []
    for tok in text.split():
        prefix, had_junk = _first_junk_prefix(tok)
        if had_junk:
            if prefix:
                kept.append(prefix)
            break
        kept.append(prefix)
    text = " ".join(kept)

    text = re.sub(r"\s*\([^)]*credit[^)]*\)\s*$", " ", text, flags=re.I)

    if show_title:
        norm_show = normalize_title(show_title)
        norm_text = normalize_title(text)
        if norm_show and norm_text.startswith(norm_show):
            pattern = r"\s*".join(re.escape(ch) for ch in show_title if not ch.isspace())
            if pattern:
                text = re.sub(
                    rf"^{pattern}(?![A-Za-z0-9])\s*",
                    "",
                    text,
                    count=1,
                    flags=re.I,
                )

    text = sanitize_name(text)
    text = text.strip(" .-_")
    if not text:
        return ""

    if not any(c.isupper() for c in text):
        text = " ".join(w.capitalize() for w in text.split())

    return text


def parse_episode(file_name: str, show_title: str = "") -> EpisodeParse | None:
    stem = _strip_extension(file_name)

    for m_sxe in re.finditer(
        r"s(\d{1,2})e(\d{1,3})(?:\s*-\s*(?:s(\d{1,2})e(\d{1,3})|e(\d{1,3}))|e(\d{1,3}))?",
        stem,
        re.I,
    ):
        season = int(m_sxe.group(1))
        ep = int(m_sxe.group(2))
        end: int | None = None
        if m_sxe.group(3) is not None:
            season = int(m_sxe.group(3))
            end = int(m_sxe.group(4)) if m_sxe.group(4) is not None else None
        elif m_sxe.group(5) is not None:
            end = int(m_sxe.group(5))
        elif m_sxe.group(6) is not None:
            end = int(m_sxe.group(6))
        title = _clean_episode_title(stem[m_sxe.end() :], show_title)
        return EpisodeParse(season, ep, end, None, title, False)

    m_ax = re.search(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)", stem, re.I)
    if m_ax is not None:
        season = int(m_ax.group(1))
        ep = int(m_ax.group(2))
        title = _clean_episode_title(stem[m_ax.end() :], show_title)
        return EpisodeParse(season, ep, None, None, title, False)

    m_ab = re.search(r"(?<!\d)(\d{3})([ab])(?![a-z])", stem, re.I)
    if m_ab is not None:
        num = int(m_ab.group(1))
        season = num // 100
        n = num % 100
        ep = n * 2 - 1 if m_ab.group(2).lower() == "a" else n * 2
        title = _clean_episode_title(stem[m_ab.end() :], show_title)
        return EpisodeParse(season, ep, None, None, title, False)

    m_padded = re.search(r"(?<!\d)(\d)(\d{2})(?:\s*\(\d{3}\))?(?!\d)", stem)
    if m_padded is not None:
        season = int(m_padded.group(1))
        ep = int(m_padded.group(2))
        title = _clean_episode_title(stem[m_padded.end() :], show_title)
        return EpisodeParse(season, ep, None, None, title, False)

    m_ep = re.search(r"(?:session|episode|ep)\s*(\d{1,3})", stem, re.I)
    if m_ep is not None:
        ep = int(m_ep.group(1))
        title = _clean_episode_title(stem[m_ep.end() :], show_title)
        return EpisodeParse(1, ep, None, None, title, True)

    plain = stem
    plain = re.sub(r"\[[^\]]*\]", " ", plain)
    plain = re.sub(r"\([^)]*\)", " ", plain)
    if show_title:
        plain = re.sub(re.escape(show_title), " ", plain, flags=re.I)
    plain = plain.replace(".", " ").replace("_", " ").replace("-", " ")
    m_plain = re.search(r"(?<!\d)(\d{1,3})(?:v(\d+))?(?!\d)", plain, re.I)
    if m_plain is not None:
        ep = int(m_plain.group(1))
        version = int(m_plain.group(2)) if m_plain.group(2) is not None else None
        title = _clean_episode_title(plain[m_plain.end() :], show_title)
        return EpisodeParse(1, ep, None, version, title, True)

    return None


def parse_part_number(name: str) -> int | None:
    m = re.search(
        r"(?<![a-z])(?:cd|part|pt|disc|disk)\s*[-_ ]?(\d{1,2})(?![a-z])",
        name,
        re.I,
    )
    if m:
        return int(m.group(1))
    return None


def is_strict_episode(name: str) -> bool:
    return bool(
        re.search(r"s\d{1,2}e\d{1,3}", name, re.I)
        or re.search(r"\d{1,2}x\d{1,3}", name, re.I)
    )
