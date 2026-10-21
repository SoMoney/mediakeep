from __future__ import annotations

import re
import uuid
from dataclasses import replace
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Sequence

from mediakeep.agree import USER_REASONS, chosen_by_user, folder_agrees, item_name, trusted_pick
from mediakeep.classify import classify
from mediakeep.destination import wants_in_place
from mediakeep.config import AppConfig, LibraryConfig
from mediakeep.hints import Hint, hint_library, import_hints_file, load_hints, path_matches_hint
from mediakeep.models import (
    Action,
    ActionKind,
    AudioVerifier,
    Batch,
    Identifier,
    Identity,
    IdStatus,
    Issue,
    MediaKind,
    Plan,
    PlanContributor,
    ProgressSink,
    RunRecord,
    Severity,
    SourceItem,
    identity_from_dict,
)
from mediakeep.parse import normalize_title
from mediakeep.paths import unsafe_name_reason
from mediakeep.plan import build_batches
from mediakeep.scan import ScannedEntry, scan_entry, scan_root
from mediakeep.store import Store


class NullIdentifier:
    async def identify(
        self, items: Sequence[SourceItem], progress: ProgressSink
    ) -> dict[str, Identity]:
        out: dict[str, Identity] = {}
        for item in items:
            kind_known = item.kind in (
                MediaKind.MOVIE,
                MediaKind.SHOW,
                MediaKind.DVD,
                MediaKind.BLURAY,
            )
            if kind_known and item.title and item.year is not None:
                status = IdStatus.AUTO
                confidence = 100.0
            else:
                status = IdStatus.NEEDS_REVIEW
                confidence = 0.0
            out[item.key] = Identity(
                item_key=item.key,
                kind=item.kind,
                title=item.title,
                year=item.year,
                tmdb_id=None,
                imdb_id=None,
                status=status,
                confidence=confidence,
                reasons=["filename only"],
            )
        return out


def _optional_callable(module_name: str, attr: str) -> Callable[..., Any] | None:
    try:
        module = import_module(module_name)
    except ImportError:
        return None
    obj = getattr(module, attr, None)
    return obj if callable(obj) else None


def create_audio_verifier(config: AppConfig) -> AudioVerifier | None:
    f = _optional_callable("mediakeep.extras", "create_audio_verifier")
    if f is None:
        return None
    result = f(config)
    verifier: AudioVerifier | None = result
    return verifier


def create_identifier(
    config: AppConfig, store: Store, audio: AudioVerifier | None
) -> Identifier:
    f = _optional_callable("mediakeep.identify", "create_identifier")
    if f is None:
        return NullIdentifier()
    result = f(config, store, audio)
    if result is None:
        return NullIdentifier()
    identifier: Identifier = result
    return identifier


def create_contributors(config: AppConfig, store: Store) -> list[PlanContributor]:
    f = _optional_callable("mediakeep.extras", "create_contributors")
    if f is None:
        return []
    result = f(config, store)
    contributors: list[PlanContributor] = result if result is not None else []
    return contributors


def _item_in_only(item: SourceItem, only: Path | None) -> bool:
    if only is None:
        return True
    paths = [item.root, *item.paths]
    for p in paths:
        try:
            if p == only or only in p.parents or p in only.parents:
                return True
        except OSError:
            continue
    return False


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


def _path_identity_for_item(
    config: AppConfig, store: Store, item: SourceItem
) -> Identity | None:
    if item.hint_options.get("_tmdb_id") or item.hint_options.get("_imdb_id"):
        return None

    path: Path | None = None
    for p in item.paths:
        if p.is_dir():
            path = p
            break
    if path is None or not _under_library(config, path):
        return None

    data = store.get_path_identity(path)
    if not data:
        return None

    try:
        identity = identity_from_dict(data)
    except Exception:
        return None

    if identity.kind == MediaKind.UNKNOWN or not identity.title:
        return None
    if identity.tmdb_id is None and identity.imdb_id is None:
        return None
    # A match remembered from an earlier apply only counts while the folder name still
    # fits it (or you chose it): an old wrong match must not come back on every scan.
    if not trusted_pick(identity, path.name) and not folder_agrees(path.name, identity):
        return None

    identity.item_key = item.key
    identity.status = IdStatus.CONFIRMED
    identity.confidence = 100.0
    identity.reasons = ["path identity"] + [r for r in identity.reasons if r in USER_REASONS]
    if identity.library and identity.library not in config.libraries:
        identity.library = None
    return identity


