from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Sequence

from mediakeep.config import AppConfig
from mediakeep.models import (
    Action,
    ActionKind,
    Batch,
    Identity,
    IdStatus,
    Issue,
    Plan,
    ProgressSink,
    RunRecord,
    Severity,
    identity_to_dict,
)
from mediakeep.paths import long_path, same_volume
from mediakeep.store import Store


class ApplyLock:
    def __init__(self, state_dir: Path) -> None:
        self.path = state_dir / "apply.lock"
        self.fd: int | None = None

    def __enter__(self) -> "ApplyLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.unlink(self.path)
        except OSError:
            pass


def _retry(fn: Callable[[], None], attempts: int = 8) -> None:
    delay = 0.25
    last: Exception | None = None
    for _ in range(attempts):
        try:
            fn()
            return
        except (PermissionError, OSError) as exc:
            last = exc
            time.sleep(delay)
            delay = min(delay * 2, 2.0)
    if last is not None:
        raise last


def _exists(path: Path) -> bool:
    return os.path.lexists(long_path(path))


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(long_path(a), long_path(b))
    except OSError:
        return False


def _fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(long_path(path), "rb") as f:
            h.update(str(os.path.getsize(long_path(path))).encode())
            head = f.read(1024 * 1024)
            h.update(head)
            if os.path.getsize(long_path(path)) > 1024 * 1024:
                f.seek(max(0, os.path.getsize(long_path(path)) - 1024 * 1024))
                h.update(f.read(1024 * 1024))
    except OSError:
        h.update(str(path).encode())
    return h.hexdigest()


def _copy_file(src: Path, dst: Path) -> None:
    if _exists(dst) and not _same_file(src, dst):
        raise OSError(f"target already exists: {dst}")

    tmp = dst.parent / f"~mk-copy-{uuid.uuid4().hex}.tmp"
    with open(long_path(src), "rb") as s, open(long_path(tmp), "wb") as d:
        shutil.copyfileobj(s, d)
        d.flush()
        os.fsync(d.fileno())
    if os.path.getsize(long_path(tmp)) != os.path.getsize(long_path(src)):
        os.unlink(long_path(tmp))
        raise OSError("size mismatch after copy")
    if _fingerprint(tmp) != _fingerprint(src):
        os.unlink(long_path(tmp))
        raise OSError("fingerprint mismatch after copy")
    os.replace(long_path(tmp), long_path(dst))


def _copy_tree(src: Path, dst: Path) -> None:
    if src.is_dir():
        os.makedirs(long_path(dst), exist_ok=True)
        with os.scandir(long_path(src)) as it:
            for entry in it:
                p = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    _copy_tree(p, dst / entry.name)
                else:
                    _copy_file(p, dst / entry.name)
    else:
        _copy_file(src, dst)


def _remove_tree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(long_path(path))
    else:
        os.unlink(long_path(path))


def _move(src: Path, dst: Path, run_id: str, seq: int, cross_volume: str = "move") -> None:
    src = Path(src)
    dst = Path(dst)
    if src == dst:
        return

    if not _exists(src):
        raise FileNotFoundError(f"source missing: {src}")

    # Never overwrite an existing user file. A case-only rename is allowed
    # because Windows reports the source and destination as the same file.
    if _exists(dst) and not _same_file(src, dst):
        raise OSError(f"target already exists: {dst}")

    dst.parent.mkdir(parents=True, exist_ok=True)

    if same_volume(src, dst):
        tmp = dst.parent / f"~mk-{run_id}-{seq}.tmp"
        _retry(lambda: os.replace(long_path(src), long_path(tmp)))
        try:
            _retry(lambda: os.replace(long_path(tmp), long_path(dst)))
        except Exception:
            try:
                os.replace(long_path(tmp), long_path(src))
            except OSError:
                pass
            raise
        return

    tmp = dst.parent / f"~mk-{run_id}-{seq}.tmp"
    if src.is_dir():
        _copy_tree(src, tmp)
    else:
        _copy_file(src, tmp)
    os.replace(long_path(tmp), long_path(dst))
    if cross_volume == "move":
        _remove_tree(src)


