from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from mediakeep.config import AppConfig
from mediakeep.models import ProgressSink
from mediakeep.store import Store


@dataclass(slots=True)
class Job:
    id: str
    kind: str
    state: str = "queued"
    progress: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    cancel: bool = False


class JobProgress(ProgressSink):
    RECENT = 40

    def __init__(self, manager: JobManager, job: Job) -> None:
        self.manager = manager
        self.job = job
        self._last_persist = 0.0
        self._seq = 0
        self._recent: list[dict[str, Any]] = []

    def _persist(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_persist > 0.2:
            self._last_persist = now
            self.manager._persist(self.job)

    def stage(self, name: str, total: int | None = None) -> None:
        self.job.progress = {
            "stage": name,
            "total": total,
            "current": 0,
            "detail": "",
            "recent": list(self._recent),
        }
        self._persist(True)

    def advance(self, n: int = 1, detail: str = "") -> None:
        current = int(self.job.progress.get("current", 0) or 0)
        self.job.progress["current"] = current + n
        if detail:
            self.job.progress["detail"] = detail
            self._seq += 1
            self._recent.append(
                {"seq": self._seq, "stage": self.job.progress.get("stage", ""), "text": detail}
            )
            del self._recent[: -self.RECENT]
            self.job.progress["recent"] = list(self._recent)
        self._persist(False)

    def log(self, message: str) -> None:
        self.job.messages.append(message)
        if len(self.job.messages) > 200:
            del self.job.messages[: len(self.job.messages) - 200]
        self._persist(True)

    def cancelled(self) -> bool:
        return self.job.cancel


class JobManager:
    def __init__(self, config: AppConfig, store: Store) -> None:
        self.config = config
        self.store = store
        self._lock = threading.RLock()
        self.current: Job | None = None
        self.history: list[Job] = []
        self._thread: threading.Thread | None = None

    def create_job(self, kind: str) -> Job:
        with self._lock:
            if self.current is not None and self.current.state in {"queued", "running"}:
                raise RuntimeError("A job is already running")
            job = Job(id=uuid.uuid4().hex[:12], kind=kind)
            self.current = job
            self.history.append(job)
            self._persist(job)
            return job

    def schedule(self, job: Job, runner: Callable[[ProgressSink], Any]) -> None:
        # Each job gets its own thread (and, for async runners, its own event loop), so it keeps running
        # independently of the HTTP request that started it - under uvicorn and under the test client alike.
        self._thread = threading.Thread(
            target=self._run, args=(job, runner), name=f"mediakeep-job-{job.id}", daemon=True
        )
        self._thread.start()

    def _run(self, job: Job, runner: Callable[[ProgressSink], Any]) -> None:
        progress = JobProgress(self, job)
        try:
            job.state = "running"
            self._persist(job)
            if asyncio.iscoroutinefunction(runner):
                result = asyncio.run(runner(progress))
            else:
                result = runner(progress)
            job.result = result
            job.state = "done"
        except Exception as exc:
            job.error = str(exc)
            job.state = "error"
        finally:
            job.finished_at = datetime.now()
            self._persist(job)

    def cancel(self, job_id: str | None = None) -> None:
        with self._lock:
            if self.current is not None and (job_id is None or self.current.id == job_id):
                self.current.cancel = True

    def current_dict(self) -> dict[str, Any] | None:
        with self._lock:
            job = self.current
            if job is None:
                return None
            return {
                "id": job.id,
                "kind": job.kind,
                "state": job.state,
                "progress": dict(job.progress),
                "messages": list(job.messages),
                "result": job.result,
                "error": job.error,
                "started_at": job.started_at.isoformat(),
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            }

    def _persist(self, job: Job) -> None:
        with self._lock:
            conn = self.store._conn()
            conn.execute(
                """
                INSERT INTO jobs (id, kind, state, progress_json, started_at, finished_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    state=excluded.state,
                    progress_json=excluded.progress_json,
                    started_at=excluded.started_at,
                    finished_at=excluded.finished_at,
                    error=excluded.error
                """,
                (
                    job.id,
                    job.kind,
                    job.state,
                    json.dumps(job.progress),
                    job.started_at.isoformat(),
                    job.finished_at.isoformat() if job.finished_at else None,
                    job.error,
                ),
            )
            conn.commit()
