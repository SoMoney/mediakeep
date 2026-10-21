from __future__ import annotations

import re
from pathlib import Path

from mediakeep.paths import sanitize_name

_VIDEO_EXTS = {
    "mkv",
    "mp4",
    "m4v",
    "avi",
    "mpg",
    "mpeg",
    "ts",
    "m2ts",
    "mts",
    "wmv",
    "mov",
    "vob",
    "webm",
    "flv",
    "divx",
    "ogm",
    "3gp",
    "iso",
}
_SUB_EXTS = {"srt", "sub", "idx", "ass", "ssa", "vtt", "smi", "sup"}
_KNOWN_EXTS = {x.lower() for x in _VIDEO_EXTS | _SUB_EXTS}

_SUB_FLAGS = {"forced", "sdh", "cc", "hi"}

_LANGUAGE_NAMES = {
    "english": "en",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "japanese": "ja",
    "chinese": "zh",
    "korean": "ko",
    "portuguese": "pt",
    "russian": "ru",
    "dutch": "nl",
    "danish": "da",
    "swedish": "sv",
    "norwegian": "no",
    "finnish": "fi",
    "polish": "pl",
    "turkish": "tr",
    "czech": "cs",
    "hungarian": "hu",
    "greek": "el",
    "hebrew": "he",
    "vietnamese": "vi",
    "thai": "th",
    "ukrainian": "uk",
    "romanian": "ro",
}

_ISO_639_1_CODES = {
    "en",
    "es",
    "fr",
    "de",
    "it",
    "ja",
    "zh",
    "ko",
    "pt",
    "ru",
    "nl",
    "da",
    "sv",
    "no",
    "fi",
    "pl",
    "tr",
    "cs",
    "hu",
    "el",
    "he",
    "vi",
    "th",
    "uk",
    "ro",
    "id",
    "ar",
    "fa",
    "hi",
    "bn",
    "ta",
    "te",
    "mr",
    "gu",
    "kn",
    "ml",
    "pa",
    "ur",
    "sw",
    "am",
    "yo",
    "ig",
    "zu",
    "xh",
    "af",
    "sq",
    "hy",
    "az",
    "be",
    "bs",
    "bg",
    "hr",
    "et",
    "gl",
    "ka",
    "kk",
    "ky",
    "lo",
    "lv",
    "lt",
    "lb",
    "mk",
    "mt",
    "sr",
    "sk",
    "sl",
    "tg",
    "tt",
    "tk",
    "ug",
    "uz",
    "is",
    "ga",
    "cy",
    "ca",
    "ms",
    "nb",
    "nn",
    "si",
    "km",
    "my",
    "ne",
    "ps",
    "sd",
}

_ISO_639_3_CODES = {
    "eng",
    "spa",
    "fra",
    "fre",
    "deu",
    "ger",
    "ita",
    "jpn",
    "zho",
    "chi",
    "kor",
    "por",
    "rus",
    "nld",
    "dut",
    "dan",
    "swe",
    "nor",
    "fin",
    "pol",
    "tur",
    "ces",
    "cze",
    "hun",
    "ell",
    "gre",
    "heb",
    "vie",
    "tha",
    "ukr",
    "rum",
    "ron",
    "bul",
    "hrv",
    "srp",
    "slk",
    "slv",
    "est",
    "lav",
    "lit",
    "per",
    "urd",
    "ben",
    "tam",
    "tel",
    "mar",
    "guj",
    "kan",
    "mal",
    "pan",
    "ara",
    "fas",
    "hin",
    "swa",
    "amh",
    "yor",
    "ibo",
    "zul",
    "xho",
    "nob",
    "nno",
    "isl",
    "gle",
    "cym",
    "cat",
    "ind",
    "msa",
    "mya",
    "khm",
    "lao",
    "bos",
    "hbs",
    "nbl",
    "nso",
    "tsn",
    "sot",
    "ssw",
    "ven",
    "sin",
}

_PROTECTED_FOLDERS = {
    "season",
    "specials",
    "subs",
    "samples",
    "extras",
    "featurettes",
    "video_ts",
    "bdmv",
}

