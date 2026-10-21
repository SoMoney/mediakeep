from __future__ import annotations

import os
import re
from pathlib import Path

_FORBIDDEN = set('<>:"/\\|?*')
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}


def strip_long(path: str | Path) -> Path:
    s = str(path)
    if s.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + s[8:])
    if s.startswith("\\\\?\\"):
        return Path(s[4:])
    return Path(s)


def long_path(path: str | Path) -> str:
    p = str(path)
    if os.name != "nt":
        return p
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    if len(p) >= 2 and p[1] == ":":
        return "\\\\?\\" + p
    return "\\\\?\\" + os.path.abspath(p)


def _replace_colons(s: str) -> str:
    out: list[str] = []
    for ch in s:
        if ch != ":":
            out.append(ch)
            continue

        left = "".join(out).rstrip()
        # "or:" marks an alternative title, not a subtitle separator.
        if left.lower().endswith("or") and (len(left) <= 2 or not left[-3].isalpha()):
            continue
        out.append(" - ")
    return "".join(out)


# Typographic characters TMDB titles use, turned into plain ones that every file system,
# player and keyboard handles ("ROBO·T" -> "ROBO-T").
_PLAIN = str.maketrans(
    {
        "\u00b7": "-",  # · middle dot
        "\u2027": "-",  # ‧ hyphenation point
        "\u2022": "-",  # • bullet
        "\u2010": "-",  # ‐ hyphen
        "\u2011": "-",  # ‑ non-breaking hyphen
        "\u2012": "-",  # ‒ figure dash
        "\u2013": "-",  # – en dash
        "\u2014": "-",  # — em dash
        "\u2015": "-",  # ― horizontal bar
        "\u2212": "-",  # − minus
        "\u2018": "'",  # ‘
        "\u2019": "'",  # ’
        "\u201b": "'",  # ‛
        "\u2032": "'",  # ′
        "\u201c": "",  # “
        "\u201d": "",  # ”
        "\u201e": "",  # „
        "\u2026": "...",  # …
        "\u00a0": " ",  # no-break space
        "\u2009": " ",  # thin space
        "\u2044": " ",  # ⁄ fraction slash
    }
)


def sanitize_name(name: str) -> str:
    s = str(name).translate(_PLAIN)
    s = _replace_colons(s)
    s = s.replace("/", " ").replace("\\", " ")
    s = _CONTROL.sub("", s)
    s = "".join(ch for ch in s if ch not in _FORBIDDEN)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*(--+)\s*", r" - ", s)
    s = s.strip()
    s = s.strip(" .-_")
    if not s:
        return ""
    stem = s.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        s = "_" + s
    return s


def unsafe_name_reason(name: str) -> str | None:
    """Return a plain reason why a single path component cannot be written, or None."""
    if not name:
        return "name is empty"
    if len(name) > 255:
        return "name is longer than 255 characters"
    if _CONTROL.search(name):
        return "name contains a control character"
    if any(ch in _FORBIDDEN for ch in name):
        return "name contains a forbidden character"
    if name[-1] in " .":
        return "name ends in a space or a dot"
    stem = name.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        return "name is a reserved Windows device name"
    return None


def same_volume(a: Path, b: Path) -> bool:
    try:
        return os.stat(long_path(a)).st_dev == os.stat(long_path(b)).st_dev
    except OSError:
        return Path(a).drive.lower() == Path(b).drive.lower()


def clear_hidden(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return
        kernel32 = getattr(windll, "kernel32", None)
        if kernel32 is None:
            return

        file_attribute_hidden = 0x00000002
        attrs = kernel32.GetFileAttributesW(long_path(path))
        if attrs == -1:
            return
        if attrs & file_attribute_hidden:
            kernel32.SetFileAttributesW(long_path(path), attrs & ~file_attribute_hidden)
    except Exception:
        pass