def _add_yearless_issues(
    items: Sequence[SourceItem],
    identities: dict[str, Identity],
    identifier: Identifier,
    issues: list[Issue],
) -> None:
    is_null_identifier = isinstance(identifier, NullIdentifier)
    for item in items:
        if item.year is not None:
            continue
        identity = identities.get(item.key)
        if identity is None:
            still_unidentified = True
        else:
            still_unidentified = identity.status == IdStatus.UNMATCHED or (
                identity.status == IdStatus.NEEDS_REVIEW and not identity.candidates
            )
        if not still_unidentified:
            continue

        if is_null_identifier:
            message = (
                "no year in the name; add one to the folder or file name, "
                "or configure TMDB for suggestions"
            )
        else:
            message = "no year in the name; choose one of the TMDB suggestions"

        issues.append(
            Issue(
                item_key=item.key,
                path=item.paths[0] if item.paths else item.root,
                severity=Severity.ATTENTION,
                message=message,
            )
        )


def _user_library(config: AppConfig, identity: Identity | None) -> str | None:
    if identity is None:
        return None
    if identity.library not in config.libraries:
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


def _apply_library_choice(
    config: AppConfig, item: SourceItem, identity: Identity | None, hints: list[Hint]
) -> None:
    if identity is None:
        return

    user = _user_library(config, identity)
    hint_lib = _hint_library_for_item(config, item, hints)
    current = item.paths[0] if item.paths else item.root
    route_identity = replace(identity, library=user)

    chosen: str | None = None
    try:
        from mediakeep.identify.route import pick_library  # lazy: avoid package cycle

        chosen = pick_library(config, route_identity, current, hint_lib)
    except Exception:
        chosen = user or hint_lib

    if chosen and chosen in config.libraries:
        identity.library = chosen


def _apply_default_title(
    config: AppConfig, item: SourceItem, identity: Identity | None
) -> None:
    """For items already in a library (renamed in place), keep the title read from the
    file/folder when TMDB's differs; the Library page offers TMDB's title as the alternative.
    Items arriving from a source keep using TMDB's title (fix 7).

    A confirmed identity is a user decision and is never silently renamed.
    """
    if identity is None or identity.status == IdStatus.CONFIRMED:
        return
    current = item.paths[0] if item.paths else item.root
    try:
        from mediakeep.identify.route import library_for_path  # lazy: avoid package cycle
    except Exception:
        return
    if library_for_path(config, current) is None:
        return
    if any("title chosen by user" in reason for reason in identity.reasons):
        return
    if not identity.tmdb_id or not identity.candidates or not item.title:
        return

    candidate = next(
        (c for c in identity.candidates if c.tmdb_id == identity.tmdb_id),
        identity.candidates[0],
    )
    if normalize_title(candidate.title) != normalize_title(item.title):
        identity.title = item.title


def _in_place_root(item: SourceItem) -> Path:
    from mediakeep.layout import in_place_parent_path

    return in_place_parent_path(item)


def _prepare_in_place(
    config: AppConfig,
    store: Store,
    items: Sequence[SourceItem],
    identities: dict[str, Identity],
) -> tuple[AppConfig, list[str]]:
    """Return a copied config containing synthetic in-place targets.

    The copy is built with ``model_copy`` and is deliberately not validated: an
    in-place target may be a folder inside a source, which a real library may not.
    """
    libs = dict(config.libraries)
    keys: list[str] = []

    for item in items:
        if not wants_in_place(config, store, item):
            continue

        root = _in_place_root(item)
        key = f"__in_place__{item.key}"
        kind = item.kind if item.kind != MediaKind.UNKNOWN else MediaKind.MOVIE
        libs[key] = LibraryConfig(path=root, kind=kind)

        identity = identities.get(item.key)
        if identity is None:
            title_known = bool(item.title)
            year_known = item.year is not None
            identities[item.key] = Identity(
                item_key=item.key,
                kind=kind,
                title=item.title,
                year=item.year,
                tmdb_id=None,
                imdb_id=None,
                status=IdStatus.AUTO if title_known and year_known else IdStatus.NEEDS_REVIEW,
                confidence=100.0 if title_known and year_known else 0.0,
                reasons=["in-place"],
                library=key,
            )
        else:
            identity.library = key

        keys.append(item.key)

    if not keys:
        return config, keys
    return config.model_copy(update={"libraries": libs}), keys