_JUNK_PATTERNS = [
    r"bdremux",
    r"remux",
    r"bluray",
    r"bdrip",
    r"brrip",
    r"web[-_]?dl",
    r"web[-_]?rip",
    r"webrip",
    r"hdtv",
    r"hdrip",
    r"dvdrip",
    r"dvdscr",
    r"dvd5",
    r"dvd9",
    r"hddvd",
    r"dvd",
    r"x264",
    r"x265",
    r"h\.264",
    r"h\.265",
    r"h264",
    r"h265",
    r"hevc",
    r"avc",
    r"av1",
    r"xvid",
    r"divx",
    r"mpeg[-_]?2",
    r"hi10p",
    r"10bit",
    r"8bit",
    r"ddp(?:[-_]?\d+(?:\.\d+)?)?",
    r"dd(?:[-_]?\d+(?:\.\d+)?)?",
    r"eac3(?:[-_]?\d+(?:\.\d+)?)?",
    r"ac3(?:[-_]?\d+(?:\.\d+)?)?",
    r"aac(?:[-_]?\d+(?:\.\d+)?)?",
    r"dts(?:[-_](?:hd|ma|x|es))?",
    r"truehd",
    r"atmos",
    r"flac",
    r"opus",
    r"lpcm",
    r"mp3",
    r"\d{3,4}p",
    r"\d{3,4}x\d{3,4}",
    r"4k",
    r"uhd",
    r"hdr",
    r"sdr",
    r"dv",
    r"amzn",
    r"nf",
    r"dsnp",
    r"hmax",
    r"atvp",
    r"hulu",
    r"pcok",
    r"repack",
    r"proper",
    r"internal",
    r"rs2",
    r"hybrid",
    r"2in1",
    r"korsub",
    r"hc",
    r"nordic",
    r"5\.1",
    r"7\.1",
]

# Matches a junk token, optionally followed by a release group such as
# x264-GROUP, WEB-DL-FLUX, H.264-NTb, or x265-TERMiNAL.
_JUNK_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    + "|".join(_JUNK_PATTERNS)
    + r")(?:-[A-Za-z0-9]+)?(?![A-Za-z0-9])",
    re.I,
)

# The same patterns without the release-group suffix, for token analysis.
_JUNK_CORE_FULL = re.compile(r"(?:" + "|".join(_JUNK_PATTERNS) + r")", re.I)

