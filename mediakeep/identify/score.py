from __future__ import annotations

import math
import re
import unicodedata

from rapidfuzz import fuzz

from mediakeep.models import Candidate, MediaKind


def normalize_title(text: str) -> str:
    """Normalize a title for comparison according to the matching contract."""
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("'", "").replace("’", "")
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    parts = text.split()
    if parts and parts[0] in {"the", "a", "an"}:
        parts = parts[1:]
    return " ".join(parts)


# A candidate title often carries a long alternative title after "or:", a
# subtitle after ":" or a part after " - ". The part before the first marker is
# the main title, and a file named after the main title is an exact match for
# that film ("Dr. Moonbeam" == "Dr. Moonbeam or: The Long Night…").
_MAIN_TITLE_MARKERS = (" or:", ":", " - ")


def _main_title(text: str) -> str:
    """The text before the first `` or:``, ``:`` or `` - ``, or ```` if none."""
    if not text:
        return ""
    low = text.lower()
    idx = -1
    for marker in _MAIN_TITLE_MARKERS:
        found = low.find(marker)
        if found != -1 and (idx == -1 or found < idx):
            idx = found
    if idx <= 0:
        return ""
    return text[:idx].strip()


def _main_title_counts(main_norm: str, query_words: int) -> bool:
    """A very short or single-word main title only counts for short queries.

    "Venture" may match "Venture: Unbound", but "Venture Unbound Final
    Reckoning" must not fall back to the main title "Venture".
    """
    if not main_norm:
        return False
    main_words = len(main_norm.split())
    if main_words <= 1 or len(main_norm) < 4:
        return query_words <= main_words
    return True


def _best_title_ratio(query: str, candidate: Candidate) -> tuple[float, bool, str]:
    """Best whole-title or main-title ratio, exactness, and the main title.

    The third value is the raw main title text when the exact match came from a
    main title, otherwise an empty string.
    """
    q = normalize_title(query)
    if not q:
        return 0.0, False, ""
    q_words = len(q.split())

    best = 0.0
    exact = False
    exact_main = ""

    for raw in (candidate.title, candidate.original_title):
        if not raw:
            continue
        v = normalize_title(raw)
        if v:
            ratio = float(fuzz.ratio(q, v))
            if ratio > best:
                best = ratio
            if q == v:
                exact = True
                exact_main = ""

        main_raw = _main_title(raw)
        m = normalize_title(main_raw)
        if not m or not _main_title_counts(m, q_words):
            continue
        main_ratio = float(fuzz.ratio(q, m))
        if main_ratio > best:
            best = main_ratio
        if q == m and not exact:
            exact = True
            exact_main = main_raw

    return best, exact, exact_main


def title_ratio(query: str, candidate: Candidate) -> float:
    """Normalized title similarity (whole title or main title), 0..100."""
    return _best_title_ratio(query, candidate)[0]


def _kind_agreement(item_kind: MediaKind, candidate_kind: MediaKind) -> int:
    if item_kind == MediaKind.UNKNOWN or item_kind == MediaKind.COLLECTION:
        return 0
    if item_kind in (MediaKind.MOVIE, MediaKind.DVD, MediaKind.BLURAY):
        return 10 if candidate_kind == MediaKind.MOVIE else -10
    if item_kind == MediaKind.SHOW:
        return 10 if candidate_kind == MediaKind.SHOW else -10
    return 0


def score_candidate(
    query_title: str,
    query_year: int | None,
    item_kind: MediaKind,
    candidate: Candidate,
    duration_min: float | None,
    vote_count: int,
    first_year: int | None = None,
    last_year: int | None = None,
) -> tuple[float, list[str]]:
    """Documented TMDB scoring formula, clamped to 0..100.

    Components:
      - Title similarity: up to +50 from normalized ``fuzz.ratio`` between the
        query and the candidate title/original_title or their main title.
      - Year: +25 exact, +15 for a show year inside the show's run, +15 off by
        one when run years are unknown, -20 otherwise. When our year is unknown,
        an exact title plus matching runtime is enough for +25.
      - Runtime: +15 within 5%, +5 within 15%, -15 when more than 30% off.
      - Kind evidence: +10 when classification agrees, -10 when it disagrees.
      - Vote tie-breaker: 0..3, log-scaled against 100k votes.
    """
    total = 0.0
    reasons: list[str] = []

    best_ratio, exact_title, exact_main = _best_title_ratio(query_title, candidate)
    title_score = (best_ratio / 100.0) * 50.0
    total += title_score
    if title_score:
        reasons.append(f"title similarity {best_ratio:.0f}%")
        if exact_title:
            if exact_main:
                reasons.append(f'title matches main title "{exact_main}"')
            else:
                reasons.append("title matches exactly")
    else:
        reasons.append("title unknown")

    runtime_pct: float | None = None
    if duration_min and candidate.runtime_min:
        runtime_pct = (
            abs(duration_min - float(candidate.runtime_min))
            / float(candidate.runtime_min)
            * 100.0
        )

    if query_year is None:
        reasons.append("year unknown, not scored")
        if exact_title and runtime_pct is not None and runtime_pct <= 5.0:
            total += 25
            reasons.append("no year in the name; exact title and runtime match instead")
    elif item_kind == MediaKind.SHOW and first_year is not None and last_year is not None:
        if first_year <= query_year <= last_year:
            total += 15
            reasons.append(f"{query_year} is within the show's run ({first_year}–{last_year})")
        else:
            total -= 20
            reasons.append(f"year mismatch ({candidate.year})")
    elif candidate.year is None:
        total -= 20
        reasons.append("candidate year unknown")
    elif candidate.year == query_year:
        total += 25
        reasons.append("year matches")
    elif abs(candidate.year - query_year) == 1:
        total += 15
        reasons.append("year off by one")
    else:
        total -= 20
        reasons.append(f"year mismatch ({candidate.year})")

    if runtime_pct is not None:
        reason = f"runtime {duration_min:.0f} min vs {candidate.runtime_min} min"
        if runtime_pct <= 5.0:
            total += 15
            reasons.append(reason)
        elif runtime_pct <= 15.0:
            total += 5
            reasons.append(reason)
        elif runtime_pct > 30.0:
            total -= 15
            reasons.append(reason)

    kind_delta = _kind_agreement(item_kind, candidate.kind)
    if kind_delta > 0:
        total += kind_delta
        reasons.append("kind matches")
    elif kind_delta < 0:
        total += kind_delta
        reasons.append("kind mismatch")

    if vote_count > 0:
        vote_bonus = 3.0 * min(
            1.0, math.log10(1.0 + float(vote_count)) / math.log10(100001.0)
        )
        total += vote_bonus
        if vote_bonus > 0.5:
            reasons.append(f"votes {vote_count}")

    return max(0.0, min(100.0, total)), reasons


def audio_overlap_bonus(transcript: str, text: str) -> int:
    if not transcript or not text:
        return 0
    a = transcript[:12000].lower()
    b = text[:12000].lower()
    ratio = float(fuzz.token_set_ratio(a, b))
    return max(0, min(10, int(round(ratio / 10.0))))
