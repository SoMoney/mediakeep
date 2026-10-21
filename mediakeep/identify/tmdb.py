from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlencode

import aiohttp

from mediakeep.config import AppConfig
from mediakeep.store import Store

logger = logging.getLogger(__name__)


class TmdbError(RuntimeError):
    pass


class TmdbUnavailable(TmdbError):
    pass


class TmdbNotFound(TmdbError):
    pass


def _retry_after_seconds(headers: Any) -> float | None:
    value = headers.get("Retry-After") if hasattr(headers, "get") else None
    if not value:
        return None
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


class TmdbClient:
    def __init__(
        self,
        config: AppConfig,
        store: Store,
        session_factory: Callable[[], Any] | None = None,
        base_url: str = "https://api.themoviedb.org/3",
        base_delay: float = 0.5,
    ) -> None:
        self.config = config
        self.store = store
        self._session_factory = session_factory
        self.base_url = base_url.rstrip("/")
        self._session: Any | None = None
        self._owns_session = session_factory is None
        self._timeout = aiohttp.ClientTimeout(total=30)
        self._base_delay = base_delay
        self.max_attempts = 4

        rps = int(config.tmdb.requests_per_second or 20)
        self._rps = float(max(1, rps))
        self._capacity = max(1.0, self._rps)
        self._tokens = self._capacity
        self._last = 0.0
        self._last_set = False
        self._lock = asyncio.Lock()

        self._cache_ready = False
        self._image_base: str | None = None
        self._image_base_failed = False

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

    def _get_session(self) -> Any:
        if self._session is None:
            if self._session_factory is not None:
                self._session = self._session_factory()
            else:
                self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    def _ensure_cache(self) -> None:
        if self._cache_ready:
            return
        conn = self.store._conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_cache (
                key TEXT PRIMARY KEY,
                body TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                ttl_s INTEGER NOT NULL
            )
            """
        )
        conn.commit()
        self._cache_ready = True

    def _cache_get(self, key: str) -> tuple[str | None, bool]:
        self._ensure_cache()
        conn = self.store._conn()
        row = conn.execute(
            "SELECT body, fetched_at, ttl_s FROM api_cache WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return None, False
        body = str(row[0])
        fetched_raw = str(row[1])
        try:
            ttl = int(row[2])
        except Exception:
            ttl = 0
        try:
            fetched = datetime.fromisoformat(fetched_raw)
        except Exception:
            return body, False
        fresh = (datetime.now() - fetched).total_seconds() < ttl
        return body, fresh

    def _cache_set(self, key: str, body: str, cache_days: int) -> None:
        self._ensure_cache()
        ttl = max(0, int(cache_days)) * 86400
        conn = self.store._conn()
        conn.execute(
            "INSERT OR REPLACE INTO api_cache (key, body, fetched_at, ttl_s) VALUES (?, ?, ?, ?)",
            (key, body, datetime.now().isoformat(), ttl),
        )
        conn.commit()

    async def _acquire(self) -> None:
        while True:
            async with self._lock:
                loop = asyncio.get_running_loop()
                now = loop.time()
                if not self._last_set:
                    self._last = now
                    self._last_set = True
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._last) * self._rps
                )
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1.0 - self._tokens) / self._rps
            await asyncio.sleep(max(0.0, wait))

    async def _sleep_retry(self, attempt: int, retry_after: float | None) -> None:
        if retry_after is not None:
            await asyncio.sleep(min(max(float(retry_after), 0.0), 60.0))
            return
        if self._base_delay <= 0:
            return
        delay = min(30.0, self._base_delay * (2**attempt))
        jitter = random.uniform(0.0, min(0.5, delay * 0.25)) if delay > 0 else 0.0
        await asyncio.sleep(delay + jitter)

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        cache: bool = True,
    ) -> Any:
        clean = {k: str(v) for k, v in (params or {}).items() if v is not None}
        key = path
        if clean:
            key += "?" + urlencode(sorted(clean.items()))

        if cache:
            cached_body, fresh = self._cache_get(key)
            if fresh and cached_body is not None:
                return json.loads(cached_body)

        attempt = 0
        last_exc: Exception | None = None
        while True:
            try:
                await self._acquire()
                session = self._get_session()
                req_params = dict(clean)
                headers: dict[str, str] = {}
                if self.config.tmdb.read_token:
                    headers["Authorization"] = f"Bearer {self.config.tmdb.read_token}"
                elif self.config.tmdb.api_key:
                    req_params["api_key"] = self.config.tmdb.api_key

                async with session.get(
                    f"{self.base_url}{path}",
                    params=req_params,
                    headers=headers,
                    timeout=self._timeout,
                ) as resp:
                    status = int(resp.status)
                    if status == 200:
                        text = await resp.text()
                        if cache:
                            self._cache_set(key, text, self.config.tmdb.cache_days)
                        return json.loads(text)
                    if status == 404:
                        raise TmdbNotFound(path)
                    if status == 429 or 500 <= status < 600:
                        delay = _retry_after_seconds(resp.headers)
                        await self._sleep_retry(attempt, delay)
                        attempt += 1
                        if attempt > self.max_attempts:
                            break
                        continue
                    raise TmdbError(f"TMDB returned HTTP {status}")
            except Exception as exc:
                if isinstance(exc, TmdbError):
                    raise
                last_exc = exc
                await self._sleep_retry(attempt, None)
                attempt += 1
                if attempt > self.max_attempts:
                    break

        if cache:
            stale, _ = self._cache_get(key)
            if stale is not None:
                return json.loads(stale)
        raise TmdbUnavailable("TMDB unreachable") from last_exc

    async def image_base_url(self) -> str | None:
        if self._image_base is not None or self._image_base_failed:
            return self._image_base
        try:
            data = await self._request("/configuration", {}, cache=True)
        except Exception:
            self._image_base_failed = True
            return None
        images = data.get("images") if isinstance(data, dict) else None
        if not isinstance(images, dict):
            images = {}
        # TMDB image URLs are <secure_base_url><size><poster_path>, e.g.
        # https://image.tmdb.org/t/p/w342/n9SicwnoNGWBwE75le27sUhCLMm.jpg - without a size the server answers 404.
        base = images.get("secure_base_url") or images.get("base_url")
        sizes = images.get("poster_sizes") or []
        size = next((s for s in ("w342", "w500", "w185") if s in sizes), "w342")
        if base:
            self._image_base = str(base).rstrip("/") + "/" + size
            return self._image_base
        self._image_base_failed = True
        return None

    async def search_movie(self, query: str, year: int | None = None) -> list[dict[str, Any]]:
        if not query:
            return []
        params: dict[str, Any] = {"query": query, "language": self.config.tmdb.language}
        if year is not None:
            params["year"] = year
        data = await self._request("/search/movie", params)
        if not isinstance(data, dict):
            return []
        results = data.get("results") or []
        return list(results) if isinstance(results, list) else []

    async def search_tv(self, query: str, year: int | None = None) -> list[dict[str, Any]]:
        if not query:
            return []
        params: dict[str, Any] = {"query": query, "language": self.config.tmdb.language}
        if year is not None:
            params["first_air_date_year"] = year
        data = await self._request("/search/tv", params)
        if not isinstance(data, dict):
            return []
        results = data.get("results") or []
        return list(results) if isinstance(results, list) else []

    async def movie_details(self, tmdb_id: int, credits: bool = False) -> dict[str, Any]:
        append = "external_ids,credits" if credits else "external_ids"
        params = {"append_to_response": append, "language": self.config.tmdb.language}
        data = await self._request(f"/movie/{tmdb_id}", params)
        return data if isinstance(data, dict) else {}

    async def tv_details(self, tmdb_id: int, credits: bool = False) -> dict[str, Any]:
        append = "external_ids,credits" if credits else "external_ids"
        params = {"append_to_response": append, "language": self.config.tmdb.language}
        data = await self._request(f"/tv/{tmdb_id}", params)
        return data if isinstance(data, dict) else {}

    async def season(self, tmdb_id: int, season: int) -> dict[str, Any]:
        params = {"language": self.config.tmdb.language}
        try:
            data = await self._request(f"/tv/{tmdb_id}/season/{season}", params)
        except TmdbNotFound:
            return {}
        return data if isinstance(data, dict) else {}
