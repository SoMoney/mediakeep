from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Sequence

from mediakeep.config import AppConfig
from mediakeep.models import (
    Action,
    ActionKind,
    Batch,
    EpisodeRef,
    FileInfo,
    Identity,
    Issue,
    MediaKind,
    PlanContributor,
    Severity,
    SourceItem,
)
from mediakeep.parse import parse_part_number, parse_release_name
from mediakeep.paths import long_path, sanitize_name, strip_long
from mediakeep.layout import (
    display_title_year,
    episode_file_base,
    episode_file_name,
    folder_base,
    movie_file_name,
    target_dir,
)
from mediakeep.store import Store

SUB_DIRS = {"sub", "subs", "subtitle", "subtitles"}

_SUB_FLAGS = ("forced", "sdh", "cc", "hi")

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


def _res_rank(res: str | None) -> int:
    return {
        "2160p": 5,
        "1080p": 4,
        "720p": 3,
        "576p": 2,
        "480p": 1,
    }.get(res or "", -1)


def _video_resolution(item: SourceItem, path: Path) -> str | None:
    if path in item.video_heights:
        h = item.video_heights[path]
        if h >= 2000:
            return "2160p"
        if h >= 1000:
            return "1080p"
        if h >= 700:
            return "720p"
        return "480p"
    return parse_release_name(path.name).resolution


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


def _subtitle_language_suffix(stem: str) -> str:
    parts = [p for p in re.split(r"[ ._-]+", stem.lower()) if p]
    if not parts:
        return ""

    flags_rev: list[str] = []
    lang: str | None = None

    for part in reversed(parts):
        if part in _SUB_FLAGS:
            if part not in flags_rev:
                flags_rev.append(part)
            continue

        code = _language_code(part)
        if code is not None:
            if lang is not None:
                break
            lang = code
            continue

        break

    if lang is None:
        return ""

    out = f".{lang}"
    for flag in reversed(flags_rev):
        out += f".{flag}"
    return out


def _existing_tag(src: Path, base: Path) -> str:
    if str(src.parent).lower() != str(base).lower():
        return ""
    stem = src.stem
    prefix = base.name + " - "
    if stem.lower().startswith(prefix.lower()):
        return stem[len(prefix) :]
    return ""


def _base_target_folder(target_dir: Path, base: str) -> Path:
    return target_dir / sanitize_name(base)


def validate_batch(
    actions: list[Action], claimed_targets: set[str]
) -> tuple[bool, str | None]:
    vacated = {
        str(a.src).lower()
        for a in actions
        if a.kind in (ActionKind.MOVE, ActionKind.RMDIR) and a.src is not None
    }
    local: dict[str, Path] = {}
    for action in actions:
        created: Path | None = None
        if action.kind in (ActionKind.MOVE, ActionKind.WRITE_FILE, ActionKind.SYMLINK):
            created = action.dst
        if created is None:
            continue
        low = str(created).lower()
        if low in local:
            return False, "two sources would go to the same target"
        if created.exists():
            if low not in vacated:
                # Case-only rename is allowed when the source is the same path.
                if not (action.src is not None and str(action.src).lower() == low):
                    return False, f"target already exists: {created}"
        if low in claimed_targets:
            return False, f"target claimed by another batch: {created}"
        local[low] = created
    for low in local:
        claimed_targets.add(low)
    return True, None


def _add_hidden_actions(item: SourceItem) -> list[Action]:
    out: list[Action] = []
    if not item.hint_options.get("_no_hidden"):
        for fi in item.videos + item.subtitles + item.sidecars:
            if fi.hidden:
                out.append(
                    Action(
                        kind=ActionKind.CLEAR_HIDDEN,
                        src=fi.path,
                        dst=fi.path,
                    )
                )
    return out


def _norm_path(path: Path | str) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


def _is_same_or_under(child: Path | str, parent: Path | str) -> bool:
    c = _norm_path(child)
    p = _norm_path(parent)
    if not c or not p:
        return False
    return c == p or c.startswith(p + "/")


def _is_target_related(path: Path, target: Path) -> bool:
    """True when path is the target, inside it, or an ancestor of it."""
    return _is_same_or_under(path, target) or _is_same_or_under(target, path)


