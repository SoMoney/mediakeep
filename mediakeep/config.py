from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from mediakeep.models import MediaKind

DEFAULT_CONFIG_TEXT = """# mediakeep settings. Most of these can be changed on the web app's Settings page.

# The folder you are tidying, and where its items go. Leave destination empty to rename
# everything where it is; set it to a folder to move items into it.
workspace:
  folder: null
  destination: null
  recent: []

naming:
  year_style: parens        # parens: Title (2014)   brackets: Title [2014]
  id_tag: none              # none | tmdb | imdb  (adds an id tag to folder names)
  keep_episode_titles: true
  lores_tagging: true       # tag the smaller copy of a duplicate as "LoRes"
  trust_folder_year: false
  folder_jpg: true          # save the matched poster as folder.jpg

behavior:
  cross_volume: move        # move | copy (when the destination is on another drive)
  auto_approve_confidence: 90
  auto_approve_margin: 15
  remember_choices: false   # tick "Remember this choice" by default when you pick a match

hints_file: ''

tmdb:
  api_key: ''               # free key from themoviedb.org (Settings -> API)
  read_token: ''            # or the longer "API Read Access Token" instead of api_key
  language: en-US
  cache_days: 30
  requests_per_second: 20

tools:
  ffprobe: ffprobe          # optional (comes with ffmpeg); runtimes improve matching

web:
  host: 127.0.0.1
  port: 8765
  access_token: ''          # required if host is not 127.0.0.1
  posters: true

state_dir: ''               # where the database, logs and undo scripts go (default: %LOCALAPPDATA%\\mediakeep)
"""


class ConfigError(RuntimeError):
    pass


class LibraryConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    path: Path
    kind: MediaKind


class RoutingConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    movie: str = "movies"
    show: str = "tv"
    rules: list[dict[str, Any]] = Field(default_factory=list)


class NamingConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    year_style: str = "parens"
    id_tag: str = "none"
    keep_episode_titles: bool = True
    lores_tagging: bool = True
    trust_folder_year: bool = False
    folder_jpg: bool = True  # save the matched poster as folder.jpg


class BehaviorConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    cross_volume: str = "move"
    # Where a source folder's items go until you choose otherwise on the Inbox:
    # "in_place" (rename them where they are) or "file" (file them into the libraries).
    default_destination: Literal["in_place", "file"] = "in_place"
    # Save a remembered fix whenever you pick a match (the "Remember this choice" box starts
    # ticked). Off: your choice still sticks for that item; it just isn't applied to other
    # files with the same name.
    remember_choices: bool = False
    auto_approve_confidence: int = 90
    auto_approve_margin: int = 15


class TmdbConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    api_key: str = ""
    read_token: str = ""
    language: str = "en-US"
    cache_days: int = 30
    requests_per_second: int = 20


class ToolsConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    ffprobe: str = "ffprobe"


def _norm_path(path: Path | str) -> str:
    s = str(path).replace("\\", "/").rstrip("/")
    return s.lower()


def _is_under(child: Path | str, parent: Path | str) -> bool:
    c = _norm_path(child)
    p = _norm_path(parent)
    if not c or not p:
        return False
    return c == p or c.startswith(p + "/")


class WorkspaceConfig(BaseModel):
    """The one folder mediakeep works on, and where its items go.

    ``folder``: the folder you are tidying. ``destination``: empty means items are renamed
    where they are (the default); a folder means they are moved into it. ``recent``: folders
    you worked in before, for quick switching.
    """

    model_config = ConfigDict(validate_assignment=True)
    folder: Path | None = None
    destination: Path | None = None
    recent: list[Path] = Field(default_factory=list)

    @model_validator(mode="after")
    def _apart(self) -> "WorkspaceConfig":
        if self.folder is not None and self.destination is not None:
            if _norm_path(self.folder) != _norm_path(self.destination) and (
                _is_under(self.destination, self.folder) or _is_under(self.folder, self.destination)
            ):
                raise ValueError("the destination can't be inside the working folder, or contain it")
        return self


class WebConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    host: str = "127.0.0.1"
    port: int = 8765
    access_token: str = ""
    posters: bool = True


_DERIVED = {"sources", "libraries", "routing"}


class AppConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    # Scanned folders, target libraries and routing, as the engine sees them. These are
    # derived from ``workspace`` (see mediakeep.workspace.effective) and never saved.
    sources: list[Path] = Field(default_factory=list)
    libraries: dict[str, LibraryConfig] = Field(default_factory=dict)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    naming: NamingConfig = Field(default_factory=NamingConfig)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
    hints_file: str = ""
    tmdb: TmdbConfig = Field(default_factory=TmdbConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    state_dir: str = ""

    @model_validator(mode="after")
    def _validate_roots(self) -> "AppConfig":
        deduped: list[Path] = []
        seen: set[str] = set()
        for src in self.sources:
            key = str(src).replace("\\", "/").rstrip("/").lower()
            if key not in seen:
                seen.add(key)
                deduped.append(src)
        if len(deduped) != len(self.sources):
            # Bypass validate_assignment (it would re-run this validator).
            self.__dict__["sources"] = deduped
        libs = {name: lib.path for name, lib in self.libraries.items()}
        for name, path in libs.items():
            for other, opath in libs.items():
                if name == other:
                    continue
                if _is_under(path, opath):
                    raise ValueError(
                        f"library '{name}' is nested inside library '{other}'"
                    )
            for src in self.sources:
                if _is_under(path, src):
                    raise ValueError(f"library '{name}' is nested inside source '{src}'")
        return self

    @property
    def root_paths(self) -> list[Path]:
        out: list[Path] = []
        for p in self.sources:
            if p not in out:
                out.append(p)
        for lib in self.libraries.values():
            if lib.path not in out:
                out.append(lib.path)
        return out

    def state_dir_path(self) -> Path:
        if self.state_dir:
            return Path(self.state_dir)
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "mediakeep"
        return Path.home() / ".local" / "state" / "mediakeep"

    def config_hash(self) -> str:
        data = self.model_dump(mode="json")
        blob = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json", exclude=_DERIVED)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def masked_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude=_DERIVED)
        for section, key in (
            ("tmdb", "api_key"),
            ("tmdb", "read_token"),
            ("web", "access_token"),
        ):
            if data.get(section, {}).get(key):
                data[section][key] = "***"
        return data


def default_config_path() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "mediakeep" / "config.yaml"
    return Path.home() / ".config" / "mediakeep" / "config.yaml"


def write_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")


def load_config(path: Path | None = None) -> AppConfig:
    """Read the settings file; on first use, write a commented default one first."""
    cfg_path = path or default_config_path()
    if not cfg_path.exists():
        write_default_config(cfg_path)
    text = cfg_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{cfg_path} is not a settings file")
    for old in _DERIVED:
        data.pop(old, None)  # from older versions; the working folder replaces them
    return AppConfig.model_validate(data)


def config_warnings(config: AppConfig) -> list[str]:
    warnings: list[str] = []
    for root in config.root_paths:
        if not root.exists():
            warnings.append(f"path does not exist (may be offline): {root}")
    return warnings
