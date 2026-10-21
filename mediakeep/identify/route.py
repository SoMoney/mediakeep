from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mediakeep.config import AppConfig
from mediakeep.models import Identity, MediaKind


def _effective_kind(kind: MediaKind) -> MediaKind:
    if kind in (MediaKind.MOVIE, MediaKind.DVD, MediaKind.BLURAY):
        return MediaKind.MOVIE
    if kind == MediaKind.SHOW:
        return MediaKind.SHOW
    return MediaKind.UNKNOWN


def _norm(path: Path | str) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


def library_for_path(config: AppConfig, path: Path | str | None) -> str | None:
    """The name of the library that ``path`` lives in, if any."""
    if path is None:
        return None
    child = _norm(path)
    if not child:
        return None
    for name, lib in config.libraries.items():
        parent = _norm(lib.path)
        if child == parent or child.startswith(parent + "/"):
            return name
    return None


def _match_when(when: dict[str, Any], identity: Identity) -> bool:
    kind = when.get("kind")
    if kind and str(kind) != _effective_kind(identity.kind):
        return False

    genres = [g.lower() for g in identity.genres]
    genres_all = when.get("genres_all") or []
    if genres_all and not all(str(g).lower() in genres for g in genres_all):
        return False

    genres_any = when.get("genres_any") or []
    if genres_any and not any(str(g).lower() in genres for g in genres_any):
        return False

    countries = [c.lower() for c in identity.countries]
    countries_any = when.get("countries_any") or []
    if countries_any and not any(str(c).lower() in countries for c in countries_any):
        return False

    original_language = when.get("original_language")
    if original_language and (identity.original_language or "").lower() != str(original_language).lower():
        return False

    title_regex = when.get("title_regex")
    if title_regex:
        try:
            if not re.search(str(title_regex), identity.title, re.IGNORECASE):
                return False
        except re.error:
            return False

    return True


def matching_rule_index(
    config: AppConfig, identity: Identity, skip_library_resident: bool = True
) -> int | None:
    """Index of the first rule that fits, or ``None``.

    Rules only route items that are *coming from* a source. When the item
    already lives in a library (``current_path`` inside one) rules are skipped,
    which is what ``pick_library`` does too.
    """
    for index, rule in enumerate(config.routing.rules):
        if not isinstance(rule, dict):
            continue
        when = rule.get("when") or {}
        to = rule.get("to")
        if not to or str(to) not in config.libraries:
            continue
        if isinstance(when, dict) and _match_when(when, identity):
            return index
    return None


def pick_library(
    config: AppConfig,
    identity: Identity,
    current_path: Path | None = None,
    hint_library: str | None = None,
) -> str | None:
    """Choose the library for an identity.

    Order:

    1. a remembered fix that names a library (``lib=NAME``);
    2. the library the user already chose for this item;
    3. **the library the file already lives in** - rules never move a file out
       of the library it is in, they only route items coming from a source;
    4. the first rule that fits;
    5. the default for the kind.
    """
    if hint_library and hint_library in config.libraries:
        return hint_library

    if identity.library and identity.library in config.libraries:
        return identity.library

    resident = library_for_path(config, current_path)
    if resident:
        return resident

    index = matching_rule_index(config, identity)
    if index is not None:
        return str(config.routing.rules[index].get("to"))

    fallback = config.routing.show if identity.kind == MediaKind.SHOW else config.routing.movie
    if fallback in config.libraries:
        return fallback
    return None
