from __future__ import annotations

import json
import os
import re
import string
from dataclasses import replace
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Sequence, cast

from pydantic import ValidationError

from mediakeep import workspace as _workspace
from mediakeep.classify import SUB_EXTS, VIDEO_EXTS
from mediakeep.agree import candidate_fits, doubtful_pick, item_name
from mediakeep.config import AppConfig, config_warnings
from mediakeep.destination import (
    folder_mode,
    source_for,
    wants_in_place,
)
from mediakeep.hints import (
    Hint,
    describe_hint,
    hint_library,
    load_hints,
    options_to_text,
    parse_options,
    path_matches_hint,
)
from mediakeep.layout import folder_base, id_tag, item_home_path, target_dir, year_suffix
from mediakeep.models import (
    ActionKind,
    Batch,
    Candidate,
    Identity,
    IdStatus,
    Issue,
    MediaKind,
    Plan,
    Severity,
    SourceItem,
)
from mediakeep.parse import normalize_title
from mediakeep.paths import sanitize_name
from mediakeep.plan import plan_movie_item, plan_show_item
from mediakeep.report import run_summary
from mediakeep.store import Store
from mediakeep.memo import is_dir, memo, remember_is_dir

MASK = "********"
_SECRET_FIELDS = (
    ("tmdb", "api_key"),
    ("tmdb", "read_token"),
    ("web", "access_token"),
)


FIX_ACTIONS = [
    {"value": "match", "label": "match it to a TMDB title…"},
    {"value": "show", "label": "treat it as a TV show"},
    {"value": "movie", "label": "treat it as a movie"},
    {"value": "split", "label": "split it into separate movies"},
    {"value": "ignore", "label": "leave it alone"},
]

_STOP_TOKENS = {
    "the",
    "a",
    "an",
    "and",
    "of",
    "to",
    "in",
    "on",
    "at",
    "for",
    "with",
    "is",
    "or",
    "&",
}

_JUNK_TOKENS = {
    "1080p",
    "720p",
    "480p",
    "2160p",
    "4k",
    "uhd",
    "hd",
    "sd",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "av1",
    "bluray",
    "blu",
    "ray",
    "web",
    "webdl",
    "webrip",
    "web",
    "dvdrip",
    "brrip",
    "hdtv",
    "hdcam",
    "aac",
    "ac3",
    "dts",
    "ddp",
    "truehd",
    "atmos",
    "remux",
    "proper",
    "repack",
    "repack",
    "internal",
    "limited",
    "extended",
    "directors",
    "imax",
    "hdr",
    "dv",
    "xvid",
    "divx",
    "mkv",
    "mp4",
}


def _abs(path: Path | str) -> str:
    s = str(path)
    if len(s) >= 2 and s[1] == ":":
        return os.path.normpath(s)
    return os.path.abspath(s)


def _norm(path: Path | str) -> str:
    return _abs(path).replace("\\", "/").rstrip("/").lower()


def _is_under(child: Path | str, parent: Path | str) -> bool:
    c = _norm(child)
    p = _norm(parent)
    if not c or not p:
        return False
    return c == p or c.startswith(p + "/")


def _empty_plan() -> Plan:
    return Plan(
        id="empty",
        created_at=datetime.now(),
        config_hash="",
        items={},
        identities={},
        batches=[],
        issues=[],
        duplicates=[],
        multi_version=[],
        samples=[],
        unchanged=0,
    )


def display_name(subject: Identity | SourceItem | Candidate, config: AppConfig) -> str:
    """The name mediakeep will write on disk for this identity, item, or candidate."""
    if isinstance(subject, Identity):
        title = subject.title
        year = subject.year
        tag = id_tag(subject, config.naming.id_tag)
    elif isinstance(subject, Candidate):
        title = subject.title
        year = subject.year
        tag = ""
    else:
        title = subject.title
        year = subject.year
        tag = ""

    name = sanitize_name(title + year_suffix(year, config.naming.year_style) + tag)
    if name:
        return name

    if isinstance(subject, SourceItem):
        for p in subject.paths:
            if p.name:
                return sanitize_name(p.name)
        return sanitize_name(subject.root.name)
    return ""


def _batch_target(batch: Batch | None) -> Path | None:
    if batch is None:
        return None
    if batch.reasons and batch.reasons[0].startswith("target: "):
        return Path(batch.reasons[0][len("target: ") :])
    for action in batch.actions:
        if action.kind == ActionKind.MOVE and action.dst is not None:
            parent = action.dst.parent
            if parent.name.lower().startswith("season "):
                return parent.parent
            return parent
    for action in batch.actions:
        if action.dst is not None:
            return action.dst.parent
    return None


def _has_changes(batch: Batch | None) -> bool:
    if batch is None:
        return False
    return any(
        a.kind in (ActionKind.MOVE, ActionKind.RMDIR, ActionKind.FETCH_FILE) for a in batch.actions
    )


def _poster_only(batch: Batch | None) -> bool:
    return bool(
        batch is not None
        and batch.actions
        and all(a.kind == ActionKind.FETCH_FILE for a in batch.actions)
    )


def _file_count(batch: Batch | None) -> int:
    if batch is None:
        return 0
    return sum(1 for a in batch.actions if a.kind in (ActionKind.MOVE, ActionKind.FETCH_FILE))


def _folder_count(batch: Batch | None) -> int:
    if batch is None:
        return 0
    return sum(1 for a in batch.actions if a.kind == ActionKind.RMDIR)


def _current_path(item: SourceItem) -> Path:
    def find() -> Path:
        for p in item.paths:
            if is_dir(p) or p.exists():
                return p
        return item.paths[0] if item.paths else item.root

    return memo(("current-path", item.key, id(item)), find)


def _library_for_path(config: AppConfig, path: Path | str | None) -> str | None:
    if path is None:
        return None
    child = _norm(path)
    for name, lib in config.libraries.items():
        parent = _norm(lib.path)
        if child == parent or child.startswith(parent + "/"):
            return name
    return None


