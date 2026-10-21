from __future__ import annotations

from pathlib import Path

import pytest

from mediakeep.config import AppConfig, LibraryConfig
from mediakeep.models import MediaKind


def test_nested_libraries_refused(tmp_path: Path):
    with pytest.raises(Exception):
        AppConfig(
            sources=[tmp_path / "src"],
            libraries={
                "a": LibraryConfig(path=tmp_path / "lib", kind=MediaKind.MOVIE),
                "b": LibraryConfig(path=tmp_path / "lib" / "inner", kind=MediaKind.SHOW),
            },
        )


def test_library_inside_source_refused(tmp_path: Path):
    with pytest.raises(Exception):
        AppConfig(
            sources=[tmp_path / "root"],
            libraries={
                "a": LibraryConfig(path=tmp_path / "root" / "lib", kind=MediaKind.MOVIE),
            },
        )
