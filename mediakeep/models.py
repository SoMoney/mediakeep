from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, Sequence


class MediaKind(StrEnum):
    MOVIE = "movie"
    SHOW = "show"
    DVD = "dvd"
    BLURAY = "bluray"
    COLLECTION = "collection"
    UNKNOWN = "unknown"


class IdStatus(StrEnum):
    AUTO = "auto"
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs_review"
    UNMATCHED = "unmatched"


class ActionKind(StrEnum):
    MKDIR = "mkdir"
    MOVE = "move"
    RMDIR = "rmdir"
    CLEAR_HIDDEN = "clear_hidden"
    WRITE_FILE = "write_file"
    FETCH_FILE = "fetch_file"  # download ``content`` (an https URL) to ``dst``
    SYMLINK = "symlink"


class Severity(StrEnum):
    ATTENTION = "attention"
    NOTE = "note"


@dataclass(slots=True, frozen=True)
class FileInfo:
    path: Path
    size: int
    mtime_ns: int
    hidden: bool = False


@dataclass(slots=True)
class EpisodeRef:
    file: FileInfo
    season: int
    episode: int
    episode_end: int | None
    title: str
    version_tag: int | None
    absolute: bool
    label: str = ""


@dataclass(slots=True)
class SourceItem:
    key: str
    root: Path
    paths: list[Path] = field(default_factory=list)
    kind: MediaKind = MediaKind.UNKNOWN
    title: str = ""
    year: int | None = None
    year_source: str = ""
    videos: list[FileInfo] = field(default_factory=list)
    subtitles: list[FileInfo] = field(default_factory=list)
    sidecars: list[FileInfo] = field(default_factory=list)
    samples: list[FileInfo] = field(default_factory=list)
    extras: list[FileInfo] = field(default_factory=list)
    episodes: list[EpisodeRef] = field(default_factory=list)
    hint_options: dict[str, str] = field(default_factory=dict)
    video_heights: dict[Path, int] = field(default_factory=dict)
    audio_languages: dict[Path, str] = field(default_factory=dict)


@dataclass(slots=True)
class Candidate:
    tmdb_id: int
    kind: MediaKind
    title: str
    original_title: str
    year: int | None
    original_language: str
    runtime_min: int | None
    genres: list[str]
    countries: list[str]
    overview: str
    poster_url: str | None
    imdb_id: str | None
    score: float
    reasons: list[str]


@dataclass(slots=True)
class Identity:
    item_key: str
    kind: MediaKind
    title: str
    year: int | None
    tmdb_id: int | None
    imdb_id: str | None
    status: IdStatus
    confidence: float
    reasons: list[str]
    candidates: list[Candidate] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    original_language: str | None = None
    overview: str = ""
    runtime_min: int | None = None
    episode_titles: dict[tuple[int, int], str] = field(default_factory=dict)
    library: str | None = None


@dataclass(slots=True)
class Action:
    kind: ActionKind
    src: Path | None
    dst: Path | None
    content: str | None = None
    owned: bool = False


@dataclass(slots=True)
class Issue:
    item_key: str | None
    path: Path
    severity: Severity
    message: str
    suggestion: str | None = None


@dataclass(slots=True)
class Batch:
    item_key: str
    label: str
    kind: MediaKind
    actions: list[Action]
    reasons: list[str]
    preconditions: list[FileInfo]
    needs_review: bool


@dataclass(slots=True)
class Plan:
    id: str
    created_at: datetime
    config_hash: str
    items: dict[str, SourceItem]
    identities: dict[str, Identity]
    batches: list[Batch]
    issues: list[Issue]
    duplicates: list[list[str]]
    multi_version: list[str]
    samples: list[Path]
    unchanged: int


@dataclass(slots=True)
class RunRecord:
    id: str
    plan_id: str | None
    kind: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    done: int
    failed: int
    skipped: int
    issues: list[Issue] = field(default_factory=list)


class ProgressSink(Protocol):
    def stage(self, name: str, total: int | None = None) -> None: ...
    def advance(self, n: int = 1, detail: str = "") -> None: ...
    def log(self, message: str) -> None: ...
    def cancelled(self) -> bool: ...


class Identifier(Protocol):
    async def identify(
        self, items: Sequence[SourceItem], progress: ProgressSink
    ) -> dict[str, Identity]: ...


class AudioVerifier(Protocol):
    async def detect_language(self, video: Path) -> tuple[str, float] | None: ...
    async def transcript(self, video: Path) -> str | None: ...


class PlanContributor(Protocol):
    def contribute(
        self,
        item: SourceItem,
        identity: Identity | None,
        target_dir: Path,
        file_base: str,
    ) -> list[Action]: ...


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, tuple):
                key = "|".join(str(x) for x in k)
            elif isinstance(k, Path):
                key = str(k)
            else:
                key = str(k)
            out[key] = _jsonable(v)
        return out
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(x) for x in obj]
    return obj


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _file_info(data: dict[str, Any]) -> FileInfo:
    return FileInfo(
        path=Path(data["path"]),
        size=int(data["size"]),
        mtime_ns=int(data["mtime_ns"]),
        hidden=bool(data.get("hidden", False)),
    )


def _episode_ref(data: dict[str, Any]) -> EpisodeRef:
    return EpisodeRef(
        file=_file_info(data["file"]),
        season=int(data["season"]),
        episode=int(data["episode"]),
        episode_end=data.get("episode_end"),
        title=str(data.get("title", "")),
        version_tag=data.get("version_tag"),
        absolute=bool(data.get("absolute", False)),
        label=str(data.get("label", "")),
    )


