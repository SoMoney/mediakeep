from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path

from mediakeep.store import Store


@dataclass(slots=True)
class Hint:
    id: int
    pattern: str
    name: str
    options: dict[str, str]
    origin: str


def parse_options(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    parts = re.split(r"[;,]", raw)
    out: dict[str, str] = {}
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
        else:
            out[part.strip().lower()] = "true"
    return out


def options_to_text(options: dict[str, str]) -> str:
    parts: list[str] = []
    for k, v in options.items():
        if v == "true":
            parts.append(k)
        else:
            parts.append(f"{k}={v}")
    return ";".join(parts)


def parse_hint_line(line: str) -> tuple[str, str, dict[str, str]] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in line.split("|", 2)]
    pattern = parts[0]
    name = parts[1] if len(parts) > 1 else ""
    options = parse_options(parts[2] if len(parts) > 2 else "")
    return pattern, name, options


def import_hints_file(store: Store, path: Path) -> int:
    store.clear_origin_hints("hints")
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parsed = parse_hint_line(line)
        if not parsed:
            continue
        pattern, name, options = parsed
        store.add_hint(pattern, name, json.dumps(options), origin="hints")
        count += 1
    return count


def load_hints(store: Store) -> list[Hint]:
    hints: list[Hint] = []
    for row in store.list_hints():
        raw_options = row["options"] or "{}"
        try:
            options = json.loads(raw_options)
        except json.JSONDecodeError:
            options = parse_options(raw_options)
        hints.append(
            Hint(
                id=int(row["id"]),
                pattern=str(row["pattern"]),
                name=str(row["name"] or ""),
                options={str(k): str(v) for k, v in options.items()},
                origin=str(row["origin"] or "ui"),
            )
        )
    return hints