_AUDIO_PREFIXES = (
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


def _core_is_junk_token(low: str) -> bool:
    """True for a known junk token, before any release-group suffix."""
    if not low or low == "-":
        return False
    if re.fullmatch(r"(19|20)\d{2}", low):
        return False
    if re.fullmatch(r"s\d{1,2}e\d{1,3}(?:-e\d{1,3})?", low):
        return False
    if low.startswith(("tmdbid-", "imdbid-")):
        return False
    if _JUNK_CORE_FULL.fullmatch(low):
        return True
    if any(low.startswith(x) for x in _AUDIO_PREFIXES):
        return True
    if "5.1" in low or "7.1" in low:
        return True
    return False


def _is_junk_token(tok: str) -> bool:
    """True for a junk token, including junk tokens with a release group."""
    low = tok.lower().strip("[]().")
    if not low or low == "-":
        return False

    # Whole token first: this correctly keeps web-dl, dts-hd, and mpeg-2 as
    # junk without mistaking their internal hyphens for a release group.
    if _core_is_junk_token(low):
        return True

    # Then try stripping one trailing -part, e.g. x264-GROUP -> x264.
    base, sep, _ = low.rpartition("-")
    if sep and base and _core_is_junk_token(base):
        return True

    return False


def _dot_spaced(text: str) -> str:
    # A dot is kept only when there is a digit on both sides: 5.1, 2.0,
    # S02.1080p. Dots next to letters become spaces.
    return re.sub(r"(?<=\D)\.|\.(?=\D)|^\.|\.$", " ", text)


def _contains_year_or_sxe(text: str) -> bool:
    return bool(
        re.search(r"(19|20)\d{2}", text)
        or re.search(r"s\d{1,2}e\d{1,3}", text, re.I)
    )


def _clean_segment(seg: str, had_spaces: bool) -> str:
    if not had_spaces:
        seg = _dot_spaced(seg)

    seg = re.sub(r"\s+", " ", seg).strip()
    seg = re.sub(r"\(\s*\)", "", seg)
    seg = re.sub(r"\[\s*\]", "", seg)
    seg = seg.strip(" .-_")
    seg = re.sub(r"\s*-\s*$", "", seg)
    seg = re.sub(r"^-\s*", "", seg)
    return seg.strip()


def _language_code(token: str) -> str | None:
    """Return a subtitle language code for a token, or None."""
    low = token.lower()
    if low in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[low]
    if len(low) == 2 and low in _ISO_639_1_CODES:
        return low
    if len(low) == 3 and low in _ISO_639_3_CODES:
        return low
    return None


def _language_tail(stem: str) -> tuple[str, str]:
    """Split trailing subtitle language/flags from a stem.

    Returns ``(core, tail)`` where ``tail`` is normalized as ``.lang.flag``
    and is empty when no trailing language is present.
    """
    pieces = re.split(r"([._-]+)", stem)
    if len(pieces) < 3:
        return stem, ""

    lang: str | None = None
    flags_rev: list[str] = []
    idx = len(pieces) - 1

    while idx > 0:
        token = pieces[idx]
        low = token.lower()

        if low in _SUB_FLAGS:
            if low not in flags_rev:
                flags_rev.append(low)
            idx -= 2
            continue

        code = _language_code(token)
        if code is not None:
            if lang is not None:
                break
            lang = code
            idx -= 2
            continue

        break

    if lang is None or not pieces[0]:
        return stem, ""

    core = "".join(pieces[: idx + 1])
    flags = list(reversed(flags_rev))
    tail = "." + lang
    for flag in flags:
        tail += "." + flag
    return core, tail


def clean_name(name: str, is_dir: bool = False) -> str:
    p = Path(name)
    ext = "" if is_dir else p.suffix

    if ext:
        ext_no_dot = ext[1:].lower()
        if ext_no_dot in _KNOWN_EXTS:
            ext = f".{ext_no_dot}"
        else:
            ext = ""

    if ext:
        stem = p.stem
    else:
        stem = name

    tail = ""
    if ext and ext[1:] in _SUB_EXTS:
        stem, tail = _language_tail(stem)

    had_spaces = " " in stem
    s = stem.replace("_", " ")

    def keep_bracket(m: re.Match[str]) -> str:
        inner = m.group(1)
        if re.search(r"(19|20)\d{2}", inner) or re.search(
            r"(tmdb|imdb)id-", inner.lower()
        ):
            return m.group(0)
        return " "

    s = re.sub(r"\[([^\]]*)\]", keep_bracket, s)
    s = re.sub(r"\[[^\]]*$", " ", s)

    def keep_paren(m: re.Match[str]) -> str:
        inner = m.group(1)
        if (
            re.search(r"(19|20)\d{2}", inner)
            or re.search(r"S\d{1,2}E\d{1,3}", inner, re.I)
            or "id-" in inner.lower()
        ):
            return m.group(0)
        if any(_is_junk_token(t) for t in inner.split()):
            return " "
        return m.group(0)

    s = re.sub(r"\(([^)]*)\)", keep_paren, s)
    s = re.sub(r"\([^)]*$", " ", s)

    matches: list[re.Match[str]] = []
    for m in _JUNK_RE.finditer(s):
        if matches and m.start() < matches[-1].end():
            continue
        matches.append(m)

    segments: list[str] = []
    prev = 0
    for m in matches:
        segments.append(s[prev : m.start()])
        prev = m.end()
    segments.append(s[prev:])

    kept: list[str] = []
    for idx, seg in enumerate(segments):
        cleaned = _clean_segment(seg, had_spaces)
        if not cleaned:
            continue
        if idx == 0 or _contains_year_or_sxe(cleaned):
            kept.append(cleaned)

    s = " ".join(kept)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"\[\s*\]", "", s)
    s = re.sub(r"\s*-\s*$", "", s)
    s = re.sub(r"^-\s*", "", s)
    s = s.strip(" .-_")

    if not s:
        return name
    return sanitize_name(s) + tail + ext

