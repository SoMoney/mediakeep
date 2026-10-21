from __future__ import annotations

import argparse
import asyncio
import importlib
import io
import json
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError
from rich.console import Console

from mediakeep import __version__, selftest, workspace
from mediakeep.config import AppConfig, default_config_path, load_config
from mediakeep.logs import setup_logging
from mediakeep.models import Issue, MediaKind, ProgressSink, Severity
from mediakeep.report import report_json, report_plan, report_run, run_summary_json
from mediakeep.store import Store


class ConsoleProgress(ProgressSink):
    def __init__(self, console: Console) -> None:
        self.console = console
        self._stage = ""
        self._total: int | None = None
        self._n = 0

    def stage(self, name: str, total: int | None = None) -> None:
        self._stage = name
        self._total = total
        self._n = 0

    def advance(self, n: int = 1, detail: str = "") -> None:
        self._n += n
        # n == 0 is a "now looking at …" line for the web page's live list; the terminal
        # only prints real steps.
        if detail and n:
            self.console.print(detail, markup=False)

    def log(self, message: str) -> None:
        self.console.print(message, markup=False)

    def cancelled(self) -> bool:
        return False


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def _make_console(stderr: bool = False) -> Console:
    return Console(stderr=stderr, markup=False, width=4096)


def _validation_detail(exc: ValidationError) -> str:
    detail = ""
    try:
        errors = exc.errors()
        if errors:
            detail = str(errors[0].get("msg", ""))
    except Exception:
        detail = str(exc)
    if not detail:
        detail = str(exc)
    for prefix in ("ValueError, ", "Value error, "):
        if detail.startswith(prefix):
            detail = detail[len(prefix) :]
            break
    return detail.replace("\n", " ").strip()