def export_hints(store: Store, path: Path) -> None:
    lines: list[str] = []
    for hint in load_hints(store):
        if hint.origin != "hints":
            continue
        opt = options_to_text(hint.options)
        lines.append(f"{hint.pattern} | {hint.name} | {opt}".rstrip(" |"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


# --------------------------------------------------------------- option kinds

def hint_is_ignore(hint: Hint) -> bool:
    return hint.options.get("ignore", "").lower() in ("true", "1", "yes")


def hint_library(hint: Hint) -> str | None:
    lib = hint.options.get("lib")
    return lib or None


def hint_is_split(hint: Hint) -> bool:
    return hint.options.get("split", "").lower() in ("true", "1", "yes")


def upsert_hint(
    store: Store,
    pattern: str,
    name: str,
    options: dict[str, str] | str,
    origin: str = "ui",
) -> int:
    """Insert or update the fix with this pattern (origin ``ui``)."""
    parsed = parse_options(options) if isinstance(options, str) else dict(options)
    text = options_to_text(parsed)
    for row in store.list_hints():
        if str(row["pattern"]) == pattern and str(row["origin"] or "ui") == origin:
            store.update_hint(int(row["id"]), pattern, name, text)
            return int(row["id"])
    return store.add_hint(pattern, name, text, origin=origin)


def describe_hint(hint: Hint) -> tuple[str, str]:
    """``(what it does, detail)`` in plain words, for the fixes table."""
    opts = hint.options
    kind_word = {"movie": "Movie", "show": "TV show"}.get(opts.get("kind", ""), "")
    if hint_is_ignore(hint):
        return "Leave it alone", "Never scanned or moved"
    if hint_is_split(hint):
        return (
            "Split it into separate movies",
            "One folder per film, each filed on its own",
        )
    lib = hint_library(hint)
    if lib:
        return f"Put it in {lib}", 'Overrides "Where things go" for this name'
    if opts.get("tmdb"):
        detail = " · ".join(x for x in (kind_word, f"TMDB {opts['tmdb']}") if x)
        return f"Match it to {hint.name or 'the saved title'}", detail
    if opts.get("imdb"):
        detail = " · ".join(x for x in (kind_word, f"IMDb {opts['imdb']}") if x)
        return f"Match it to {hint.name or 'the saved title'}", detail
    if opts.get("kind") == "show":
        return "Treat it as a TV show", hint.name
    if opts.get("kind") == "movie":
        return "Treat it as a movie", hint.name
    if hint.name:
        return f"Call it {hint.name}", options_to_text(opts)
    return "Remembered fix", options_to_text(opts)


# ------------------------------------------------------------ name matching

_JUNK_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    r"1080p|720p|480p|2160p|4k|uhd|fhd|hd|sd"
    r"|blu[-.?]?ray|bluray|bdrip|brrip|br|dvdrip|dvdr|hdrip|hdtv|web[-.]?dl|webrip|web"
    r"|x264|x265|h[.]?264|h[.]?265|hevc|av1|xvid|divx|mpeg[-]?2|vc1"
    r"|aac\d*(?:[.]?\d)?|ac3|eac3|dd[p51.]+|dolby[-]?digital|atmos|dts(?:[-]?hd)?|flac|opus|lpcm|mp3|5[.]1|7[.]1|2[.]0"
    r"|10[-]?bit|8[-]?bit|hdr10[+]?|hdr|10bits?|sdr|dv|dolby[-]?vision"
    r"|remastered|uncut|unrated|extended|directors[-]?cut|imax|proper|repack|internal|limited|multi|dual"
    r"|s\d{1,2}(?:e\d{1,3})?|\d{1,2}x\d{1,3}|e\d{1,3}|vol(?:ume)?\.?\d+|part\.?\d+"
    r")(?:-[a-z0-9]+)?(?![a-z0-9])",
    re.I,
)
_YEAR_RE = re.compile(r"(?<![a-z0-9])(?:19|20)\d{2}(?![a-z0-9])")
_GROUP_RE = re.compile(r"[-_]([a-z0-9]{2,12})$")
_EXT_RE = re.compile(r"\.[a-z0-9]{1,5}$", re.I)
_ARTICLES = ("the ", "a ", "an ")


def _strip_ext(name: str) -> str:
    return _EXT_RE.sub("", name)


def normalize_hint_name(text: str) -> str:
    """Normalize a file, folder or pattern name for title matching.

    Ignores case, dots, underscores, release junk (resolution, codec, source,
    group), episode numbers and years, so ``the.lighthouse.keepers.s01e01.720p.mkv``
    normalizes the same way as ``The Lighthouse Keepers``.
    """
    out = _strip_ext(str(text))
    out = out.replace(".", " ").replace("_", " ").replace("/", " ")
    out = _JUNK_RE.sub(" ", out)
    out = _YEAR_RE.sub(" ", out)
    out = re.sub(r"[\[\](){}]+", " ", out)
    out = re.sub(r"[^a-z0-9]+", " ", out.lower()).strip()
    # drop a trailing release group ("hdc", "byndr") when words remain
    m = _GROUP_RE.search(out)
    if m and len(out.split()) > 1:
        stripped = out[: m.start()].strip()
        if stripped:
            out = stripped
    out = re.sub(r"\s+", " ", out).strip()
    low = out
    for article in _ARTICLES:
        if low.startswith(article):
            low = low[len(article) :]
            break
    return low


def pattern_is_title(pattern: str) -> bool:
    """A pattern with no path separator or wildcard matches by title."""
    pat = (pattern or "").strip()
    if not pat:
        return False
    return not any(ch in pat for ch in ("\\", "/", "*"))


def title_matches(pattern: str, name: str) -> bool:
    pat = normalize_hint_name(pattern)
    if not pat:
        return False
    for candidate in (name, _strip_ext(name)):
        norm = normalize_hint_name(candidate)
        if norm and norm == pat:
            return True
    try:  # the parsed release title is a second opinion
        from mediakeep.parse import parse_release_name

        parsed = parse_release_name(_strip_ext(name)).title
        if parsed and normalize_hint_name(parsed) == pat:
            return True
    except Exception:
        pass
    return False


def hint_year_conflicts(hint: Hint, name: str) -> bool:
    """A title fix is for one film: it doesn't apply to a name with a different year.
    ("Lucy in Paperland" fixed to the 1951 film must not catch "Lucy in Paperland (2010)".)
    ``name_year`` is the year the name had when the fix was made; older fixes only have
    the film's ``year``."""
    raw = hint.options.get("name_year") or hint.options.get("year")
    if not raw:
        return False
    try:
        fix_year = int(float(str(raw)))
    except ValueError:
        return False
    try:
        from mediakeep.parse import parse_release_name

        name_year = parse_release_name(_strip_ext(name)).year
    except Exception:
        return False
    return name_year is not None and name_year != fix_year


def path_matches_hint(hint: Hint, path: Path) -> bool:
    """True when ``hint`` applies to ``path`` (absolute paths allowed)."""
    pat = (hint.pattern or "").strip()
    if not pat:
        return False
    name = Path(path).name
    if pattern_is_title(pat):
        return title_matches(pat, name) and not hint_year_conflicts(hint, name)
    full = str(path).replace("\\", "/").rstrip("/").lower()
    needle = pat.replace("\\", "/").strip("/").lower()
    if not needle:
        return False
    if fnmatch.fnmatch(full, needle) or fnmatch.fnmatch(full, "*/" + needle):
        return True
    if full == needle or full.endswith("/" + needle):
        return True
    base = full.rsplit("/", 1)[-1]
    return base == needle.rsplit("/", 1)[-1] and needle.count("/") <= full.count("/")


def match_hint(
    root: Path,
    rel: Path,
    hints: list[Hint],
    folder: bool = False,
) -> Hint | None:
    """Find the fix that applies to ``rel`` (a path relative to ``root``).

    Two kinds of pattern:

    * a **path** pattern (contains ``\\``, ``/`` or ``*``) matches as before -
      a wildcard, or a folder prefix;
    * a **title** pattern (plain text) also matches when the name normalizes
      to the same title, so ``The Lighthouse Keepers`` matches
      ``the.lighthouse.keepers.s01e01.720p.mkv``.

    ``ignore`` fixes are never returned here: they keep paths out of the plan,
    they do not name anything. The planner applies them (``planner.enforce_plan_rules``).
    """
    reln = str(rel).replace("\\", "/").strip("/")
    rel_l = reln.lower()
    base = rel_l.rsplit("/", 1)[-1]
    parts = [p for p in rel_l.split("/") if p]
    best: Hint | None = None
    best_score = -1
    for hint in hints:
        if hint_is_ignore(hint):
            continue
        pat = hint.pattern.replace("\\", "/").strip("/")
        if not pat:
            continue
        pat_l = pat.lower()
        score = -1
        if fnmatch.fnmatch(rel_l, pat_l):
            score = 1000 + len(pat)
        elif rel_l.startswith(pat_l + "/"):
            score = 1000 + len(pat)
        elif pattern_is_title(pat):
            names = {base, *parts}
            if any(title_matches(pat, n) and not hint_year_conflicts(hint, n) for n in names):
                score = len(pat)
        if score > best_score:
            best = hint
            best_score = score
    return best