def _clear_in_place_identity_libraries(
    identities: dict[str, Identity], keys: Sequence[str]
) -> None:
    for key in keys:
        identity = identities.get(key)
        if identity is not None:
            identity.library = None


POSTER_NAMES = ("folder.jpg", "poster.jpg", "folder.png", "poster.png", "cover.jpg")
POSTER_SIZE = "w780"


def _poster_url_for(identity: Identity | None) -> str | None:
    """The matched title's TMDB poster, at a size good for a folder image."""
    if identity is None or not identity.tmdb_id:
        return None
    url = next(
        (c.poster_url for c in identity.candidates if c.tmdb_id == identity.tmdb_id and c.poster_url),
        None,
    )
    if not url:
        return None
    # .../t/p/w342/abc.jpg -> .../t/p/w780/abc.jpg
    return re.sub(r"/(w\d+|original)/(?=[^/]+$)", f"/{POSTER_SIZE}/", url)


def _item_folder_of(batch: Batch) -> Path | None:
    """The item's own folder after the change (the show folder for episodes)."""
    if batch.reasons and batch.reasons[0].startswith("target: "):
        return Path(batch.reasons[0][len("target: ") :])
    for action in batch.actions:
        if action.kind == ActionKind.MOVE and action.dst is not None:
            parent = action.dst.parent
            if parent.name.lower().startswith(("season ", "specials")):
                return parent.parent
            return parent
    return None


def _add_poster_only_batches(
    config: AppConfig,
    items: Sequence[SourceItem],
    batches: list[Batch],
    identities: dict[str, Identity],
) -> None:
    """A match you confirmed for an item that is already named right still has one thing
    to do: save its poster. Offer that as a change of its own (only for confirmed matches,
    so a scan never adds posters to a whole library on its own)."""
    if not config.naming.folder_jpg:
        return
    from mediakeep.layout import item_home_path

    planned = {b.item_key for b in batches}
    for item in items:
        if item.key in planned:
            continue
        identity = identities.get(item.key)
        if identity is None or identity.status != IdStatus.CONFIRMED or not chosen_by_user(identity):
            continue
        url = _poster_url_for(identity)
        folder = item_home_path(item)
        if not url or folder is None or not folder.is_dir():
            continue
        if any((folder / name).exists() for name in POSTER_NAMES):
            continue
        batches.append(
            Batch(
                item_key=item.key,
                label=folder.name,
                kind=identity.kind,
                actions=[Action(kind=ActionKind.FETCH_FILE, src=None, dst=folder / "folder.jpg", content=url)],
                reasons=[f"target: {folder}", "poster only"],
                preconditions=[],
                needs_review=False,
            )
        )


def _add_poster_actions(
    config: AppConfig, batches: Sequence[Batch], identities: dict[str, Identity]
) -> None:
    """Add "save the poster as folder.jpg" to each change, last, so the folder exists.
    Skipped when the folder already has a poster image, or one is being moved in."""
    if not config.naming.folder_jpg:
        return
    for batch in batches:
        if any(a.kind == ActionKind.FETCH_FILE for a in batch.actions):
            continue
        if not any(a.kind == ActionKind.MOVE for a in batch.actions):
            continue
        url = _poster_url_for(identities.get(batch.item_key))
        folder = _item_folder_of(batch)
        if not url or folder is None:
            continue
        incoming = {a.dst.name.lower() for a in batch.actions if a.dst is not None and a.dst.parent == folder}
        if incoming & set(POSTER_NAMES):
            continue
        if any((folder / name).exists() for name in POSTER_NAMES):
            continue
        batch.actions.append(Action(kind=ActionKind.FETCH_FILE, src=None, dst=folder / "folder.jpg", content=url))


