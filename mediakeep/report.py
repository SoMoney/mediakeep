from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from mediakeep.config import AppConfig
from mediakeep.models import (
    ActionKind,
    Batch,
    Candidate,
    Identity,
    IdStatus,
    MediaKind,
    Plan,
    RunRecord,
    Severity,
    SourceItem,
)
from mediakeep.plan import plan_movie_item, plan_show_item
from mediakeep.store import Store


# The text report must survive Windows console code pages and Tee-Object, so it
# is plain ASCII: arrows are "->", dashes are "-", ellipses are "...".
_ASCII_REPLACEMENTS: dict[str, str] = {
    "→": "->",
    "←": "<-",
    "↔": "<->",
    "–": "-",
    "—": "-",
    "−": "-",
    "…": "...",
    "‘": "'",
    "’": "'",
    "‚": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    " ": " ",
    "•": "-",
}

_SEASON_RE = re.compile(r"Season (\d{2})", re.I)


def _ascii(text: str) -> str:
    for src, dst in _ASCII_REPLACEMENTS.items():
        if src in text:
            text = text.replace(src, dst)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.encode("ascii", "replace").decode("ascii")


def _abs_path(path: Path | str) -> str:
    return os.path.abspath(str(path))


def _norm_path(path: Path | str) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


def _roots(config: AppConfig | None, plan: Plan) -> list[Path]:
    roots: list[Path] = []

    def add(path: Path) -> None:
        if path not in roots:
            roots.append(path)

    if config is not None:
        for path in config.root_paths:
            add(path)
    for item in plan.items.values():
        add(item.root)

    return sorted(roots, key=lambda p: len(str(p)), reverse=True)


def _display(path: Path, roots: list[Path]) -> str:
    for root in roots:
        try:
            rel = path.relative_to(root)
            return str(Path(root.name) / rel)
        except ValueError:
            pass

    s = str(path).replace("\\", "/").lower()
    for root in roots:
        r = str(root).replace("\\", "/").rstrip("/").lower()
        if s.startswith(r + "/"):
            rel_text = str(path)[len(str(root)) + 1 :]
            return str(Path(root.name) / rel_text)
    return str(path)


def _library_for(
    batch: Batch, identity: Identity | None, config: AppConfig | None
) -> str:
    if identity is not None and identity.library:
        return identity.library
    if config is not None:
        if batch.kind in (MediaKind.MOVIE, MediaKind.DVD, MediaKind.BLURAY):
            return config.routing.movie
        if batch.kind == MediaKind.SHOW:
            return config.routing.show
    return "?"


def _library_name(
    batch: Batch, identity: Identity | None, config: AppConfig | None
) -> str:
    if (
        identity is not None
        and identity.library
        and config is not None
        and identity.library in config.libraries
    ):
        return identity.library
    if config is not None:
        if batch.kind in (MediaKind.MOVIE, MediaKind.DVD, MediaKind.BLURAY):
            return config.routing.movie
        if batch.kind == MediaKind.SHOW:
            return config.routing.show
    return ""


def _library_path(name: str, config: AppConfig | None) -> Path | None:
    if config is None or name not in config.libraries:
        return None
    return config.libraries[name].path


def _batch_label(batch: Batch, identity: Identity | None) -> str:
    label = batch.label
    if (
        identity is not None
        and identity.year is None
        and "(" not in label
        and "[" not in label
    ):
        return f"{label} (?)"
    return label


def _batch_header(
    batch: Batch,
    identity: Identity | None,
    config: AppConfig | None,
) -> str:
    label = _batch_label(batch, identity)
    lib = _library_for(batch, identity, config)
    if identity is not None:
        status = f"TMDB {identity.confidence:.0f}%"
    else:
        status = "not identified"

    dest = ""
    if config is not None and lib in config.libraries:
        dest = _abs_path(config.libraries[lib].path)

    if dest:
        return f"== {label:<56} {batch.kind.value:<5} -> {lib:<8} {status:<15} {dest}"
    return f"== {label:<56} {batch.kind.value:<5} -> {lib:<8} {status}"


def _candidate_line(index: int, candidate: Candidate) -> str:
    year = f" ({candidate.year})" if candidate.year is not None else ""
    reasons = ", ".join(candidate.reasons) if candidate.reasons else "no reasons"
    return (
        f"   {index}. {candidate.title}{year}  "
        f"score {candidate.score:.0f}  {reasons}"
    )


