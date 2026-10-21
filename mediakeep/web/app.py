from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
import secrets
import shutil
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mediakeep.classify import VIDEO_EXTS
from mediakeep.config import (
    AppConfig,
    ConfigError,
    config_warnings,
    default_config_path,
    load_config,
)
from mediakeep.agree import KEPT, chosen_by_user
from mediakeep.destination import library_for, source_for, wants_in_place
from mediakeep.hints import import_hints_file, options_to_text, parse_options, upsert_hint
from mediakeep.identify import identity_for, search
from mediakeep.models import (
    Identity,
    IdStatus,
    MediaKind,
    Plan,
    ProgressSink,
    SourceItem,
    identity_to_dict,
)
from mediakeep.parse import parse_release_name
from mediakeep.pipeline import apply_plan, build_plan, purge, replan_item, undo_run
from mediakeep.report import run_summary
from mediakeep.store import Store
from mediakeep import workspace
from mediakeep.web import planner
from mediakeep.memo import render_cache
from mediakeep.web.jobs import JobManager

SESSION_COOKIE = "mediakeep_session"
CSRF_COOKIE = "mediakeep_csrf"
_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


class NullProgress(ProgressSink):
    def stage(self, name: str, total: int | None = None) -> None:
        return None

    def advance(self, n: int = 1, detail: str = "") -> None:
        return None

    def log(self, message: str) -> None:
        return None

    def cancelled(self) -> bool:
        return False


def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1", "[::1]"}


def _norm_path(path: Path | str) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


def _is_under(child: Path | str, parent: Path | str) -> bool:
    c = _norm_path(child)
    p = _norm_path(parent)
    if not c or not p:
        return False
    return c == p or c.startswith(p + "/")


def _abs_path(path: Path | str) -> str:
    return os.path.abspath(str(path))


def _empty_plan() -> Plan:
    return planner._empty_plan()


WORKING_FOLDER_KEY = "ui:working_folder"


def _norm_text(p: Path | str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))


def _source_allowed(config: AppConfig, path: Path) -> bool:
    return any(_is_under(path, root) for root in config.sources)


def _path_allowed(config: AppConfig, plan: Plan | None, path: Path) -> bool:
    for root in config.root_paths:
        if _is_under(path, root):
            return True
    if plan is None:
        return False
    for item in plan.items.values():
        for p in item.paths:
            if path == p or _is_under(path, p) or _is_under(p, path):
                return True
    for batch in plan.batches:
        for action in batch.actions:
            if action.dst is not None and (path == action.dst or _is_under(path, action.dst.parent)):
                return True
            if action.src is not None and (path == action.src or _is_under(path, action.src.parent)):
                return True
    return False


def _latest_plan_id(store: Store) -> str | None:
    try:
        conn = store._conn()
        row = conn.execute("SELECT id FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()
        return str(row[0]) if row is not None else None
    except Exception:
        return None


def _current_plan(app: FastAPI) -> Plan | None:
    store: Store = app.state.store
    plan_id = app.state.plan_id
    if plan_id:
        plan = store.get_plan(plan_id)
        if plan is not None:
            return plan
    plan_id = _latest_plan_id(store)
    if plan_id:
        app.state.plan_id = plan_id
        return store.get_plan(plan_id)
    return None


def _default_inbox_folder(config: AppConfig, requested: str | None) -> Path:
    if requested:
        p = Path(requested).expanduser()
        if not p.is_absolute():
            p = p.absolute()
        if _source_allowed(config, p):
            return p
    return config.sources[0]


def _use_config(app: FastAPI, saved: AppConfig) -> AppConfig:
    """Use new saved settings: keep them for saving, and run with the working view
    (one folder at a time) derived from them."""
    app.state.base_config = saved
    config = workspace.effective(saved)
    app.state.config = config
    app.state.jobs.config = config
    return config


def _save_config(app: FastAPI, saved: AppConfig) -> AppConfig:
    """Save settings to config.yaml (the previous file kept as config.yaml.bak), then use them."""
    config_path: Path = app.state.config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        shutil.copy2(config_path, config_path.with_name(config_path.name + ".bak"))
    saved.save(config_path)
    return _use_config(app, load_config(config_path))


_PACKAGE_DIR = Path(__file__).resolve().parents[1]


def _code_mtime() -> float:
    """Newest change to mediakeep's own Python files."""
    newest = 0.0
    try:
        for p in _PACKAGE_DIR.rglob("*.py"):
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return newest


def _code_changed(app: FastAPI) -> bool:
    """True when mediakeep's code changed after this web app started: the running app is
    still the old version until it is restarted. Checked at most every 10 seconds."""
    now = time.monotonic()
    last, value = getattr(app.state, "code_checked", (float("-inf"), False))
    if now - last < 10:
        return bool(value)
    value = _code_mtime() > float(getattr(app.state, "code_mtime", 0.0)) + 1.0
    app.state.code_checked = (now, value)
    return bool(value)


def _page(
    request: Request,
    name: str,
    active: str,
    context: dict[str, Any] | None = None,
) -> HTMLResponse:
    app = request.app
    config: AppConfig = app.state.config
    store: Store = app.state.store
    plan = _current_plan(app)
    ctx = dict(context or {})
    ctx["nav"] = planner.nav_counts(config, store, plan, active)
    ctx["code_changed"] = _code_changed(app)
    templates: Jinja2Templates = app.state.templates
    return templates.TemplateResponse(request, name, ctx)


def _register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def render_cache_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Reading a page asks the same questions many times; answer each once per request.
        # Only for reads: a request that changes something must always see fresh answers.
        if request.method != "GET":
            return await call_next(request)
        with render_cache():
            return await call_next(request)

    @app.middleware("http")
    async def security_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        csrf = request.cookies.get(CSRF_COOKIE)
        new_csrf = False
        if not csrf:
            csrf = secrets.token_urlsafe(32)
            new_csrf = True

        path = request.url.path
        if (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and path.startswith("/api")
            and path != "/api/login"
        ):
            supplied = request.headers.get("X-CSRF-Token") or request.query_params.get("csrf")
            if not supplied or supplied != csrf:
                response: Response = JSONResponse(
                    {"detail": "CSRF token missing or invalid"}, status_code=403
                )
                if new_csrf:
                    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="strict", path="/")
                return response

        access = request.app.state.config.web.access_token
        if access:
            public = (
                path == "/login"
                or path == "/api/login"
                or path == "/api/health"
                or path.startswith("/static")
                or path == "/favicon.ico"
            )
            session = request.cookies.get(SESSION_COOKIE)
            if not public and session != access:
                if path.startswith("/api"):
                    response = JSONResponse({"detail": "login required"}, status_code=401)
                else:
                    response = RedirectResponse("/login", status_code=303)
                if new_csrf:
                    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="strict", path="/")
                return response

        response = await call_next(request)
        if new_csrf:
            response.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="strict", path="/")
        return response