def _file_label(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    if ext in VIDEO_EXTS:
        return "Video"
    if ext in SUB_EXTS:
        return "Subtitle"
    return "Sidecar"


def _kind_word(kind: str) -> str:
    return {
        "movie": "movie",
        "show": "TV show",
        "dvd": "DVD",
        "bluray": "Blu-ray",
        "collection": "collection",
        "unknown": "item",
    }.get(kind, "item")


def _in_place_parent_path(item: SourceItem) -> Path:
    from mediakeep.layout import in_place_parent_path

    return memo(("in-place-parent", item.key, id(item)), lambda: in_place_parent_path(item))


def in_place_parent(item: SourceItem) -> str:
    return _abs(_in_place_parent_path(item))


def in_place_target(config: AppConfig, item: SourceItem, identity: Identity | None = None) -> str:
    parent = _in_place_parent_path(item)
    base = folder_base(item, identity, config)
    if not base:
        base = display_name(identity if identity is not None and identity.title else item, config)
    if not base:
        base = item.paths[0].name if item.paths else item.root.name
    return _abs(parent / base)


def candidate_dict(candidate: Candidate, posters: bool) -> dict[str, Any]:
    return {
        "tmdb_id": candidate.tmdb_id,
        "kind": str(candidate.kind),
        "title": display_name(candidate, _dummy_config_for_candidate(candidate)),
        "tmdb_title": candidate.title,
        "original_title": sanitize_name(candidate.original_title),
        "year": candidate.year,
        "runtime_min": candidate.runtime_min,
        "overview": candidate.overview,
        "poster_url": candidate.poster_url if posters else None,
        "score": candidate.score,
        "reasons": list(candidate.reasons),
        "imdb_id": candidate.imdb_id,
        "original_language": candidate.original_language,
        "genres": list(candidate.genres),
        "countries": list(candidate.countries),
    }


def _dummy_config_for_candidate(candidate: Candidate) -> AppConfig:
    # Candidate display names only need naming style defaults. This avoids passing
    # config through every low-level candidate formatter while keeping the contract.
    return AppConfig()


def _identity_dict(identity: Identity | None, config: AppConfig) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {
        "kind": str(identity.kind),
        "title": display_name(identity, config),
        "year": identity.year,
        "tmdb_id": identity.tmdb_id,
        "imdb_id": identity.imdb_id,
        "status": str(identity.status),
        "confidence": identity.confidence,
        "reasons": list(identity.reasons),
        "library": identity.library,
        "overview": identity.overview,
        "runtime_min": identity.runtime_min,
        "genres": list(identity.genres),
        "countries": list(identity.countries),
        "candidates": [candidate_dict(c, config.web.posters) for c in identity.candidates[:5]],
    }


def _title_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    out: set[str] = set()
    for word in words:
        if word in _STOP_TOKENS:
            continue
        if word.isdigit():
            try:
                year = int(word)
            except ValueError:
                year = -1
            if 1900 <= year <= 2099:
                continue
        if word in _JUNK_TOKENS:
            continue
        out.add(word)
    return out


def _plain_reason(text: str) -> str:
    t = str(text or "")
    replacements = (
        ("needs review", "needs a look"),
        ("needs Review", "needs a look"),
        ("Needs review", "needs a look"),
        ("identity", "match"),
        ("Identity", "Match"),
        ("IDENTITY", "MATCH"),
        ("rename in place", "rename here"),
        ("Rename in place", "Rename here"),
        ("override", "change"),
        ("Override", "Change"),
        ("OVERRIDE", "CHANGE"),
        ("batch", "item"),
        ("Batch", "Item"),
        ("plan", "item"),
        ("Plan", "Item"),
        ("hint", "fix"),
        ("Hint", "Fix"),
    )
    for raw, plain in replacements:
        t = t.replace(raw, plain)
    return " ".join(t.split())


def _append_diff_part(parts: list[dict[str, str]], text: str, op: str) -> None:
    if not text:
        return
    if parts and parts[-1]["op"] == op:
        parts[-1]["text"] = f"{parts[-1]['text']} {text}"
    else:
        parts.append({"text": text, "op": op})


def _name_diff_parts(old: str, new: str) -> list[dict[str, str]]:
    if old == new:
        return [{"text": old, "op": "same"}] if old else []

    old_words = old.split()
    new_words = new.split()
    matcher = SequenceMatcher(None, old_words, new_words)
    parts: list[dict[str, str]] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            _append_diff_part(parts, " ".join(old_words[i1:i2]), "same")
        elif tag == "delete":
            _append_diff_part(parts, " ".join(old_words[i1:i2]), "removed")
        elif tag == "insert":
            _append_diff_part(parts, " ".join(new_words[j1:j2]), "added")
        else:
            _append_diff_part(parts, " ".join(old_words[i1:i2]), "removed")
            _append_diff_part(parts, " ".join(new_words[j1:j2]), "added")

    return parts


def _candidate_label(candidate: Candidate) -> str:
    title = sanitize_name(candidate.title)
    if candidate.year is None:
        return title
    return f"{title} ({candidate.year})"


def _identity_label(identity: Identity | None) -> str:
    if identity is None:
        return ""
    title = sanitize_name(identity.title)
    return f"{title} ({identity.year})" if identity.year is not None else title


def _why_needs(
    config: AppConfig,
    item: SourceItem,
    identity: Identity | None,
    issues: list[Any],
    doubtful: bool = False,
) -> str:
    name_title = item.title or _current_path(item).name
    name_title = sanitize_name(name_title) or "this item"

    if doubtful and identity is not None:
        return (
            f'You confirmed "{_identity_label(identity)}", but the name says "{item_name(item)}". '
            "Keep it if that's right (an alternative title, say), or check the match."
        )

    # A problem with the change itself comes first: it's what actually stops the item.
    for issue in issues:
        if getattr(issue, "severity", None) != Severity.ATTENTION:
            continue
        message = str(getattr(issue, "message", "") or "")
        if message.startswith("target already exists:"):
            where = message.split(":", 1)[1].strip()
            matched = _identity_label(identity)
            return (
                f'Matched as "{matched}", but that name is already taken: {where}. '
                "If it's a different title, check the match and pick the right one."
            )
        if message:
            return message[0].upper() + message[1:] + "."

    if identity is None or not identity.candidates or identity.status == IdStatus.UNMATCHED:
        return f'No TMDB match found for "{name_title}".'

    if (
        identity.status == IdStatus.CONFIRMED
        and item.year is not None
        and identity.year is not None
        and identity.year != item.year
    ):
        return (
            f'Matched as "{_identity_label(identity)}", but the name says {item.year}. '
            "Check the match if that's the wrong one."
        )

    candidates = sorted(identity.candidates, key=lambda c: c.score, reverse=True)
    best = candidates[0]
    best_title = sanitize_name(best.title)

    item_words = _title_words(name_title)
    best_words = _title_words(best.title)
    extra_words = item_words - best_words
    if extra_words:
        return (
            f'"{name_title}" doesn\'t match TMDB\'s best guess "{best_title}". '
            "Pick the right one."
        )

    if len(candidates) >= 2 and candidates[0].score - candidates[1].score <= 10:
        return (
            f'Two close matches: "{_candidate_label(candidates[0])}" and '
            f'"{_candidate_label(candidates[1])}". Pick one.'
        )

    if item.year is None:
        return f'No year in the name, so mediakeep isn\'t sure which "{name_title}" this is.'

    reasons = [_plain_reason(reason) for reason in identity.reasons if str(reason or "").strip()]
    if not reasons:
        reasons = [_plain_reason(reason) for reason in best.reasons if str(reason or "").strip()]
    reasons = [reason for reason in reasons if reason][:2]

    confidence = int(round(identity.confidence))
    if reasons:
        return f"{confidence}% sure – " + " · ".join(reasons)
    return f"{confidence}% sure."


def _quick_match(
    config: AppConfig, item: SourceItem, identity: Identity | None
) -> dict[str, Any] | None:
    """The top TMDB suggestion, offered on the Needs-you row itself when the year in the
    name agrees with it, so a likely match takes one click instead of a trip to the match page."""
    if identity is None or not identity.candidates or item.year is None:
        return None
    top = max(identity.candidates, key=lambda c: c.score)
    if not top.tmdb_id or top.year != item.year:
        return None
    # The year alone is not enough: "Mr. Whiskers & Pip (2014)" is not "Mr. Grumble (2014)".
    if not candidate_fits(item_name(item), top):
        return None
    return {
        "tmdb_id": top.tmdb_id,
        "kind": str(top.kind if top.kind != MediaKind.UNKNOWN else item.kind),
        "label": _candidate_label(top),
        "tmdb_title": top.title,
        "year": top.year,
        "score": int(round(top.score)),
        "poster_url": top.poster_url if config.web.posters else None,
    }


def _best_guess_target(
    config: AppConfig,
    store: Store,
    item: SourceItem,
    identity: Identity | None,
) -> str:
    if identity is None or not identity.candidates:
        return ""

    candidate = max(identity.candidates, key=lambda c: c.score)
    kind = candidate.kind
    if kind == MediaKind.UNKNOWN:
        kind = identity.kind if identity.kind != MediaKind.UNKNOWN else item.kind
    if kind == MediaKind.UNKNOWN:
        kind = MediaKind.MOVIE

    hyp = Identity(
        item_key=item.key,
        kind=kind,
        title=candidate.title,
        year=candidate.year,
        tmdb_id=candidate.tmdb_id,
        imdb_id=candidate.imdb_id,
        status=IdStatus.CONFIRMED,
        confidence=100.0,
        reasons=["best guess"],
        genres=list(candidate.genres),
        countries=list(candidate.countries),
        original_language=candidate.original_language or None,
        overview=candidate.overview,
        runtime_min=candidate.runtime_min,
    )

    library = _chosen_library(config, store, item, hyp)
    if library and library in config.libraries:
        hyp.library = library

    # Plan with the candidate's kind, so a show match goes to the show library.
    as_kind = replace(item, kind=kind)
    base = folder_base(as_kind, hyp, config)
    if not base:
        return ""
    if wants_in_place(config, store, item):
        return _abs(_in_place_parent_path(item) / base)
    target = target_dir(config, hyp, as_kind)
    return _abs(target / base) if target is not None else ""


def _library_target(
    config: AppConfig, store: Store, item: SourceItem, identity: Identity | None
) -> tuple[str | None, str]:
    """(destination name, full folder) the item would be moved into, ignoring rename here."""
    if identity is None or not identity.title:
        return None, ""
    kind = identity.kind if identity.kind != MediaKind.UNKNOWN else item.kind
    as_kind = replace(item, kind=kind) if kind != MediaKind.UNKNOWN else item
    own = identity.library if identity.library in config.libraries else None
    hyp = replace(identity, library=own)
    library = _chosen_library(config, store, item, hyp)
    if library and library in config.libraries:
        hyp = replace(hyp, library=library)
    target = target_dir(config, hyp, as_kind)
    base = folder_base(as_kind, hyp, config)
    if target is None or not base:
        return None, ""
    name = hyp.library
    if not name:
        name = config.routing.show if kind == MediaKind.SHOW else config.routing.movie
    if name in config.libraries:
        # Say where, not the internal name.
        name = Path(config.libraries[name].path).name or str(config.libraries[name].path)
    return name, _abs(target / base)


def _plan_index(plan: Plan) -> tuple[dict[str, Batch], dict[str, list[Issue]]]:
    """Batches and issues by item key (built once per page load)."""

    def build() -> tuple[dict[str, Batch], dict[str, list[Issue]]]:
        batches: dict[str, Batch] = {}
        for b in plan.batches:
            batches.setdefault(b.item_key, b)
        issues: dict[str, list[Issue]] = {}
        for i in plan.issues:
            if i.item_key:
                issues.setdefault(i.item_key, []).append(i)
        return batches, issues

    return memo(("plan-index", id(plan)), build)


def card(
    config: AppConfig, store: Store, plan: Plan | None, key: str
) -> dict[str, Any] | None:
    """One item's view data. Cached for the rest of the page load; callers get a copy."""
    if plan is None:
        return None
    c = memo(("card", id(plan), key), lambda: _card(config, store, plan, key))
    return dict(c) if c is not None else None


def _card(
    config: AppConfig, store: Store, plan: Plan, key: str
) -> dict[str, Any] | None:
    item = plan.items.get(key)
    if item is None:
        return None

    batches_by_key, issues_by_key = _plan_index(plan)
    batch = batches_by_key.get(key)
    identity = plan.identities.get(key)
    issues = list(issues_by_key.get(key, []))
    current = _current_path(item)
    target = _batch_target(batch)
    current_str = _abs(current)
    target_str = _abs(target) if target is not None else None
    in_place_str = in_place_target(config, item, identity)

    file_target: Path | None = None
    if batch is not None:
        for action in batch.actions:
            if action.kind == ActionKind.MOVE and action.dst is not None:
                file_target = action.dst
                break

    new_target_path = in_place_str if wants_in_place(config, store, item) else (target_str or in_place_str)

    # The name that changes: the item's own folder, or the file itself when it is a loose
    # file in a shared folder.
    home = memo(("item-home", item.key, id(item)), lambda: item_home_path(item))
    if home is not None:
        current_name = home.name
        new_name = Path(new_target_path).name if new_target_path else home.name
        new_path = _abs(new_target_path) if new_target_path else _abs(home)
    elif file_target is not None:
        current_name = current.name or item.root.name
        new_name = file_target.name
        new_path = _abs(file_target)
    else:
        current_name = current.name or item.root.name
        new_name = current_name
        new_path = current_str

    same_place = bool(target_str and in_place_str and _norm(target_str) == _norm(in_place_str))
    name_diff = _name_diff_parts(current_name, new_name)

    lib = _library_for_path(config, target if target is not None else current)
    if lib is None and identity is not None and identity.library in config.libraries:
        lib = identity.library

    moves: list[dict[str, Any]] = []
    if batch is not None:
        if target is not None and _norm(target) != _norm(current):
            moves.append({"label": "Folder", "old": current_str, "new": target_str})
        for action in batch.actions:
            if action.kind == ActionKind.MOVE and action.src is not None and action.dst is not None:
                moves.append(
                    {
                        "label": _file_label(action.dst),
                        "old": _abs(action.src),
                        "new": _abs(action.dst),
                    }
                )

    skipped = store.get_decision(key) == "skipped" or store.get_review(key) == "skipped"
    in_place = wants_in_place(config, store, item)
    needs_review = bool(batch is not None and batch.needs_review)
    attention = any(i.severity == Severity.ATTENTION for i in issues)
    has_changes = bool(batch is not None and _has_changes(batch))

    doubtful = doubtful_pick(item, identity)
    ready = bool(
        batch is not None and not needs_review and not skipped and has_changes and not doubtful
    )
    needs = _needs_you(batch, identity, issues, skipped, doubtful)

    reasons: list[str] = []
    if skipped:
        reasons.append("skipped")
    if in_place:
        reasons.append("rename in place")
    if batch is None:
        reasons.append("no changes")
    elif needs_review:
        reasons.append("needs review")
    if attention:
        reasons.append("attention")
    if identity is not None and identity.status in (IdStatus.NEEDS_REVIEW, IdStatus.UNMATCHED):
        reasons.append("identity")

    label_subject = identity if identity is not None and identity.title else item
    label = display_name(label_subject, config)
    if not label and batch is not None:
        label = batch.label
    if not label and item.paths:
        label = item.paths[0].name
    if not label:
        label = item.root.name

    why = _why_needs(config, item, identity, issues, doubtful) if needs else ""
    quick = _quick_match(config, item, identity) if needs else None
    file_library, file_target_str = _library_target(config, store, item, identity)
    src = source_for(config, current)
    folder_mode_str = folder_mode(config, src) if src is not None else ""
    # Both choices lead to the same folder: there is nothing to choose between.
    same_place = bool(
        in_place_str
        and (file_target_str or target_str)
        and _norm(file_target_str or str(target_str)) == _norm(in_place_str)
    )
    best_guess = _best_guess_target(config, store, item, identity) if needs else ""

    return {
        "item_key": item.key,
        "label": label,
        "kind": str(batch.kind if batch is not None else item.kind),
        "library": lib,
        "library_path": _abs(config.libraries[lib].path) if lib and lib in config.libraries else None,
        "current_path": current_str,
        "current_paths": [{"path": _abs(p)} for p in (item.paths or [current])],
        "target": target_str,
        "moves": moves,
        "identity": _identity_dict(identity, config),
        "confidence": identity.confidence if identity is not None else 0.0,
        "issues": [
            {
                "severity": str(i.severity),
                "message": i.message,
                "suggestion": i.suggestion,
                "path": _abs(i.path),
            }
            for i in issues
        ],
        "reasons": reasons,
        "ready": ready,
        "needs": needs,
        "skipped": skipped,
        "in_place": in_place,
        "in_place_target": in_place_str,
        "has_changes": has_changes,
        "file_count": _file_count(batch),
        "folder_count": _folder_count(batch),
        "poster": next(
            (_abs(a.dst) for a in (batch.actions if batch is not None else [])
             if a.kind == ActionKind.FETCH_FILE and a.dst is not None),
            "",
        ),
        "review_state": store.get_review(key) or store.get_decision(key),
        "current_name": current_name,
        "new_name": new_name,
        "new_path": new_path,
        "name_diff": name_diff,
        "same_place": same_place,
        "file_library": file_library,
        "file_target": file_target_str,
        "folder_mode": folder_mode_str,
        "why_needs": why,
        "quick_match": quick,
        "doubtful_pick": doubtful,
        "poster_only": _poster_only(batch),
        "best_guess_target": best_guess,
        # Settle the match first; the title choice only makes sense for a match you trust.
        "title_choice": None if needs else _title_choice(item, identity, config),
    }


def _needs_you(
    batch: Batch | None,
    identity: Identity | None,
    issues: Sequence[Issue],
    skipped: bool,
    doubtful: bool = False,
) -> bool:
    """The one rule for "Needs you" (used by the cards and the sidebar count)."""
    if doubtful and not skipped:
        return True
    needs_review = bool(batch is not None and batch.needs_review)
    attention = any(i.severity == Severity.ATTENTION for i in issues)
    # Nothing planned is only a question for you when mediakeep isn't sure what the
    # item is; a matched item with nothing to do is already tidy.
    identity_ok = identity is not None and identity.status in (IdStatus.AUTO, IdStatus.CONFIRMED)
    return ((batch is None and not identity_ok) or needs_review or attention) and not skipped


def _review_counts(config: AppConfig, store: Store, plan: Plan | None) -> tuple[int, int]:
    """Ready and Needs-you counts for the whole plan, without touching the disk."""
    if plan is None:
        return 0, 0
    batches_by_key, issues_by_key = _plan_index(plan)
    ready = 0
    needs = 0
    for key in plan.items:
        batch = batches_by_key.get(key)
        skipped = store.get_decision(key) == "skipped" or store.get_review(key) == "skipped"
        identity = plan.identities.get(key)
        doubtful = doubtful_pick(plan.items[key], identity)
        if _needs_you(batch, identity, issues_by_key.get(key, []), skipped, doubtful):
            needs += 1
        elif batch is not None and not batch.needs_review and not skipped and _has_changes(batch):
            ready += 1
    return ready, needs


def nav_counts(
    config: AppConfig, store: Store, plan: Plan | None, active: str
) -> dict[str, Any]:
    _ready, needs = _review_counts(config, store, plan)
    return {
        "active": active,
        "needs_count": needs,
        "queue_count": queue_count(config, store, plan),
    }


def _breadcrumb(path: Path, source: Path | None = None) -> list[dict[str, str]]:
    """Crumbs for the folder being browsed. Inside a source they start at the source (its
    full path is the first crumb) and never go above it; elsewhere they start at the drive."""
    path = Path(_abs(path))
    if source is not None:
        root = Path(_abs(source))
        out = [{"name": str(root), "path": str(root)}]
        try:
            rel = path.relative_to(root).parts
        except ValueError:
            rel = ()
        current = root
        for part in rel:
            current = current / part
            out.append({"name": part, "path": _abs(current)})
        return out
    parts = path.parts
    if not parts:
        return []
    current = Path(parts[0])
    out = [{"name": parts[0], "path": _abs(current)}]
    for part in parts[1:]:
        current = current / part
        out.append({"name": part, "path": _abs(current)})
    return out


def working_view(config: AppConfig, store: Store, folder: Path) -> dict[str, Any]:
    """The Folder page header: the working folder and where its items go."""
    ws = config.workspace
    source = source_for(config, folder)
    return {
        "destination_path": _abs(ws.destination) if ws.destination is not None else "",
        "recent": [_abs(p) for p in ws.recent],
        "folder": _abs(folder),
        "source": _abs(source) if source is not None else "",
    }


def _items_by_child(plan: Plan | None, folder: Path) -> dict[str, list[SourceItem]]:
    """Which items each entry of ``folder`` holds, in one pass over the plan.
    Key "*" lists items whose own folder contains ``folder`` (they own every entry)."""
    out: dict[str, list[SourceItem]] = {}
    if plan is None:
        return out
    base = _norm(folder)
    prefix = base.rstrip("/") + "/"
    for item in plan.items.values():
        seen: set[str] = set()
        for p in item.paths:
            n = _norm(p)
            if n.startswith(prefix):
                child = prefix + n[len(prefix):].split("/", 1)[0]
            elif base == n or base.startswith(n.rstrip("/") + "/"):
                child = "*"
            else:
                continue
            if child not in seen:
                seen.add(child)
                out.setdefault(child, []).append(item)
    return out


def _inbox_row_from_cards(
    config: AppConfig, path: Path, cards: list[dict[str, Any]]
) -> dict[str, Any]:
    needs = any(c["needs"] for c in cards)
    ready = all(c["ready"] for c in cards) and bool(cards)
    any_changes = any(bool(c.get("has_changes")) for c in cards)
    in_place_chosen = any(bool(c.get("in_place")) for c in cards)
    # "Already tidy here": renaming in place would not change any name.
    tidy_here = bool(cards) and all(
        bool(c.get("current_path"))
        and bool(c.get("in_place_target"))
        and _norm(str(c["current_path"])) == _norm(str(c["in_place_target"]))
        for c in cards
    )
    if needs:
        status = "needs"
    elif in_place_chosen and not any_changes:
        # Only when nothing at all would change: a folder can be named right while the
        # files inside it (or its poster) still need doing.
        status = "tidy"
    elif ready:
        status = "ready"
    elif cards and not any_changes and all(c.get("identity") for c in cards):
        status = "tidy"
    else:
        status = "left"

    targets = [c["target"] for c in cards if c["target"]]
    target = targets[0] if len(set(targets)) == 1 else None

    in_place_targets = [str(c.get("in_place_target") or "") for c in cards if c.get("in_place_target")]
    in_place = any(bool(c.get("in_place")) for c in cards)
    in_place_target = in_place_targets[0] if in_place_targets else ""
    same_place = bool(cards) and all(bool(c.get("same_place")) for c in cards)

    item_keys = [c["item_key"] for c in cards if c["ready"]]
    file_count = sum(int(c["file_count"]) for c in cards)

    if len(cards) == 1:
        c = cards[0]
        reason = c["reasons"][0] if c["reasons"] else "ready"
    else:
        kind_counts: dict[str, int] = {}
        for c in cards:
            kind_counts[c["kind"]] = kind_counts.get(c["kind"], 0) + 1
        parts = [f"{n} {_kind_word(k)}" for k, n in kind_counts.items()]
        titles = ", ".join(str(c["label"]) for c in cards[:3])
        if len(cards) > 3:
            titles += ", …"
        reason = " · ".join(parts) + (f" · {titles}" if titles else "")

    match_key = next((c["item_key"] for c in cards if c["needs"]), None)
    return {
        "type": "item",
        "name": path.name,
        "path": _abs(path),
        "status": status,
        "reason": reason,
        "target": target,
        "in_place": in_place,
        "in_place_target": in_place_target,
        "tidy_here": tidy_here,
        "same_place": same_place,
        "item_keys": item_keys,
        "file_count": file_count,
        "selectable": ready and status == "ready",
        "match_key": match_key,
        "cards": cards,
    }


def _left_row(paths: list[Path]) -> dict[str, Any]:
    if len(paths) == 1:
        summary = "1 file or folder left alone"
    else:
        summary = f"{len(paths)} files and folders left alone"
    return {
        "type": "left",
        "name": "Left alone",
        "path": "",
        "status": "left",
        "reason": summary,
        "target": None,
        "in_place": False,
        "in_place_target": "",
        "tidy_here": False,
        "same_place": False,
        "item_keys": [],
        "file_count": len(paths),
        "selectable": False,
        "match_key": None,
        "cards": [],
    }


def inbox_rows(
    config: AppConfig, store: Store, plan: Plan | None, folder: Path | str
) -> dict[str, Any]:
    folder_path = Path(folder)
    if folder_path.is_file():
        folder_path = folder_path.parent
    folder_path = Path(_abs(folder_path))

    sources = [
        {
            "name": _abs(p),
            "path": _abs(p),
            "active": _norm(p) == _norm(folder_path),
            "exists": p.exists(),
        }
        for p in config.sources
    ]
    working = working_view(config, store, folder_path)
    breadcrumb = _breadcrumb(folder_path, source_for(config, folder_path))

    rows: list[dict[str, Any]] = []
    left_paths: list[Path] = []

    if folder_path.exists():
        try:
            # scandir knows each entry's type without asking the disk again.
            with os.scandir(folder_path) as it:
                listed = [(Path(e.path), e.is_dir(follow_symlinks=False)) for e in it]
            for child_path, child_is_dir in listed:
                remember_is_dir(child_path, child_is_dir)
            children = [
                c for c, _d in sorted(listed, key=lambda e: (not e[1], e[0].name.lower()))
            ]
        except OSError:
            children = []

        by_child = _items_by_child(plan, folder_path)
        for child in children:
            if child.name.lower() == "jf-hints.txt":
                left_paths.append(child)
                continue

            items = by_child.get(_norm(child), []) + by_child.get("*", [])
            if items:
                cards = []
                for item in items:
                    c = card(config, store, plan, item.key)
                    if c is not None:
                        cards.append(c)
                if cards:
                    rows.append(_inbox_row_from_cards(config, child, cards))
                    continue

            left_paths.append(child)

    if left_paths:
        rows.append(_left_row(left_paths))

    ready_count = sum(1 for r in rows if r["status"] == "ready")
    needs_count = sum(1 for r in rows if r["status"] == "needs")
    tidy_count = sum(1 for r in rows if r["status"] == "tidy")
    left_count = len(left_paths)

    return {
        "folder": _abs(folder_path),
        "exists": folder_path.exists(),
        "sources": sources,
        "working": working,
        "breadcrumb": breadcrumb,
        "rows": rows,
        "left_paths": [_abs(p) for p in left_paths],
        "counts": {
            "all": ready_count + needs_count + tidy_count + left_count,
            "ready": ready_count,
            "needs": needs_count,
            "tidy": tidy_count,
            "left": left_count,
        },
        "plan_id": plan.id if plan is not None else None,
    }


def inbox_row_for_item(
    config: AppConfig, store: Store, plan: Plan | None, item_key: str
) -> dict[str, Any] | None:
    if plan is None:
        return None
    item = plan.items.get(item_key)
    if item is None:
        return None
    c = card(config, store, plan, item_key)
    if c is None:
        return None
    top = item.paths[0] if item.paths else item.root
    return _inbox_row_from_cards(config, top, [c])


def _title_choice(
    item: SourceItem, identity: Identity | None, config: AppConfig | None = None
) -> dict[str, Any] | None:
    if identity is None or not identity.tmdb_id or not identity.candidates:
        return None
    cfg = config or AppConfig()
    name_title = item.title or (_current_path(item).name if item.paths else "")
    if not name_title:
        return None
    candidate = next(
        (c for c in identity.candidates if c.tmdb_id == identity.tmdb_id),
        identity.candidates[0],
    )
    if normalize_title(candidate.title) == normalize_title(name_title):
        return None

    current = "tmdb" if normalize_title(identity.title) == normalize_title(candidate.title) else "keep"
    keep_name = display_name(item, cfg)
    tmdb_name = display_name(candidate, cfg)
    return {
        "item_key": item.key,
        "keep_title": keep_name,
        "keep_name": keep_name,
        "tmdb_title": candidate.title,
        "tmdb_name": tmdb_name,
        "current": current,
    }


def main_title(title: str) -> str:
    for marker in (" or:", ":", " - "):
        if marker in title:
            main = title.split(marker, 1)[0].strip()
            if main:
                return main
    return title


def _short_title(title: str) -> str | None:
    main = main_title(title)
    return main if main != title else None


def _user_library(config: AppConfig, identity: Identity | None) -> str | None:
    if identity is None or identity.library not in config.libraries:
        return None
    if any("library chosen by user" in reason for reason in identity.reasons):
        return identity.library
    return None


def _hint_library_for_item(
    config: AppConfig, item: SourceItem, hints: list[Hint]
) -> str | None:
    for path in item.paths or [item.root]:
        for hint in hints:
            lib = hint_library(hint)
            if lib and lib in config.libraries and path_matches_hint(hint, path):
                return lib
    return None


def _chosen_library(
    config: AppConfig, store: Store, item: SourceItem, identity: Identity | None
) -> str | None:
    hints = memo(("hints", id(store)), lambda: load_hints(store))
    user = _user_library(config, identity)
    hint_lib = _hint_library_for_item(config, item, hints)
    current = _current_path(item)

    if identity is None:
        return hint_lib

    route_identity = replace(identity, library=user)
    try:
        from mediakeep.identify.route import pick_library

        return pick_library(config, route_identity, current, hint_lib)
    except Exception:
        return user or hint_lib


def match_preview(
    config: AppConfig,
    store: Store,
    plan: Plan | None,
    key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if plan is None:
        return {}
    item = plan.items.get(key)
    if item is None:
        return {}

    identity = plan.identities.get(key)

    kind_str = payload.get("kind") or (identity.kind if identity is not None else item.kind)
    try:
        kind = MediaKind(str(kind_str))
    except ValueError:
        kind = item.kind if item.kind != MediaKind.UNKNOWN else MediaKind.MOVIE

    title = str(
        payload.get("title")
        or (identity.title if identity is not None and identity.title else item.title)
        or (item.paths[0].name if item.paths else "Item")
    )

    raw_year = payload.get("year")
    if raw_year in (None, ""):
        year = identity.year if identity is not None else item.year
    else:
        try:
            year = int(raw_year)
        except (TypeError, ValueError):
            year = identity.year if identity is not None else item.year

    raw_tmdb = payload.get("tmdb_id")
    if raw_tmdb in (None, ""):
        tmdb_id = identity.tmdb_id if identity is not None else None
    else:
        try:
            tmdb_id = int(raw_tmdb)
        except (TypeError, ValueError):
            tmdb_id = identity.tmdb_id if identity is not None else None

    if payload.get("use_short_title"):
        title = main_title(title)

    library = payload.get("library")
    if not library or library not in config.libraries:
        library = _chosen_library(config, store, item, identity)

    hyp = Identity(
        item_key=item.key,
        kind=kind,
        title=title,
        year=year,
        tmdb_id=tmdb_id,
        imdb_id=None,
        status=IdStatus.CONFIRMED,
        confidence=100.0,
        reasons=["preview"],
        genres=list(identity.genres) if identity is not None else [],
        countries=list(identity.countries) if identity is not None else [],
        original_language=identity.original_language if identity is not None else None,
        overview=identity.overview if identity is not None else "",
        runtime_min=identity.runtime_min if identity is not None else None,
        library=str(library) if library in config.libraries else None,
    )

    if kind == MediaKind.SHOW:
        batch, issues, _unchanged = plan_show_item(config, item, hyp)
    else:
        batch, issues, _unchanged = plan_movie_item(config, item, hyp)

    folder = _batch_target(batch) if batch is not None else target_dir(config, hyp, item)
    file_target: Path | None = None
    if batch is not None:
        for action in batch.actions:
            if action.kind == ActionKind.MOVE and action.dst is not None:
                file_target = action.dst
                break

    if file_target is None and folder is not None:
        name = item.paths[0].name if item.paths else "video"
        file_target = folder / name

    return {
        "target": _abs(file_target) if file_target is not None else "",
        "folder": _abs(folder) if folder is not None else "",
        "kind": str(kind),
        "title": display_name(hyp, config),
        "year": year,
        "tmdb_id": tmdb_id,
        "library": hyp.library,
        "issues": [i.message for i in issues],
    }


def match_view(
    config: AppConfig, store: Store, plan: Plan | None, key: str
) -> dict[str, Any]:
    if plan is None:
        return {"item_key": key, "error": "scan a folder first"}
    item = plan.items.get(key)
    if item is None:
        return {"item_key": key, "error": "item is not in the current plan"}

    c = card(config, store, plan, key)
    identity = plan.identities.get(key)
    candidates = (
        [candidate_dict(x, config.web.posters) for x in identity.candidates[:5]]
        if identity is not None
        else []
    )
    selected_title = display_name(identity if identity is not None and identity.title else item, config)
    if not selected_title and item.paths:
        selected_title = item.paths[0].name
    selected = {
        "kind": str(identity.kind if identity is not None else item.kind),
        "title": selected_title,
        "year": identity.year if identity is not None else item.year,
        "tmdb_id": identity.tmdb_id if identity is not None else None,
    }
    preview = match_preview(config, store, plan, key, {})

    return {
        "item_key": key,
        "file": _abs(_current_path(item)),
        "card": c,
        "candidates": candidates,
        "selected": selected,
        "preview": preview,
        "short_title": _short_title(str(selected["title"])),
        "remember_default": True,
        "kinds": ["movie", "show"],
    }


def _change_kind(batch: Batch | None) -> str:
    if batch is None:
        return "Nothing to do"
    moves = sum(1 for a in batch.actions if a.kind == ActionKind.MOVE)
    rmdir = sum(1 for a in batch.actions if a.kind == ActionKind.RMDIR)
    if _poster_only(batch):
        return "Save poster"
    if moves > 1:
        kind = f"Move × {moves}"
    elif moves == 1:
        kind = "Move"
    else:
        kind = "Change"
    if rmdir:
        kind += " + Remove empty folder"
    return kind


def _item_row_base(
    config: AppConfig,
    entry: dict[str, Any],
    source: str,
    note: str,
    checked: bool,
    updated: bool,
    c: dict[str, Any] | None,
    batch: Batch | None,
    in_place: bool = False,
) -> dict[str, Any]:
    payload = entry.get("payload") or {}
    current = str(c["current_path"]) if c is not None else str(payload.get("current_path") or "")
    target = str(c["target"]) if c is not None else str(payload.get("target") or "")
    moves = list(c["moves"]) if c is not None else []
    file_count = int(c["file_count"]) if c is not None else int(payload.get("file_count") or 0)
    folder_count = int(c["folder_count"]) if c is not None else 0

    cur_lib = _library_for_path(config, current) if current else None
    tgt_lib = _library_for_path(config, target) if target else None

    return {
        "queue_id": int(entry["id"]),
        "entry_kind": "item",
        "kind": _change_kind(batch),
        "label": str(c["label"]) if c is not None else str(payload.get("label") or entry.get("item_key") or ""),
        "source": source,
        "item_key": entry.get("item_key"),
        "current_path": current,
        "target": target,
        "from_paths": [current] if current else [],
        "to_paths": [target] if target else [],
        "moves": moves,
        "file_count": file_count,
        "folder_count": folder_count,
        "changed": bool(file_count or folder_count),
        "checked": checked,
        "updated": updated,
        "note": note,
        "moved_to_library": cur_lib is None and tgt_lib is not None,
        "renamed_in_place": bool(in_place)
        or source.startswith("in_place:")
        or (cur_lib is not None and tgt_lib == cur_lib),
        "link": f"/match/{entry.get('item_key')}" if entry.get("item_key") else "/inbox",
        "poster": str(c.get("poster") or "") if c is not None else "",
    }


def resolve_item_row(
    config: AppConfig, store: Store, plan: Plan | None, entry: dict[str, Any]
) -> dict[str, Any] | None:
    key = entry.get("item_key")
    if not key:
        return None

    source = str(entry.get("source") or "inbox")
    payload = entry.get("payload") or {}

    if plan is None:
        return _item_row_base(
            config, entry, source, "scan needed", False, False, None, None,
            in_place=store.get_decision(str(key)) == "in_place",
        )

    item = plan.items.get(str(key))
    if item is None:
        return None

    c = card(config, store, plan, str(key))
    batch = next((b for b in plan.batches if b.item_key == str(key)), None)
    has = bool(batch is not None and _has_changes(batch))
    in_place = wants_in_place(config, store, item)

    stored = str(payload.get("target") or "")
    current_target = str(c["target"]) if c is not None and c["target"] else ""

    if c is not None and c.get("doubtful_pick"):
        note = "check the match first (it's on Needs you)"
        checked = False
        updated = False
    elif not has:
        note = "nothing to do any more"
        checked = False
        updated = False
    else:
        note = ""
        checked = True
        updated = bool(stored and current_target and _norm(stored) != _norm(current_target))

    return _item_row_base(config, entry, source, note, checked, updated, c, batch, in_place=in_place)


def _group_label(config: AppConfig, source: str) -> str:
    if source.startswith("in_place:"):
        return f"Rename here · {source.split(':', 1)[1]}"
    if source == "match":
        return "Fixed matches"
    dest = config.workspace.destination
    return f"Move into {_abs(dest)}" if dest is not None else "Rename here"


def _group_path(config: AppConfig, source: str) -> str:
    if source.startswith("in_place:"):
        return source.split(":", 1)[1]
    if config.sources:
        return _abs(config.sources[0])
    return ""


def _group_order(source: str) -> int:
    if source == "inbox":
        return 0
    if source.startswith("in_place:"):
        return 1
    return 2


def prune_queue(config: AppConfig, store: Store, plan: Plan | None) -> list[dict[str, Any]]:
    """Drop Execute entries that can't do anything any more: the item is gone from the
    scan, or it no longer needs a change (already done, or its destination now leaves it
    as it is). Returns what was dropped, with the reason, so the page can say so."""
    if plan is None:
        return []
    dropped: list[dict[str, Any]] = []
    ids: list[int] = []
    for entry in store.queue_list():
        if str(entry.get("kind") or "") != "item":
            continue
        payload = entry.get("payload") or {}
        label = str(payload.get("label") or entry.get("item_key") or "")
        row = resolve_item_row(config, store, plan, entry)
        if row is None:
            reason = "no longer found in your folders"
        elif not row.get("checked"):
            reason = (
                str(row.get("note"))
                if str(row.get("note") or "").startswith("check the match")
                else "nothing to change any more"
            )
        else:
            continue
        ids.append(int(entry["id"]))
        dropped.append({"queue_id": int(entry["id"]), "label": label, "reason": reason})
    if ids:
        store.queue_remove(ids)
    return dropped


def queue_count(config: AppConfig, store: Store, plan: Plan | None) -> int:
    """The Execute badge: entries that will actually do something."""
    prune_queue(config, store, plan)
    return len(store.queue_list())


def resolve_queue(
    config: AppConfig, store: Store, plan: Plan | None
) -> dict[str, Any]:
    dropped: list[dict[str, Any]] = prune_queue(config, store, plan)
    entries = store.queue_list()
    rows: list[dict[str, Any]] = []

    for entry in entries:
        kind = str(entry.get("kind") or "")
        if kind == "item":
            row = resolve_item_row(config, store, plan, entry)
            if row is None:
                payload = entry.get("payload") or {}
                dropped.append(
                    {
                        "queue_id": int(entry["id"]),
                        "label": str(payload.get("label") or entry.get("item_key") or ""),
                        "reason": "gone",
                    }
                )
            else:
                rows.append(row)

    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        source = str(row["source"])
        g = groups.get(source)
        if g is None:
            g = {
                "source": source,
                "label": _group_label(config, source),
                "path": _group_path(config, source),
                "link": "/inbox",
                "rows": [],
                "count": 0,
            }
            groups[source] = g
        g["rows"].append(row)
        g["count"] = int(g["count"]) + 1

    group_list = sorted(groups.values(), key=lambda g: _group_order(str(g["source"])))
    for g in group_list:
        g["keep_all"] = any(bool(r["checked"]) for r in g["rows"])

    tiles = {"moved_files": 0, "renamed_files": 0, "settings": 0, "deleted": 0}
    for row in rows:
        if row["entry_kind"] == "setting":
            tiles["settings"] += 1
        else:
            if row.get("moved_to_library"):
                tiles["moved_files"] += int(row["file_count"])
            elif row.get("renamed_in_place"):
                tiles["renamed_files"] += int(row["file_count"])

    counts = {
        "items": sum(1 for r in rows if r["entry_kind"] == "item"),
        "settings": tiles["settings"],
        "files": sum(int(r["file_count"]) for r in rows if r["entry_kind"] == "item"),
        "folders": sum(int(r["folder_count"]) for r in rows if r["entry_kind"] == "item"),
    }

    return {
        "groups": group_list,
        "rows": rows,
        "counts": counts,
        "tiles": tiles,
        "dropped": dropped,
        "apply_summary": {
            "changes": len(rows),
            "files": counts["files"],
            "folders": counts["folders"],
            "settings": counts["settings"],
            "deleted": 0,
            "copy_bytes": 0,
        },
        "plan_id": plan.id if plan is not None else None,
    }


def fixes_view(config: AppConfig, store: Store) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for row in store.list_hints():
        raw_options = str(row["options"] or "{}")
        try:
            options = cast(dict[str, Any], json.loads(raw_options))
        except Exception:
            options = parse_options(raw_options)

        hint = Hint(
            id=int(row["id"]),
            pattern=str(row["pattern"]),
            name=str(row["name"] or ""),
            options={str(k): str(v) for k, v in options.items()},
            origin=str(row["origin"] or "ui"),
        )
        action, detail = describe_hint(hint)
        display = sanitize_name(hint.name) if hint.name else ""
        detail_display = (
            detail.replace(hint.name, display)
            if hint.name and display and display != hint.name
            else detail
        )
        rows.append(
            {
                "id": hint.id,
                "pattern": hint.pattern,
                "name": display,
                "display_name": display,
                "options": hint.options,
                "origin": hint.origin,
                "origin_label": "You saved this" if hint.origin == "ui" else "From jf-hints.txt",
                "created_at": str(row["created_at"] or ""),
                "action": action,
                "detail": detail_display,
                "options_text": options_to_text(hint.options),
            }
        )

    return {"rows": rows, "actions": FIX_ACTIONS}


def naming_example(config: AppConfig) -> dict[str, Any]:
    """Where an example movie and episode would go with the current naming settings."""
    lib = next(iter(config.libraries.values()), None)
    root = _abs(lib.path) if lib is not None else "<your folder>"
    sep = "\\" if os.name == "nt" or "\\" in root else "/"
    year = " (2020)" if config.naming.year_style == "parens" else " [2020]"
    tag = {"tmdb": " [tmdbid-12345]", "imdb": " [imdbid-tt0012345]"}.get(config.naming.id_tag, "")
    movie = f"Example Movie{year}{tag}"
    show = f"Example Show{year}{tag}"
    episode = f"Example Show{year} S01E01" + (" - Pilot" if config.naming.keep_episode_titles else "")
    return {
        "movie": sep.join([root, movie, movie + ".mp4"]),
        "episode": sep.join([root, show, "Season 01", episode + ".mkv"]),
        "year_style": config.naming.year_style,
        "id_tag": config.naming.id_tag,
        "keep_episode_titles": config.naming.keep_episode_titles,
        "lores_tagging": config.naming.lores_tagging,
    }


def masked_config(config: AppConfig) -> dict[str, Any]:
    data = config.model_dump(mode="json")
    for section, field in _SECRET_FIELDS:
        sec = data.get(section)
        if isinstance(sec, dict) and sec.get(field):
            sec[field] = MASK
    return data


def settings_view(config: AppConfig, store: Store) -> dict[str, Any]:
    ws = config.workspace
    working = _workspace.effective(config)
    return {
        "config": masked_config(config),
        "warnings": config_warnings(working),
        # The example uses where items really go: the working folder or its destination.
        "naming_example": naming_example(working),
        "secret_mask": MASK,
        "workspace": {
            "folder": _abs(ws.folder) if ws.folder is not None else "",
            "destination": _abs(ws.destination) if ws.destination is not None else "",
            "recent": [
                {
                    "path": _abs(p),
                    "exists": p.exists(),
                    "current": ws.folder is not None and _norm(p) == _norm(ws.folder),
                }
                for p in ws.recent
            ],
        },
    }


def _sanitize_run_summary(summary: Any, plan: Plan | None, config: AppConfig) -> Any:
    if plan is None:
        return summary

    identities = plan.identities

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            key = obj.get("item_key")
            if isinstance(key, str) and key in identities:
                disp = display_name(identities[key], config)
                if disp:
                    for name in ("label", "title", "name"):
                        if isinstance(obj.get(name), str):
                            obj[name] = disp
            else:
                for identity in identities.values():
                    raw = identity.title
                    if not raw:
                        continue
                    disp = display_name(identity, config)
                    if raw == disp:
                        continue
                    for name in ("label", "title", "name"):
                        if isinstance(obj.get(name), str) and obj[name] == raw:
                            obj[name] = disp
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(summary)
    return summary


def history_view(config: AppConfig, store: Store, limit: int = 50) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for run in store.list_runs(limit):
        plan = store.get_plan(run.plan_id) if run.plan_id else None
        if plan is None:
            plan = _empty_plan()
        summary = run_summary(run, plan, config, store)
        summary = _sanitize_run_summary(summary, plan, config)
        runs.append(
            {
                "id": run.id,
                "kind": run.kind,
                "status": run.status,
                "done": run.done,
                "failed": run.failed,
                "skipped": run.skipped,
                "started_at": run.started_at.isoformat(),
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "plan_id": run.plan_id,
                "summary": summary,
                "undo_script": undo_script_preview(config, run.id),
            }
        )

    return {"runs": runs, "total": store.count_runs()}


UNDO_PREVIEW_LINES = 100


def undo_script_preview(
    config: AppConfig, run_id: str, limit: int = UNDO_PREVIEW_LINES
) -> dict[str, Any]:
    """The first ``limit`` lines of a run's undo script, for the History page."""
    path = config.state_dir_path() / "undo" / f"undo-{run_id}.ps1"
    out: dict[str, Any] = {"exists": False, "path": _abs(path), "lines": [], "total": 0}
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            lines: list[str] = []
            total = 0
            for line in fh:
                total += 1
                if total <= limit:
                    lines.append(line.rstrip("\r\n"))
    except OSError:
        return out
    out.update({"exists": True, "lines": lines, "total": total})
    return out


# The settings the Settings page can change, each set straight away.
SETTINGS: dict[str, type] = {
    "behavior.remember_choices": bool,
    "naming.year_style": str,
    "naming.id_tag": str,
    "naming.keep_episode_titles": bool,
    "naming.lores_tagging": bool,
    "naming.folder_jpg": bool,
    "tmdb.api_key": str,
    "web.access_token": str,
}


def _validation_message(exc: ValidationError) -> str:
    errors = exc.errors()
    if errors:
        err = errors[0]
        loc = ".".join(str(x) for x in err.get("loc", []))
        msg = str(err.get("msg", "invalid value"))
        text = f"{loc}: {msg}" if loc else msg
        return " ".join(text.split())
    return " ".join(str(exc).split())


def apply_setting(config: AppConfig, path: str, value: Any) -> AppConfig:
    """A copy of the settings with one value changed. Raises ValueError with a message
    for the page when the setting or the value isn't allowed."""
    kind = SETTINGS.get(path)
    if kind is None:
        raise ValueError(f"{path} can't be changed here")
    if kind is bool and not isinstance(value, bool):
        raise ValueError(f"{path} must be on or off")
    if kind is str:
        if not isinstance(value, str):
            raise ValueError(f"{path} must be text")
        if path.endswith(("api_key", "access_token")) and value == MASK:
            return config  # the masked placeholder came back unchanged
    data = config.model_dump(mode="json")
    section, key = path.split(".", 1)
    data.setdefault(section, {})[key] = value
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(_validation_message(exc)) from exc


def folders_view(config: AppConfig, path: str | None) -> dict[str, Any]:
    def add_root(roots: list[dict[str, Any]], seen: set[str], p: Path, name: str | None = None) -> None:
        try:
            if not p.exists():
                return
        except OSError:
            return
        key = _norm(p)
        if key in seen:
            return
        seen.add(key)
        has_children = False
        try:
            has_children = any(child.is_dir() for child in p.iterdir())
        except OSError:
            pass
        roots.append({"name": name or p.name or str(p), "path": _abs(p), "has_children": has_children})

    if path is None:
        roots: list[dict[str, Any]] = []
        seen: set[str] = set()
        if os.name == "nt":
            for letter in string.ascii_uppercase:
                add_root(roots, seen, Path(f"{letter}:\\"), f"{letter}:\\")
        else:
            add_root(roots, seen, Path.home(), "Home")
            add_root(roots, seen, Path("/"), "/")
        for p in config.root_paths:
            parent = p.parent if p.parent != p else p
            add_root(roots, seen, parent)
            if parent != p and p.exists():
                add_root(roots, seen, p)
        return {"path": None, "parent": None, "folders": [], "roots": roots}

    p = Path(path).expanduser()
    if not p.is_absolute():
        p = p.absolute()
    if not p.exists():
        raise FileNotFoundError(str(p))
    if not p.is_dir():
        raise NotADirectoryError(str(p))

    folders: list[dict[str, Any]] = []
    try:
        children = sorted(p.iterdir(), key=lambda x: x.name.lower())
    except OSError:
        children = []

    for child in children:
        if not child.is_dir():
            continue
        has_children = False
        try:
            has_children = any(sub.is_dir() for sub in child.iterdir())
        except OSError:
            pass
        folders.append({"name": child.name, "path": _abs(child), "has_children": has_children})

    up: Path | None = p.parent if p.parent != p else None
    return {
        "path": _abs(p),
        "parent": _abs(up) if up is not None else None,
        "folders": folders,
        "roots": [],
    }