def _review_target(batch: Batch, roots: list[Path]) -> str:
    for action in batch.actions:
        if action.kind == ActionKind.MOVE and action.dst is not None:
            return _display(action.dst.parent, roots)
    if batch.reasons and batch.reasons[0].startswith("target: "):
        return _display(Path(batch.reasons[0][len("target: ") :]), roots)
    return ""


def _move_count(batch: Batch) -> int:
    return sum(1 for a in batch.actions if a.kind == ActionKind.MOVE)


def _batch_moves(batch: Batch) -> list[tuple[Path, Path]]:
    out: list[tuple[Path, Path]] = []
    for action in batch.actions:
        if action.kind == ActionKind.MOVE and action.src is not None and action.dst is not None:
            out.append((action.src, action.dst))
    return out


def _batch_target_folder(batch: Batch) -> Path | None:
    if batch.reasons and batch.reasons[0].startswith("target: "):
        return Path(batch.reasons[0][len("target: ") :])
    for action in batch.actions:
        if action.kind == ActionKind.MOVE and action.dst is not None:
            parent = action.dst.parent
            if _SEASON_RE.fullmatch(parent.name):
                return parent.parent
            return parent
    for action in batch.actions:
        if action.dst is not None:
            return action.dst.parent
    return None


def _confirm_preview(
    config: AppConfig,
    item: SourceItem,
    candidate: Candidate,
    identity: Identity | None,
) -> list[tuple[Path, Path]]:
    """Renames the item would get if this candidate were confirmed.

    Built with the planner's own layout code by planning the item against a
    hypothetical CONFIRMED identity, so the preview can never drift from what
    confirming really does.
    """
    hypothetical = Identity(
        item_key=item.key,
        kind=candidate.kind,
        title=candidate.title,
        year=candidate.year,
        tmdb_id=candidate.tmdb_id,
        imdb_id=candidate.imdb_id,
        status=IdStatus.CONFIRMED,
        confidence=100.0,
        reasons=[],
        candidates=[candidate],
        episode_titles=dict(identity.episode_titles) if identity else {},
        library=identity.library if identity else None,
    )

    if candidate.kind == MediaKind.SHOW:
        batch, _issues, _unchanged = plan_show_item(config, item, hypothetical)
    else:
        batch, _issues, _unchanged = plan_movie_item(config, item, hypothetical)

    if batch is None:
        return []
    return _batch_moves(batch)