def _scan_tree(root: Path) -> tuple[list[Path], list[Path]]:
    """Return all directories and files beneath root, without following links."""
    dirs: list[Path] = []
    files: list[Path] = []
    stack: list[Path] = [root]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(long_path(current)) as it:
                for entry in it:
                    try:
                        path = strip_long(entry.path)
                        if entry.is_dir(follow_symlinks=False):
                            dirs.append(path)
                            stack.append(path)
                        elif entry.is_file(follow_symlinks=False):
                            files.append(path)
                    except OSError:
                        continue
        except OSError:
            continue

    return dirs, files


def _plan_rmdir_actions(
    source_dirs: Sequence[Path],
    target_folder: Path,
    moved: set[Path],
) -> list[Action]:
    """Plan RMDIR actions for every source directory emptied by the moves.

    Empty means no file remains after the planned moves: samples, extras,
    unknown files, and anything not moved keep their folder alive. Actions are
    ordered deepest first so apply can remove children before parents.
    """
    moved_norm = {_norm_path(p) for p in moved}
    candidates: dict[str, Path] = {}
    seen_roots: set[str] = set()

    for root in source_dirs:
        root_norm = _norm_path(root)
        if not root_norm or root_norm in seen_roots:
            continue
        seen_roots.add(root_norm)

        try:
            if not root.is_dir():
                continue
        except OSError:
            continue

        # Never remove the target folder or anything inside it.
        if _is_same_or_under(root, target_folder):
            continue

        dirs, files = _scan_tree(root)
        remaining = [
            _norm_path(f) for f in files if _norm_path(f) not in moved_norm
        ]

        for d in [root] + dirs:
            d_norm = _norm_path(d)
            if not d_norm or d_norm in candidates:
                continue
            if _is_target_related(d, target_folder):
                continue
            if any(
                f_norm == d_norm or f_norm.startswith(d_norm + "/")
                for f_norm in remaining
            ):
                continue
            candidates[d_norm] = d

    ordered = sorted(
        candidates.values(),
        key=lambda p: (len(p.parts), _norm_path(p)),
        reverse=True,
    )
    return [Action(kind=ActionKind.RMDIR, src=p, dst=None) for p in ordered]


def _plan_subtitles(
    item: SourceItem,
    video_targets: dict[Path, Path],
    target_folder: Path,
    issues: list[Issue],
) -> tuple[list[Action], set[Path]]:
    actions: list[Action] = []
    moved: set[Path] = set()
    claimed: set[str] = set()
    single = len(video_targets) == 1

    for sub in item.subtitles:
        matched: Path | None = None
        if single:
            matched = next(iter(video_targets))
        else:
            for v_src in video_targets:
                if sub.path.stem.lower().startswith(v_src.stem.lower() + "."):
                    matched = v_src
                    break
                if sub.path.stem.lower() == v_src.stem.lower():
                    matched = v_src
                    break

        if matched is None:
            issues.append(
                Issue(
                    item_key=item.key,
                    path=sub.path,
                    severity=Severity.NOTE,
                    message="subtitle does not match any video; left alone",
                )
            )
            continue

        v_target = video_targets[matched]
        matched_stem = matched.stem
        sub_stem = sub.path.stem

        if sub_stem.lower().startswith(matched_stem.lower() + ".") or sub_stem.lower() == matched_stem.lower():
            rest = sub_stem[len(matched_stem) :]
            suffix = rest if rest.startswith(".") else (f".{rest}" if rest else "")
        else:
            suffix = _subtitle_language_suffix(sub_stem)
            if not suffix and not single:
                issues.append(
                    Issue(
                        item_key=item.key,
                        path=sub.path,
                        severity=Severity.NOTE,
                        message="subtitle language unknown; left alone",
                    )
                )
                continue
            suffix = suffix or ""

        target_name = sanitize_name(v_target.stem + suffix) + sub.path.suffix.lower()
        target = target_folder / target_name

        if sub.path == target:
            continue

        low = str(target).lower()
        same_case_insensitive = low == str(sub.path).lower()
        if low in claimed or (target.exists() and not same_case_insensitive):
            issues.append(
                Issue(
                    item_key=item.key,
                    path=sub.path,
                    severity=Severity.NOTE,
                    message="subtitle name already taken; left alone",
                )
            )
            continue

        claimed.add(low)
        moved.add(sub.path)
        if sub.path != target:
            actions.append(Action(kind=ActionKind.MOVE, src=sub.path, dst=target))
    return actions, moved


