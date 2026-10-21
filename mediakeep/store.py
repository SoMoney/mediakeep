from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from mediakeep.models import (
    Plan,
    RunRecord,
    plan_from_json,
    plan_to_json,
    run_record_from_json,
    run_record_to_json,
)

_SCHEMA_VERSION = 3


def _norm_identity_path(path: Path | str) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._init_schema()

    @property
    def state_dir(self) -> Path:
        return self.db_path.parent

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
            )
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            version = int(row["v"]) if row is not None and row["v"] is not None else 0
            if version < 1:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS files (
                        path TEXT PRIMARY KEY,
                        size INTEGER,
                        mtime_ns INTEGER,
                        fingerprint TEXT,
                        probe_json TEXT,
                        seen_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS overrides (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        pattern TEXT NOT NULL,
                        name,
                        options TEXT,
                        origin TEXT,
                        created_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS identities (
                        item_key TEXT PRIMARY KEY,
                        json TEXT,
                        status TEXT,
                        updated_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS api_cache (
                        key TEXT PRIMARY KEY,
                        body TEXT,
                        fetched_at TEXT,
                        ttl_s INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS plans (
                        id TEXT PRIMARY KEY,
                        created_at TEXT,
                        json TEXT
                    );
                    CREATE TABLE IF NOT EXISTS review (
                        item_key TEXT PRIMARY KEY,
                        plan_id TEXT,
                        state TEXT,
                        updated_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS decisions (
                        item_key TEXT PRIMARY KEY,
                        state TEXT,
                        updated_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS runs (
                        id TEXT PRIMARY KEY,
                        plan_id TEXT,
                        kind TEXT,
                        started_at TEXT,
                        finished_at TEXT,
                        status TEXT,
                        summary_json TEXT
                    );
                    CREATE TABLE IF NOT EXISTS journal (
                        run_id TEXT,
                        seq INTEGER,
                        kind TEXT,
                        src TEXT,
                        dst TEXT,
                        content_sha256 TEXT,
                        owned INTEGER,
                        status TEXT,
                        error TEXT,
                        PRIMARY KEY (run_id, seq)
                    );
                    CREATE TABLE IF NOT EXISTS owned (
                        path TEXT PRIMARY KEY,
                        run_id TEXT,
                        kind TEXT,
                        sha256 TEXT
                    );
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        kind TEXT,
                        state TEXT,
                        progress_json TEXT,
                        started_at TEXT,
                        finished_at TEXT,
                        error TEXT
                    );
                    INSERT INTO schema_version (version) VALUES (1);
                    """
                )
            if version < 2:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS path_identities (
                        path TEXT PRIMARY KEY,
                        json TEXT,
                        status TEXT,
                        updated_at TEXT
                    );
                    INSERT INTO schema_version (version) VALUES (2);
                    """
                )
            if version < 3:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL,
                        item_key TEXT,
                        plan_id TEXT,
                        payload_json TEXT,
                        source TEXT,
                        added_at TEXT
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS queue_item_unique
                        ON queue (kind, item_key) WHERE item_key IS NOT NULL;
                    INSERT INTO schema_version (version) VALUES (3);
                    """
                )
            conn.commit()

    # ------------------------------------------------------------------ fixes

    def clear_origin_hints(self, origin: str) -> None:
        with self._lock:
            self._conn().execute("DELETE FROM overrides WHERE origin=?", (origin,))
            self._conn().commit()

    def add_hint(
        self, pattern: str, name: str, options: str, origin: str = "ui"
    ) -> int:
        with self._lock:
            cur = self._conn().execute(
                "INSERT INTO overrides (pattern, name, options, origin, created_at) VALUES (?,?,?,?,?)",
                (pattern, name, options, origin, datetime.now().isoformat()),
            )
            self._conn().commit()
            return int(cur.lastrowid or 0)

    def update_hint(
        self, hint_id: int, pattern: str, name: str, options: str
    ) -> None:
        with self._lock:
            self._conn().execute(
                "UPDATE overrides SET pattern=?, name=?, options=?, created_at=? WHERE id=?",
                (pattern, name, options, datetime.now().isoformat(), hint_id),
            )
            self._conn().commit()

    def delete_hint(self, hint_id: int) -> None:
        with self._lock:
            self._conn().execute("DELETE FROM overrides WHERE id=?", (hint_id,))
            self._conn().commit()

    def get_hint(self, hint_id: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn().execute(
            "SELECT * FROM overrides WHERE id=?", (hint_id,)
        ).fetchone()
        return row

    def list_hints(self) -> list[sqlite3.Row]:
        return list(self._conn().execute("SELECT * FROM overrides ORDER BY id"))

    # -------------------------------------------------------------- identities

    def get_identity(self, item_key: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT json FROM identities WHERE item_key=?", (item_key,)
        ).fetchone()
        if row is None:
            return None
        return cast(dict[str, Any], json.loads(row["json"]))

    def set_identity(self, item_key: str, data: dict[str, Any], status: str) -> None:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO identities (item_key, json, status, updated_at) VALUES (?,?,?,?)",
                (item_key, json.dumps(data), status, datetime.now().isoformat()),
            )
            self._conn().commit()

    def set_path_identity(
        self, path: Path, data: dict[str, Any], status: str
    ) -> None:
        key = _norm_identity_path(path)
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO path_identities (path, json, status, updated_at) VALUES (?,?,?,?)",
                (key, json.dumps(data), status, datetime.now().isoformat()),
            )
            self._conn().commit()

    def get_path_identity(self, path: Path) -> dict[str, Any] | None:
        key = _norm_identity_path(path)
        row = self._conn().execute(
            "SELECT json FROM path_identities WHERE path=?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return cast(dict[str, Any], json.loads(row["json"]))
        except Exception:
            return None

    # ------------------------------------------------------------------ plans

    def save_plan(self, plan: Plan) -> None:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO plans (id, created_at, json) VALUES (?,?,?)",
                (plan.id, plan.created_at.isoformat(), plan_to_json(plan)),
            )
            self._conn().commit()

    def get_plan(self, plan_id: str) -> Plan | None:
        row = self._conn().execute(
            "SELECT json FROM plans WHERE id=?", (plan_id,)
        ).fetchone()
        if row is None:
            return None
        return plan_from_json(cast(str, row["json"]))

    def set_review(self, item_key: str, plan_id: str, state: str) -> None:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO review (item_key, plan_id, state, updated_at) VALUES (?,?,?,?)",
                (item_key, plan_id, state, datetime.now().isoformat()),
            )
            self._conn().commit()

    def get_review(self, item_key: str) -> str | None:
        row = self._conn().execute(
            "SELECT state FROM review WHERE item_key=?", (item_key,)
        ).fetchone()
        if row is None:
            return None
        return cast(str, row["state"])

    def set_decision(self, item_key: str, state: str) -> None:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO decisions (item_key, state, updated_at) VALUES (?,?,?)",
                (item_key, state, datetime.now().isoformat()),
            )
            self._conn().commit()

    def get_decision(self, item_key: str) -> str | None:
        row = self._conn().execute(
            "SELECT state FROM decisions WHERE item_key=?", (item_key,)
        ).fetchone()
        if row is None:
            return None
        return cast(str, row["state"])

    # ------------------------------------------------------------------- runs

    def create_run(self, run: RunRecord) -> None:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO runs (id, plan_id, kind, started_at, finished_at, status, summary_json) VALUES (?,?,?,?,?,?,?)",
                (
                    run.id,
                    run.plan_id,
                    run.kind,
                    run.started_at.isoformat(),
                    run.finished_at.isoformat() if run.finished_at else None,
                    run.status,
                    run_record_to_json(run),
                ),
            )
            self._conn().commit()

    def finish_run(self, run: RunRecord) -> None:
        with self._lock:
            self._conn().execute(
                "UPDATE runs SET finished_at=?, status=?, summary_json=? WHERE id=?",
                (
                    run.finished_at.isoformat() if run.finished_at else None,
                    run.status,
                    run_record_to_json(run),
                    run.id,
                ),
            )
            self._conn().commit()

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self._conn().execute(
            "SELECT summary_json FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return run_record_from_json(cast(dict[str, Any], json.loads(row["summary_json"])))

    def list_runs(self, limit: int = 20) -> list[RunRecord]:
        rows = self._conn().execute(
            "SELECT summary_json FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            run_record_from_json(cast(dict[str, Any], json.loads(r["summary_json"])))
            for r in rows
        ]

    def delete_runs(self, run_ids: list[str] | None = None) -> int:
        """Forget runs and their journals (all of them when ``run_ids`` is None).
        Files on disk are not touched; the runs just can't be undone from here any more."""
        with self._lock:
            conn = self._conn()
            if run_ids is None:
                n = int(conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"])
                conn.execute("DELETE FROM journal")
                conn.execute("DELETE FROM runs")
            else:
                n = 0
                for rid in run_ids:
                    conn.execute("DELETE FROM journal WHERE run_id=?", (rid,))
                    n += conn.execute("DELETE FROM runs WHERE id=?", (rid,)).rowcount
            conn.commit()
            return n

    def count_runs(self) -> int:
        row = self._conn().execute("SELECT COUNT(*) AS n FROM runs").fetchone()
        return int(row["n"]) if row is not None else 0

    def add_journal(
        self,
        run_id: str,
        seq: int,
        kind: str,
        src: str | None,
        dst: str | None,
        content_sha256: str | None,
        owned: bool,
        status: str,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._conn().execute(
                """
                INSERT OR REPLACE INTO journal
                (run_id, seq, kind, src, dst, content_sha256, owned, status, error)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    seq,
                    kind,
                    src,
                    dst,
                    content_sha256,
                    1 if owned else 0,
                    status,
                    error,
                ),
            )
            self._conn().commit()

    def update_journal(
        self, run_id: str, seq: int, status: str, error: str | None = None
    ) -> None:
        with self._lock:
            self._conn().execute(
                "UPDATE journal SET status=?, error=? WHERE run_id=? AND seq=?",
                (status, error, run_id, seq),
            )
            self._conn().commit()

    def get_journal(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM journal WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
        return [cast(dict[str, Any], dict(r)) for r in rows]

    def add_owned(
        self, path: Path, run_id: str, kind: str, sha256: str | None
    ) -> None:
        with self._lock:
            self._conn().execute(
                "INSERT OR REPLACE INTO owned (path, run_id, kind, sha256) VALUES (?,?,?,?)",
                (str(path), run_id, kind, sha256),
            )
            self._conn().commit()

    def list_owned(self) -> list[dict[str, Any]]:
        rows = self._conn().execute("SELECT * FROM owned").fetchall()
        return [cast(dict[str, Any], dict(r)) for r in rows]

    def remove_owned(self, path: Path) -> None:
        with self._lock:
            self._conn().execute("DELETE FROM owned WHERE path=?", (str(path),))
            self._conn().commit()

    def is_owned(self, path: Path) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM owned WHERE path=?", (str(path),)
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ queue

    def queue_add(
        self,
        kind: str,
        item_key: str | None = None,
        plan_id: str | None = None,
        payload: dict[str, Any] | None = None,
        source: str = "",
    ) -> int:
        """Add an entry. An item that is already queued is updated in place."""
        body = json.dumps(payload or {})
        now = datetime.now().isoformat()
        with self._lock:
            conn = self._conn()
            if item_key:
                row = conn.execute(
                    "SELECT id FROM queue WHERE kind=? AND item_key=?", (kind, item_key)
                ).fetchone()
                if row is not None:
                    conn.execute(
                        "UPDATE queue SET plan_id=?, payload_json=?, source=?, added_at=? WHERE id=?",
                        (plan_id, body, source, now, int(row["id"])),
                    )
                    conn.commit()
                    return int(row["id"])
            cur = conn.execute(
                "INSERT INTO queue (kind, item_key, plan_id, payload_json, source, added_at)"
                " VALUES (?,?,?,?,?,?)",
                (kind, item_key, plan_id, body, source, now),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def queue_list(self) -> list[dict[str, Any]]:
        rows = self._conn().execute("SELECT * FROM queue ORDER BY id").fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            data = cast(dict[str, Any], dict(row))
            try:
                data["payload"] = json.loads(str(data.get("payload_json") or "{}"))
            except Exception:
                data["payload"] = {}
            out.append(data)
        return out

    def queue_get(self, queue_id: int) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM queue WHERE id=?", (queue_id,)
        ).fetchone()
        if row is None:
            return None
        data = cast(dict[str, Any], dict(row))
        try:
            data["payload"] = json.loads(str(data.get("payload_json") or "{}"))
        except Exception:
            data["payload"] = {}
        return data

    def queue_remove(self, queue_ids: list[int]) -> int:
        if not queue_ids:
            return 0
        with self._lock:
            conn = self._conn()
            marks = ",".join("?" for _ in queue_ids)
            cur = conn.execute(f"DELETE FROM queue WHERE id IN ({marks})", queue_ids)
            conn.commit()
            return int(cur.rowcount or 0)