def report_plan(
    plan: Plan,
    console: Console | None = None,
    config: AppConfig | None = None,
    dry_run: bool = False,
) -> None:
    console = console or Console(width=4096, markup=False)
    roots = _roots(config, plan)

    def emit(line: str = "") -> None:
        console.print(_ascii(line), markup=False)

    if dry_run:
        emit("DRY RUN - nothing is changed. Apply with: mediakeep scan --apply")
    else:
        emit("APPLYING - changing files now")

    ready = [b for b in plan.batches if not b.needs_review]
    review = [b for b in plan.batches if b.needs_review]

    for batch in ready:
        identity = plan.identities.get(batch.item_key)
        emit(_batch_header(batch, identity, config))
        for action in batch.actions:
            if action.kind == ActionKind.MOVE and action.src and action.dst:
                emit(f"   move  {_display(action.src, roots)}")
                emit(f"      -> {_display(action.dst, roots)}")
            elif action.kind == ActionKind.MKDIR and action.src:
                emit(f"   mkdir {_display(action.src, roots)}")
            elif action.kind == ActionKind.RMDIR and action.src:
                emit(f"   rmdir {_display(action.src, roots)}")
            elif action.kind == ActionKind.WRITE_FILE and action.dst:
                emit(f"   write {_display(action.dst, roots)}")
            elif action.kind == ActionKind.SYMLINK and action.dst:
                emit(f"   link  {_display(action.dst, roots)}")
            elif action.kind == ActionKind.FETCH_FILE and action.dst:
                emit(f"   poster {_display(action.dst, roots)}")

    if review:
        emit("")
        emit("Waiting for your review (not applied by scan --apply)")
        for batch in review:
            identity = plan.identities.get(batch.item_key)
            emit(_batch_header(batch, identity, config))
            candidates = list(identity.candidates) if identity is not None else []
            for idx, candidate in enumerate(candidates[:3], start=1):
                emit(_candidate_line(idx, candidate))

            item = plan.items.get(batch.item_key)
            preview: list[tuple[Path, Path]] = []
            if config is not None and item is not None and candidates:
                preview = _confirm_preview(config, item, candidates[0], identity)

            if preview:
                emit("   if you confirm 1:")
                limit = 2 if batch.kind == MediaKind.SHOW else 3
                for src, dst in preview[:limit]:
                    emit(f"   move  {_display(src, roots)}")
                    emit(f"      -> {_display(dst, roots)}")
                rest = len(preview) - limit
                if rest > 0:
                    if batch.kind == MediaKind.SHOW:
                        emit(f"   ... and {rest} more episodes")
                    else:
                        emit(f"   ... and {rest} more")
                continue

            moves = _batch_moves(batch)
            for src, dst in moves[:3]:
                emit(f"   {_display(src, roots)} -> {_display(dst, roots)}")
            if len(moves) > 3:
                emit(f"   ... and {len(moves) - 3} more")
            if not moves:
                target = _review_target(batch, roots)
                if target:
                    emit(f"   target: {target}")

    attention = [i for i in plan.issues if i.severity == Severity.ATTENTION]
    if attention:
        emit("")
        emit("Left alone, needs you:")
        for issue in attention:
            suffix = f"  {issue.suggestion}" if issue.suggestion else ""
            emit(f"- {_display(issue.path, roots)}: {issue.message}{suffix}")

    notes = [i for i in plan.issues if i.severity == Severity.NOTE]
    if notes:
        emit("")
        emit("Notes:")
        for issue in notes:
            emit(f"- {_display(issue.path, roots)}: {issue.message}")

    if plan.duplicates:
        emit("")
        emit("Possible duplicates:")
        for group in plan.duplicates:
            emit("- " + ", ".join(group))

    if plan.multi_version:
        emit("")
        emit("Multi-version items:")
        for label in plan.multi_version:
            emit(f"- {label}")

    if plan.samples:
        emit("")
        emit("Samples (left alone):")
        for path in plan.samples[:50]:
            emit(f"- {_display(path, roots)}")
        if len(plan.samples) > 50:
            emit(f"- ... {len(plan.samples) - 50} more")

    if not ready and not review:
        emit("")
        emit("Nothing to do.")

    ready_files = sum(_move_count(b) for b in ready)
    verb = "would change" if dry_run else "changing"
    emit(
        f"{verb} {len(ready)} items ({ready_files} files)   "
        f"waiting for review {len(review)}   "
        f"need attention {len(attention)}   "
        f"already fine {plan.unchanged}"
    )


def report_json(plan: Plan) -> str:
    from mediakeep.models import plan_to_json

    return plan_to_json(plan)


def _run_seconds(run: RunRecord) -> float:
    end = run.finished_at or datetime.now()
    return max(0.0, (end - run.started_at).total_seconds())


def _failed_reason(message: str) -> str:
    rest = message[len("batch failed: ") :]
    _label, sep, reason = rest.partition(": ")
    return reason or rest


