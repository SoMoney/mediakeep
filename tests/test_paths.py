from __future__ import annotations

import os
from pathlib import Path

import pytest

from mediakeep.paths import long_path, sanitize_name, strip_long


def test_sanitize_name():
    assert sanitize_name('A:B/C') == "A - B C"
    assert sanitize_name("Movie (2019).mkv") == "Movie (2019).mkv"


def test_sanitize_name_or_colon_alternative_title():
    assert (
        sanitize_name(
            "Dr. Moonbeam or: How I Learned to Stop Fretting and Love the Comet"
        )
        == "Dr. Moonbeam or How I Learned to Stop Fretting and Love the Comet"
    )
    assert sanitize_name("Iron Owl: Vol. 1") == "Iron Owl - Vol. 1"
    assert sanitize_name("Skyport: The Ark of Dawn") == "Skyport - The Ark of Dawn"
    assert (
        sanitize_name("Birdman or (The Unexpected Virtue of Ignorance)")
        == "Birdman or (The Unexpected Virtue of Ignorance)"
    )


def test_strip_long_roundtrip():
    if os.name != "nt":
        pytest.skip("Windows long paths")
    assert strip_long("\\\\?\\C:\\x") == Path("C:\\x")


@pytest.mark.skipif(os.name != "nt", reason="Windows long paths")
def test_long_path_windows():
    assert long_path("C:\\x").startswith("\\\\?\\C:")