def _start_scan_job(app: FastAPI) -> str | None:
    try:
        job = app.state.jobs.create_job("scan")
    except RuntimeError:
        return None

    async def runner(progress: ProgressSink) -> dict[str, Any]:
        plan = await build_plan(app.state.config, app.state.store, progress, None, None)
        app.state.plan_id = plan.id
        return {"plan_id": plan.id}

    app.state.jobs.schedule(job, runner)
    return str(job.id)


async def _execute_job(app: FastAPI, ids: list[int], progress: ProgressSink) -> dict[str, Any]:
    config: AppConfig = app.state.config
    store: Store = app.state.store
    entries = [e for e in (store.queue_get(i) for i in ids) if e is not None]
    items = [e for e in entries if str(e.get("kind") or "") == "item"]

    plan = _current_plan(app)
    if plan is None:
        plan = await build_plan(config, store, progress, None, None)
        app.state.plan_id = plan.id

    item_keys = [str(e.get("item_key")) for e in items if e.get("item_key")]
    run = await asyncio.to_thread(apply_plan, config, store, plan, item_keys, progress)
    store.queue_remove([int(e["id"]) for e in items])
    summary = run_summary(run, plan, config, store)
    return {"run": summary, "run_id": run.id, "applied": len(item_keys)}


def _json_body_data(data: Any) -> dict[str, Any]:
    return data if isinstance(data, dict) else {}