def _run_infos(
    run: RunRecord,
    plan: Plan,
    config: AppConfig | None,
    store: Store | None = None,
) -> tuple[list[dict[str, Any]], int, float]:
    seconds = _run_seconds(run)

    failed_errors: dict[str, str] = {}
    skipped_messages: dict[str, str] = {}
    for issue in run.issues:
        if issue.item_key is None:
            continue
        msg = issue.message
        if issue.severity == Severity.ATTENTION and msg.startswith("batch failed: "):
            failed_errors[issue.item_key] = _failed_reason(msg)
        elif issue.severity == Severity.ATTENTION and "changed since" in msg:
            skipped_messages[issue.item_key] = msg
        elif issue.severity == Severity.NOTE and "needs review" in msg:
            skipped_messages[issue.item_key] = msg

    done_src: set[str] = set()
    done_dst: set[str] = set()
    rmdir_count = 0
    if store is not None:
        for e in store.get_journal(run.id):
            if e.get("status") != "done":
                continue
            kind = str(e.get("kind") or "")
            src = e.get("src")
            dst = e.get("dst")
            if src:
                done_src.add(_norm_path(str(src)))
            if dst:
                done_dst.add(_norm_path(str(dst)))
            if kind == "rmdir":
                rmdir_count += 1

    infos: list[dict[str, Any]] = []
    for batch in plan.batches:
        identity = plan.identities.get(batch.item_key)
        lib = _library_name(batch, identity, config)
        lib_path_obj = _library_path(lib, config)
        lib_path = _abs_path(lib_path_obj) if lib_path_obj is not None else ""
        target = _batch_target_folder(batch)

        done_moves = 0
        done_any = False
        seasons: dict[str, int] = {}

        if store is not None:
            for action in batch.actions:
                src_norm = _norm_path(action.src) if action.src is not None else None
                dst_norm = _norm_path(action.dst) if action.dst is not None else None
                if (src_norm and src_norm in done_src) or (dst_norm and dst_norm in done_dst):
                    done_any = True
                if (
                    action.kind == ActionKind.MOVE
                    and action.src is not None
                    and action.dst is not None
                    and src_norm in done_src
                ):
                    done_moves += 1
                    m = _SEASON_RE.fullmatch(action.dst.parent.name)
                    if m:
                        season = m.group(1)
                        seasons[season] = seasons.get(season, 0) + 1

            if batch.item_key in failed_errors:
                status = "failed"
            elif batch.item_key in skipped_messages:
                status = "skipped"
            elif done_any:
                status = "ok"
            else:
                status = "skipped"
        else:
            if batch.item_key in failed_errors:
                status = "failed"
            elif batch.item_key in skipped_messages:
                status = "skipped"
            else:
                status = "ok"
                done_moves = _move_count(batch)
                done_any = True
                for action in batch.actions:
                    if action.kind == ActionKind.MOVE and action.dst is not None:
                        m = _SEASON_RE.fullmatch(action.dst.parent.name)
                        if m:
                            season = m.group(1)
                            seasons[season] = seasons.get(season, 0) + 1

        error: str | None = None
        if status == "failed":
            error = failed_errors.get(batch.item_key)
        elif status == "skipped":
            error = skipped_messages.get(batch.item_key)

        infos.append(
            {
                "item_key": batch.item_key,
                "label": batch.label,
                "kind": batch.kind,
                "library": lib,
                "library_path": lib_path,
                "target": _abs_path(target) if target is not None else None,
                "files": done_moves,
                "status": status,
                "error": error,
                "seasons": seasons,
                "done_any": done_any,
            }
        )

    return infos, rmdir_count, seconds


def _capitalized(name: str) -> str:
    if name.lower() == "tv":
        return "TV"
    return name[:1].upper() + name[1:]


def _kept_rmdir_entries(store: Store | None, run_id: str) -> list[tuple[str, str]]:
    if store is None:
        return []
    out: list[tuple[str, str]] = []
    for e in store.get_journal(run_id):
        if str(e.get("kind") or "") != "rmdir":
            continue
        if str(e.get("status") or "") != "skipped":
            continue
        error = str(e.get("error") or "")
        if not error.startswith("kept: not empty"):
            continue
        src = str(e.get("src") or "")
        if src:
            out.append((src, error))
    return out


def _emit_kept_folders(
    kept: list[tuple[str, str]],
    roots: list[Path],
    emit: Any,
) -> None:
    if not kept:
        return
    emit("Kept folders that weren't empty:")
    prefix = "kept: not empty "
    for src, error in kept:
        suffix = error[len(prefix) :] if error.startswith(prefix) else error
        emit(f"   {_display(Path(src), roots)}   {suffix}")