def _count_entries(path: Path) -> int:
    try:
        with os.scandir(long_path(path)) as it:
            return sum(1 for _ in it)
    except OSError:
        return 0


def _rmdir_result(path: Path) -> tuple[bool, int]:
    """Remove an empty directory.

    Returns ``(removed, entry_count)``. ``entry_count`` is ``-1`` when the
    folder does not exist, otherwise the number of entries still inside it.
    A non-empty directory is never removed and never raises.
    """
    p = long_path(path)
    if not os.path.exists(p):
        return False, -1
    try:
        os.rmdir(p)
        return True, 0
    except OSError:
        return False, _count_entries(path)


def _rmdir(path: Path) -> bool:
    return _rmdir_result(path)[0]


def _write_file(dst: Path, content: str, store: Store, run_id: str) -> None:
    if _exists(dst) and not store.is_owned(dst):
        raise OSError(f"refusing to overwrite unowned file: {dst}")
    tmp = dst.parent / f"~mk-write-{uuid.uuid4().hex}.tmp"
    tmp.write_text(content, encoding="utf-8")
    with open(long_path(tmp), "rb") as f:
        os.fsync(f.fileno())
    os.replace(long_path(tmp), long_path(dst))
    store.add_owned(dst, run_id, "write_file", hashlib.sha256(content.encode()).hexdigest())


POSTER_MAX_BYTES = 15 * 1024 * 1024