def _scan_root_for(config: AppConfig, item: SourceItem, path: Path) -> Path | None:
    """The folder ``path`` was scanned from: the item's root when it contains the path,
    else the source or library holding it. None when the path is that folder itself."""
    for root in (item.root, _root_for_path(config, path)):
        if root is None:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if rel.parts:
            return root
    return None


def _root_for_path(config: AppConfig, path: Path) -> Path | None:
    for lib in config.libraries.values():
        if _is_under(path, lib.path):
            return lib.path
    for src in config.sources:
        if _is_under(path, src):
            return src
    return None


def _relative_parts(child: Path, root: Path | None) -> list[str]:
    if root is None:
        return [child.name] if child.name else []

    try:
        rel = child.relative_to(root)
        return [p for p in rel.parts if p and p not in {".", ".."}]
    except ValueError:
        c = _norm_path(child)
        r = _norm_path(root)
        if c == r:
            return []
        if c.startswith(r + "/"):
            rel_text = c[len(r) + 1 :]
            return [p for p in rel_text.split("/") if p and p not in {".", ".."}]
        return [child.name] if child.name else []


def _unsafe_batch_reason(config: AppConfig, batch: Batch) -> str | None:
    for action in batch.actions:
        if action.kind != ActionKind.MOVE or action.dst is None:
            continue
        root = _root_for_path(config, action.dst)
        for component in _relative_parts(action.dst, root):
            reason = unsafe_name_reason(component)
            if reason is not None:
                return f'name not allowed on disk: "{component}" – {reason}'
    return None


def _batch_issue_path(batch: Batch) -> Path:
    for action in batch.actions:
        if action.src is not None:
            return action.src
        if action.dst is not None:
            return action.dst
    return Path.cwd()


def _drop_unsafe_batches(
    config: AppConfig, batches: list[Batch], issues: list[Issue]
) -> list[Batch]:
    safe: list[Batch] = []
    for batch in batches:
        reason = _unsafe_batch_reason(config, batch)
        if reason is not None:
            issues.append(
                Issue(
                    item_key=batch.item_key,
                    path=_batch_issue_path(batch),
                    severity=Severity.ATTENTION,
                    message=reason,
                )
            )
        else:
            safe.append(batch)
    return safe


def _batch_selected(batch: Batch, keys: set[str] | None) -> bool:
    if keys is None:
        return not batch.needs_review
    return batch.item_key in keys