def report_run(
    run: RunRecord,
    plan: Plan,
    config: AppConfig,
    console: Console | None = None,
    store: Store | None = None,
) -> None:
    console = console or Console(width=4096, markup=False)

    def emit(line: str = "") -> None:
        console.print(_ascii(line), markup=False)

    infos, rmdir_count, seconds = _run_infos(run, plan, config, store)
    ok_infos = [i for i in infos if i["status"] == "ok"]
    failed_infos = [i for i in infos if i["status"] == "failed"]
    total_files = run.done
    roots = _roots(config, plan)
    kept = _kept_rmdir_entries(store, run.id)

    if total_files == 0 and rmdir_count == 0:
        emit("DONE - nothing was changed.")
        if kept:
            emit("")
            _emit_kept_folders(kept, roots, emit)
        if failed_infos:
            emit("")
            emit("Failed:")
            for info in failed_infos:
                emit(f"   {info['label']}   {info['error'] or 'failed'}")
        return

    emit(
        f"DONE - run {run.id}: {len(ok_infos)} items, {total_files} files moved, "
        f"{run.failed} failed, {run.skipped} skipped ({seconds:.1f} s)"
    )

    if ok_infos:
        emit("")
        for name, lib in config.libraries.items():
            group = [i for i in ok_infos if i["library"] == name]
            if not group:
                continue
            movie_count = sum(1 for i in group if i["kind"] != MediaKind.SHOW)
            show_count = sum(1 for i in group if i["kind"] == MediaKind.SHOW)
            episode_count = sum(int(i["files"]) for i in group if i["kind"] == MediaKind.SHOW)
            parts: list[str] = []
            if movie_count:
                parts.append(f"{movie_count} movies")
            if show_count:
                parts.append(f"{show_count} shows, {episode_count} episodes")
            emit(f"{_capitalized(name)} -> {_abs_path(lib.path)}   {', '.join(parts)}")
            for info in group:
                if info["kind"] == MediaKind.SHOW and info["seasons"]:
                    season_text = ", ".join(
                        f"Season {season}: {count}"
                        for season, count in sorted(info["seasons"].items())
                    )
                    emit(f"   {str(info['label']):<60} {season_text} episodes")
                elif info["kind"] == MediaKind.SHOW:
                    emit(f"   {str(info['label']):<60} {info['files']} episodes")
                else:
                    emit(f"   {info['label']}")

    left_count = sum(
        1
        for issue in plan.issues
        if issue.severity == Severity.NOTE
        and ("left alone" in issue.message or "not a video" in issue.message)
    )
    emit("")
    emit(
        f"Left where they were: {left_count} files that aren't videos "
        "(listed under Notes above)"
    )
    emit(f"Removed empty folders: {rmdir_count}")

    if kept:
        emit("")
        _emit_kept_folders(kept, roots, emit)

    if failed_infos:
        emit("")
        emit("Failed:")
        for info in failed_infos:
            emit(f"   {info['label']}   {info['error'] or 'failed'}")

    if total_files > 0 or rmdir_count > 0:
        script = config.state_dir_path() / "undo" / f"undo-{run.id}.ps1"
        emit("")
        emit("Undo everything from this run:")
        emit(f"   mediakeep undo {run.id} --apply")
        if script.exists():
            emit(f"   or run: {_abs_path(script)}")


def run_summary(
    run: RunRecord,
    plan: Plan,
    config: AppConfig,
    store: Store | None = None,
) -> dict[str, Any]:
    infos, rmdir_count, seconds = _run_infos(run, plan, config, store)
    script = config.state_dir_path() / "undo" / f"undo-{run.id}.ps1"
    kept = _kept_rmdir_entries(store, run.id)
    kept_folders: list[dict[str, str]] = []
    prefix = "kept: not empty "
    for src, error in kept:
        reason = error[len(prefix) :] if error.startswith(prefix) else error
        kept_folders.append({"path": _abs_path(Path(src)), "reason": reason})

    failures = [
        {"label": info["label"], "error": info["error"] or "failed"}
        for info in infos
        if info["status"] == "failed"
    ]

    libraries: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    for info in infos:
        kind = str(info["kind"])
        lib = str(info["library"] or "?")
        lib_path = str(info["library_path"] or "")
        item = {
            "item_key": info["item_key"],
            "label": info["label"],
            "kind": kind,
            "library": lib,
            "library_path": lib_path,
            "target": info["target"],
            "files": int(info["files"]),
            "status": info["status"],
            "error": info["error"],
            "seasons": info["seasons"],
        }
        items.append(item)
        entry = libraries.setdefault(
            lib, {"path": lib_path, "items": [], "movies": 0, "shows": 0, "episodes": 0}
        )
        entry["items"].append(item)
        if info["status"] == "ok":
            if kind == "show":
                entry["shows"] += 1
                entry["episodes"] += int(info["files"])
            else:
                entry["movies"] += 1

    return {
        "id": run.id,
        "plan_id": run.plan_id,
        "kind": run.kind,
        "status": run.status,
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "done": run.done,
        "failed": run.failed,
        "skipped": run.skipped,
        "seconds": round(seconds, 1),
        "undo_script": str(script) if script.exists() else None,
        "undo_command": f"mediakeep undo {run.id} --apply",
        "removed_folders": rmdir_count,
        "kept_folders": kept_folders,
        "failures": failures,
        "libraries": libraries,
        "items": items,
    }


def run_summary_json(
    run: RunRecord,
    plan: Plan,
    config: AppConfig,
    store: Store | None = None,
) -> dict[str, Any]:
    return run_summary(run, plan, config, store)