def _download(url: str, timeout: float = 30.0) -> bytes:
    import urllib.request

    if not url.lower().startswith("https://"):
        raise ValueError("only https downloads are allowed")
    req = urllib.request.Request(url, headers={"User-Agent": "mediakeep"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https only
        data = resp.read(POSTER_MAX_BYTES + 1)
    if len(data) > POSTER_MAX_BYTES:
        raise ValueError("image is too large")
    return bytes(data)


def _fetch_file(dst: Path, url: str, store: Store, run_id: str) -> str | None:
    """Download a poster to ``dst``. Never fails the change it belongs to: problems are
    reported as a skip reason and the moves still go ahead."""
    if _exists(dst):
        return f"kept: {dst.name} already there"
    try:
        data = _download(url)
    except Exception as exc:  # network, HTTP or size problem
        return f"skipped: poster not downloaded ({_one_line(exc)})"
    if not data.startswith(b"\xff\xd8"):
        return "skipped: the poster wasn't a JPEG image"
    try:
        tmp = dst.parent / f"~mk-fetch-{uuid.uuid4().hex}.tmp"
        with open(long_path(tmp), "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(long_path(tmp), long_path(dst))
    except OSError as exc:
        return f"skipped: poster not saved ({_one_line(exc)})"
    store.add_owned(dst, run_id, "fetch_file", hashlib.sha256(data).hexdigest())
    return None


def _one_line(exc: Exception) -> str:
    return " ".join(str(exc).split()) or type(exc).__name__


def _symlink(src: Path, dst: Path, store: Store, run_id: str) -> None:
    try:
        os.symlink(str(src), long_path(dst))
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            raise RuntimeError(
                "symlink privilege missing; enable Windows Developer Mode"
            ) from exc
        raise
    store.add_owned(dst, run_id, "symlink", None)


def _do_action(
    action: Action,
    store: Store,
    run_id: str,
    seq: int,
    config: AppConfig,
) -> str | None:
    """Execute one action.

    Returns ``None`` on success, or a skip reason for actions that are not
    failures but did not change the filesystem (currently only RMDIR).
    """
    if action.kind == ActionKind.MKDIR:
        if action.src is not None:
            os.makedirs(long_path(action.src), exist_ok=True)
        return None

    if action.kind == ActionKind.MOVE:
        if action.src is None or action.dst is None:
            raise ValueError("move needs src and dst")
        _move(action.src, action.dst, run_id, seq, config.behavior.cross_volume)
        return None

    if action.kind == ActionKind.RMDIR:
        if action.src is None:
            return None
        removed, count = _rmdir_result(action.src)
        if removed:
            return None
        if count < 0:
            return "kept: missing"
        word = "item" if count == 1 else "items"
        return f"kept: not empty ({count} {word})"

    if action.kind == ActionKind.CLEAR_HIDDEN:
        from mediakeep.paths import clear_hidden

        if action.src is not None:
            clear_hidden(action.src)
        return None

    if action.kind == ActionKind.WRITE_FILE:
        if action.dst is None or action.content is None:
            raise ValueError("write_file needs dst and content")
        _write_file(action.dst, action.content, store, run_id)
        return None

    if action.kind == ActionKind.SYMLINK:
        if action.src is None or action.dst is None:
            raise ValueError("symlink needs src and dst")
        _symlink(action.src, action.dst, store, run_id)
        return None

    if action.kind == ActionKind.FETCH_FILE:
        if action.dst is None or not action.content:
            return "skipped: no poster to fetch"
        return _fetch_file(action.dst, action.content, store, run_id)

    return None


def _undo_action(action: Action, store: Store, run_id: str, seq: int) -> bool:
    try:
        if action.kind == ActionKind.MOVE and action.src and action.dst:
            if _exists(action.dst) and not _exists(action.src):
                _move(action.dst, action.src, run_id, seq)
        elif action.kind == ActionKind.MKDIR and action.src:
            _rmdir(action.src)
        elif action.kind == ActionKind.RMDIR and action.src:
            os.makedirs(long_path(action.src), exist_ok=True)
        elif (
            action.kind in (ActionKind.WRITE_FILE, ActionKind.SYMLINK, ActionKind.FETCH_FILE)
            and action.dst
        ):
            if _exists(action.dst):
                if action.dst.is_dir():
                    _rmdir(action.dst)
                else:
                    os.unlink(long_path(action.dst))
        elif action.kind == ActionKind.CLEAR_HIDDEN:
            pass
        return True
    except Exception:
        return False


def _content_sha(action: Action) -> str | None:
    if action.kind == ActionKind.WRITE_FILE and action.content is not None:
        return hashlib.sha256(action.content.encode("utf-8")).hexdigest()
    return None


def _batch_path(batch: Batch) -> Path:
    if batch.preconditions:
        return batch.preconditions[0].path
    for action in batch.actions:
        if action.src is not None:
            return action.src
        if action.dst is not None:
            return action.dst
    return Path(".")


def _new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def _move_count(batch: Batch) -> int:
    return sum(1 for a in batch.actions if a.kind == ActionKind.MOVE)


def _progress_line(status: str, label: str, detail: str) -> str:
    return f"  {status:<7} {label}   {detail}"


def _file_word(files: int) -> str:
    return "file" if files == 1 else "files"


def _target_folder(batch: Batch) -> Path | None:
    if batch.reasons and batch.reasons[0].startswith("target: "):
        return Path(batch.reasons[0][len("target: ") :])

    for action in batch.actions:
        if action.kind == ActionKind.MOVE and action.dst is not None:
            parent = action.dst.parent
            if re.fullmatch(r"Season \d{2}", parent.name, re.I):
                return parent.parent
            return parent

    for action in batch.actions:
        if action.dst is not None:
            return action.dst.parent
    return None


def _norm_path(path: Path | str) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


def _is_under(child: Path, parent: Path) -> bool:
    c = _norm_path(child)
    p = _norm_path(parent)
    if not c or not p:
        return False
    return c == p or c.startswith(p + "/")


def _under_library(config: AppConfig, path: Path) -> bool:
    return any(_is_under(path, lib.path) for lib in config.libraries.values())


def _record_path_identity(
    config: AppConfig,
    store: Store,
    plan: Plan,
    batch: Batch,
) -> None:
    identity: Identity | None = plan.identities.get(batch.item_key)
    if identity is None:
        return
    if identity.status not in (IdStatus.AUTO, IdStatus.CONFIRMED):
        return
    if identity.tmdb_id is None and identity.imdb_id is None:
        return
    if not identity.title:
        return

    target = _target_folder(batch)
    if target is None or not _under_library(config, target):
        return

    try:
        store.set_path_identity(
            target, identity_to_dict(identity), IdStatus.CONFIRMED.value
        )
    except Exception:
        # A failed cache write must never turn a successful apply into a failure.
        pass


def apply_batch(
    store: Store,
    run_id: str,
    batch: Batch,
    config: AppConfig,
    seq: int,
) -> tuple[bool, int, str | None]:
    applied: list[tuple[int, Action]] = []
    try:
        for action in batch.actions:
            seq += 1
            store.add_journal(
                run_id,
                seq,
                action.kind.value,
                str(action.src) if action.src else None,
                str(action.dst) if action.dst else None,
                _content_sha(action),
                action.owned,
                "started",
            )
            skip = _do_action(action, store, run_id, seq, config)
            if skip is None:
                store.update_journal(run_id, seq, "done")
                applied.append((seq, action))
            else:
                store.update_journal(run_id, seq, "skipped", skip)
        return True, seq, None
    except Exception as exc:
        store.update_journal(run_id, seq, "failed", str(exc))
        for s, action in reversed(applied):
            if _undo_action(action, store, run_id, s):
                store.update_journal(run_id, s, "undone")
            else:
                store.update_journal(run_id, s, "undo_failed")
        return False, seq, str(exc)


def _psq(value: str) -> str:
    return value.replace('"', '""')


def write_undo_script(store: Store, run_id: str, journal: list[dict[str, Any]]) -> None:
    undo_dir = store.state_dir / "undo"
    undo_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# mediakeep undo script",
        "$ErrorActionPreference = 'Stop'",
    ]
    for e in reversed(journal):
        if e.get("status") != "done":
            continue
        kind = e.get("kind")
        src = e.get("src")
        dst = e.get("dst")
        if kind == "move" and src and dst:
            lines.append(
                f'Move-Item -LiteralPath "{_psq(dst)}" -Destination "{_psq(src)}"'
            )
        elif kind == "mkdir" and src:
            lines.append(
                f'if (Test-Path -LiteralPath "{_psq(src)}") {{ Remove-Item -LiteralPath "{_psq(src)}" }}'
            )
        elif kind == "rmdir" and src:
            lines.append(
                f'New-Item -ItemType Directory -Force -Path "{_psq(src)}" | Out-Null'
            )
        elif kind in ("write_file", "symlink", "fetch_file") and dst:
            lines.append(
                f'Remove-Item -LiteralPath "{_psq(dst)}" -Force -ErrorAction SilentlyContinue'
            )
    (undo_dir / f"undo-{run_id}.ps1").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_undo_script_if_needed(store: Store, run_id: str) -> None:
    journal = store.get_journal(run_id)
    if any(e.get("status") == "done" for e in journal):
        write_undo_script(store, run_id, journal)


def apply_plan(
    config: AppConfig,
    store: Store,
    plan: Plan,
    item_keys: Sequence[str] | None,
    progress: ProgressSink,
) -> RunRecord:
    run = RunRecord(
        id=_new_run_id(),
        plan_id=plan.id,
        kind="apply",
        started_at=datetime.now(),
        finished_at=None,
        status="ok",
        done=0,
        failed=0,
        skipped=0,
        issues=[],
    )
    store.create_run(run)

    with ApplyLock(store.state_dir):
        selected: list[Batch] = []
        if item_keys is None:
            for b in plan.batches:
                if b.needs_review:
                    run.skipped += 1
                    run.issues.append(
                        Issue(
                            item_key=b.item_key,
                            path=_batch_path(b),
                            severity=Severity.NOTE,
                            message="needs review; not applied",
                        )
                    )
                    progress.advance(
                        1,
                        _progress_line("skipped", b.label, "needs review; not applied"),
                    )
                else:
                    selected.append(b)
        else:
            keys = set(item_keys)
            selected = [b for b in plan.batches if b.item_key in keys]

        skipped: set[str] = set()
        for b in selected:
            changed = False
            for pre in b.preconditions:
                try:
                    st = os.stat(long_path(pre.path))
                    if st.st_size != pre.size or st.st_mtime_ns != pre.mtime_ns:
                        changed = True
                        break
                except OSError:
                    changed = True
                    break
            if changed:
                run.skipped += 1
                skipped.add(b.item_key)
                run.issues.append(
                    Issue(
                        item_key=b.item_key,
                        path=_batch_path(b),
                        severity=Severity.ATTENTION,
                        message="changed since it was planned; scan again",
                    )
                )
                progress.advance(
                    1,
                    _progress_line(
                        "skipped", b.label, "changed since it was planned; scan again"
                    ),
                )

        seq = 0
        for b in selected:
            if b.item_key in skipped:
                continue
            ok, seq, err = apply_batch(store, run.id, b, config, seq)
            files = _move_count(b)
            if ok:
                run.done += files
                progress.advance(1, _progress_line("ok", b.label, f"{files} {_file_word(files)}"))
                _record_path_identity(config, store, plan, b)
            else:
                run.failed += 1
                run.status = "partial"
                detail = err or "failed"
                progress.advance(1, _progress_line("FAILED", b.label, detail))
                message = f"batch failed: {b.label}"
                if err:
                    message += f": {err}"
                run.issues.append(
                    Issue(
                        item_key=b.item_key,
                        path=_batch_path(b),
                        severity=Severity.ATTENTION,
                        message=message,
                    )
                )

        _write_undo_script_if_needed(store, run.id)
        run.finished_at = datetime.now()
        if run.failed and not run.done:
            run.status = "failed"
        store.finish_run(run)
        return run


def undo_run(
    config: AppConfig,
    store: Store,
    run_id: str,
    apply: bool,
    progress: ProgressSink,
) -> RunRecord:
    run = RunRecord(
        id=_new_run_id(),
        plan_id=None,
        kind="undo",
        started_at=datetime.now(),
        finished_at=None,
        status="ok",
        done=0,
        failed=0,
        skipped=0,
        issues=[],
    )
    store.create_run(run)
    if not apply:
        run.finished_at = datetime.now()
        store.finish_run(run)
        return run

    with ApplyLock(store.state_dir):
        entries: list[dict[str, Any]] = [
            e for e in store.get_journal(run_id) if e.get("status") == "done"
        ]
        seq = 0
        vacated: set[Path] = set()
        for e in reversed(entries):
            kind = e.get("kind")
            src = Path(e["src"]) if e.get("src") else None
            dst = Path(e["dst"]) if e.get("dst") else None
            progress.advance(1, f"{kind} {dst or src or ''}")
            try:
                if kind == "move" and src and dst:
                    if _exists(src):
                        run.skipped += 1
                        run.issues.append(
                            Issue(
                                item_key=None,
                                path=src,
                                severity=Severity.NOTE,
                                message="undo source already exists; skipped",
                            )
                        )
                        continue
                    if not _exists(dst):
                        run.skipped += 1
                        run.issues.append(
                            Issue(
                                item_key=None,
                                path=dst,
                                severity=Severity.NOTE,
                                message="undo target missing; skipped",
                            )
                        )
                        continue
                    seq += 1
                    store.add_journal(run.id, seq, "move", str(dst), str(src), None, False, "started")
                    _move(dst, src, run.id, seq)
                    store.update_journal(run.id, seq, "done")
                    run.done += 1
                    vacated.add(dst.parent)
                elif kind == "mkdir" and src:
                    if _exists(src):
                        _rmdir(src)
                        run.done += 1
                elif kind == "rmdir" and src:
                    seq += 1
                    store.add_journal(run.id, seq, "mkdir", str(src), None, None, False, "started")
                    os.makedirs(long_path(src), exist_ok=True)
                    store.update_journal(run.id, seq, "done")
                    run.done += 1
                elif kind in ("write_file", "symlink", "fetch_file") and dst:
                    if _exists(dst):
                        seq += 1
                        store.add_journal(
                            run.id,
                            seq,
                            kind,
                            None,
                            str(dst),
                            None,
                            False,
                            "started",
                        )
                        if dst.is_dir():
                            _rmdir(dst)
                        else:
                            os.unlink(long_path(dst))
                        store.update_journal(run.id, seq, "done")
                        run.done += 1
            except Exception as exc:
                run.failed += 1
                run.status = "partial"
                run.issues.append(
                    Issue(
                        item_key=None,
                        path=src or dst or Path("."),
                        severity=Severity.ATTENTION,
                        message=f"undo failed: {exc}",
                    )
                )

        _remove_emptied_folders(config, vacated)
        _write_undo_script_if_needed(store, run.id)
        run.finished_at = datetime.now()
        if run.failed and not run.done:
            run.status = "failed"
        store.finish_run(run)
        return run


def _remove_emptied_folders(config: AppConfig, folders: set[Path]) -> None:
    """After an undo, remove the folders the original run created that are now empty
    (e.g. "The Lantern Keepers (2023)" after undoing a rename in place).

    Only empty folders are removed (os.rmdir), deepest first, walking up until a configured
    source or library root, which is never removed. Folders outside every root are left alone.
    """
    roots = [Path(p) for p in config.sources] + [lib.path for lib in config.libraries.values()]
    root_keys = {str(r).replace("\\", "/").rstrip("/").lower() for r in roots}

    def key(p: Path) -> str:
        return str(p).replace("\\", "/").rstrip("/").lower()

    def under_root(p: Path) -> bool:
        k = key(p)
        return any(k.startswith(r + "/") for r in root_keys)

    for folder in sorted(folders, key=lambda p: len(p.parts), reverse=True):
        current = folder
        while under_root(current) and key(current) not in root_keys:
            if not _rmdir(current):
                break
            current = current.parent


def purge(
    config: AppConfig,
    store: Store,
    apply: bool,
    progress: ProgressSink,
) -> RunRecord:
    run = RunRecord(
        id=_new_run_id(),
        plan_id=None,
        kind="purge",
        started_at=datetime.now(),
        finished_at=None,
        status="ok",
        done=0,
        failed=0,
        skipped=0,
        issues=[],
    )
    store.create_run(run)
    if not apply:
        run.finished_at = datetime.now()
        store.finish_run(run)
        return run

    with ApplyLock(store.state_dir):
        for row in store.list_owned():
            path = Path(row["path"])
            progress.advance(1, str(path))
            try:
                if not path.exists():
                    store.remove_owned(path)
                    continue
                if path.is_symlink():
                    path.unlink()
                else:
                    sha = row.get("sha256")
                    if sha:
                        current = hashlib.sha256(path.read_bytes()).hexdigest()
                        if current != sha:
                            continue
                    path.unlink()
                store.remove_owned(path)
                run.done += 1
            except Exception as exc:
                run.failed += 1
                run.status = "partial"
                run.issues.append(
                    Issue(
                        item_key=None,
                        path=path,
                        severity=Severity.ATTENTION,
                        message=f"purge failed: {exc}",
                    )
                )
        run.finished_at = datetime.now()
        if run.failed and not run.done:
            run.status = "failed"
        store.finish_run(run)
        return run