async def _json_body(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            return _json_body_data(await request.json())
        except Exception:
            return {}
    try:
        form = await request.form()
        return {str(k): v for k, v in form.items()}
    except Exception:
        return {}


def _queue_item(
    app: FastAPI, key: str, source: str, plan: Plan | None = None, entry: bool = True
) -> dict[str, Any]:
    config: AppConfig = app.state.config
    store: Store = app.state.store
    if plan is None:
        plan = _current_plan(app)
    c = planner.card(config, store, plan, key)
    if c is None:
        raise HTTPException(status_code=404, detail="item is not in the current plan")

    item = plan.items.get(key) if plan is not None else None
    if (
        item is not None
        and wants_in_place(config, store, item)
        and not source.startswith(("library:", "in_place:"))
    ):
        source = f"in_place:{planner.in_place_parent(item)}"

    payload = {
        "item_key": key,
        "label": c["label"],
        "kind": c["kind"],
        "target": c["target"] or c["current_path"],
        "current_path": c["current_path"],
        "file_count": c["file_count"],
    }
    qid = store.queue_add("item", key, plan.id if plan is not None else None, payload, source)
    if not entry:
        return {"id": qid}
    row = store.queue_get(qid)
    if row is None:
        raise HTTPException(status_code=500, detail="queue entry missing")
    return row


async def _item_and_plan(request: Request, item_key: str) -> tuple[Plan, SourceItem]:
    plan = _current_plan(request.app)
    if plan is None:
        raise HTTPException(status_code=409, detail="scan a folder first")
    item = plan.items.get(item_key)
    if item is None:
        raise HTTPException(status_code=404, detail="item is not in the current plan")
    return plan, item


def _remember(config: AppConfig, data: dict[str, Any]) -> bool:
    """Save a remembered fix for this choice? The page's "Remember this choice" box
    decides; without it, the Settings default (off unless you turn it on)."""
    raw = data.get("remember")
    if raw is None:
        return bool(config.behavior.remember_choices)
    return bool(raw)


def _keep_known_poster(identity: Identity, previous: Identity | None, tmdb_id: int) -> None:
    """The chosen title keeps the poster its suggestion showed, even if TMDB's details
    lookup didn't return one (the poster is what folder.jpg is made from)."""
    if any(c.tmdb_id == tmdb_id and c.poster_url for c in identity.candidates):
        return
    known = next(
        (c for c in (previous.candidates if previous is not None else []) if c.tmdb_id == tmdb_id and c.poster_url),
        None,
    )
    if known is None:
        return
    for c in identity.candidates:
        if c.tmdb_id == tmdb_id:
            c.poster_url = known.poster_url
            return
    identity.candidates = [replace(known)] + list(identity.candidates)


def _register_routes(app: FastAPI) -> None:
    @app.get("/api/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/login")
    async def login(request: Request) -> JSONResponse:
        data = await _json_body(request)
        config = request.app.state.config
        if not config.web.access_token or data.get("token") != config.web.access_token:
            raise HTTPException(status_code=401, detail="invalid token")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            SESSION_COOKIE,
            config.web.access_token,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/logout")
    async def logout() -> JSONResponse:
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/browse")
    async def browse(request: Request, path: str | None = None) -> dict[str, Any]:
        if not path:
            raise HTTPException(status_code=422, detail="path is required")
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = p.absolute()
        if not _source_allowed(request.app.state.config, p):
            raise HTTPException(status_code=403, detail="that path is not inside the working folder")
        if not p.exists():
            raise HTTPException(status_code=404, detail="path does not exist")
        return planner.inbox_rows(
            request.app.state.config,
            request.app.state.store,
            _current_plan(request.app),
            p,
        )

    @app.get("/api/queue")
    async def queue_list(request: Request) -> list[dict[str, Any]]:
        store = request.app.state.store
        planner.prune_queue(request.app.state.config, store, _current_plan(request.app))
        rows: list[dict[str, Any]] = store.queue_list()
        return rows

    @app.post("/api/queue")
    async def queue_add(request: Request) -> dict[str, Any]:
        data = await _json_body(request)
        kind = str(data.get("kind") or "")

        if kind == "item":
            key = str(data.get("item_key") or "")
            source = str(data.get("source") or "inbox")
            return _queue_item(request.app, key, source)

        if kind == "items":
            # Many at once, so a big selection doesn't take one round trip per item.
            keys = data.get("item_keys")
            if not isinstance(keys, list):
                raise HTTPException(status_code=422, detail="item_keys must be a list")
            source = str(data.get("source") or "inbox")
            # The plan is read once for the whole batch (reading it is the slow part).
            plan = _current_plan(request.app)
            if plan is None:
                raise HTTPException(status_code=409, detail="scan a folder first")
            added = 0
            missing = 0
            for raw_key in keys:
                try:
                    _queue_item(request.app, str(raw_key), source, plan=plan, entry=False)
                    added += 1
                except HTTPException:
                    missing += 1
            return {"ok": True, "added": added, "missing": missing}

        raise HTTPException(status_code=422, detail="kind must be item or items")

    @app.post("/api/tmdb/test")
    async def tmdb_test(request: Request) -> dict[str, Any]:
        """Is the TMDB key in Settings accepted? Asks TMDB once, without the cache."""
        from mediakeep.identify.tmdb import TmdbClient, TmdbError, TmdbUnavailable

        config: AppConfig = request.app.state.config
        if not (config.tmdb.api_key or config.tmdb.read_token):
            return {"ok": False, "message": "No TMDB key yet. Paste it in the box first."}
        client = TmdbClient(config, request.app.state.store)
        try:
            await client._request("/configuration", cache=False)
        except TmdbUnavailable:
            return {"ok": False, "message": "Couldn't reach TMDB. Check the internet connection."}
        except TmdbError as exc:
            text = str(exc)
            if "401" in text:
                return {"ok": False, "message": "TMDB refused this key. Check it was copied in full."}
            return {"ok": False, "message": f"TMDB didn't answer as expected ({text})."}
        except Exception as exc:  # network down, DNS, proxy...
            return {"ok": False, "message": f"Couldn't reach TMDB ({exc.__class__.__name__})."}
        finally:
            await client.close()
        return {"ok": True, "message": "The key works."}

    @app.post("/api/workspace")
    async def workspace_set(request: Request) -> dict[str, Any]:
        """Choose the folder to work in and where its items go. Switching to another folder
        starts it on "Rename here", so a destination never carries over by accident."""
        data = await _json_body(request)
        saved: AppConfig = request.app.state.base_config
        current = saved.workspace
        folder = current.folder
        destination = current.destination

        if "folder" in data:
            raw = str(data.get("folder") or "").strip()
            if not raw:
                raise HTTPException(status_code=422, detail="Choose a folder to work in.")
            new_folder = Path(raw).expanduser()
            if not new_folder.is_dir():
                raise HTTPException(status_code=422, detail=f"{new_folder} isn't a folder that exists.")
            if folder is None or _norm_text(new_folder) != _norm_text(folder):
                destination = None
            folder = new_folder

        if "destination" in data:
            raw = str(data.get("destination") or "").strip()
            destination = Path(raw).expanduser() if raw else None

        if folder is None:
            raise HTTPException(status_code=422, detail="Choose a folder to work in first.")
        if destination is not None:
            if not destination.is_dir():
                raise HTTPException(status_code=422, detail=f"{destination} isn't a folder that exists.")
            if _norm_text(destination) == _norm_text(folder):
                destination = None
            elif _is_under(destination, folder):
                raise HTTPException(
                    status_code=422,
                    detail="The destination can't be inside the folder you're working in.",
                )
            elif _is_under(folder, destination):
                raise HTTPException(
                    status_code=422,
                    detail="The destination can't contain the folder you're working in.",
                )

        try:
            _save_config(request.app, workspace.with_workspace(saved, folder, destination))
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        request.app.state.store.set_decision(WORKING_FOLDER_KEY, str(folder))
        return {
            "ok": True,
            "folder": str(folder),
            "destination": str(destination) if destination is not None else "",
            "rescan": True,
        }

    @app.post("/api/workspace/forget")
    async def workspace_forget(request: Request) -> dict[str, Any]:
        """Take a folder off the recent list (the folder itself is untouched)."""
        data = await _json_body(request)
        target = _norm_text(str(data.get("path") or ""))
        saved: AppConfig = request.app.state.base_config
        recent = [p for p in saved.workspace.recent if _norm_text(p) != target]
        ws = saved.workspace.model_copy(update={"recent": recent})
        _save_config(request.app, saved.model_copy(update={"workspace": ws}))
        return {"ok": True, "recent": [str(p) for p in recent]}

    @app.post("/api/settings/apply")
    async def settings_apply(request: Request) -> dict[str, Any]:
        """Change one setting now and save config.yaml (the previous file is kept as
        config.yaml.bak). Settings never touch media files, so they don't wait on Execute."""
        data = await _json_body(request)
        payload = data.get("payload")
        if not isinstance(payload, dict) or not payload.get("path"):
            raise HTTPException(status_code=422, detail="payload.path is required")
        path = str(payload["path"])
        try:
            new_config = planner.apply_setting(request.app.state.base_config, path, payload.get("value"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            _save_config(request.app, new_config)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "ok": True,
            "label": str(payload.get("label") or path),
            # New names: the plan must be rebuilt.
            "rescan": path.startswith("naming."),
        }

    @app.delete("/api/runs")
    async def runs_clear(request: Request) -> dict[str, Any]:
        data = await _json_body(request)
        ids = data.get("ids")
        store = request.app.state.store
        if ids is None:
            n = store.delete_runs(None)
        elif isinstance(ids, list):
            n = store.delete_runs([str(i) for i in ids])
        else:
            raise HTTPException(status_code=422, detail="ids must be a list")
        return {"ok": True, "removed": n}

    @app.delete("/api/queue")
    async def queue_delete(request: Request) -> dict[str, Any]:
        data = await _json_body(request)
        ids = data.get("ids")
        if not isinstance(ids, list):
            qid = request.query_params.get("id")
            ids = [qid] if qid else []
        try:
            clean = [int(x) for x in ids]
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="ids must be integers") from exc
        removed = request.app.state.store.queue_remove(clean)
        return {"ok": True, "removed": removed}

    @app.get("/api/execute")
    async def execute_view(request: Request) -> dict[str, Any]:
        config = request.app.state.config
        store = request.app.state.store
        plan = _current_plan(request.app)
        resolved = planner.resolve_queue(config, store, plan)
        if resolved["dropped"]:
            store.queue_remove([int(d["queue_id"]) for d in resolved["dropped"]])
        return resolved

    @app.post("/api/jobs/execute")
    async def job_execute(request: Request) -> dict[str, str]:
        data = await _json_body(request)
        ids: list[int] = []
        for x in data.get("ids") or []:
            try:
                ids.append(int(x))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail="ids must be integers") from exc
        if not ids:
            raise HTTPException(status_code=422, detail="ids are required")

        manager = request.app.state.jobs
        try:
            job = manager.create_job("execute")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        async def runner(progress: ProgressSink) -> dict[str, Any]:
            return await _execute_job(request.app, ids, progress)

        manager.schedule(job, runner)
        return {"id": job.id}

    @app.get("/api/match/{item_key}")
    async def match_get(request: Request, item_key: str) -> dict[str, Any]:
        plan = _current_plan(request.app)
        if plan is None:
            raise HTTPException(status_code=409, detail="scan a folder first")
        if item_key not in plan.items:
            raise HTTPException(status_code=404, detail="item is not in the current plan")
        return planner.match_view(request.app.state.config, request.app.state.store, plan, item_key)

    @app.post("/api/match/{item_key}/preview")
    async def match_preview(request: Request, item_key: str) -> dict[str, Any]:
        plan = _current_plan(request.app)
        if plan is None:
            raise HTTPException(status_code=409, detail="scan a folder first")
        if item_key not in plan.items:
            raise HTTPException(status_code=404, detail="item is not in the current plan")
        data = await _json_body(request)
        return planner.match_preview(
            request.app.state.config, request.app.state.store, plan, item_key, data
        )

    @app.post("/api/match/{item_key}/search")
    async def match_search(request: Request, item_key: str) -> dict[str, Any]:
        config = request.app.state.config
        store = request.app.state.store
        plan, item = await _item_and_plan(request, item_key)
        data = await _json_body(request)
        query = str(data.get("query") or item.title or "")
        raw_year = data.get("year")
        year = int(raw_year) if raw_year not in (None, "") else None
        kind = None
        if data.get("kind"):
            try:
                kind = MediaKind(str(data["kind"]))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="invalid kind") from exc
        candidates = await search(config, store, query, year, kind)
        return {
            "candidates": [
                planner.candidate_dict(c, config.web.posters) for c in candidates[:10]
            ]
        }

    @app.post("/api/match/{item_key}/choose")
    async def match_choose(request: Request, item_key: str) -> dict[str, Any]:
        config = request.app.state.config
        store = request.app.state.store
        plan, item = await _item_and_plan(request, item_key)
        data = await _json_body(request)

        try:
            tmdb_id = int(data["tmdb_id"])
        except Exception as exc:
            raise HTTPException(status_code=422, detail="tmdb_id is required") from exc

        try:
            kind = MediaKind(str(data.get("kind") or item.kind))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid kind") from exc

        identity = await identity_for(config, store, item, tmdb_id, kind)
        identity.item_key = item.key
        _keep_known_poster(identity, plan.identities.get(item.key), tmdb_id)

        if data.get("title"):
            identity.title = str(data["title"])
        if data.get("use_short_title"):
            identity.title = planner.main_title(identity.title)

        if data.get("library") and str(data["library"]) in config.libraries:
            identity.library = str(data["library"])
            identity.reasons = list(identity.reasons) + ["library chosen by user"]

        identity.status = IdStatus.CONFIRMED
        identity.confidence = 100.0
        identity.reasons = list(identity.reasons) + ["match chosen by user"]
        if str(data.get("source") or "match") == "match":
            # Picked from the full list on the match page: deliberate, even when TMDB's
            # title differs from the name on disk ("The Lagoon" is "Fin Fighters").
            identity.reasons.append(KEPT)

        store.set_identity(identity.item_key, identity_to_dict(identity), identity.status.value)
        plan.identities[item.key] = identity
        plan = await replan_item(config, store, plan, item.key, NullProgress())

        if _remember(config, data):
            pattern = item.title or (item.paths[0].name if item.paths else "unknown")
            pattern = pattern.replace("/", " ").strip()
            options: dict[str, str] = {"kind": kind.value}
            if identity.year is not None:
                options["year"] = str(identity.year)
            options["tmdb"] = str(tmdb_id)
            if item.year is not None:
                # The year in the name this fix was made for, so it never catches a
                # same-titled film from another year.
                options["name_year"] = str(item.year)
            upsert_hint(store, pattern, identity.title, options)

        c = planner.card(config, store, plan, item.key)
        # Only add it to Execute when there is something to do; an item that is already
        # named right (and has its poster) is simply confirmed.
        entry = None
        doubtful = bool(c is not None and c.get("doubtful_pick"))
        if c is not None and c.get("has_changes") and not doubtful:
            entry = _queue_item(request.app, item.key, str(data.get("source") or "match"))
        if entry is not None:
            message = "Added to Execute. Nothing has changed on disk yet."
        elif doubtful:
            message = (
                "Match confirmed, but it doesn't look like the name on disk, so it stays on "
                "Needs you. Keep it there if it's right."
            )
        else:
            message = "Match confirmed. It's already named right, so there is nothing to add to Execute."
        return {
            "ok": True,
            "queue": entry,
            "queued": entry is not None,
            "message": message,
            "card": c,
        }

    @app.post("/api/item/{item_key}/keep_match")
    async def item_keep_match(request: Request, item_key: str) -> dict[str, Any]:
        """Keep a match you picked even though the folder name says otherwise (an
        alternative title): it stops being questioned."""
        config = request.app.state.config
        store = request.app.state.store
        plan, item = await _item_and_plan(request, item_key)
        identity = plan.identities.get(item.key)
        if identity is None or not identity.tmdb_id:
            raise HTTPException(status_code=409, detail="there is no match to keep")
        if KEPT not in identity.reasons:
            identity.reasons = list(identity.reasons) + [KEPT]
        if not chosen_by_user(identity):
            identity.reasons = list(identity.reasons) + ["match chosen by user"]
        identity.status = IdStatus.CONFIRMED
        identity.confidence = 100.0
        store.set_identity(item.key, identity_to_dict(identity), identity.status.value)
        plan.identities[item.key] = identity
        plan = await replan_item(config, store, plan, item.key, NullProgress())
        return {"ok": True, "card": planner.card(config, store, plan, item.key)}

    @app.post("/api/item/{item_key}/skip")
    async def item_skip(request: Request, item_key: str) -> dict[str, Any]:
        config = request.app.state.config
        store = request.app.state.store
        plan, item = await _item_and_plan(request, item_key)
        store.set_review(item_key, plan.id, "skipped")
        store.set_decision(item_key, "skipped")
        plan = await replan_item(config, store, plan, item.key, NullProgress())
        card = planner.card(config, store, plan, item_key)
        return card or {"ok": True}

    @app.post("/api/item/{item_key}/in_place")
    async def item_in_place(request: Request, item_key: str) -> dict[str, Any]:
        config = request.app.state.config
        store = request.app.state.store
        plan, item = await _item_and_plan(request, item_key)
        data = await _json_body(request)
        on = bool(data.get("on"))

        # The item's own choice; it wins over its folder's destination.
        store.set_decision(item_key, "in_place" if on else "file")

        plan = await replan_item(config, store, plan, item_key, NullProgress())

        queued = [e for e in store.queue_list() if e.get("item_key") == item_key]
        if queued:
            source = str(queued[0].get("source") or "inbox")
            store.queue_remove([int(e["id"]) for e in queued])
            _queue_item(request.app, item_key, source)

        row = planner.inbox_row_for_item(config, store, plan, item_key)
        if row is None:
            card = planner.card(config, store, plan, item_key)
            return card or {"ok": True}
        row["ok"] = True
        return row

    @app.post("/api/item/{item_key}/title_override")
    async def item_title_override(request: Request, item_key: str) -> dict[str, Any]:
        config = request.app.state.config
        store = request.app.state.store
        plan, item = await _item_and_plan(request, item_key)
        identity = plan.identities.get(item_key)
        if identity is None:
            raise HTTPException(status_code=409, detail="this item has no match to override")

        data = await _json_body(request)
        choice = str(data.get("choice") or "")
        title = str(data.get("title") or "")

        if choice == "tmdb" and identity.candidates:
            candidate = next(
                (c for c in identity.candidates if c.tmdb_id == identity.tmdb_id),
                identity.candidates[0],
            )
            title = candidate.title
        elif choice == "keep":
            title = item.title or identity.title

        if not title:
            raise HTTPException(status_code=422, detail="title is required")

        identity.title = title
        identity.status = IdStatus.CONFIRMED
        identity.confidence = 100.0
        # Choosing the title after seeing TMDB's is accepting this match: keep it even when
        # TMDB's title differs from the name on disk ("The Moth Queen" / "Lantern Folk and …").
        identity.reasons = [
            r for r in identity.reasons if r not in ("title chosen by user", KEPT)
        ] + ["title chosen by user", KEPT]

        store.set_identity(identity.item_key, identity_to_dict(identity), identity.status.value)
        plan.identities[item_key] = identity
        plan = await replan_item(config, store, plan, item_key, NullProgress())
        c = planner.card(config, store, plan, item_key)
        if c is None:
            return {"ok": True}

        # A rename to do goes straight to Execute (replacing an earlier entry for the item).
        queued = [e for e in store.queue_list() if e.get("item_key") == item_key]
        if queued:
            store.queue_remove([int(e["id"]) for e in queued])
        entry = None
        if c.get("has_changes") and not c.get("needs"):
            lib = library_for(config, c.get("current_path") or "")
            entry = _queue_item(request.app, item_key, f"library:{lib}" if lib else "inbox")
        c["queued"] = entry is not None
        c["message"] = (
            "Added to Execute. Nothing has changed on disk yet."
            if entry is not None
            else "Saved. The name on disk already matches, so there is nothing to add to Execute."
        )
        return c

    @app.post("/api/item/{item_key}/edit")
    async def item_edit(request: Request, item_key: str) -> dict[str, Any]:
        config = request.app.state.config
        store = request.app.state.store
        plan, item = await _item_and_plan(request, item_key)
        data = await _json_body(request)

        title = str(data.get("title") or item.title or "Untitled")
        raw_year = data.get("year")
        year = int(raw_year) if raw_year not in (None, "") else None
        try:
            kind = MediaKind(str(data.get("kind") or item.kind))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid kind") from exc

        identity = plan.identities.get(item.key) or Identity(
            item_key=item.key,
            kind=kind,
            title=title,
            year=year,
            tmdb_id=None,
            imdb_id=None,
            status=IdStatus.CONFIRMED,
            confidence=100.0,
            reasons=[],
        )
        identity.item_key = item.key
        identity.kind = kind
        identity.title = title
        identity.year = year
        identity.status = IdStatus.CONFIRMED
        identity.confidence = 100.0
        identity.reasons = [
            r for r in identity.reasons if r not in {"manual edit", "title chosen by user"}
        ] + ["manual edit", "title chosen by user"]

        store.set_identity(identity.item_key, identity_to_dict(identity), identity.status.value)

        pattern = title or (item.paths[0].name if item.paths else "unknown")
        pattern = pattern.replace("/", " ").strip()
        options: dict[str, str] = {"kind": kind.value}
        if year is not None:
            options["year"] = str(year)
            if item.year is not None:
                options["name_year"] = str(item.year)
        if _remember(config, data):
            upsert_hint(store, pattern, title, options)

        plan.identities[item.key] = identity
        plan = await replan_item(config, store, plan, item.key, NullProgress())
        return planner.card(config, store, plan, item.key) or {"ok": True}

    @app.post("/api/item/{item_key}/kind")
    async def item_kind(request: Request, item_key: str) -> dict[str, Any]:
        config = request.app.state.config
        store = request.app.state.store
        plan, item = await _item_and_plan(request, item_key)
        data = await _json_body(request)
        try:
            kind = MediaKind(str(data.get("kind") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid kind") from exc

        identity = plan.identities.get(item.key) or Identity(
            item_key=item.key,
            kind=kind,
            title=item.title,
            year=item.year,
            tmdb_id=None,
            imdb_id=None,
            status=IdStatus.CONFIRMED,
            confidence=100.0,
            reasons=[],
        )
        identity.item_key = item.key
        identity.kind = kind
        identity.status = IdStatus.CONFIRMED
        identity.confidence = 100.0
        identity.reasons = [
            r for r in identity.reasons if r != "kind chosen by user"
        ] + ["kind chosen by user"]

        store.set_identity(identity.item_key, identity_to_dict(identity), identity.status.value)
        if item.paths and _remember(config, data):
            pattern = item.paths[0].name
            upsert_hint(store, pattern, identity.title or item.title, {"kind": kind.value})

        plan.identities[item.key] = identity
        plan = await replan_item(config, store, plan, item.key, NullProgress())
        return planner.card(config, store, plan, item_key) or {"ok": True}

    @app.post("/api/item/{item_key}/search")
    async def item_search(request: Request, item_key: str) -> dict[str, Any]:
        config = request.app.state.config
        store = request.app.state.store
        _plan, item = await _item_and_plan(request, item_key)
        data = await _json_body(request)
        query = str(data.get("query") or item.title or "")
        raw_year = data.get("year")
        year = int(raw_year) if raw_year not in (None, "") else None
        kind = None
        if data.get("kind"):
            try:
                kind = MediaKind(str(data["kind"]))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="invalid kind") from exc
        candidates = await search(config, store, query, year, kind)
        return {
            "candidates": [
                planner.candidate_dict(c, config.web.posters) for c in candidates[:10]
            ]
        }

    @app.post("/api/item/{item_key}/split")
    async def item_split(request: Request, item_key: str) -> dict[str, Any]:
        store = request.app.state.store
        _plan, item = await _item_and_plan(request, item_key)
        base = item.paths[0] if item.paths else item.root
        added = 0

        if base.is_dir():
            for child in sorted(base.iterdir()):
                if child.is_dir():
                    first_video: Path | None = None
                    for f in child.rglob("*"):
                        if f.is_file() and f.suffix.lower().lstrip(".") in VIDEO_EXTS:
                            first_video = f
                            break
                    if first_video is not None:
                        parsed = parse_release_name(first_video.stem)
                        kind = (
                            MediaKind.SHOW
                            if parsed.title and len(list(child.rglob("*.mkv"))) > 1
                            else MediaKind.MOVIE
                        )
                        upsert_hint(
                            store,
                            str(child),
                            parsed.title or child.name,
                            {"kind": kind.value},
                        )
                        added += 1
                elif child.is_file() and child.suffix.lower().lstrip(".") in VIDEO_EXTS:
                    parsed = parse_release_name(child.stem)
                    options: dict[str, str] = {"kind": "movie"}
                    if parsed.year is not None:
                        options["year"] = str(parsed.year)
                    upsert_hint(store, str(child), parsed.title or child.stem, options)
                    added += 1

        job_id = _start_scan_job(request.app)
        return {"ok": True, "hints_added": added, "scan_job": job_id}

    @app.get("/api/jobs/current")
    async def job_current(request: Request) -> dict[str, Any]:
        return request.app.state.jobs.current_dict() or {}

    @app.post("/api/jobs/cancel")
    async def job_cancel(request: Request) -> dict[str, Any]:
        data = await _json_body(request)
        request.app.state.jobs.cancel(data.get("id"))
        return {"ok": True}

    @app.post("/api/jobs/scan")
    async def job_scan(request: Request) -> dict[str, str]:
        data = await _json_body(request)
        current_app = request.app
        plan = _current_plan(current_app)

        only_path: Path | None = None
        if data.get("only"):
            only_path = Path(str(data["only"])).expanduser()
            if not only_path.is_absolute():
                only_path = only_path.absolute()
            if not _path_allowed(current_app.state.config, plan, only_path):
                raise HTTPException(
                    status_code=403,
                    detail="that path is not in the working folder, its destination, or the current plan",
                )
            if not only_path.exists():
                raise HTTPException(status_code=404, detail="path does not exist")

        kind: MediaKind | None = None
        if data.get("kind"):
            try:
                kind = MediaKind(str(data["kind"]))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="invalid kind") from exc

        manager = current_app.state.jobs
        try:
            job = manager.create_job("scan")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        async def runner(progress: ProgressSink) -> dict[str, Any]:
            plan2 = await build_plan(
                current_app.state.config,
                current_app.state.store,
                progress,
                only_path,
                kind,
            )
            current_app.state.plan_id = plan2.id
            return {"plan_id": plan2.id}

        manager.schedule(job, runner)
        return {"id": job.id}

    @app.post("/api/jobs/undo")
    async def job_undo(request: Request) -> dict[str, str]:
        data = await _json_body(request)
        current_app = request.app
        run_id = str(data.get("run_id") or "")
        apply_flag = bool(data.get("apply", True))
        target = current_app.state.store.get_run(run_id)
        plan = current_app.state.store.get_plan(target.plan_id) if target and target.plan_id else None
        if plan is None:
            plan = _current_plan(current_app) or _empty_plan()

        manager = current_app.state.jobs
        try:
            job = manager.create_job("undo")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        def runner(progress: ProgressSink) -> dict[str, Any]:
            run = undo_run(current_app.state.config, current_app.state.store, run_id, apply_flag, progress)
            return run_summary(run, plan, current_app.state.config, current_app.state.store)

        manager.schedule(job, runner)
        return {"id": job.id}

    @app.post("/api/jobs/purge")
    async def job_purge(request: Request) -> dict[str, str]:
        data = await _json_body(request)
        current_app = request.app
        apply_flag = bool(data.get("apply", True))
        manager = current_app.state.jobs
        try:
            job = manager.create_job("purge")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        def runner(progress: ProgressSink) -> dict[str, Any]:
            run = purge(current_app.state.config, current_app.state.store, apply_flag, progress)
            return {
                "id": run.id,
                "status": run.status,
                "done": run.done,
                "failed": run.failed,
                "skipped": run.skipped,
            }

        manager.schedule(job, runner)
        return {"id": job.id}

    @app.get("/api/jobs/events")
    async def job_events(request: Request) -> StreamingResponse:
        manager = request.app.state.jobs

        async def gen() -> AsyncIterator[str]:
            for _ in range(1200):
                job = manager.current_dict()
                if job is None:
                    yield "event: ping\ndata: {}\n\n"
                    return
                yield "data: " + json.dumps(job) + "\n\n"
                if job.get("state") in ("done", "error", "cancelled"):
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/runs")
    async def runs(request: Request, limit: int = 20) -> list[dict[str, Any]]:
        out = []
        for run in request.app.state.store.list_runs(limit):
            out.append(
                {
                    "id": run.id,
                    "kind": run.kind,
                    "status": run.status,
                    "done": run.done,
                    "failed": run.failed,
                    "skipped": run.skipped,
                    "plan_id": run.plan_id,
                    "started_at": run.started_at.isoformat(),
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                }
            )
        return out

    @app.get("/api/runs/{run_id}")
    async def run_detail(
        request: Request, run_id: str, offset: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        store = request.app.state.store
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        plan = store.get_plan(run.plan_id) if run.plan_id else _current_plan(request.app)
        if plan is None:
            plan = _empty_plan()
        journal = store.get_journal(run_id)
        return {
            "run": run_summary(run, plan, request.app.state.config, store),
            "journal": journal[offset : offset + limit],
            "journal_total": len(journal),
        }

    @app.post("/api/runs/{run_id}/undo-preview")
    async def undo_preview(request: Request, run_id: str) -> dict[str, Any]:
        store = request.app.state.store
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        plan = store.get_plan(run.plan_id) if run.plan_id else _current_plan(request.app)
        if plan is None:
            plan = _empty_plan()
        preview = await asyncio.to_thread(
            undo_run, request.app.state.config, store, run_id, False, NullProgress()
        )
        return run_summary(preview, plan, request.app.state.config, store)

    @app.get("/api/runs/{run_id}/undo-script")
    async def undo_script(request: Request, run_id: str) -> FileResponse:
        path = request.app.state.config.state_dir_path() / "undo" / f"undo-{run_id}.ps1"
        if not path.exists():
            raise HTTPException(status_code=404, detail="undo script not found")
        return FileResponse(path, media_type="text/plain", filename=path.name)

    @app.get("/api/config")
    async def config_get(request: Request) -> dict[str, Any]:
        config = request.app.state.config
        dest_env = {}
        for name in config.libraries:
            env = os.environ.get(f"MEDIAKEEP_DEST_{name.upper()}")
            if env:
                dest_env[name] = env
        return {
            "config": planner.masked_config(config),
            "warnings": config_warnings(config),
            "dest_env": dest_env,
            "config_path": str(request.app.state.config_path),
        }

    @app.get("/api/settings")
    async def settings_get(request: Request) -> dict[str, Any]:
        return planner.settings_view(request.app.state.base_config, request.app.state.store)

    @app.get("/api/folders")
    async def folders(request: Request, path: str | None = None) -> dict[str, Any]:
        try:
            return planner.folders_view(request.app.state.config, path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="path does not exist") from exc
        except NotADirectoryError as exc:
            raise HTTPException(status_code=422, detail="path is not a folder") from exc

    @app.get("/api/fixes")
    async def fixes_list(request: Request) -> dict[str, Any]:
        return planner.fixes_view(request.app.state.config, request.app.state.store)

    @app.post("/api/fixes")
    async def fixes_add(request: Request) -> dict[str, Any]:
        store = request.app.state.store
        data = await _json_body(request)
        pattern = str(data.get("pattern") or "").strip()
        name = str(data.get("name") or "").strip()
        if not pattern:
            raise HTTPException(status_code=422, detail="pattern is required")

        options = data.get("options") or {}
        if isinstance(options, str):
            options = parse_options(options)

        upsert_hint(store, pattern, name, options)
        job_id = _start_scan_job(request.app) if bool(data.get("scan", True)) else None
        return {"ok": True, "scan_job": job_id}

    @app.put("/api/fixes/{hint_id}")
    async def fixes_update(request: Request, hint_id: int) -> dict[str, Any]:
        store = request.app.state.store
        row = store.get_hint(hint_id)
        if row is None:
            raise HTTPException(status_code=404, detail="fix not found")

        data = await _json_body(request)
        pattern = str(data.get("pattern") or row["pattern"]).strip()
        name = str(data.get("name") if data.get("name") is not None else row["name"]).strip()
        options = data.get("options") if data.get("options") is not None else row["options"]
        if isinstance(options, str):
            options = parse_options(options)

        from mediakeep.hints import options_to_text

        clean_options = {str(k): str(v) for k, v in dict(options or {}).items()}
        store.update_hint(hint_id, pattern, name, options_to_text(clean_options))
        job_id = _start_scan_job(request.app) if bool(data.get("scan", True)) else None
        return {"ok": True, "scan_job": job_id}

    @app.delete("/api/fixes/{hint_id}")
    async def fixes_delete(request: Request, hint_id: int) -> dict[str, Any]:
        store = request.app.state.store
        store.delete_hint(hint_id)
        job_id = _start_scan_job(request.app)
        return {"ok": True, "scan_job": job_id}

    @app.post("/api/fixes/import")
    async def fixes_import(request: Request) -> dict[str, Any]:
        data = await _json_body(request)
        path = Path(str(data.get("path") or "")).expanduser()
        if not path.is_absolute():
            path = path.absolute()
        plan = _current_plan(request.app)
        if not _path_allowed(request.app.state.config, plan, path):
            raise HTTPException(status_code=403, detail="that path is not in the working folder, its destination, or the current plan")
        if not path.exists():
            raise HTTPException(status_code=404, detail="hints file not found")
        count = import_hints_file(request.app.state.store, path)
        job_id = _start_scan_job(request.app) if bool(data.get("scan", True)) else None
        return {"ok": True, "count": count, "scan_job": job_id}

    @app.get("/api/fixes/export")
    async def fixes_export(request: Request) -> Response:
        from mediakeep.hints import load_hints

        lines: list[str] = []
        for hint in load_hints(request.app.state.store):
            if hint.origin != "hints":
                continue
            opt = options_to_text(hint.options)
            lines.append(f"{hint.pattern} | {hint.name} | {opt}".rstrip(" |"))
        text = "\n".join(lines)
        if lines:
            text += "\n"
        return Response(
            content=text,
            media_type="text/plain",
            headers={"Content-Disposition": 'attachment; filename="jf-hints.txt"'},
        )

    @app.get("/", response_class=HTMLResponse)
    async def page_root() -> RedirectResponse:
        return RedirectResponse("/inbox", status_code=303)

    @app.get("/inbox", response_class=HTMLResponse)
    async def page_inbox(request: Request, folder: str | None = None) -> HTMLResponse:
        config = request.app.state.config
        store = request.app.state.store
        if not config.sources:
            # No working folder yet: the page only offers to choose one.
            return _page(
                request,
                "start.html",
                "inbox",
                {
                    "recent": [str(p) for p in config.workspace.recent],
                    "tmdb_ready": bool(config.tmdb.api_key or config.tmdb.read_token),
                },
            )
        if not folder:
            # Come back to the folder you were working in.
            folder = store.get_decision(WORKING_FOLDER_KEY) or None
        f = _default_inbox_folder(config, folder)
        if _source_allowed(config, f):
            # Remember the source you work in, not a subfolder you opened inside it:
            # coming back to the Inbox shows the whole source again.
            src = source_for(config, f)
            store.set_decision(WORKING_FOLDER_KEY, str(src or f))
        return _page(
            request,
            "inbox.html",
            "inbox",
            {
                "inbox": planner.inbox_rows(
                    config, request.app.state.store, _current_plan(request.app), f
                )
            },
        )

    @app.get("/match/{item_key}", response_class=HTMLResponse)
    async def page_match(request: Request, item_key: str) -> HTMLResponse:
        plan = _current_plan(request.app)
        if plan is None or item_key not in plan.items:
            raise HTTPException(status_code=404, detail="item is not in the current plan")
        return _page(
            request,
            "match.html",
            "inbox",
            {
                "match": planner.match_view(
                    request.app.state.config, request.app.state.store, plan, item_key
                ),
                "config_folder_jpg": request.app.state.config.naming.folder_jpg,
                "config_remember": request.app.state.config.behavior.remember_choices,
            },
        )

    @app.get("/fixes", response_class=HTMLResponse)
    async def page_fixes(request: Request) -> HTMLResponse:
        return _page(
            request,
            "fixes.html",
            "fixes",
            {
                "fixes": planner.fixes_view(
                    request.app.state.config, request.app.state.store
                )
            },
        )

    @app.get("/execute", response_class=HTMLResponse)
    async def page_execute(request: Request) -> HTMLResponse:
        return _page(
            request,
            "execute.html",
            "execute",
            {
                "execute": planner.resolve_queue(
                    request.app.state.config,
                    request.app.state.store,
                    _current_plan(request.app),
                )
            },
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def page_settings(request: Request) -> HTMLResponse:
        return _page(
            request,
            "settings.html",
            "settings",
            {
                "settings": planner.settings_view(
                    request.app.state.base_config, request.app.state.store
                )
            },
        )

    @app.get("/history", response_class=HTMLResponse)
    async def page_history(request: Request) -> HTMLResponse:
        return _page(
            request,
            "history.html",
            "history",
            {
                "history": planner.history_view(
                    request.app.state.config, request.app.state.store
                )
            },
        )

    @app.get("/login", response_class=HTMLResponse)
    async def page_login(request: Request) -> HTMLResponse:
        return _page(request, "login.html", "login", {})


def create_app(config: AppConfig, store: Store | None = None) -> FastAPI:
    if not _is_loopback(config.web.host) and not config.web.access_token:
        raise ConfigError("web.access_token is required when web.host is not a loopback address")

    if store is None:
        store = Store(config.state_dir_path() / "mediakeep.db")

    _TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="mediakeep", docs_url=None, redoc_url=None)
    app.state.base_config = config
    app.state.config = workspace.effective(config)
    app.state.store = store
    app.state.config_path = Path(os.environ.get("MEDIAKEEP_CONFIG", str(default_config_path())))
    app.state.plan_id = None
    app.state.jobs = JobManager(app.state.config, store)
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    app.state.code_mtime = _code_mtime()

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    _register_middleware(app)
    _register_routes(app)
    return app


def serve(config: AppConfig) -> int:
    if not _is_loopback(config.web.host) and not config.web.access_token:
        print(
            "mediakeep serve: web.access_token is required when web.host is not loopback",
            file=sys.stderr,
        )
        return 2
    try:
        import uvicorn
    except ImportError:
        print("mediakeep serve: uvicorn is not installed", file=sys.stderr)
        return 2
    app = create_app(config)
    try:
        uvicorn.run(app, host=config.web.host, port=config.web.port, log_level="info")
    except KeyboardInterrupt:
        return 0
    return 0