async def build_plan(
    config: AppConfig,
    store: Store,
    progress: ProgressSink,
    only: Path | None = None,
    kind: MediaKind | None = None,
) -> Plan:
    if config.hints_file:
        import_hints_file(store, Path(config.hints_file))
    hints = load_hints(store)
    used_hints: set[int] = set()

    progress.stage("scan", len(config.root_paths))
    inventory: dict[Path, list[ScannedEntry]] = {}
    for root in config.root_paths:
        if progress.cancelled():
            break
        inventory[root] = scan_root(root)
        for entry in inventory[root]:
            progress.advance(0, str(entry.path))
        progress.advance(1, f"scan: {root}")

    progress.stage("classify")
    items, issues, loose_samples = classify(config, inventory, hints, used_hints)
    if only is not None:
        items = [i for i in items if _item_in_only(i, only)]
    if kind is not None:
        items = [i for i in items if i.kind == kind]
    for found in items:
        year = f" ({found.year})" if found.year else ""
        progress.advance(0, f"{found.kind.value}: {found.title or found.key}{year}")
    progress.advance(len(items))

    progress.stage("identify", len(items))
    audio = create_audio_verifier(config)
    identifier = create_identifier(config, store, audio)

    path_identities: dict[str, Identity] = {}
    remaining: list[SourceItem] = []
    for item in items:
        ident = _path_identity_for_item(config, store, item)
        if ident is not None:
            path_identities[item.key] = ident
            progress.advance(1)
        else:
            remaining.append(item)

    identities = await identifier.identify(remaining, progress)
    identities.update(path_identities)
    _add_yearless_issues(items, identities, identifier, issues)

    for item in items:
        identity = identities.get(item.key)
        _apply_default_title(config, item, identity)
        _apply_library_choice(config, item, identity, hints)

    progress.stage("plan", len(items))
    planning_config, in_place_keys = _prepare_in_place(config, store, items, identities)
    contributors = create_contributors(planning_config, store)
    batches, plan_issues, duplicates, multi_version, samples, unchanged = build_batches(
        planning_config, store, items, identities, contributors
    )
    issues.extend(plan_issues)
    batches = _drop_unsafe_batches(config, batches, issues)
    _add_poster_actions(config, batches, identities)
    _add_poster_only_batches(config, items, batches, identities)
    _clear_in_place_identity_libraries(identities, in_place_keys)

    seen_samples = set(samples)
    for fi in loose_samples:
        if fi.path not in seen_samples:
            samples.append(fi.path)
            seen_samples.add(fi.path)

    plan = Plan(
        id=str(uuid.uuid4()),
        created_at=datetime.now(),
        config_hash=config.config_hash(),
        items={i.key: i for i in items},
        identities=identities,
        batches=batches,
        issues=issues,
        duplicates=duplicates,
        multi_version=multi_version,
        samples=samples,
        unchanged=unchanged,
    )
    store.save_plan(plan)
    progress.advance(len(items))
    return plan


async def replan_item(
    config: AppConfig,
    store: Store,
    plan: Plan,
    item_key: str,
    progress: ProgressSink,
) -> Plan:
    item = plan.items.get(item_key)
    if item is None:
        return plan

    progress.stage("replan", 1)

    paths = item.paths or [item.root]
    inventory: dict[Path, list[ScannedEntry]] = {}
    seen_tops: set[Path] = set()
    for path in paths:
        # Re-scan the item where the full scan saw it: as an entry of the folder it was
        # scanned from (its source or library). Scanning its own folder as a root would turn
        # a movie folder into a loose file whose "root" is that folder, and renaming in place
        # would then nest the new folder inside the old one.
        root = _scan_root_for(config, item, path)
        if root is None:
            fallback = path if path.is_dir() else path.parent
            if fallback not in inventory:
                try:
                    inventory[fallback] = scan_root(fallback)
                except OSError:
                    inventory[fallback] = []
            continue
        top = root / path.relative_to(root).parts[0]
        if top in seen_tops:
            continue
        seen_tops.add(top)
        entry = scan_entry(top)
        inventory.setdefault(root, [])
        if entry is not None:
            inventory[root].append(entry)

    if inventory:
        hints = load_hints(store)
        items, classify_issues, _loose_samples = classify(config, inventory, hints, set())
        new_item = next((i for i in items if i.key == item.key), item)
    else:
        items = [item]
        classify_issues = []
        new_item = item
        hints = load_hints(store)

    stored_identity = None
    stored = store.get_identity(item_key)
    if stored:
        try:
            stored_identity = identity_from_dict(stored)
        except Exception:
            stored_identity = None

    if (
        stored_identity is not None
        and stored_identity.status == IdStatus.CONFIRMED
        and trusted_pick(stored_identity, item_name(new_item))
    ):
        # You confirmed this match: use it as is, no new TMDB lookup.
        stored_identity.item_key = new_item.key
        identities: dict[str, Identity] = {new_item.key: stored_identity}
        new_identity: Identity | None = stored_identity
    else:
        audio = create_audio_verifier(config)
        identifier = create_identifier(config, store, audio)
        identities = await identifier.identify([new_item], progress)
        new_identity = identities.get(new_item.key) or identities.get(item.key)

    # Plan with the identity chosen above (the user's confirmed choice wins over a fresh match):
    # build_batches reads `identities`, so it must hold that same object.
    if new_identity is not None:
        identities = dict(identities)
        identities.pop(item.key, None)
        identities[new_item.key] = new_identity

    _apply_default_title(config, new_item, new_identity)
    _apply_library_choice(config, new_item, new_identity, hints)

    contributors = create_contributors(config, store)
    planning_config, in_place_keys = _prepare_in_place(
        config, store, [new_item], identities
    )
    batches, batch_issues, _duplicates, _multi_version, _samples, _unchanged = build_batches(
        planning_config,
        store,
        [new_item],
        identities,
        contributors,
    )
    batches = _drop_unsafe_batches(config, batches, batch_issues)
    _add_poster_actions(config, batches, identities)
    _add_poster_only_batches(config, [new_item], batches, identities)
    _clear_in_place_identity_libraries(identities, in_place_keys)

    old_keys = {item.key, new_item.key}
    plan.items.pop(item.key, None)
    plan.identities.pop(item.key, None)
    plan.items[new_item.key] = new_item
    if new_identity is not None:
        plan.identities[new_item.key] = new_identity
    else:
        plan.identities.pop(new_item.key, None)

    plan.batches = [b for b in plan.batches if b.item_key not in old_keys] + batches

    def _old_issue(issue: Issue) -> bool:
        if issue.item_key in old_keys:
            return True
        if issue.item_key is None:
            return any(_is_under(issue.path, p) for p in item.paths)
        return False

    def _new_issue(issue: Issue) -> bool:
        if issue.item_key in old_keys:
            return True
        if issue.item_key is None:
            return any(_is_under(issue.path, p) for p in new_item.paths)
        return False

    new_issues = [i for i in classify_issues + batch_issues if _new_issue(i)]
    plan.issues = [i for i in plan.issues if not _old_issue(i)] + new_issues

    store.save_plan(plan)
    progress.advance(1)
    return plan


