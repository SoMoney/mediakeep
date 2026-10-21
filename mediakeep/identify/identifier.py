from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import mediakeep.models as models
from mediakeep.config import AppConfig
from mediakeep.models import (
    AudioVerifier,
    Candidate,
    IdStatus,
    Identity,
    MediaKind,
    ProgressSink,
    SourceItem,
)
from mediakeep.parse import is_strict_episode, parse_episode, parse_release_name
from mediakeep.store import Store
from rapidfuzz import fuzz

from .probe import ProbeRunner
from .route import pick_library
from .score import audio_overlap_bonus, normalize_title, score_candidate, title_ratio
from .tmdb import TmdbClient, TmdbError, TmdbNotFound, TmdbUnavailable

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Hit:
    candidate: Candidate
    vote_count: int
    raw: dict[str, Any]
    first_year: int | None = None
    last_year: int | None = None



def _poster_url(image_base: str | None, poster_path: Any) -> str | None:
    """TMDB poster URL: <base with size>/<path>; TMDB sends the path with a leading slash, be lenient."""
    if not image_base or not poster_path:
        return None
    return f"{image_base.rstrip('/')}/{str(poster_path).lstrip('/')}"


def _identity_to_dict(identity: Identity) -> dict[str, Any]:
    return dict(models._jsonable(identity))


def _identity_from_dict(data: dict[str, Any]) -> Identity:
    return models._identity(data)


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _year_from_date(value: Any) -> int | None:
    if not value:
        return None
    text = str(value)
    m = re.search(r"\d{4}", text)
    if not m:
        return None
    try:
        year = int(m.group())
    except ValueError:
        return None
    if 1800 <= year <= 2100:
        return year
    return None


