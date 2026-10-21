"""A cache that lives for one page load.

Building a page asks the same questions many times (each item's card, its folder on disk,
the remembered fixes). Inside ``render_cache()`` the first answer is kept and reused until
the page is built; outside it nothing is cached, so anything that changes state (a POST)
always sees fresh answers.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Hashable, Iterator, TypeVar

T = TypeVar("T")

_cache: ContextVar[dict[Hashable, Any] | None] = ContextVar("mediakeep_render_cache", default=None)


@contextmanager
def render_cache() -> Iterator[None]:
    token = _cache.set({})
    try:
        yield
    finally:
        _cache.reset(token)


def memo(key: Hashable, compute: Callable[[], T]) -> T:
    cache = _cache.get()
    if cache is None:
        return compute()
    if key not in cache:
        cache[key] = compute()
    value: T = cache[key]
    return value


def is_dir(path: Path) -> bool:
    """``path.is_dir()``, asked of the disk at most once per page load."""
    return memo(("is-dir", str(path)), path.is_dir)


def remember_is_dir(path: Path, value: bool) -> None:
    """Record a folder check that is already known (e.g. from a directory listing)."""
    cache = _cache.get()
    if cache is not None:
        cache[("is-dir", str(path))] = value