def _plan_movie_versions(
    config: AppConfig,
    item: SourceItem,
    base: str,
    target_folder: Path,
) -> tuple[dict[Path, Path], list[Issue]]:
    issues: list[Issue] = []
    videos = item.videos
    if not videos:
        return {}, issues

    parts = [parse_part_number(v.path.name) for v in videos]
    if len(videos) > 1 and all(p is not None for p in parts) and len(set(parts)) == len(parts):
        out: dict[Path, Path] = {}
        for v, part in zip(videos, parts):
            target = target_folder / movie_file_name(base, f"part{part}", v.path.suffix.lower())
            out[v.path] = target
        return out, issues

    class Meta:
        def __init__(self, v: FileInfo) -> None:
            self.v = v
            self.res = _video_resolution(item, v.path)
            self.tag = _existing_tag(v.path, target_folder)

    metas = [Meta(v) for v in videos]
    explicit_plain = [m for m in metas if m.tag == ""]
    explicit_tag = [m for m in metas if m.tag != ""]

    if len(explicit_plain) == 1 and explicit_tag:
        keeper = explicit_plain[0]
    else:
        keeper = max(metas, key=lambda m: (_res_rank(m.res), m.v.size))

    assigned: list[str] = []
    cannot_tell = False
    for m in metas:
        if m is keeper:
            assigned.append("")
            continue
        if m.tag and m.tag.lower().startswith("lores"):
            assigned.append(m.tag)
            continue
        if config.naming.lores_tagging:
            if len(metas) == 2:
                suffix = "LoRes"
            else:
                label = m.tag or m.res or ""
                suffix = f"LoRes {label}".strip()
        else:
            suffix = m.tag or m.res or ""
        if not suffix:
            cannot_tell = True
        assigned.append(suffix)

    if cannot_tell:
        issues.append(
            Issue(
                item_key=item.key,
                path=videos[0].path,
                severity=Severity.ATTENTION,
                message="copies can't be told apart",
            )
        )
        return {}, issues

    out = {}
    used: set[str] = set()
    for m, suffix in zip(metas, assigned):
        n = 1
        while True:
            if suffix and n > 1:
                suffix_n = f"{suffix} {n}"
            elif not suffix and n > 1:
                suffix_n = f"LoRes {n}"
            else:
                suffix_n = suffix
            target = target_folder / movie_file_name(base, suffix_n, m.v.path.suffix.lower())
            low = str(target).lower()
            if low not in used:
                used.add(low)
                out[m.v.path] = target
                break
            n += 1
    return out, issues


def plan_movie_item(
    config: AppConfig,
    item: SourceItem,
    identity: Identity | None,
) -> tuple[Batch | None, list[Issue], int]:
    issues: list[Issue] = []
    if item.kind in (MediaKind.UNKNOWN, MediaKind.COLLECTION):
        return None, issues, 0
    tdir = target_dir(config, identity, item)
    if tdir is None:
        issues.append(
            Issue(
                item_key=item.key,
                path=item.paths[0] if item.paths else item.root,
                severity=Severity.ATTENTION,
                message="no destination library",
            )
        )
        return None, issues, 0

    base = folder_base(item, identity, config)
    if not base:
        issues.append(
            Issue(
                item_key=item.key,
                path=item.paths[0] if item.paths else item.root,
                severity=Severity.ATTENTION,
                message="no title for destination folder",
            )
        )
        return None, issues, 0

    target_folder = _base_target_folder(tdir, base)
    reasons = [f"target: {target_folder}"]
    actions: list[Action] = []
    source_video_dirs: set[Path] = set()

    if item.kind == MediaKind.MOVIE:
        video_targets, v_issues = _plan_movie_versions(config, item, base, target_folder)
        issues.extend(v_issues)
        if v_issues:
            return None, issues, 0
        for src, dst in video_targets.items():
            if src != dst:
                actions.append(Action(kind=ActionKind.MOVE, src=src, dst=dst))

        sub_actions, _moved_subs = _plan_subtitles(item, video_targets, target_folder, issues)
        actions.extend(sub_actions)

        for side in item.sidecars:
            for v_src, v_target in video_targets.items():
                if side.path.name.startswith(v_src.name + "."):
                    rest = side.path.name[len(v_src.name) :]
                    target = v_target.with_name(v_target.name + rest)
                    if side.path != target:
                        actions.append(Action(kind=ActionKind.MOVE, src=side.path, dst=target))
                    break

        # Samples are never planned: they stay where they are and keep their
        # folder alive (see _plan_rmdir_actions), and the report lists them.

    elif item.kind == MediaKind.DVD:
        video_dir = target_folder / "VIDEO_TS"
        for v in item.videos:
            target = video_dir / v.path.name.upper()
            if v.path != target:
                actions.append(Action(kind=ActionKind.MOVE, src=v.path, dst=target))

        source_video_dirs = {
            v.path.parent
            for v in item.videos
            if v.path.parent.name.lower() == "video_ts"
            and str(v.path.parent).lower() != str(video_dir).lower()
        }

    elif item.kind == MediaKind.BLURAY:
        src = item.paths[0] if item.paths else target_folder
        if src != target_folder:
            actions.append(Action(kind=ActionKind.MOVE, src=src, dst=target_folder))

    actions.extend(_add_hidden_actions(item))

    if not actions:
        return None, issues, 1

    source_dirs: list[Path] = [p for p in item.paths if p.is_dir()]
    if item.kind == MediaKind.MOVIE:
        source_dirs.extend(
            s.path.parent
            for s in item.subtitles
            if s.path.parent.name.lower() in SUB_DIRS
        )
        container = item.hint_options.get("container_rmdir")
        if container:
            source_dirs.append(Path(container))
    elif item.kind == MediaKind.DVD:
        source_dirs.extend(source_video_dirs)

    if item.kind != MediaKind.BLURAY:
        moved = {
            a.src
            for a in actions
            if a.kind == ActionKind.MOVE and a.src is not None
        }
        actions.extend(_plan_rmdir_actions(source_dirs, target_folder, moved))

    preconditions = item.videos + item.subtitles + item.sidecars
    batch = Batch(
        item_key=item.key,
        label=base,
        kind=item.kind,
        actions=actions,
        reasons=reasons,
        preconditions=preconditions,
        needs_review=bool(
            identity is None
            or identity.status.value in {"needs_review", "unmatched"}
        ),
    )
    return batch, issues, 0