def _config_error_message(path: Path, exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        detail = _validation_detail(exc)
    else:
        detail = str(exc).replace("\n", " ").strip()
    return f"config error in {path}: {detail}"


def _strip_trailing_quote(value: str) -> str:
    s = value.strip()
    if s.endswith('"'):
        s = s[:-1].rstrip()
    return s


def _clean_only_arg(only: Path | None) -> Path | None:
    if only is None:
        return None
    s = _strip_trailing_quote(str(only))
    if not s:
        return None
    return Path(s)


def _norm_abs(path: Path | str) -> str:
    return os.path.abspath(str(path)).replace("\\", "/").rstrip("/").lower()


def _is_under(child: Path | str, parent: Path | str) -> bool:
    c = _norm_abs(child)
    p = _norm_abs(parent)
    return c == p or c.startswith(p + "/")


def _check_only(
    config: AppConfig,
    only: Path | None,
    console: Console,
) -> int | None:
    clean = _clean_only_arg(only)
    if clean is None:
        return None

    roots = config.root_paths
    for root in roots:
        if _is_under(clean, root):
            return None

    console.print(f"--only {clean} is not inside the working folder or its destination", markup=False)
    for root in roots:
        console.print(f"  {root}", markup=False)
    return 2


def _attention_count(issues: Sequence[Issue]) -> int:
    return sum(1 for issue in issues if issue.severity == Severity.ATTENTION)


def _load_saved(args: argparse.Namespace, json_mode: bool = False) -> tuple[AppConfig, Console]:
    """The settings file as saved (written with defaults on first use)."""
    config_path = args.config or default_config_path()
    console = _make_console(stderr=json_mode)
    try:
        config = load_config(config_path)
    except Exception as exc:
        console.print(_config_error_message(config_path, exc), markup=False)
        if args.verbose:
            traceback.print_exc()
        raise SystemExit(2)
    return config, console


def _load_store(args: argparse.Namespace, json_mode: bool = False) -> tuple[AppConfig, Store, Console]:
    """The settings to run with (derived from the working folder), the database and a console."""
    saved, console = _load_saved(args, json_mode)
    config = workspace.effective(saved)
    state_dir = config.state_dir_path()
    store = Store(state_dir / "mediakeep.db")
    setup_logging(state_dir, args.verbose)
    return config, store, console


def _need_folder(config: AppConfig, console: Console) -> int | None:
    if config.sources:
        return None
    console.print(
        "No working folder yet. Choose one in the web app (mediakeep serve) or with:\n"
        "  mediakeep folder PATH",
        markup=False,
    )
    return 2


def cmd_scan(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    config, store, console = _load_store(args, json_mode)
    from mediakeep.pipeline import apply_plan, build_plan

    rc = _need_folder(config, console) or _check_only(config, args.only, console)
    if rc is not None:
        return rc

    only = _clean_only_arg(args.only)
    progress = ConsoleProgress(console)
    kind = MediaKind(args.type) if args.type else None
    plan = asyncio.run(build_plan(config, store, progress, only=only, kind=kind))

    if args.json:
        if args.apply:
            run = apply_plan(config, store, plan, None, progress)
            run.kind = "scan"
            store.finish_run(run)

            plan_obj = json.loads(report_json(plan))
            run_obj = run_summary_json(run, plan, config, store)
            text = json.dumps({"plan": plan_obj, "run": run_obj}, ensure_ascii=False)
            sys.stdout.write(text)
            if not text.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()

            if run.failed:
                return 3
            return 1 if _attention_count(plan.issues) or _attention_count(run.issues) else 0

        text = report_json(plan)
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        return 1 if _attention_count(plan.issues) else 0

    report_plan(plan, console=console, config=config, dry_run=not args.apply)

    if not args.apply:
        return 1 if _attention_count(plan.issues) else 0

    run = apply_plan(config, store, plan, None, progress)
    run.kind = "scan"
    store.finish_run(run)
    report_run(run, plan, config, console, store)

    if run.failed:
        return 3
    return 1 if _attention_count(plan.issues) or _attention_count(run.issues) else 0


def cmd_apply(args: argparse.Namespace) -> int:
    config, store, console = _load_store(args)
    from mediakeep.pipeline import apply_plan

    plan = store.get_plan(args.plan_id)
    if plan is None:
        console.print("plan not found", markup=False)
        return 2

    progress = ConsoleProgress(console)
    run = apply_plan(config, store, plan, args.items or None, progress)
    report_run(run, plan, config, console, store)
    return 3 if run.failed else 0


def cmd_undo(args: argparse.Namespace) -> int:
    config, store, console = _load_store(args)
    from mediakeep.pipeline import undo_run

    progress = ConsoleProgress(console)
    run = undo_run(config, store, args.run_id, args.apply, progress)
    console.print(f"undo run {run.id}: status={run.status}", markup=False)
    return 3 if run.failed else 0


def cmd_runs(args: argparse.Namespace) -> int:
    config, store, console = _load_store(args)
    run_id = getattr(args, "run_id", None)

    if run_id:
        run = store.get_run(str(run_id))
        if run is None:
            console.print("run not found", markup=False)
            return 2
        plan = store.get_plan(run.plan_id) if run.plan_id else None
        if plan is None:
            console.print(
                f"{run.id}  {run.kind}  {run.status}  "
                f"{run.done} done, {run.failed} failed, {run.skipped} skipped",
                markup=False,
            )
            return 0
        report_run(run, plan, config, console, store)
        return 0

    for run in store.list_runs():
        started = run.started_at.strftime("%Y-%m-%d %H:%M")
        script = config.state_dir_path() / "undo" / f"undo-{run.id}.ps1"
        undo = f"   undo: {script}" if script.exists() else ""
        console.print(
            f"{run.id}  {started}  {run.kind:<6}  {run.status:<8}  "
            f"{run.done} done, {run.failed} failed, {run.skipped} skipped{undo}",
            markup=False,
        )
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    config, store, console = _load_store(args)
    from mediakeep.pipeline import purge

    progress = ConsoleProgress(console)
    run = purge(config, store, args.apply, progress)
    console.print(f"purge run {run.id}: done={run.done} failed={run.failed}", markup=False)
    return 3 if run.failed else 0


def cmd_hints(args: argparse.Namespace) -> int:
    config, store, console = _load_store(args)
    from mediakeep.hints import export_hints, import_hints_file, load_hints

    if args.hints_cmd == "import":
        n = import_hints_file(store, Path(args.file))
        console.print(f"imported {n} hints", markup=False)
        return 0
    if args.hints_cmd == "export":
        export_hints(store, Path(args.file))
        console.print(f"exported hints to {args.file}", markup=False)
        return 0
    for hint in load_hints(store):
        console.print(
            f"{hint.origin:5} {hint.pattern} | {hint.name} | {hint.options}",
            markup=False,
        )
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    config, console = _load_saved(args)
    console.print_json(json.dumps(config.masked_dict()))
    return 0


def cmd_folder(args: argparse.Namespace) -> int:
    """Show or set the working folder and where its items go."""
    saved, console = _load_saved(args)
    config_path = Path(args.config or default_config_path())
    ws = saved.workspace
    if args.path is None:
        if args.here or args.move_to:
            console.print("Give the folder too: mediakeep folder PATH --move-to DEST", markup=False)
            return 2
        console.print(f"working folder: {ws.folder or '(none yet)'}", markup=False)
        console.print(f"destination:    {ws.destination or 'rename here'}", markup=False)
        return 0
    folder = Path(_strip_trailing_quote(str(args.path))).expanduser().absolute()
    if not folder.is_dir():
        console.print(f"{folder} isn't a folder that exists", markup=False)
        return 2
    dest: Path | None = None
    if args.move_to:
        dest = Path(_strip_trailing_quote(str(args.move_to))).expanduser().absolute()
        if not dest.is_dir():
            console.print(f"{dest} isn't a folder that exists", markup=False)
            return 2
        if _is_under(dest, folder) or _is_under(folder, dest):
            console.print("The destination can't be inside the working folder, or contain it.", markup=False)
            return 2
    saved = workspace.with_workspace(saved, folder, dest)
    if config_path.exists():
        shutil.copy2(config_path, config_path.with_name(config_path.name + ".bak"))
    saved.save(config_path)
    console.print(f"working folder: {folder}", markup=False)
    console.print(f"destination:    {dest or 'rename here'}", markup=False)
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    return selftest.run()


def cmd_doctor(args: argparse.Namespace) -> int:
    config, store, console = _load_store(args)
    from mediakeep.config import config_warnings

    for warning in config_warnings(config):
        console.print(warning, markup=False)

    state = config.state_dir_path()
    console.print(f"state_dir: {state} {'OK' if state.exists() else 'missing'}", markup=False)

    console.print(f"LongPathsEnabled: {_check_long_paths()}", markup=False)
    console.print(f"ffprobe: {_which(config.tools.ffprobe)}", markup=False)
    console.print(
        f"TMDB: {'configured' if config.tmdb.api_key or config.tmdb.read_token else 'not configured'}",
        markup=False,
    )
    console.print(f"working folder: {config.workspace.folder or '(none yet)'}", markup=False)
    return 0


def _check_long_paths() -> str:
    if os.name != "nt":
        return "not Windows"
    try:
        winreg: Any = importlib.import_module("winreg")
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
            0,
            winreg.KEY_READ,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
            return "enabled" if value else "disabled"
    except Exception:
        return "unknown"


def _which(cmd: str) -> str:
    import shutil

    return shutil.which(cmd) or "not found"


def cmd_serve(args: argparse.Namespace) -> int:
    saved, console = _load_saved(args)
    os.environ["MEDIAKEEP_CONFIG"] = str(Path(args.config or default_config_path()))
    from mediakeep.web import serve

    console.print(f"Open http://{saved.web.host}:{saved.web.port} in your browser. Ctrl+C stops mediakeep.", markup=False)
    return serve(saved)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mediakeep", description="Tidy movie and TV folders into Jellyfin-friendly names."
    )
    parser.add_argument("--config", type=Path, help="settings file (default: %%APPDATA%%\\mediakeep\\config.yaml)")
    parser.add_argument("--verbose", action="store_true", help="more detail, and tracebacks on errors")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="scan the working folder and show the plan (--apply to carry it out)")
    scan.add_argument("--apply", action="store_true")
    scan.add_argument("--only", type=Path)
    scan.add_argument("--type", choices=["movie", "show"])
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=cmd_scan)

    applyp = sub.add_parser("apply", help="carry out a saved plan")
    applyp.add_argument("plan_id")
    applyp.add_argument("--items", nargs="*")
    applyp.set_defaults(func=cmd_apply)

    undo = sub.add_parser("undo", help="undo a run (--apply to do it)")
    undo.add_argument("run_id")
    undo.add_argument("--apply", action="store_true")
    undo.set_defaults(func=cmd_undo)

    runs = sub.add_parser("runs", help="list past runs, or show one")
    runs.add_argument("run_id", nargs="?")
    runs.set_defaults(func=cmd_runs)

    purge = sub.add_parser("purge", help="delete files mediakeep created, such as folder.jpg (--apply)")
    purge.add_argument("--apply", action="store_true")
    purge.set_defaults(func=cmd_purge)

    hints = sub.add_parser("hints", help="import, export or list remembered fixes")
    hints_sub = hints.add_subparsers(dest="hints_cmd", required=True)
    hi = hints_sub.add_parser("import")
    hi.add_argument("file")
    he = hints_sub.add_parser("export")
    he.add_argument("file")
    hints_sub.add_parser("list")
    hints.set_defaults(func=cmd_hints)

    cfg = sub.add_parser("config", help="show the settings")
    cfg_sub = cfg.add_subparsers(dest="config_cmd", required=True)
    cs = cfg_sub.add_parser("show")
    cs.set_defaults(func=cmd_config)

    st = sub.add_parser("selftest", help="check the name parser")
    st.set_defaults(func=cmd_selftest)

    doctor = sub.add_parser("doctor", help="check the set-up")
    doctor.set_defaults(func=cmd_doctor)

    serve = sub.add_parser("serve", help="start the web app")
    serve.set_defaults(func=cmd_serve)

    folder = sub.add_parser("folder", help="show or set the working folder")
    folder.add_argument("path", nargs="?", type=Path)
    where = folder.add_mutually_exclusive_group()
    where.add_argument("--move-to", type=Path, metavar="DEST", help="move items into this folder")
    where.add_argument("--here", action="store_true", help="rename items where they are (default)")
    folder.set_defaults(func=cmd_folder)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    _utf8_stdio()
    json_mode = bool(getattr(args, "json", False))
    out_console = _make_console(stderr=json_mode)
    config_path = args.config or default_config_path()
    out_console.print(f"mediakeep {__version__} - config: {config_path}", markup=False)

    try:
        return int(args.func(args))
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        return 2 if code else 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
