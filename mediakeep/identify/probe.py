from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

from mediakeep.config import AppConfig
from mediakeep.models import AudioVerifier, FileInfo, SourceItem
from mediakeep.paths import long_path
from mediakeep.store import Store

logger = logging.getLogger(__name__)


@cache
def _warn_missing_ffprobe() -> None:
    logger.warning("ffprobe was not found; duration, height and audio tags will be limited.")


@dataclass(slots=True)
class ProbeResult:
    duration: float | None
    height: int | None
    codec: str | None
    audio_language: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration": self.duration,
            "height": self.height,
            "codec": self.codec,
            "audio_language": self.audio_language,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProbeResult":
        return cls(
            duration=data.get("duration"),
            height=data.get("height"),
            codec=data.get("codec"),
            audio_language=data.get("audio_language"),
        )


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


class ProbeRunner:
    def __init__(self, config: AppConfig, store: Store) -> None:
        self.config = config
        self.store = store
        self._semaphore = asyncio.Semaphore(4)
        self._cache_ready = False

    def _ensure_files(self) -> None:
        if self._cache_ready:
            return
        conn = self.store._conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                size INTEGER,
                mtime_ns INTEGER,
                fingerprint TEXT,
                probe_json TEXT,
                seen_at TEXT
            )
            """
        )
        conn.commit()
        self._cache_ready = True

    def _cache_get(self, fi: FileInfo) -> ProbeResult | None:
        self._ensure_files()
        conn = self.store._conn()
        row = conn.execute(
            "SELECT probe_json FROM files WHERE path = ? AND size = ? AND mtime_ns = ?",
            (str(fi.path), fi.size, fi.mtime_ns),
        ).fetchone()
        if not row or not row[0]:
            return None
        try:
            data = json.loads(str(row[0]))
            if not isinstance(data, dict):
                return None
            return ProbeResult.from_dict(data)
        except Exception:
            return None

    def _cache_set(self, fi: FileInfo, result: ProbeResult) -> None:
        self._ensure_files()
        conn = self.store._conn()
        payload = json.dumps(result.to_dict(), ensure_ascii=False)
        cur = conn.execute(
            "UPDATE files SET size = ?, mtime_ns = ?, probe_json = ?, seen_at = ? WHERE path = ?",
            (fi.size, fi.mtime_ns, payload, datetime.now().isoformat(), str(fi.path)),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO files (path, size, mtime_ns, fingerprint, probe_json, seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                (str(fi.path), fi.size, fi.mtime_ns, "", payload, datetime.now().isoformat()),
            )
        conn.commit()

    async def probe_file(self, fi: FileInfo) -> ProbeResult | None:
        cached = self._cache_get(fi)
        if cached is not None:
            return cached

        ffprobe = self.config.tools.ffprobe
        if not ffprobe or shutil.which(ffprobe) is None:
            _warn_missing_ffprobe()
            return None

        async with self._semaphore:
            args = [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                long_path(str(fi.path)),
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
            except FileNotFoundError:
                _warn_missing_ffprobe()
                return None
            except OSError:
                return None

            if proc.returncode != 0 or not stdout:
                return None
            try:
                data = json.loads(stdout.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                return None
            if not isinstance(data, dict):
                return None

        result = self._parse(data)
        self._cache_set(fi, result)
        return result

    def _parse(self, data: dict[str, Any]) -> ProbeResult:
        duration: float | None = None
        fmt = data.get("format") or {}
        if isinstance(fmt, dict):
            duration = _to_float(fmt.get("duration"))

        height: int | None = None
        codec: str | None = None
        audio_language: str | None = None

        streams = data.get("streams") or []
        if isinstance(streams, list):
            for stream in streams:
                if not isinstance(stream, dict):
                    continue
                ctype = stream.get("codec_type")
                if ctype == "video" and height is None:
                    raw_height = stream.get("height")
                    if raw_height is not None:
                        try:
                            height = int(raw_height)
                        except Exception:
                            height = None
                    codec = str(stream.get("codec_name") or "") or None
                    if duration is None:
                        duration = _to_float(stream.get("duration"))
                elif ctype == "audio" and audio_language is None:
                    tags = stream.get("tags") or {}
                    if isinstance(tags, dict):
                        lang = tags.get("language")
                        if lang:
                            low = str(lang).lower()
                            if low not in {"und", "undetermined"}:
                                audio_language = low

        return ProbeResult(
            duration=duration, height=height, codec=codec, audio_language=audio_language
        )

    async def enrich_item(
        self, item: SourceItem, audio: AudioVerifier | None
    ) -> float | None:
        if not item.videos:
            return None

        results = await asyncio.gather(*(self.probe_file(fi) for fi in item.videos))
        duration_by_path: dict[Path, float] = {}
        for fi, result in zip(item.videos, results):
            if result is None:
                continue
            if result.height:
                item.video_heights[fi.path] = result.height
            if result.audio_language:
                item.audio_languages[fi.path] = result.audio_language
            if result.duration:
                duration_by_path[fi.path] = result.duration

        if audio is not None:
            for fi in item.videos:
                if fi.path in item.audio_languages:
                    continue
                try:
                    detected = await audio.detect_language(fi.path)
                except Exception:
                    detected = None
                if detected and detected[0]:
                    lang = str(detected[0]).lower()
                    if lang != "und":
                        item.audio_languages[fi.path] = lang

        largest = max(item.videos, key=lambda f: f.size, default=None)
        duration = duration_by_path.get(largest.path) if largest else None
        if duration is None and duration_by_path:
            duration = next(iter(duration_by_path.values()))
        return duration