def plan_show_item(
    config: AppConfig,
    item: SourceItem,
    identity: Identity | None,
) -> tuple[Batch | None, list[Issue], int]:
    issues: list[Issue] = []
    tdir = target_dir(config, identity, item)
    if tdir is None:
        issues.append(
            Issue(
                item_key=item.key,
                path=item.paths[0] if item.paths else item.root,
                severity=Severity.ATTENTION,
                message="no destination library",
            )
        )
        return None, issues, 0

    base = folder_base(item, identity, config)
    episode_base = episode_file_base(item, identity, config)
    if not base or not episode_base:
        issues.append(
            Issue(
                item_key=item.key,
                path=item.paths[0] if item.paths else item.root,
                severity=Severity.ATTENTION,
                message="no title for destination folder",
            )
        )
        return None, issues, 0

    target_folder = tdir / base
    reasons = [f"target: {target_folder}"]
    actions: list[Action] = []

    groups: dict[tuple[int, int], list[EpisodeRef]] = {}
    for ep in item.episodes:
        groups.setdefault((ep.season, ep.episode), []).append(ep)

    for (season, episode), eps in groups.items():
        ep_titles: dict[tuple[int, int], str] = (
            identity.episode_titles if identity is not None else {}
        )

        if len(eps) == 1:
            ep = eps[0]
            title = ep_titles.get((season, episode), ep.title)
            target = (
                target_folder
                / f"Season {season:02d}"
                / episode_file_name(
                    episode_base,
                    season,
                    episode,
                    ep.episode_end,
                    title,
                    "",
                    ep.file.path.suffix.lower(),
                    config.naming.keep_episode_titles,
                )
            )
            if ep.file.path != target:
                actions.append(Action(kind=ActionKind.MOVE, src=ep.file.path, dst=target))
            continue

        labels: list[str] = []
        for ep in eps:
            label = ep.label
            if not label:
                lang = item.audio_languages.get(ep.file.path)
                if lang:
                    label = lang.capitalize()
            labels.append(label)

        use_labels = all(labels) and len(set(labels)) == len(labels)
        keeper = max(
            eps,
            key=lambda e: (e.version_tag or 0, e.file.size),
        )
        for ep, label in zip(eps, labels):
            if ep is keeper:
                vlabel = ""
            elif use_labels:
                vlabel = label
            else:
                if config.naming.lores_tagging and len(eps) == 2:
                    vlabel = "LoRes"
                else:
                    vlabel = f"LoRes {label}".strip()
                    if not vlabel:
                        vlabel = "LoRes"
            title = ep_titles.get((season, episode), ep.title)
            target = (
                target_folder
                / f"Season {season:02d}"
                / episode_file_name(
                    episode_base,
                    season,
                    episode,
                    ep.episode_end,
                    title,
                    vlabel,
                    ep.file.path.suffix.lower(),
                    config.naming.keep_episode_titles,
                )
            )
            if ep.file.path != target:
                actions.append(Action(kind=ActionKind.MOVE, src=ep.file.path, dst=target))

    actions.extend(_add_hidden_actions(item))

    if not actions:
        return None, issues, 1

    source_dirs = [p for p in item.paths if p.is_dir()]
    moved = {
        a.src
        for a in actions
        if a.kind == ActionKind.MOVE and a.src is not None
    }
    actions.extend(_plan_rmdir_actions(source_dirs, target_folder, moved))

    batch = Batch(
        item_key=item.key,
        label=base,
        kind=MediaKind.SHOW,
        actions=actions,
        reasons=reasons,
        preconditions=item.videos + item.subtitles + item.sidecars,
        needs_review=bool(
            identity is None
            or identity.status.value in {"needs_review", "unmatched"}
        ),
    )
    return batch, issues, 0


