"""One folder at a time.

You work in one folder. By default its items are renamed where they are; optionally you
pick one destination folder and they are moved into it (movies and shows alike).

The scanner and planner think in "sources" and "libraries". ``effective`` turns the
working folder into exactly that: the working folder is the only source, and the
destination (or the working folder itself) is the only library. Nothing else is scanned
or offered. With no working folder chosen yet, there is nothing to scan.

Code that builds an AppConfig itself (tests, scripts) may still set ``sources`` and
``libraries`` directly and leave the working folder empty; those are then used as given.
The settings file can't: ``load_config`` ignores them.
"""

from __future__ import annotations

import os
from pathlib import Path

from mediakeep.config import AppConfig, LibraryConfig
from mediakeep.models import MediaKind

DESTINATION = "destination"
RECENT_MAX = 8


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def effective(config: AppConfig) -> AppConfig:
    """The settings the scanner and planner run with, derived from the working folder."""
    ws = config.workspace
    routing = config.routing.model_copy(update={"movie": DESTINATION, "show": DESTINATION, "rules": []})
    if ws.folder is None and config.sources:
        return config  # sources and libraries set in code, not from the settings file
    if ws.folder is None:
        return config.model_copy(
            update={
                "sources": [],
                "libraries": {},
                "routing": routing,
                "behavior": config.behavior.model_copy(update={"default_destination": "in_place"}),
            }
        )
    folder = Path(ws.folder)
    dest = Path(ws.destination) if ws.destination is not None else None
    moving = dest is not None and _norm(dest) != _norm(folder)
    library = LibraryConfig.model_construct(path=dest if moving else folder, kind=MediaKind.MOVIE)
    behavior = config.behavior.model_copy(
        update={"default_destination": "file" if moving else "in_place"}
    )
    # model_copy skips validation on purpose: the working folder may be the library.
    return config.model_copy(
        update={
            "sources": [folder],
            "libraries": {DESTINATION: library},
            "routing": routing,
            "behavior": behavior,
        }
    )


def with_workspace(
    config: AppConfig, folder: Path | None, destination: Path | None
) -> AppConfig:
    """A copy of the saved settings with a new working folder and destination; the folder
    goes to the front of the recent list."""
    recent = list(config.workspace.recent)
    if folder is not None:
        recent = [Path(folder)] + [p for p in recent if _norm(p) != _norm(folder)]
    ws = config.workspace.model_copy(
        update={"folder": folder, "destination": destination, "recent": recent[:RECENT_MAX]}
    )
    return config.model_copy(update={"workspace": ws})