def apply_plan(
    config: AppConfig,
    store: Store,
    plan: Plan,
    item_keys: Sequence[str] | None,
    progress: ProgressSink,
) -> RunRecord:
    from mediakeep.apply import apply_plan as _apply

    keys = None if item_keys is None else set(item_keys)
    selected = [b for b in plan.batches if _batch_selected(b, keys)]

    unsafe_issues: list[Issue] = []
    unsafe_keys: set[str] = set()
    for batch in selected:
        reason = _unsafe_batch_reason(config, batch)
        if reason is not None:
            unsafe_keys.add(batch.item_key)
            unsafe_issues.append(
                Issue(
                    item_key=batch.item_key,
                    path=_batch_issue_path(batch),
                    severity=Severity.ATTENTION,
                    message=reason,
                )
            )

    safe_batches = [b for b in plan.batches if b.item_key not in unsafe_keys]
    plan_to_apply = replace(plan, batches=safe_batches)
    selected_safe = [b for b in safe_batches if _batch_selected(b, keys)]

    if selected_safe:
        run = _apply(config, store, plan_to_apply, item_keys, progress)
    else:
        run = RunRecord(
            id=str(uuid.uuid4()),
            plan_id=plan.id,
            kind="apply",
            started_at=datetime.now(),
            finished_at=datetime.now(),
            status="failed" if unsafe_issues else "ok",
            done=0,
            failed=len(unsafe_issues),
            skipped=0,
            issues=list(unsafe_issues),
        )
        store.create_run(run)

    if unsafe_issues:
        run.failed += len(unsafe_issues)
        run.issues = list(run.issues) + unsafe_issues
        if run.done and run.failed:
            run.status = "partial"
        elif run.failed and not run.done:
            run.status = "failed"
        else:
            run.status = "ok"
        try:
            store.finish_run(run)
        except Exception:
            pass

    return run


def undo_run(
    config: AppConfig,
    store: Store,
    run_id: str,
    apply: bool,
    progress: ProgressSink,
) -> RunRecord:
    from mediakeep.apply import undo_run as _undo

    return _undo(config, store, run_id, apply, progress)


def purge(
    config: AppConfig,
    store: Store,
    apply: bool,
    progress: ProgressSink,
) -> RunRecord:
    from mediakeep.apply import purge as _purge

    return _purge(config, store, apply, progress)