def _source_item(data: dict[str, Any]) -> SourceItem:
    return SourceItem(
        key=str(data["key"]),
        root=Path(data["root"]),
        paths=[Path(x) for x in data.get("paths", [])],
        kind=MediaKind(data.get("kind", "unknown")),
        title=str(data.get("title", "")),
        year=data.get("year"),
        year_source=str(data.get("year_source", "")),
        videos=[_file_info(x) for x in data.get("videos", [])],
        subtitles=[_file_info(x) for x in data.get("subtitles", [])],
        sidecars=[_file_info(x) for x in data.get("sidecars", [])],
        samples=[_file_info(x) for x in data.get("samples", [])],
        extras=[_file_info(x) for x in data.get("extras", [])],
        episodes=[_episode_ref(x) for x in data.get("episodes", [])],
        hint_options={str(k): str(v) for k, v in data.get("hint_options", {}).items()},
        video_heights={
            Path(k): int(v) for k, v in data.get("video_heights", {}).items()
        },
        audio_languages={
            Path(k): str(v) for k, v in data.get("audio_languages", {}).items()
        },
    )


def _candidate(data: dict[str, Any]) -> Candidate:
    return Candidate(
        tmdb_id=int(data["tmdb_id"]),
        kind=MediaKind(data.get("kind", "unknown")),
        title=str(data.get("title", "")),
        original_title=str(data.get("original_title", "")),
        year=data.get("year"),
        original_language=str(data.get("original_language", "")),
        runtime_min=data.get("runtime_min"),
        genres=list(data.get("genres", [])),
        countries=list(data.get("countries", [])),
        overview=str(data.get("overview", "")),
        poster_url=data.get("poster_url"),
        imdb_id=data.get("imdb_id"),
        score=float(data.get("score", 0.0)),
        reasons=list(data.get("reasons", [])),
    )


def _identity(data: dict[str, Any]) -> Identity:
    episode_titles: dict[tuple[int, int], str] = {}
    for key, value in data.get("episode_titles", {}).items():
        try:
            s, e = key.split("|", 1)
            episode_titles[(int(s), int(e))] = str(value)
        except ValueError:
            continue
    return Identity(
        item_key=str(data["item_key"]),
        kind=MediaKind(data.get("kind", "unknown")),
        title=str(data.get("title", "")),
        year=data.get("year"),
        tmdb_id=data.get("tmdb_id"),
        imdb_id=data.get("imdb_id"),
        status=IdStatus(data.get("status", "needs_review")),
        confidence=float(data.get("confidence", 0.0)),
        reasons=list(data.get("reasons", [])),
        candidates=[_candidate(x) for x in data.get("candidates", [])],
        genres=list(data.get("genres", [])),
        countries=list(data.get("countries", [])),
        original_language=data.get("original_language"),
        overview=str(data.get("overview", "")),
        runtime_min=data.get("runtime_min"),
        episode_titles=episode_titles,
        library=data.get("library"),
    )


def identity_to_dict(identity: Identity) -> dict[str, Any]:
    return dict(_jsonable(identity))


def identity_from_dict(data: dict[str, Any]) -> Identity:
    return _identity(data)


def _action(data: dict[str, Any]) -> Action:
    return Action(
        kind=ActionKind(data["kind"]),
        src=Path(data["src"]) if data.get("src") else None,
        dst=Path(data["dst"]) if data.get("dst") else None,
        content=data.get("content"),
        owned=bool(data.get("owned", False)),
    )


def _issue(data: dict[str, Any]) -> Issue:
    return Issue(
        item_key=data.get("item_key"),
        path=Path(data["path"]),
        severity=Severity(data.get("severity", "note")),
        message=str(data.get("message", "")),
        suggestion=data.get("suggestion"),
    )


def _batch(data: dict[str, Any]) -> Batch:
    return Batch(
        item_key=str(data["item_key"]),
        label=str(data.get("label", "")),
        kind=MediaKind(data.get("kind", "unknown")),
        actions=[_action(x) for x in data.get("actions", [])],
        reasons=list(data.get("reasons", [])),
        preconditions=[_file_info(x) for x in data.get("preconditions", [])],
        needs_review=bool(data.get("needs_review", False)),
    )


def plan_to_json(plan: Plan) -> str:
    return json.dumps(_jsonable(plan), indent=2, ensure_ascii=False)


def plan_from_json(text: str) -> Plan:
    data = json.loads(text)
    return Plan(
        id=str(data["id"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        config_hash=str(data.get("config_hash", "")),
        items={k: _source_item(v) for k, v in data.get("items", {}).items()},
        identities={k: _identity(v) for k, v in data.get("identities", {}).items()},
        batches=[_batch(x) for x in data.get("batches", [])],
        issues=[_issue(x) for x in data.get("issues", [])],
        duplicates=[[str(x) for x in group] for group in data.get("duplicates", [])],
        multi_version=[str(x) for x in data.get("multi_version", [])],
        samples=[Path(x) for x in data.get("samples", [])],
        unchanged=int(data.get("unchanged", 0)),
    )


def run_record_to_json(run: RunRecord) -> str:
    return json.dumps(_jsonable(run), ensure_ascii=False)


def run_record_from_json(data: dict[str, Any]) -> RunRecord:
    return RunRecord(
        id=str(data["id"]),
        plan_id=data.get("plan_id"),
        kind=str(data.get("kind", "apply")),
        started_at=datetime.fromisoformat(data["started_at"]),
        finished_at=_dt(data.get("finished_at")),
        status=str(data.get("status", "ok")),
        done=int(data.get("done", 0)),
        failed=int(data.get("failed", 0)),
        skipped=int(data.get("skipped", 0)),
        issues=[_issue(x) for x in data.get("issues", [])],
    )