def build_batches(
    config: AppConfig,
    store: Store,
    items: list[SourceItem],
    identities: dict[str, Identity],
    contributors: list[PlanContributor],
) -> tuple[list[Batch], list[Issue], list[list[str]], list[str], list[Path], int]:
    batches: list[Batch] = []
    issues: list[Issue] = []
    claimed: set[str] = set()
    duplicates: list[list[str]] = []
    multi_version: list[str] = []
    samples: list[Path] = []
    seen_samples: set[Path] = set()
    unchanged = 0

    norm_groups: dict[str, list[str]] = {}

    for item in items:
        identity = identities.get(item.key)
        for sample in item.samples:
            if sample.path not in seen_samples:
                seen_samples.add(sample.path)
                samples.append(sample.path)

        if item.kind == MediaKind.UNKNOWN:
            continue

        if item.kind == MediaKind.SHOW:
            batch, iss, unchanged_add = plan_show_item(config, item, identity)
        else:
            batch, iss, unchanged_add = plan_movie_item(config, item, identity)
        issues.extend(iss)
        unchanged += unchanged_add

        if batch is None:
            continue

        tdir = target_dir(config, identity, item)
        if tdir is not None:
            base = folder_base(item, identity, config)
            if base:
                target_folder = tdir / sanitize_name(base)
                for contrib in contributors:
                    try:
                        extra = contrib.contribute(item, identity, target_folder, base)
                        batch.actions.extend(extra)
                    except Exception as exc:
                        issues.append(
                            Issue(
                                item_key=item.key,
                                path=item.paths[0] if item.paths else item.root,
                                severity=Severity.NOTE,
                                message=f"contributor failed: {exc}",
                            )
                        )

        ok, err = validate_batch(batch.actions, claimed)
        if not ok:
            issues.append(
                Issue(
                    item_key=item.key,
                    path=item.paths[0] if item.paths else item.root,
                    severity=Severity.ATTENTION,
                    message=err or "invalid batch",
                )
            )
            continue

        batches.append(batch)

        if item.kind == MediaKind.SHOW:
            ep_groups: dict[tuple[int, int], list[EpisodeRef]] = {}
            for ep in item.episodes:
                ep_groups.setdefault((ep.season, ep.episode), []).append(ep)
            show_name, _ = display_title_year(item, identity)
            if not show_name:
                show_name = batch.label
            for (season, episode), eps in ep_groups.items():
                if len(eps) > 1:
                    multi_version.append(
                        f"{show_name} S{season:02d}E{episode:02d} ({len(eps)} versions)"
                    )
        elif item.kind == MediaKind.MOVIE and len(item.videos) > 1:
            parts = [parse_part_number(v.path.name) for v in item.videos]
            if not (all(p is not None for p in parts) and len(set(parts)) == len(parts)):
                multi_version.append(batch.label)

        key = f"{normalize_title(item.title)}|{item.year}"
        norm_groups.setdefault(key, []).append(batch.label)

    for group in norm_groups.values():
        if len(group) > 1:
            duplicates.append(group)

    return batches, issues, duplicates, multi_version, samples, unchanged


def normalize_title(text: str) -> str:
    from mediakeep.parse import normalize_title as _n

    return _n(text)
