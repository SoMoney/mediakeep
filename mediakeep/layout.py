from __future__ import annotations

import re
from pathlib import Path

from mediakeep.config import AppConfig
from mediakeep.models import Identity, IdStatus, MediaKind, SourceItem
from mediakeep.paths import sanitize_name


def year_suffix(year: int | None, style: str) -> str:
    if year is None:
        return ""
    if style == "brackets":
        return f" [{year}]"
    return f" ({year})"


def _identity_is_usable(identity: Identity | None) -> bool:
    return identity is not None and identity.status in (
        IdStatus.AUTO,
        IdStatus.CONFIRMED,
    )


def display_title_year(
    item: SourceItem, identity: Identity | None
) -> tuple[str, int | None]:
    if _identity_is_usable(identity) and identity is not None and identity.title:
        return identity.title, identity.year
    return item.title, item.year


def id_tag(identity: Identity | None, style: str) -> str:
    if not _identity_is_usable(identity) or identity is None:
        return ""
    if style == "tmdb" and identity.tmdb_id is not None:
        return f" [tmdbid-{identity.tmdb_id}]"
    if style == "imdb" and identity.imdb_id:
        return f" [imdbid-{identity.imdb_id}]"
    return ""


def folder_base(item: SourceItem, identity: Identity | None, config: AppConfig) -> str:
    title, year = display_title_year(item, identity)
    return sanitize_name(
        title
        + year_suffix(year, config.naming.year_style)
        + id_tag(identity, config.naming.id_tag)
    )


def episode_file_base(
    item: SourceItem, identity: Identity | None, config: AppConfig
) -> str:
    title, year = display_title_year(item, identity)
    return sanitize_name(title + year_suffix(year, config.naming.year_style))


def target_dir(config: AppConfig, identity: Identity | None, item: SourceItem) -> Path | None:
    kind = item.kind
    if identity and identity.library:
        name = identity.library
    elif kind in (MediaKind.MOVIE, MediaKind.DVD, MediaKind.BLURAY):
        name = config.routing.movie
    elif kind == MediaKind.SHOW:
        name = config.routing.show
    else:
        return None
    lib = config.libraries.get(name)
    if not lib:
        return None
    return lib.path


def movie_file_name(base: str, suffix: str, ext: str) -> str:
    if suffix:
        return sanitize_name(f"{base} - {suffix}") + ext
    return sanitize_name(base) + ext


def episode_file_name(
    base: str,
    season: int,
    episode: int,
    episode_end: int | None,
    title: str,
    version_label: str,
    ext: str,
    keep_title: bool,
) -> str:
    name = f"{base} S{season:02d}E{episode:02d}"
    if episode_end is not None:
        name += f"-E{episode_end:02d}"
    if keep_title and title:
        name += f" - {title}"
    if version_label:
        name += f" - {version_label}"
    return sanitize_name(name) + ext


def _owned_by_item(folder: Path, item: SourceItem) -> bool:
    """True when the folder is the item's own folder (its name parses to the item's title),
    not a shared folder such as a source root or a generic "TV" folder."""
    from mediakeep.parse import normalize_title, parse_release_name

    if not item.title:
        return False
    try:
        folder_title = parse_release_name(folder.name + ".x").title
    except Exception:
        return False
    return bool(folder_title) and normalize_title(folder_title) == normalize_title(item.title)


def _is_dir(p: Path) -> bool:
    from mediakeep.memo import is_dir

    return is_dir(p)


def _norm_path(p: Path) -> str:
    import os

    return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))


def _holds_only_item(folder: Path, item: SourceItem) -> bool:
    """True when every video under ``folder`` belongs to this item."""
    from mediakeep.classify import VIDEO_EXTS

    mine: set[str] = set()
    for group in (item.videos, item.samples, item.extras):
        mine.update(_norm_path(fi.path) for fi in group)
    mine.update(_norm_path(ep.file.path) for ep in item.episodes)
    mine.update(_norm_path(p) for p in item.paths if p.suffix.lower() in VIDEO_EXTS)
    if not mine:
        return False
    try:
        for f in folder.rglob("*"):
            if f.suffix.lower() in VIDEO_EXTS and f.is_file() and _norm_path(f) not in mine:
                return False
    except OSError:
        return False
    return True


def _is_own_folder(folder: Path, item: SourceItem) -> bool:
    """The folder belongs to the item: named after it, or holding nothing but its videos.
    The folder the item was scanned from (a source or library root) never counts."""
    if _norm_path(folder) == _norm_path(item.root):
        return False
    return _owned_by_item(folder, item) or _holds_only_item(folder, item)


_SEASON_DIR = re.compile(
    r"^(season[\s._-]*\d{1,3}|s\d{1,3}|series[\s._-]*\d{1,3}|specials?|extras?|featurettes?)$",
    re.IGNORECASE,
)


def _is_season_dir(folder: Path) -> bool:
    return bool(_SEASON_DIR.match(folder.name.strip()))


def in_place_parent_path(item: SourceItem) -> Path:
    """The folder an item stays in when it is renamed in place: the new
    "Title (Year)" folder is created here, next to the item's current folder.

    - An item given as its own folder stays next to it: the folder's parent.
    - Season folders ("Season 01", "S02", "Specials", ...) are never the item's home: they
      belong to a show folder, so we step up past them to the show folder, then to its parent.
    - Loose files stay in the folder that holds them, unless that folder is the item's own
      folder (named after it, or a show folder holding season folders), then its parent.
    """
    import os

    dirs = [p for p in item.paths if _is_dir(p)]
    if dirs:
        top = min(dirs, key=lambda p: len(p.parts))
        while _is_season_dir(top) and top.parent != top:
            top = top.parent
        return top.parent

    files = [p for p in item.paths if not _is_dir(p)]
    files += [v.path for v in item.videos]
    files += [ep.file.path for ep in item.episodes]
    if not files:
        return item.root

    holder = Path(os.path.commonpath([str(p.parent) for p in files]))
    climbed = False
    while _is_season_dir(holder) and holder.parent != holder:
        holder = holder.parent
        climbed = True
    if climbed:
        return holder.parent  # holder is the show folder

    # Files spread over season subfolders of `holder`: holder is the show folder.
    for f in files:
        try:
            between = f.parent.relative_to(holder).parts
        except ValueError:
            continue
        if between and _is_season_dir(Path(between[0])):
            return holder.parent

    return holder.parent if _is_own_folder(holder, item) else holder


def item_home_path(item: SourceItem) -> Path | None:
    """The item's own folder, the one a rename in place renames, or None when the item is
    loose files sitting in a shared folder (then only the files are renamed)."""
    import os

    dirs = [p for p in item.paths if _is_dir(p)]
    if dirs:
        top = min(dirs, key=lambda p: len(p.parts))
        while _is_season_dir(top) and top.parent != top:
            top = top.parent
        return top

    files = [p for p in item.paths if not _is_dir(p)]
    files += [v.path for v in item.videos]
    files += [ep.file.path for ep in item.episodes]
    if not files:
        return None

    holder = Path(os.path.commonpath([str(p.parent) for p in files]))
    climbed = False
    while _is_season_dir(holder) and holder.parent != holder:
        holder = holder.parent
        climbed = True
    if climbed:
        return holder
    for f in files:
        try:
            between = f.parent.relative_to(holder).parts
        except ValueError:
            continue
        if between and _is_season_dir(Path(between[0])):
            return holder
    return holder if _is_own_folder(holder, item) else None
