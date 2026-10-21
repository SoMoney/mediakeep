"""Where an item goes: moved into the destination folder, or renamed where it is.

The working folder's setting decides (see ``workspace``): with no destination folder, items
are renamed where they are ("in_place"); with one, they are moved into it ("file"). A
folder that is itself the library is always tidied where it is. An item's own decision
(``"in_place"`` or ``"file"``, from "Rename here instead" on its row) wins.
"""

from __future__ import annotations

import os
from pathlib import Path

from mediakeep.config import AppConfig
from mediakeep.models import SourceItem
from mediakeep.store import Store


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _is_under(path: Path | str, root: Path | str) -> bool:
    child, parent = _norm(path), _norm(root)
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


def source_for(config: AppConfig, path: Path | str) -> Path | None:
    """The scanned folder that contains ``path`` (the deepest one), or None."""
    best: Path | None = None
    for src in config.sources:
        if _is_under(path, src) and (best is None or len(_norm(src)) > len(_norm(best))):
            best = src
    return best


def library_for(config: AppConfig, path: Path | str) -> str | None:
    for name, lib in config.libraries.items():
        if _is_under(path, lib.path):
            return name
    return None


def folder_mode(config: AppConfig, source: Path | str) -> str:
    """"in_place" (rename here) or "file" (move into the destination) for a folder."""
    if library_for(config, source):
        return "in_place"
    return str(config.behavior.default_destination)


def item_source(config: AppConfig, item: SourceItem) -> Path | None:
    for p in item.paths or [item.root]:
        src = source_for(config, p)
        if src is not None:
            return src
    return None


def wants_in_place(config: AppConfig, store: Store, item: SourceItem) -> bool:
    """True when the item is renamed where it is rather than moved."""
    own = store.get_decision(item.key)
    if own == "in_place":
        return True
    if own in ("file", "skipped"):
        return False
    src = item_source(config, item)
    return src is not None and folder_mode(config, src) == "in_place"
