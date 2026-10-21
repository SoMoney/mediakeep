from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from mediakeep.models import FileInfo
from mediakeep.paths import long_path, strip_long

_SKIP_NAMES = {
    "thumbs.db",
    "desktop.ini",
    "$recycle.bin",
    "system volume information",
}


def should_skip_name(name: str) -> bool:
    low = name.lower()
    if low.startswith((".", "@", "#", "~")):
        return True
    return low in _SKIP_NAMES


@dataclass(slots=True)
class ScannedEntry:
    path: Path
    is_dir: bool
    files: list[FileInfo] = field(default_factory=list)
    subdirs: list[Path] = field(default_factory=list)


def _file_info(path: Path, size: int, mtime_ns: int, hidden: bool) -> FileInfo:
    return FileInfo(path=path, size=size, mtime_ns=mtime_ns, hidden=hidden)


def _walk_dir(root: Path, out: list[FileInfo], subdirs: list[Path]) -> None:
    try:
        with os.scandir(long_path(root)) as it:
            for entry in it:
                name = entry.name
                if should_skip_name(name):
                    continue
                path = strip_long(entry.path)
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(path)
                        _walk_dir(path, out, subdirs)
                    elif entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        hidden = bool(getattr(entry, "is_hidden", False))
                        out.append(_file_info(path, st.st_size, st.st_mtime_ns, hidden))
                except OSError:
                    continue
    except OSError:
        return


def scan_root(root: Path) -> list[ScannedEntry]:
    entries: list[ScannedEntry] = []
    if not root.exists():
        return entries
    try:
        with os.scandir(long_path(root)) as it:
            for entry in it:
                if should_skip_name(entry.name):
                    continue
                path = strip_long(entry.path)
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        files: list[FileInfo] = []
                        subdirs: list[Path] = []
                        _walk_dir(path, files, subdirs)
                        entries.append(
                            ScannedEntry(path=path, is_dir=True, files=files, subdirs=subdirs)
                        )
                    elif entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        hidden = bool(getattr(entry, "is_hidden", False))
                        fi = _file_info(path, st.st_size, st.st_mtime_ns, hidden)
                        entries.append(
                            ScannedEntry(path=path, is_dir=False, files=[fi], subdirs=[])
                        )
                except OSError:
                    continue
    except OSError:
        return entries
    return entries


def scan_entry(path: Path) -> ScannedEntry | None:
    """One top-level entry of a root, exactly as ``scan_root`` would report it."""
    if should_skip_name(path.name) or path.is_symlink():
        return None
    try:
        if path.is_dir():
            files: list[FileInfo] = []
            subdirs: list[Path] = []
            _walk_dir(path, files, subdirs)
            return ScannedEntry(path=path, is_dir=True, files=files, subdirs=subdirs)
        if path.is_file():
            st = path.stat()
            return ScannedEntry(
                path=path,
                is_dir=False,
                files=[_file_info(path, st.st_size, st.st_mtime_ns, False)],
                subdirs=[],
            )
    except OSError:
        return None
    return None
