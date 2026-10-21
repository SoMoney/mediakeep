from __future__ import annotations

import fnmatch
import hashlib
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from mediakeep.config import AppConfig
from mediakeep.hints import (
    Hint,
    hint_is_ignore,
    hint_is_split,
    match_hint,
    pattern_is_title,
    path_matches_hint,
    title_matches,
)
from mediakeep.models import EpisodeRef, FileInfo, Issue, MediaKind, Severity, SourceItem
from mediakeep.parse import (
    episode_prefix,
    id_tag_options,
    is_strict_episode,
    normalize_title,
    parse_episode,
    parse_release_name,
)
from mediakeep.scan import ScannedEntry

VIDEO_EXTS = {
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
SUB_EXTS = {"srt", "sub", "idx", "ass", "ssa", "vtt", "smi", "sup"}
COLLECTION_WORDS = (
    "series",
    "collection",
    "trilogy",
    "quadrilogy",
    "anthology",
    "box set",
    "boxset",
)
SUB_DIRS = {"sub", "subs", "subtitle", "subtitles"}

GENERIC_FOLDER_NAMES = {
    "tv",
    "shows",
    "series",
    "tv shows",
    "episodes",
    "movies",
    "films",
    "downloads",
    "complete",
    "staging",
    "zstaging",
    "incoming",
    "new",
    "media",
    "videos",
    "sample",
    "samples",
}

_SEASON_RE = re.compile(r"season \d{1,2}", re.I)
_SPECIAL_RE = re.compile(r"specials?", re.I)


@dataclass(slots=True)
class SplitFiles:
    videos: list[FileInfo] = field(default_factory=list)
    subtitles: list[FileInfo] = field(default_factory=list)
    sidecars: list[FileInfo] = field(default_factory=list)
    samples: list[FileInfo] = field(default_factory=list)
    extras: list[FileInfo] = field(default_factory=list)


@dataclass(slots=True)
class ShowCandidate:
    norm: str
    year: int | None
    year_source: str
    paths: list[Path] = field(default_factory=list)
    videos: list[FileInfo] = field(default_factory=list)
    subtitles: list[FileInfo] = field(default_factory=list)
    sidecars: list[FileInfo] = field(default_factory=list)
    samples: list[FileInfo] = field(default_factory=list)
    extras: list[FileInfo] = field(default_factory=list)
    title_variants: list[str] = field(default_factory=list)
    hint_name: str = ""
    hint_options: dict[str, str] = field(default_factory=dict)
    roots: list[Path] = field(default_factory=list)


def ext_of(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def is_video(path: Path) -> bool:
    return ext_of(path) in VIDEO_EXTS


def is_sub(path: Path) -> bool:
    return ext_of(path) in SUB_EXTS


def is_sample_video(fi: FileInfo) -> bool:
    name = fi.path.name.lower()
    if re.search(r"(^|[^a-z])sample([^a-z]|$)", name):
        return True
    for part in fi.path.parts[:-1]:
        if part.lower() in {"sample", "samples"}:
            return True
    return False


def _non_sample_videos(files: list[FileInfo]) -> list[FileInfo]:
    return [f for f in files if is_video(f.path) and not is_sample_video(f)]


def split_files(files: list[FileInfo]) -> SplitFiles:
    out = SplitFiles()
    for f in files:
        if is_video(f.path):
            if is_sample_video(f):
                out.samples.append(f)
            else:
                out.videos.append(f)
        elif is_sub(f.path):
            out.subtitles.append(f)
        else:
            out.extras.append(f)

    real_extras: list[FileInfo] = []
    for f in out.extras:
        if any(f.path.name.startswith(v.path.name + ".") for v in out.videos):
            out.sidecars.append(f)
        else:
            real_extras.append(f)
    out.extras = real_extras
    return out


def fingerprint(fi: FileInfo) -> str:
    h = hashlib.sha256()
    h.update(str(fi.size).encode("utf-8"))
    try:
        with open(fi.path, "rb") as f:
            head = f.read(1024 * 1024)
            h.update(head)
            if fi.size > 1024 * 1024:
                f.seek(max(0, fi.size - 1024 * 1024))
                h.update(f.read(1024 * 1024))
    except OSError:
        h.update(str(fi.path).encode("utf-8"))
    return h.hexdigest()[:16]


def item_key_for(videos: list[FileInfo], fallback: Path) -> str:
    if videos:
        largest = max(videos, key=lambda f: f.size)
        return fingerprint(largest)
    return hashlib.sha256(str(fallback).encode("utf-8")).hexdigest()[:16]


def ensure_unique_keys(items: list[SourceItem]) -> None:
    seen: set[str] = set()
    for item in items:
        key = item.key
        if key in seen:
            extra = hashlib.sha256(
                "\n".join(str(p) for p in item.paths).encode("utf-8")
            ).hexdigest()[:8]
            key = f"{item.key}-{extra}"
        item.key = key
        seen.add(key)


def _is_generic_folder(name: str) -> bool:
    low = name.strip().lower()
    if low in GENERIC_FOLDER_NAMES:
        return True
    return bool(_SEASON_RE.fullmatch(low) or _SPECIAL_RE.fullmatch(low))


def _is_ignored_loose_extra(path: Path, config: AppConfig) -> bool:
    if path.name.lower() == "jf-hints.txt":
        return True
    if config.hints_file and str(path).lower() == str(Path(config.hints_file)).lower():
        return True
    return False


def _hint_options_with_ids(
    hint: Hint | None, id_opts: dict[str, str]
) -> tuple[str, dict[str, str]]:
    options = dict(id_opts)
    hint_name = ""
    if hint:
        options.update(hint.options)
        if hint.name:
            hint_clean, hint_id_opts = id_tag_options(hint.name)
            hint_name = hint_clean
            options.update(hint_id_opts)
    return hint_name, options


def resolve_title_year(
    folder_name: str,
    videos: list[FileInfo],
    hint_name: str,
    has_hint: bool,
) -> tuple[str, int | None, str]:
    title = ""
    year: int | None = None
    source = ""

    if hint_name:
        p = parse_release_name(hint_name)
        title = p.title
        year = p.year
        source = "hint" if year is not None else ""

    if not title and folder_name and not _is_generic_folder(folder_name):
        p = parse_release_name(folder_name)
        if p.title:
            title = p.title
        if year is None and p.year is not None:
            year = p.year
            source = "folder"

    if year is None and videos:
        years: list[int] = []
        file_titles: list[str] = []
        for v in videos:
            p = parse_release_name(v.path.name)
            if p.year is not None:
                years.append(p.year)
            if p.title and not file_titles:
                file_titles.append(p.title)
        if years and len(set(years)) == 1:
            year = years[0]
            source = "file"
        if not title and file_titles:
            title = file_titles[0]

    return title, year, source


def _is_dvd(entry: ScannedEntry) -> bool:
    if (entry.path / "VIDEO_TS" / "VIDEO_TS.IFO").exists():
        return True
    return any(f.path.name.lower() == "video_ts.ifo" for f in entry.files)


def _is_bluray(entry: ScannedEntry) -> bool:
    if (entry.path / "BDMV").is_dir():
        return True
    return any("bdmv" in p.parts for p in [f.path for f in entry.files])


def _should_expand_dir(entry: ScannedEntry) -> bool:
    if not entry.is_dir:
        return False
    if _is_dvd(entry) or _is_bluray(entry):
        return False
    if _is_generic_folder(entry.path.name):
        return True

    videos = _non_sample_videos(entry.files)
    if not videos:
        return False

    prefixes: set[str] = set()
    for v in videos:
        prefix = episode_prefix(v.path.name)
        if prefix is None:
            return False
        prefixes.add(normalize_title(prefix))
    return len(prefixes) >= 2


def _expand_entries(entries: list[ScannedEntry]) -> list[ScannedEntry]:
    q: deque[ScannedEntry] = deque(entries)
    out: list[ScannedEntry] = []
    while q:
        entry = q.popleft()
        if not _should_expand_dir(entry):
            out.append(entry)
            continue

        children: list[ScannedEntry] = []
        for sub in entry.subdirs:
            if sub.parent != entry.path:
                continue
            children.append(
                ScannedEntry(
                    path=sub,
                    is_dir=True,
                    files=[f for f in entry.files if sub in f.path.parents],
                    subdirs=[s for s in entry.subdirs if sub in s.parents],
                )
            )
        for f in entry.files:
            if f.path.parent == entry.path:
                children.append(
                    ScannedEntry(path=f.path, is_dir=False, files=[f], subdirs=[])
                )
        q.extendleft(reversed(children))
    return out


def _looks_show(entry: ScannedEntry) -> bool:
    videos = _non_sample_videos(entry.files)
    if videos and any(d.name.lower().startswith("season") for d in entry.subdirs):
        return True

    numbered = 0
    distinct: set[tuple[int, int]] = set()
    for v in videos:
        if re.search(r"s\d{1,2}e\d{1,3}", v.path.name, re.I) or re.search(
            r"\d{1,2}x\d{1,3}", v.path.name, re.I
        ):
            numbered += 1
        ep = parse_episode(v.path.name)
        if ep:
            distinct.add((ep.season, ep.episode))
    if numbered >= 3:
        return True
    if len(videos) >= 6 and len(distinct) * 2 > len(videos):
        return True
    return False


def _looks_like_movie_folder(name: str) -> bool:
    p = parse_release_name(name)
    return p.year is not None


def _files_under(files: list[FileInfo], root: Path) -> list[FileInfo]:
    return [f for f in files if f.path == root or root in f.path.parents]


def _is_container(entry: ScannedEntry) -> bool:
    if any(is_video(f.path) for f in _non_sample_videos(entry.files) if f.path.parent == entry.path):
        return False
    for sub in entry.subdirs:
        if sub.parent != entry.path:
            continue
        if not _looks_like_movie_folder(sub.name):
            continue
        if any(is_video(f.path) for f in _non_sample_videos(_files_under(entry.files, sub))):
            return True
    return False


def _collection_title(name: str) -> str:
    p = parse_release_name(name)
    return p.title or name


def _make_movie_item(
    root: Path,
    path: Path,
    files: list[FileInfo],
    hint: Hint | None,
    kind: MediaKind = MediaKind.MOVIE,
) -> tuple[SourceItem | None, list[Issue]]:
    split = split_files(files)
    clean_name, id_opts = id_tag_options(path.name)
    hint_name, options = _hint_options_with_ids(hint, id_opts)
    title, year, year_source = resolve_title_year(
        clean_name, split.videos, hint_name, bool(hint)
    )
    issues: list[Issue] = []

    if kind == MediaKind.DVD:
        # DVD planning treats every disc file as a movable video file.
        split.videos = list(files)
        split.extras = []

    if (
        year is not None
        and year_source not in {"file"}
        and not options
        and not _trust_folder_year_from_options(options)
    ):
        mismatches: list[FileInfo] = []
        for v in split.videos:
            y = parse_release_name(v.path.name).year
            if y is not None and y != year:
                mismatches.append(v)
        if mismatches:
            if len(mismatches) == 1:
                issues.append(
                    Issue(
                        item_key=None,
                        path=path,
                        severity=Severity.ATTENTION,
                        message="year in the file differs from the folder; fix whichever is wrong",
                    )
                )
            else:
                issues.append(
                    Issue(
                        item_key=None,
                        path=path,
                        severity=Severity.ATTENTION,
                        message="probably several movies; give each its own folder",
                    )
                )
            return None, issues

    key = item_key_for(split.videos, path)
    item = SourceItem(
        key=key,
        root=root,
        paths=[path],
        kind=kind,
        title=title,
        year=year,
        year_source=year_source,
        videos=split.videos,
        subtitles=split.subtitles,
        sidecars=split.sidecars,
        samples=split.samples,
        extras=split.extras,
        episodes=[],
        hint_options={k: str(v) for k, v in options.items()},
    )
    return item, issues


def _trust_folder_year_from_options(options: dict[str, str]) -> bool:
    return "movie" in options or "show" in options


def _show_candidate_from_dir(
    root: Path,
    path: Path,
    files: list[FileInfo],
    hint: Hint | None,
) -> tuple[ShowCandidate | None, list[Issue]]:
    split = split_files(files)
    clean_name, id_opts = id_tag_options(path.name)
    hint_name, options = _hint_options_with_ids(hint, id_opts)
    title, year, year_source = resolve_title_year(
        clean_name, split.videos, hint_name, bool(hint)
    )

    if not title:
        for v in split.videos:
            prefix = episode_prefix(v.path.name)
            if prefix:
                p = parse_release_name(prefix)
                title = p.title or prefix
                if year is None and p.year is not None:
                    year = p.year
                    year_source = "file"
                break

    if not title:
        return None, [
            Issue(
                item_key=None,
                path=path,
                severity=Severity.ATTENTION,
                message="could not determine show title",
            )
        ]

    if not year_source:
        year_source = "file" if year is not None else ""

    cand = ShowCandidate(
        norm=normalize_title(title),
        year=year,
        year_source=year_source,
        paths=[path],
        videos=split.videos,
        subtitles=split.subtitles,
        sidecars=split.sidecars,
        samples=split.samples,
        extras=split.extras,
        title_variants=[title],
        hint_name=hint_name,
        hint_options={k: str(v) for k, v in options.items()},
        roots=[root],
    )
    return cand, []


def _show_candidate_from_file(
    root: Path,
    file: FileInfo,
    hint: Hint | None,
) -> tuple[ShowCandidate | None, list[Issue]]:
    clean_name, id_opts = id_tag_options(file.path.name)
    prefix = episode_prefix(clean_name)
    if prefix:
        p = parse_release_name(prefix)
        title = p.title or prefix
        year = p.year
        year_source = "file" if year is not None else ""
    else:
        p = parse_release_name(clean_name)
        title = p.title
        year = p.year
        year_source = "file" if year is not None else ""

    hint_name, options = _hint_options_with_ids(hint, id_opts)
    if hint_name:
        hint_release = parse_release_name(hint_name)
        title = hint_release.title or hint_name
        if hint_release.year is not None:
            year = hint_release.year
            year_source = "hint"

    if not title:
        return None, [
            Issue(
                item_key=None,
                path=file.path,
                severity=Severity.ATTENTION,
                message="could not determine show title",
            )
        ]

    cand = ShowCandidate(
        norm=normalize_title(title),
        year=year,
        year_source=year_source,
        paths=[file.path],
        videos=[file],
        title_variants=[title],
        hint_name=hint_name,
        hint_options={k: str(v) for k, v in options.items()},
        roots=[root],
    )
    return cand, []


def _best_show_title(cand: ShowCandidate) -> str:
    if cand.hint_name:
        p = parse_release_name(cand.hint_name)
        return p.title or cand.hint_name
    if not cand.title_variants:
        return ""
    return max(cand.title_variants, key=lambda s: sum(c.isupper() for c in s))


def _merge_show_candidate(old: ShowCandidate, new: ShowCandidate) -> None:
    old_paths = set(old.paths)
    for p in new.paths:
        if p not in old_paths:
            old.paths.append(p)
            old_paths.add(p)

    for name, old_list, new_list in (
        ("videos", old.videos, new.videos),
        ("subtitles", old.subtitles, new.subtitles),
        ("sidecars", old.sidecars, new.sidecars),
        ("samples", old.samples, new.samples),
        ("extras", old.extras, new.extras),
    ):
        seen = {f.path for f in old_list}
        for f in new_list:
            if f.path not in seen:
                old_list.append(f)
                seen.add(f.path)

    old.title_variants.extend(new.title_variants)
    old.hint_name = old.hint_name or new.hint_name
    old.hint_options.update(new.hint_options)
    old.roots.extend(new.roots)
    if old.year is None and new.year is not None:
        old.year = new.year
        old.year_source = new.year_source or "file"


def _merge_show_candidates(
    candidates: dict[str, ShowCandidate],
) -> list[SourceItem]:
    items: list[SourceItem] = []
    for key, cand in candidates.items():
        final_title = _best_show_title(cand)
        if not final_title:
            continue

        merged_videos = cand.videos
        fallback = merged_videos[0].path if merged_videos else Path(key)
        key_hash = item_key_for(merged_videos, fallback)

        episodes: list[EpisodeRef] = []
        for v in merged_videos:
            ep = parse_episode(v.path.name, final_title)
            if not ep:
                continue
            season = ep.season
            episode = ep.episode
            end = ep.episode_end
            version = ep.version_tag
            title = ep.title
            absolute = ep.absolute
            label = cand.hint_options.get("version", "")
            if "ep" in cand.hint_options:
                m = re.match(
                    r"s(\d{1,2})e(\d{1,3})(?:[-_ ]?e?(\d{1,3}))?",
                    cand.hint_options["ep"],
                    re.I,
                )
                if m:
                    season = int(m.group(1))
                    episode = int(m.group(2))
                    end = int(m.group(3)) if m.group(3) else None
            if absolute:
                if "season" in cand.hint_options:
                    try:
                        season = int(cand.hint_options["season"])
                    except ValueError:
                        season = 1
                if "offset" in cand.hint_options:
                    try:
                        episode -= int(cand.hint_options["offset"])
                    except ValueError:
                        pass
            episodes.append(
                EpisodeRef(
                    file=v,
                    season=season,
                    episode=episode,
                    episode_end=end,
                    title=title,
                    version_tag=version,
                    absolute=absolute,
                    label=label,
                )
            )

        item = SourceItem(
            key=key_hash,
            root=cand.roots[0] if cand.roots else Path("."),
            paths=list(dict.fromkeys(cand.paths)),
            kind=MediaKind.SHOW,
            title=final_title,
            year=cand.year,
            year_source=cand.year_source,
            videos=merged_videos,
            subtitles=cand.subtitles,
            sidecars=cand.sidecars,
            samples=cand.samples,
            extras=cand.extras,
            episodes=episodes,
            hint_options=cand.hint_options,
        )
        items.append(item)
    return items


def _norm_match(path: Path | str) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


def _ignore_hint_for(
    root: Path, rel: Path, path: Path, hints: list[Hint]
) -> Hint | None:
    """Return an ``ignore`` fix that matches this path (or an ancestor)."""
    full = _norm_match(path)
    reln = _norm_match(rel)
    parts = [p for p in reln.split("/") if p]

    for hint in hints:
        if not hint_is_ignore(hint):
            continue
        pat = str(hint.pattern).replace("\\", "/").strip("/").lower()
        if not pat:
            continue

        if pattern_is_title(hint.pattern):
            if title_matches(pat, path.name) or any(
                title_matches(pat, part) for part in parts
            ):
                return hint
        else:
            if (
                fnmatch.fnmatch(full, pat)
                or fnmatch.fnmatch(full, "*/" + pat)
                or full == pat
                or full.startswith(pat + "/")
            ):
                return hint
            if path_matches_hint(hint, path):
                return hint

    return None


def _split_hint_items(
    root: Path,
    entry: ScannedEntry,
    hints: list[Hint],
    used_hints: set[int],
) -> tuple[list[SourceItem], list[Issue]]:
    """A ``split`` fix: each loose video or child folder becomes a movie item."""
    out: list[SourceItem] = []
    issues: list[Issue] = []
    first = True

    for sub in entry.subdirs:
        if sub.parent != entry.path:
            continue
        sub_files = _files_under(entry.files, sub)
        if not any(is_video(f.path) for f in _non_sample_videos(sub_files)):
            continue

        sub_rel = sub.relative_to(root)
        if _ignore_hint_for(root, sub_rel, sub, hints):
            continue

        sub_hint = match_hint(root, sub_rel, hints, folder=True)
        if sub_hint:
            used_hints.add(sub_hint.id)
        if sub_hint and "skip" in sub_hint.options:
            continue

        item, iss = _make_movie_item(root, sub, sub_files, sub_hint)
        issues.extend(iss)
        if item is not None:
            if first:
                item.hint_options["container_rmdir"] = str(entry.path)
                first = False
            out.append(item)

    for fi in entry.files:
        if fi.path.parent != entry.path:
            continue
        if not is_video(fi.path) or is_sample_video(fi):
            continue

        file_rel = fi.path.relative_to(root)
        if _ignore_hint_for(root, file_rel, fi.path, hints):
            continue

        file_hint = match_hint(root, file_rel, hints, folder=False)
        if file_hint:
            used_hints.add(file_hint.id)
        if file_hint and "skip" in file_hint.options:
            continue

        item, iss = _make_movie_item(root, fi.path, [fi], file_hint)
        issues.extend(iss)
        if item is not None:
            if first:
                item.hint_options["container_rmdir"] = str(entry.path)
                first = False
            out.append(item)

    return out, issues


def classify(
    config: AppConfig,
    inventory: dict[Path, list[ScannedEntry]],
    hints: list[Hint],
    used_hints: set[int],
) -> tuple[list[SourceItem], list[Issue], list[FileInfo]]:
    items: list[SourceItem] = []
    issues: list[Issue] = []
    loose_samples: list[FileInfo] = []
    movie_by_norm: dict[str, SourceItem] = {}
    show_candidates: dict[str, ShowCandidate] = {}
    show_keys_by_norm: dict[str, list[str]] = {}
    ignored_dirs: set[str] = set()

    def mark_hint(hint: Hint | None) -> None:
        if hint:
            used_hints.add(hint.id)

    def add_show_candidate(cand: ShowCandidate) -> None:
        if not cand.norm:
            return
        for key in show_keys_by_norm.get(cand.norm, []):
            existing = show_candidates[key]
            if (
                existing.year is None
                or cand.year is None
                or existing.year == cand.year
            ):
                _merge_show_candidate(existing, cand)
                return

        key = f"{cand.norm}|{cand.year if cand.year is not None else ''}"
        if key in show_candidates:
            key = f"{key}-{len(show_candidates)}"
        show_candidates[key] = cand
        show_keys_by_norm.setdefault(cand.norm, []).append(key)

    expanded_inventory: dict[Path, list[ScannedEntry]] = {}
    for root, entries in inventory.items():
        expanded_inventory[root] = _expand_entries(entries)
    inventory = expanded_inventory

    # Pass 1: directories.
    for root, entries in inventory.items():
        for entry in sorted(entries, key=lambda e: len(e.path.parts)):
            if not entry.is_dir:
                continue
            rel = entry.path.relative_to(root)
            full = _norm_match(entry.path)

            if any(full.startswith(d + "/") for d in ignored_dirs):
                continue

            ignore = _ignore_hint_for(root, rel, entry.path, hints)
            if ignore is not None:
                used_hints.add(ignore.id)
                ignored_dirs.add(full)
                issues.append(
                    Issue(
                        item_key=None,
                        path=entry.path,
                        severity=Severity.NOTE,
                        message="left alone: remembered fix",
                    )
                )
                continue

            hint = match_hint(root, rel, hints, folder=True)
            mark_hint(hint)
            if hint and "skip" in hint.options:
                continue

            if hint and hint_is_split(hint):
                split_items, split_issues = _split_hint_items(
                    root, entry, hints, used_hints
                )
                issues.extend(split_issues)
                if split_items:
                    items.extend(split_items)
                    continue
                continue

            if _is_dvd(entry):
                item, iss = _make_movie_item(
                    root, entry.path, entry.files, hint, kind=MediaKind.DVD
                )
                issues.extend(iss)
                if item:
                    items.append(item)
                continue

            if _is_bluray(entry):
                item, iss = _make_movie_item(
                    root, entry.path, entry.files, hint, kind=MediaKind.BLURAY
                )
                issues.extend(iss)
                if item:
                    items.append(item)
                continue

            early_split = split_files(entry.files)
            if not early_split.videos and early_split.samples:
                continue

            options = hint.options if hint else {}
            is_show_hint = "show" in options
            is_movie_hint = "movie" in options

            if is_show_hint or (not is_movie_hint and _looks_show(entry)):
                cand, iss = _show_candidate_from_dir(
                    root, entry.path, entry.files, hint
                )
                issues.extend(iss)
                if cand:
                    add_show_candidate(cand)
                continue

            collection_title = _collection_title(entry.path.name)
            if collection_title.lower().endswith(COLLECTION_WORDS):
                parsed: list[tuple[str, int]] = []
                for f in entry.files:
                    if is_video(f.path):
                        p = parse_release_name(f.path.name)
                        if p.title and p.year is not None:
                            parsed.append((p.title, p.year))
                parsed = sorted(set(parsed))
                suggestion: str | None = None
                if parsed:
                    names = [f"{t} ({y})" for t, y in parsed]
                    suggestion = " and ".join(f'"{n}"' for n in names)
                issues.append(
                    Issue(
                        item_key=None,
                        path=entry.path,
                        severity=Severity.ATTENTION,
                        message="collection; nothing moved",
                        suggestion=suggestion,
                    )
                )
                continue

            if _is_container(entry):
                first = True
                for sub in entry.subdirs:
                    if sub.parent != entry.path:
                        continue
                    if not _looks_like_movie_folder(sub.name):
                        continue
                    sub_files = _files_under(entry.files, sub)
                    if not any(is_video(f.path) for f in _non_sample_videos(sub_files)):
                        continue
                    sub_rel = sub.relative_to(root)
                    sub_hint = match_hint(root, sub_rel, hints, folder=True)
                    mark_hint(sub_hint)
                    if sub_hint and "skip" in sub_hint.options:
                        continue
                    item, iss = _make_movie_item(root, sub, sub_files, sub_hint)
                    issues.extend(iss)
                    if item:
                        if first:
                            item.hint_options["container_rmdir"] = str(entry.path)
                            first = False
                        items.append(item)
                        movie_by_norm[f"{normalize_title(item.title)}|{item.year}"] = item
                continue

            split = early_split
            if not split.videos and not split.samples and split.extras:
                issues.append(
                    Issue(
                        item_key=None,
                        path=entry.path,
                        severity=Severity.ATTENTION,
                        message="might be the video with a wrong extension",
                    )
                )
                continue

            if not split.videos and not split.samples and not split.extras:
                continue

            item, iss = _make_movie_item(root, entry.path, entry.files, hint)
            issues.extend(iss)
            if item:
                items.append(item)
                movie_by_norm[f"{normalize_title(item.title)}|{item.year}"] = item

    # Pass 2: loose files.
    for root, entries in inventory.items():
        for entry in sorted(entries, key=lambda e: len(e.path.parts)):
            if entry.is_dir or not entry.files:
                continue
            fi = entry.files[0]
            rel = entry.path.relative_to(root)
            full = _norm_match(entry.path)

            if any(full.startswith(d + "/") for d in ignored_dirs):
                continue

            ignore = _ignore_hint_for(root, rel, entry.path, hints)
            if ignore is not None:
                used_hints.add(ignore.id)
                issues.append(
                    Issue(
                        item_key=None,
                        path=entry.path,
                        severity=Severity.NOTE,
                        message="left alone: remembered fix",
                    )
                )
                continue

            hint = match_hint(root, rel, hints, folder=False)
            mark_hint(hint)
            if hint and "skip" in hint.options:
                continue

            if is_video(fi.path):
                if is_sample_video(fi):
                    continue

                options = hint.options if hint else {}
                if "show" in options or is_strict_episode(fi.path.name):
                    cand, iss = _show_candidate_from_file(root, fi, hint)
                    issues.extend(iss)
                    if cand:
                        add_show_candidate(cand)
                    continue

                clean_name, id_opts = id_tag_options(fi.path.name)
                p = parse_release_name(clean_name)
                hint_name, options = _hint_options_with_ids(hint, id_opts)
                title = hint_name if hint_name else p.title
                year = p.year
                norm_key = f"{normalize_title(title)}|{year}"
                if title and year is not None and norm_key in movie_by_norm:
                    movie_by_norm[norm_key].videos.append(fi)
                    if entry.path not in movie_by_norm[norm_key].paths:
                        movie_by_norm[norm_key].paths.append(entry.path)
                    continue

                split = SplitFiles(videos=[fi])
                item = SourceItem(
                    key=item_key_for([fi], entry.path),
                    root=root,
                    paths=[entry.path],
                    kind=MediaKind.MOVIE,
                    title=title,
                    year=year,
                    year_source="file",
                    videos=[fi],
                    hint_options={k: str(v) for k, v in options.items()},
                )
                items.append(item)
                if title and year is not None:
                    movie_by_norm[norm_key] = item
            else:
                if not _is_ignored_loose_extra(fi.path, config):
                    issues.append(
                        Issue(
                            item_key=None,
                            path=fi.path,
                            severity=Severity.NOTE,
                            message="not a video; left alone",
                        )
                    )

    items.extend(_merge_show_candidates(show_candidates))

    covered: set[Path] = set()
    for item in items:
        for group in (item.videos, item.subtitles, item.sidecars, item.samples, item.extras):
            covered.update(f.path for f in group)
        covered.update(ep.file.path for ep in item.episodes)

    seen_samples = {fi.path for fi in loose_samples}
    for entries in inventory.values():
        for entry in entries:
            for fi in entry.files:
                if (
                    is_sample_video(fi)
                    and fi.path not in covered
                    and fi.path not in seen_samples
                ):
                    loose_samples.append(fi)
                    seen_samples.add(fi.path)

    for hint in hints:
        if hint.id not in used_hints:
            issues.append(
                Issue(
                    item_key=None,
                    path=Path(hint.pattern),
                    severity=Severity.NOTE,
                    message=f"hint matched nothing: {hint.pattern}",
                )
            )

    ensure_unique_keys(items)
    return items, issues, loose_samples
