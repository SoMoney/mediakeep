from __future__ import annotations

import logging
from functools import cache

from mediakeep.config import AppConfig
from mediakeep.models import AudioVerifier, Identifier
from mediakeep.pipeline import NullIdentifier
from mediakeep.store import Store

from .identifier import TmdbIdentifier, identity_for, search
from .probe import ProbeRunner
from .route import pick_library
from .score import score_candidate
from .tmdb import TmdbClient

__all__ = [
    "ProbeRunner",
    "TmdbClient",
    "TmdbIdentifier",
    "create_identifier",
    "identity_for",
    "pick_library",
    "score_candidate",
    "search",
]

logger = logging.getLogger(__name__)


@cache
def _log_no_tmdb() -> None:
    logger.warning("TMDB is not configured; identification will use filenames only.")


def create_identifier(
    config: AppConfig, store: Store, audio: AudioVerifier | None
) -> Identifier:
    if not (config.tmdb.api_key or config.tmdb.read_token):
        _log_no_tmdb()
        return NullIdentifier()
    return TmdbIdentifier(config, store, audio)