def _hint_int(item: SourceItem, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        raw = item.hint_options.get(key)
        if raw is None:
            continue
        try:
            return int(float(str(raw)))
        except Exception:
            continue
    return None


def _hint_tmdb_id(item: SourceItem) -> int | None:
    return _hint_int(item, ("_tmdb_id", "tmdb_id", "tmdbid", "id"))


def _hint_imdb_id(item: SourceItem) -> str | None:
    raw = (
        item.hint_options.get("_imdb_id")
        or item.hint_options.get("imdb_id")
        or item.hint_options.get("imdbid")
    )
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _hint_kind(item: SourceItem) -> MediaKind | None:
    raw = item.hint_options.get("kind") or item.hint_options.get("type")
    if not raw:
        return None
    low = str(raw).lower()
    if low in {"movie", "dvd", "bluray"}:
        return MediaKind.MOVIE
    if low in {"show", "tv", "series"}:
        return MediaKind.SHOW
    return None


def _effective_kind(kind: MediaKind) -> MediaKind:
    if kind in (MediaKind.MOVIE, MediaKind.DVD, MediaKind.BLURAY):
        return MediaKind.MOVIE
    if kind == MediaKind.SHOW:
        return MediaKind.SHOW
    return MediaKind.UNKNOWN


def _item_kind(item: SourceItem) -> MediaKind:
    hint = _hint_kind(item)
    if hint is not None:
        return hint
    return _effective_kind(item.kind)


def _kind_for_choice(item: SourceItem, kind: MediaKind | None) -> MediaKind:
    if kind in (MediaKind.MOVIE, MediaKind.DVD, MediaKind.BLURAY):
        return MediaKind.MOVIE
    if kind == MediaKind.SHOW:
        return MediaKind.SHOW
    hint = _hint_kind(item)
    if hint is not None:
        return hint
    effective = _effective_kind(item.kind)
    if effective != MediaKind.UNKNOWN:
        return effective
    return MediaKind.SHOW if item.episodes else MediaKind.MOVIE


def _query_title(item: SourceItem) -> str:
    hint_title = item.hint_options.get("title") or item.hint_options.get("name")
    if hint_title:
        return str(hint_title)
    if item.title:
        return item.title
    if item.videos:
        try:
            parsed = parse_release_name(item.videos[0].path.stem)
            if parsed.title:
                return parsed.title
        except Exception:
            pass
    return ""


def _query_year(config: AppConfig, item: SourceItem) -> int | None:
    hint_year = _hint_int(item, ("year", "y"))
    if hint_year is not None:
        return hint_year
    if item.year is None:
        return None
    if item.year_source == "folder" and not config.naming.trust_folder_year:
        # The folder's year counts unless a video file names a different year
        # ("The Glass Garden (2017)" must not be searched without its year).
        for v in item.videos:
            file_year = parse_release_name(v.path.name).year
            if file_year is not None and file_year != item.year:
                return None
    return item.year


def _is_weak(item: SourceItem, kind: MediaKind) -> bool:
    if kind == MediaKind.UNKNOWN:
        return True
    if 2 <= len(item.videos) <= 5 and all(
        is_strict_episode(v.path.name) for v in item.videos
    ):
        return True
    if len(item.videos) == 1:
        stem = item.videos[0].path.stem
        try:
            parsed = parse_episode(stem, item.title or "")
        except Exception:
            parsed = None
        if parsed is not None and parsed.absolute:
            return True
        norm_title = normalize_title(item.title)
        norm_stem = normalize_title(stem)
        if (
            norm_title
            and norm_stem.startswith(norm_title)
            and re.search(r"\d{1,3}$", norm_stem)
        ):
            return True
    return False


def _unmatched(item: SourceItem, message: str) -> Identity:
    return Identity(
        item_key=item.key,
        kind=item.kind,
        title=item.title,
        year=item.year,
        tmdb_id=None,
        imdb_id=None,
        status=IdStatus.UNMATCHED,
        confidence=0.0,
        reasons=[message],
        candidates=[],
    )


def _parse_genres(data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for genre in data.get("genres") or []:
        if isinstance(genre, dict) and genre.get("name"):
            out.append(str(genre["name"]))
    return out


def _parse_countries(data: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for country in data.get("production_countries") or []:
        if not isinstance(country, dict):
            continue
        iso = country.get("iso_3166_1")
        name = country.get("name")
        if iso and str(iso) not in out:
            out.append(str(iso))
        if name and str(name) not in out:
            out.append(str(name))
    return out


def _runtime_from_details(data: dict[str, Any], kind: MediaKind) -> int | None:
    if kind == MediaKind.MOVIE:
        runtime = data.get("runtime")
        try:
            return int(runtime) if runtime else None
        except Exception:
            return None
    runtimes = data.get("episode_run_time") or []
    if isinstance(runtimes, list) and runtimes:
        try:
            return int(runtimes[0]) if runtimes[0] else None
        except Exception:
            return None
    return None


def _run_years_from_details(
    data: dict[str, Any], kind: MediaKind
) -> tuple[int | None, int | None]:
    if kind != MediaKind.SHOW:
        return None, None
    first = _year_from_date(data.get("first_air_date"))
    last = _year_from_date(data.get("last_air_date"))
    return first, last


def _candidate_from_raw(
    raw: dict[str, Any], kind: MediaKind, image_base: str | None
) -> Candidate:
    title = (
        raw.get("title")
        or raw.get("name")
        or raw.get("original_title")
        or raw.get("original_name")
        or ""
    )
    original_title = raw.get("original_title") or raw.get("original_name") or title
    poster_path = raw.get("poster_path")
    poster_url = _poster_url(image_base, poster_path)
    return Candidate(
        tmdb_id=int(raw.get("id") or 0),
        kind=kind,
        title=str(title),
        original_title=str(original_title),
        year=_year_from_date(raw.get("release_date") or raw.get("first_air_date")),
        original_language=str(raw.get("original_language") or ""),
        runtime_min=None,
        genres=[],
        countries=[],
        overview=str(raw.get("overview") or ""),
        poster_url=poster_url,
        imdb_id=None,
        score=0.0,
        reasons=[],
    )


def _apply_details(
    candidate: Candidate, data: dict[str, Any], image_base: str | None
) -> None:
    if not data:
        return
    title = data.get("title") or data.get("name")
    if title:
        candidate.title = str(title)
    original_title = data.get("original_title") or data.get("original_name")
    if original_title:
        candidate.original_title = str(original_title)
    year = _year_from_date(data.get("release_date") or data.get("first_air_date"))
    if year is not None:
        candidate.year = year
    original_language = data.get("original_language")
    if original_language:
        candidate.original_language = str(original_language)
    overview = data.get("overview")
    if overview is not None:
        candidate.overview = str(overview)
    candidate.genres = _parse_genres(data)
    candidate.countries = _parse_countries(data)
    external = data.get("external_ids") or {}
    imdb = data.get("imdb_id") or (
        external.get("imdb_id") if isinstance(external, dict) else None
    )
    candidate.imdb_id = str(imdb) if imdb else candidate.imdb_id
    candidate.runtime_min = _runtime_from_details(data, candidate.kind)
    poster_path = data.get("poster_path")
    if image_base and poster_path:
        candidate.poster_url = _poster_url(image_base, poster_path)


def _sort_hits(hits: list[_Hit]) -> None:
    hits.sort(key=lambda h: (h.candidate.score, h.vote_count), reverse=True)


def _merge_hits(a: list[_Hit], b: list[_Hit]) -> list[_Hit]:
    """Union of two hit lists, keeping the better-scored copy of each hit."""
    best: dict[tuple[MediaKind, int], _Hit] = {}
    for hit in [*a, *b]:
        key = (hit.candidate.kind, hit.candidate.tmdb_id)
        old = best.get(key)
        if old is None or hit.candidate.score > old.candidate.score:
            best[key] = hit
    out = list(best.values())
    _sort_hits(out)
    return out


def _needs_yearless_retry(title: str, hits: list[_Hit]) -> bool:
    if not hits:
        return True
    return all(title_ratio(title, hit.candidate) < 85.0 for hit in hits[:5])


async def _search_candidates(
    config: AppConfig,
    client: TmdbClient,
    title: str,
    search_year: int | None,
    score_year: int | None,
    item_kind: MediaKind,
    kinds: Sequence[MediaKind],
    duration_min: float | None,
    limit: int = 8,
) -> list[_Hit]:
    """Search TMDB and score the hits.

    ``search_year`` filters the search request; ``score_year`` is the year we
    compare against. The year-less retry passes ``search_year=None`` but keeps
    ``score_year`` so the run-of-the-show and exact-year rules still apply.
    """
    if not title:
        return []
    image_base = await client.image_base_url()
    raw_hits: dict[tuple[MediaKind, int], _Hit] = {}

    for kind in kinds:
        try:
            if kind == MediaKind.MOVIE:
                results = await client.search_movie(title, search_year)
            else:
                results = await client.search_tv(title, search_year)
        except TmdbNotFound:
            results = []

        for raw in results:
            if not isinstance(raw, dict):
                continue
            try:
                tmdb_id = int(raw.get("id") or 0)
            except Exception:
                tmdb_id = 0
            if tmdb_id <= 0:
                continue
            key = (kind, tmdb_id)
            if key in raw_hits:
                continue
            candidate = _candidate_from_raw(raw, kind, image_base)
            try:
                votes = int(raw.get("vote_count") or 0)
            except Exception:
                votes = 0
            candidate.score, candidate.reasons = score_candidate(
                title, score_year, item_kind, candidate, None, votes
            )
            raw_hits[key] = _Hit(candidate=candidate, vote_count=votes, raw=raw)

    hits = sorted(
        raw_hits.values(), key=lambda h: (h.candidate.score, h.vote_count), reverse=True
    )
    final: list[_Hit] = []
    for hit in hits[:limit]:
        try:
            if hit.candidate.kind == MediaKind.MOVIE:
                data = await client.movie_details(hit.candidate.tmdb_id)
            else:
                data = await client.tv_details(hit.candidate.tmdb_id)
        except TmdbNotFound:
            continue
        _apply_details(hit.candidate, data, image_base)
        hit.raw = data
        hit.first_year, hit.last_year = _run_years_from_details(
            data, hit.candidate.kind
        )
        hit.candidate.score, hit.candidate.reasons = score_candidate(
            title,
            score_year,
            item_kind,
            hit.candidate,
            duration_min,
            hit.vote_count,
            hit.first_year,
            hit.last_year,
        )
        final.append(hit)

    _sort_hits(final)
    return final


async def _episode_evidence(
    client: TmdbClient,
    item: SourceItem,
    hits: list[_Hit],
    limit: int = 3,
) -> None:
    """Add episode-title and episode-number evidence to the top show candidates."""
    show_hits = [h for h in hits[:limit] if h.candidate.kind == MediaKind.SHOW]
    if not show_hits or not item.episodes:
        return

    episodes = [ep for ep in item.episodes if not ep.absolute]
    if not episodes:
        return

    seasons = sorted({ep.season for ep in episodes})
    if not seasons:
        return

    for hit in show_hits:
        if hit.candidate.tmdb_id <= 0:
            continue

        names_by_season: dict[int, dict[int, str]] = {}
        numbers_by_season: dict[int, set[int]] = {}
        counts_by_season: dict[int, int] = {}

        for season in seasons:
            try:
                data = await client.season(hit.candidate.tmdb_id, season)
            except TmdbError:
                continue
            ep_list = data.get("episodes") or []
            if not isinstance(ep_list, list) or not ep_list:
                continue

            season_names: dict[int, str] = {}
            season_numbers: set[int] = set()
            for ep_data in ep_list:
                if not isinstance(ep_data, dict):
                    continue
                ep_num = ep_data.get("episode_number")
                if ep_num is None:
                    continue
                try:
                    num = int(ep_num)
                except Exception:
                    continue
                season_numbers.add(num)
                name = ep_data.get("name")
                if name:
                    season_names[num] = str(name)

            names_by_season[season] = season_names
            numbers_by_season[season] = season_numbers
            counts_by_season[season] = len(ep_list)

        titled = 0
        matches = 0
        for ep in episodes:
            if not ep.title:
                continue
            titled += 1
            tmdb_name = names_by_season.get(ep.season, {}).get(ep.episode)
            if not tmdb_name:
                continue
            ratio = float(
                fuzz.ratio(normalize_title(ep.title), normalize_title(tmdb_name))
            )
            if ratio >= 85.0:
                matches += 1

        if titled:
            points = 30.0 * matches / titled
            hit.candidate.score += points
            hit.candidate.reasons.append(f"{matches} of {titled} episode titles match")

        all_known = True
        all_exist = True
        beyond_reason: str | None = None
        for season in seasons:
            season_eps = [ep.episode for ep in episodes if ep.season == season]
            if not season_eps:
                continue
            if season not in numbers_by_season:
                all_known = False
                continue
            nums = numbers_by_season[season]
            count = counts_by_season.get(season, 0)
            if any(ep not in nums for ep in season_eps):
                all_exist = False
            if any(ep > count for ep in season_eps):
                mn, mx = min(season_eps), max(season_eps)
                beyond_reason = (
                    f"episodes {mn}–{mx} go beyond season {season} ({count} episodes)"
                )

        if beyond_reason is not None:
            hit.candidate.score -= 10.0
            hit.candidate.reasons.append(beyond_reason)
        elif all_known and all_exist:
            hit.candidate.score += 5.0
            for season in seasons:
                season_eps = [ep.episode for ep in episodes if ep.season == season]
                if not season_eps:
                    continue
                mn, mx = min(season_eps), max(season_eps)
                hit.candidate.reasons.append(
                    f"episodes {mn}–{mx} all exist in season {season}"
                )

        hit.candidate.score = _clamp(hit.candidate.score)

    _sort_hits(hits)


async def _audio_tiebreak(
    config: AppConfig,
    client: TmdbClient,
    item: SourceItem,
    hits: list[_Hit],
    audio: AudioVerifier | None,
    progress: ProgressSink,
) -> None:
    if len(hits) < 2 or audio is None or not item.videos:
        return
    margin = config.behavior.auto_approve_margin
    if hits[0].candidate.score - hits[1].candidate.score >= margin:
        return

    video = max(item.videos, key=lambda f: f.size)
    lang = item.audio_languages.get(video.path)
    if not lang:
        try:
            detected = await audio.detect_language(video.path)
        except Exception:
            detected = None
        if detected and detected[0]:
            lang = str(detected[0]).lower()
            if lang == "und":
                lang = None
            else:
                item.audio_languages[video.path] = lang

    if lang:
        progress.log(f"audio language {lang} used for tie-break")
        for hit in hits:
            if hit.candidate.original_language == lang:
                hit.candidate.score = _clamp(hit.candidate.score + 15)
                hit.candidate.reasons.append(
                    f"audio language {lang} matches original_language"
                )
            else:
                hit.candidate.score = _clamp(hit.candidate.score - 10)
                hit.candidate.reasons.append(
                    f"audio language {lang} differs from original_language"
                )
        _sort_hits(hits)
        if hits[0].candidate.score - hits[1].candidate.score >= margin:
            return

    try:
        transcript = await audio.transcript(video.path)
    except Exception:
        transcript = None
    if not transcript:
        return

    progress.log("audio transcript used for tie-break")
    for hit in hits[:2]:
        try:
            if hit.candidate.kind == MediaKind.MOVIE:
                data = await client.movie_details(hit.candidate.tmdb_id, credits=True)
            else:
                data = await client.tv_details(hit.candidate.tmdb_id, credits=True)
        except Exception:
            data = None
        terms = _cast_terms(data)
        if hit.candidate.overview:
            terms.append(hit.candidate.overview)
        text = " ".join(str(t) for t in terms if t)
        bonus = audio_overlap_bonus(transcript, text)
        if bonus:
            hit.candidate.score = _clamp(hit.candidate.score + bonus)
            hit.candidate.reasons.append(f"audio transcript overlap +{bonus}")
    _sort_hits(hits)


async def _season_counts_until(
    client: TmdbClient, tmdb_id: int, stop_season: int
) -> int:
    cumulative = 0
    for season in range(1, max(1, stop_season)):
        data = await client.season(tmdb_id, season)
        episodes = data.get("episodes") or []
        cumulative += len(episodes) if isinstance(episodes, list) else 0
    return cumulative


async def _season_counts_until_max(
    client: TmdbClient, tmdb_id: int, max_abs: int
) -> list[tuple[int, int, int, int]]:
    counts: list[tuple[int, int, int, int]] = []
    if max_abs <= 0:
        return counts
    cumulative = 0
    season = 1
    while cumulative < max_abs and season <= 30:
        data = await client.season(tmdb_id, season)
        episodes = data.get("episodes") or []
        count = len(episodes) if isinstance(episodes, list) else 0
        if count == 0:
            break
        counts.append((season, count, cumulative, cumulative + count))
        cumulative += count
        season += 1
    return counts


def _cast_terms(data: dict[str, Any] | None) -> list[str]:
    terms: list[str] = []
    if not data:
        return terms
    for cast in data.get("cast") or []:
        if not isinstance(cast, dict):
            continue
        characters = cast.get("characters")
        if isinstance(characters, list):
            terms.extend(str(x) for x in characters if x)
        elif cast.get("character"):
            terms.append(str(cast["character"]))
    return terms


async def _episode_titles(
    config: AppConfig,
    client: TmdbClient,
    identity: Identity,
    item: SourceItem,
) -> tuple[dict[tuple[int, int], str], list[str], bool]:
    titles: dict[tuple[int, int], str] = {}
    reasons: list[str] = []
    ambiguous = False

    if identity.kind != MediaKind.SHOW or identity.tmdb_id is None or not item.episodes:
        return titles, reasons, ambiguous

    absolute_eps = [ep for ep in item.episodes if ep.absolute]
    hint_season = _hint_int(item, ("season", "s"))
    hint_offset = _hint_int(item, ("offset", "abs_offset"))
    counts: list[tuple[int, int, int, int]] = []

    if absolute_eps:
        try:
            if hint_season is not None and hint_offset is None:
                hint_offset = await _season_counts_until(
                    client, identity.tmdb_id, hint_season
                )
            if hint_season is None and hint_offset is not None:
                hint_season = 1
            if hint_season is None and hint_offset is None:
                max_abs = max((ep.episode for ep in absolute_eps), default=0)
                counts = await _season_counts_until_max(
                    client, identity.tmdb_id, max_abs
                )
        except TmdbUnavailable:
            reasons.append("season data unavailable for absolute mapping")
            ambiguous = True

        for ep in absolute_eps:
            original = ep.episode
            original_end = ep.episode_end
            was_absolute = ep.absolute
            if hint_season is not None and hint_offset is not None:
                season = hint_season
                episode = original - hint_offset
                if episode < 1:
                    ambiguous = True
                    reasons.append(f"absolute {original} could not be mapped with hint offset")
                    continue
                ep.season = season
                ep.episode = episode
                ep.absolute = False
                if original_end is not None and was_absolute:
                    end = original_end - hint_offset
                    if end < 1:
                        ambiguous = True
                        ep.episode_end = None
                    else:
                        ep.episode_end = end
                reasons.append(f"absolute {original} -> S{season:02d}E{episode:02d} (hint)")
            else:
                found: tuple[int, int, int, int] | None = None
                for season, _count, before, after in counts:
                    if before < original <= after:
                        found = (season, original - before, before, after)
                        break
                if found:
                    season, episode, before, after = found
                    ep.season = season
                    ep.episode = episode
                    ep.absolute = False
                    if original_end is not None and was_absolute:
                        if before < original_end <= after:
                            ep.episode_end = original_end - before
                        else:
                            ambiguous = True
                            ep.episode_end = None
                    reasons.append(f"absolute {original} -> S{season:02d}E{episode:02d}")
                else:
                    ambiguous = True
                    reasons.append(f"absolute {original} could not be mapped")

    seasons = {
        ep.season
        for ep in item.episodes
        if not ep.absolute and ep.season is not None
    }
    if not seasons:
        return titles, reasons, ambiguous

    for season in sorted(seasons):
        try:
            data = await client.season(identity.tmdb_id, season)
        except TmdbUnavailable:
            reasons.append(f"season {season} unavailable")
            continue
        except TmdbNotFound:
            continue

        for ep in data.get("episodes") or []:
            if not isinstance(ep, dict):
                continue
            ep_num = ep.get("episode_number")
            if ep_num is None:
                continue
            try:
                season_num = int(ep.get("season_number", season))
                episode_num = int(ep_num)
            except Exception:
                continue
            name = ep.get("name")
            if name:
                titles[(season_num, episode_num)] = str(name)

    return titles, reasons, ambiguous


def _candidate_from_identity(identity: Identity) -> Candidate:
    return Candidate(
        tmdb_id=identity.tmdb_id or 0,
        kind=identity.kind,
        title=identity.title,
        original_title="",
        year=identity.year,
        original_language=identity.original_language or "",
        runtime_min=identity.runtime_min,
        genres=list(identity.genres),
        countries=list(identity.countries),
        overview=identity.overview,
        poster_url=None,
        imdb_id=identity.imdb_id,
        score=100.0,
        reasons=list(identity.reasons),
    )


async def _identity_from_tmdb_id(
    config: AppConfig,
    client: TmdbClient,
    item: SourceItem,
    tmdb_id: int,
    kind: MediaKind,
    status: IdStatus,
    reason: str,
) -> Identity:
    image_base = await client.image_base_url()
    title = _query_title(item)
    year = _query_year(config, item)
    candidate = Candidate(
        tmdb_id=tmdb_id,
        kind=kind,
        title=title,
        original_title="",
        year=year,
        original_language="",
        runtime_min=None,
        genres=[],
        countries=[],
        overview="",
        poster_url=None,
        imdb_id=None,
        score=100.0,
        reasons=[reason],
    )

    try:
        if kind == MediaKind.MOVIE:
            data = await client.movie_details(tmdb_id)
        else:
            data = await client.tv_details(tmdb_id)
        _apply_details(candidate, data, image_base)
    except TmdbError:
        pass

    identity = Identity(
        item_key=item.key,
        kind=kind,
        title=candidate.title,
        year=candidate.year,
        tmdb_id=tmdb_id,
        imdb_id=candidate.imdb_id,
        status=status,
        confidence=100.0,
        reasons=[reason],
        candidates=[candidate],
        genres=list(candidate.genres),
        countries=list(candidate.countries),
        original_language=candidate.original_language or None,
        overview=candidate.overview,
        runtime_min=candidate.runtime_min,
        episode_titles={},
        library=None,
    )

    if kind == MediaKind.SHOW:
        ep_titles, ep_reasons, ambiguous = await _episode_titles(
            config, client, identity, item
        )
        identity.episode_titles = ep_titles
        identity.reasons.extend(ep_reasons)
        if ambiguous and identity.status != IdStatus.CONFIRMED:
            identity.status = IdStatus.NEEDS_REVIEW

    identity.library = pick_library(config, identity)
    return identity


def _identity_from_imdb_id(item: SourceItem, kind: MediaKind, imdb_id: str) -> Identity:
    identity = Identity(
        item_key=item.key,
        kind=kind,
        title=item.title,
        year=item.year,
        tmdb_id=None,
        imdb_id=imdb_id,
        status=IdStatus.CONFIRMED,
        confidence=100.0,
        reasons=["recorded IMDb id"],
        candidates=[],
        episode_titles={},
        library=None,
    )
    return identity


async def _enrich_confirmed(
    config: AppConfig,
    client: TmdbClient,
    identity: Identity,
    item: SourceItem,
) -> Identity:
    if identity.tmdb_id is None:
        if not identity.library or identity.library not in config.libraries:
            identity.library = pick_library(config, identity)
        return identity

    image_base = await client.image_base_url()
    candidate = (
        identity.candidates[0] if identity.candidates else _candidate_from_identity(identity)
    )
    try:
        if identity.kind == MediaKind.SHOW:
            data = await client.tv_details(identity.tmdb_id)
        else:
            data = await client.movie_details(identity.tmdb_id)
        _apply_details(candidate, data, image_base)
        identity.title = candidate.title
        identity.year = candidate.year
        identity.imdb_id = candidate.imdb_id
        identity.genres = list(candidate.genres)
        identity.countries = list(candidate.countries)
        identity.original_language = candidate.original_language or None
        identity.overview = candidate.overview
        identity.runtime_min = candidate.runtime_min
        if identity.candidates:
            identity.candidates[0] = candidate
        else:
            identity.candidates = [candidate]
    except TmdbError:
        pass

    if identity.kind == MediaKind.SHOW:
        titles, ep_reasons, _ambiguous = await _episode_titles(
            config, client, identity, item
        )
        if titles:
            identity.episode_titles = titles
        identity.reasons.extend(ep_reasons)

    if not identity.library or identity.library not in config.libraries:
        identity.library = pick_library(config, identity)
    return identity


def _confirmed_from_store(store: Store, item: SourceItem) -> Identity | None:
    try:
        raw = store.get_identity(item.key)
        if not raw:
            return None
        identity = _identity_from_dict(raw)
    except Exception:
        return None
    if identity.status != IdStatus.CONFIRMED:
        return None
    from mediakeep.agree import item_name, trusted_pick

    # Only a match you picked is kept as is, and only while it fits the name on disk or
    # you kept it. One that came from a remembered fix ("recorded TMDB id"), or a pick
    # that doesn't fit (the old one-click button matched on the year alone), is worked
    # out again.
    if not trusted_pick(identity, item_name(item)):
        return None
    return identity


def _identity_from_path_store(store: Store, item: SourceItem) -> Identity | None:
    if item.hint_options.get("_tmdb_id") or item.hint_options.get("_imdb_id"):
        return None

    path: Path | None = None
    for p in item.paths:
        if p.is_dir():
            path = p
            break
    if path is None:
        return None

    data = store.get_path_identity(path)
    if not data:
        return None

    try:
        identity = models.identity_from_dict(data)
    except Exception:
        return None

    if identity.kind == MediaKind.UNKNOWN or not identity.title:
        return None
    if identity.tmdb_id is None and identity.imdb_id is None:
        return None
    from mediakeep.agree import USER_REASONS, folder_agrees, trusted_pick

    if not trusted_pick(identity, path.name) and not folder_agrees(path.name, identity):
        return None

    identity.item_key = item.key
    identity.status = IdStatus.CONFIRMED
    identity.confidence = 100.0
    identity.reasons = ["path identity"] + [r for r in identity.reasons if r in USER_REASONS]
    return identity


def _identify_detail(item: SourceItem, identity: Identity | None) -> str:
    """One line for the progress list: the name on disk and what it was matched to."""
    name = item.paths[0].name if item.paths else (item.title or item.key)
    if identity is None or not identity.title or identity.status == IdStatus.UNMATCHED:
        return f"{name}  →  no match"
    year = f" ({identity.year})" if identity.year else ""
    return f"{name}  →  {identity.title}{year}"


class TmdbIdentifier:
    def __init__(
        self,
        config: AppConfig,
        store: Store,
        audio: AudioVerifier | None,
        client: TmdbClient | None = None,
        probe: ProbeRunner | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.audio = audio
        self.client = client or TmdbClient(config, store)
        self.probe = probe or ProbeRunner(config, store)

    async def identify(
        self, items: Sequence[SourceItem], progress: ProgressSink
    ) -> dict[str, Identity]:
        out: dict[str, Identity] = {}
        if not items:
            try:
                await self.client.close()
            except Exception:
                pass
            return out

        sem = asyncio.Semaphore(8)

        async def worker(item: SourceItem) -> tuple[str, Identity]:
            async with sem:
                identity: Identity | None = None
                try:
                    identity = await self._identify_one(item, progress)
                except Exception as exc:
                    logger.exception("identification failed for %s", item.key)
                    identity = _unmatched(item, f"identification error: {exc}")
                finally:
                    progress.advance(1, _identify_detail(item, identity))
                return item.key, identity

        results = await asyncio.gather(*(worker(item) for item in items))
        out = dict(results)
        try:
            await self.client.close()
        except Exception:
            pass
        return out

    async def _identify_one(self, item: SourceItem, progress: ProgressSink) -> Identity:
        if progress.cancelled():
            return _unmatched(item, "cancelled")

        confirmed = _confirmed_from_store(self.store, item)
        if confirmed is not None:
            confirmed = await _enrich_confirmed(self.config, self.client, confirmed, item)
            if not confirmed.library or confirmed.library not in self.config.libraries:
                confirmed.library = pick_library(self.config, confirmed)
            return confirmed

        path_ident = _identity_from_path_store(self.store, item)
        if path_ident is not None:
            if (
                not path_ident.library
                or path_ident.library not in self.config.libraries
            ):
                path_ident.library = pick_library(self.config, path_ident)
            return path_ident

        hint_id = _hint_tmdb_id(item)
        if hint_id is not None:
            kind = _kind_for_choice(item, _hint_kind(item))
            identity = await _identity_from_tmdb_id(
                self.config,
                self.client,
                item,
                hint_id,
                kind,
                IdStatus.CONFIRMED,
                "recorded TMDB id",
            )
            self.store.set_identity(item.key, _identity_to_dict(identity), identity.status.value)
            return identity

        hint_imdb = _hint_imdb_id(item)
        if hint_imdb is not None:
            kind = _kind_for_choice(item, _hint_kind(item))
            identity = _identity_from_imdb_id(item, kind, hint_imdb)
            identity.library = pick_library(self.config, identity)
            self.store.set_identity(item.key, _identity_to_dict(identity), identity.status.value)
            return identity

        title = _query_title(item)
        if not title:
            return _unmatched(item, "no title to search")

        year = _query_year(self.config, item)
        item_kind = _item_kind(item)
        weak = _is_weak(item, item_kind)

        try:
            duration_sec = await self.probe.enrich_item(item, self.audio)
        except Exception:
            duration_sec = None
        duration_min = duration_sec / 60.0 if duration_sec and duration_sec > 0 else None

        kinds: list[MediaKind] = (
            [MediaKind.MOVIE, MediaKind.SHOW]
            if weak or item_kind == MediaKind.UNKNOWN
            else [item_kind]
        )

        try:
            hits = await _search_candidates(
                self.config,
                self.client,
                title,
                year,
                year,
                item_kind,
                kinds,
                duration_min,
            )

            if year is not None and (
                item_kind == MediaKind.SHOW
                or any(h.candidate.kind == MediaKind.SHOW for h in hits)
            ):
                if _needs_yearless_retry(title, hits):
                    # Drop the year only for the request; keep scoring with it.
                    hits2 = await _search_candidates(
                        self.config,
                        self.client,
                        title,
                        None,
                        year,
                        item_kind,
                        kinds,
                        duration_min,
                    )
                    hits = _merge_hits(hits, hits2)

            if not hits and item_kind in (MediaKind.MOVIE, MediaKind.SHOW):
                other = (
                    MediaKind.SHOW if item_kind == MediaKind.MOVIE else MediaKind.MOVIE
                )
                hits = await _search_candidates(
                    self.config,
                    self.client,
                    title,
                    year,
                    year,
                    item_kind,
                    [other],
                    duration_min,
                )
        except TmdbUnavailable:
            return _unmatched(item, "TMDB unreachable")
        except TmdbError as exc:
            return _unmatched(item, f"TMDB error: {exc}")

        if not hits:
            return _unmatched(item, "no TMDB candidates")

        await _audio_tiebreak(self.config, self.client, item, hits, self.audio, progress)
        await _episode_evidence(self.client, item, hits)

        top = hits[0]
        top_kind = top.candidate.kind
        kind_mismatch = item_kind != MediaKind.UNKNOWN and item_kind != top_kind

        second = hits[1] if len(hits) > 1 else None
        lead = top.candidate.score - second.candidate.score if second else 100.0
        threshold = self.config.behavior.auto_approve_confidence
        margin = self.config.behavior.auto_approve_margin
        needs_review = top.candidate.score < threshold or lead < margin or kind_mismatch
        # A year in the name that disagrees with the match (by more than one) always needs
        # a look, however good the title looks: "101 Hedgehogs (1961)" is not the 1996 film.
        name_year = item.year
        year_conflict = (
            name_year is not None
            and top.candidate.year is not None
            and top_kind != MediaKind.SHOW
            and abs(top.candidate.year - name_year) > 1
        )
        if year_conflict:
            needs_review = True

        reasons = [f"score {top.candidate.score:.0f}"]
        if year_conflict:
            reasons.append(f"the name says {name_year}, the match is {top.candidate.year}")
        if kind_mismatch:
            reasons.append(f"TMDB kind {top_kind} differs from classification {item.kind}")
        reasons.extend(top.candidate.reasons)

        identity = Identity(
            item_key=item.key,
            kind=top_kind,
            title=top.candidate.title,
            year=top.candidate.year,
            tmdb_id=top.candidate.tmdb_id,
            imdb_id=top.candidate.imdb_id,
            status=IdStatus.NEEDS_REVIEW if needs_review else IdStatus.AUTO,
            confidence=top.candidate.score,
            reasons=reasons,
            candidates=[h.candidate for h in hits[:5]],
            genres=list(top.candidate.genres),
            countries=list(top.candidate.countries),
            original_language=top.candidate.original_language or None,
            overview=top.candidate.overview,
            runtime_min=top.candidate.runtime_min,
            episode_titles={},
            library=None,
        )

        if identity.kind == MediaKind.SHOW:
            titles, ep_reasons, ambiguous = await _episode_titles(
                self.config, self.client, identity, item
            )
            identity.episode_titles = titles
            identity.reasons.extend(ep_reasons)
            if ambiguous:
                identity.status = IdStatus.NEEDS_REVIEW
                identity.reasons.append("episode numbering ambiguous")

        identity.library = pick_library(self.config, identity)
        return identity


async def search(
    config: AppConfig,
    store: Store,
    query: str,
    year: int | None,
    kind: MediaKind | None,
) -> list[Candidate]:
    if not query or not (config.tmdb.api_key or config.tmdb.read_token):
        return []

    client = TmdbClient(config, store)
    try:
        if kind == MediaKind.SHOW:
            kinds = [MediaKind.SHOW]
        elif kind in (MediaKind.MOVIE, MediaKind.DVD, MediaKind.BLURAY):
            kinds = [MediaKind.MOVIE]
        else:
            kinds = [MediaKind.MOVIE, MediaKind.SHOW]
        hits = await _search_candidates(
            config,
            client,
            query,
            year,
            year,
            kind or MediaKind.UNKNOWN,
            kinds,
            None,
            limit=20,
        )
        return [h.candidate for h in hits]
    except TmdbError:
        return []
    finally:
        try:
            await client.close()
        except Exception:
            pass


async def identity_for(
    config: AppConfig,
    store: Store,
    item: SourceItem,
    tmdb_id: int,
    kind: MediaKind,
) -> Identity:
    eff_kind = _kind_for_choice(item, kind)

    if not (config.tmdb.api_key or config.tmdb.read_token):
        identity = Identity(
            item_key=item.key,
            kind=eff_kind,
            title=item.title,
            year=item.year,
            tmdb_id=tmdb_id,
            imdb_id=None,
            status=IdStatus.CONFIRMED,
            confidence=100.0,
            reasons=["confirmed without TMDB"],
            candidates=[],
            library=None,
        )
        identity.library = pick_library(config, identity)
        store.set_identity(item.key, _identity_to_dict(identity), identity.status.value)
        return identity

    client = TmdbClient(config, store)
    try:
        identity = await _identity_from_tmdb_id(
            config, client, item, tmdb_id, eff_kind, IdStatus.CONFIRMED, "confirmed by user"
        )
        store.set_identity(item.key, _identity_to_dict(identity), identity.status.value)
        return identity
    finally:
        try:
            await client.close()
        except Exception:
            pass
