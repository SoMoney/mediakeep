"""Does a remembered match still fit the folder it was remembered for?

After an apply, mediakeep records the match for the destination folder, so the next scan
doesn't have to ask TMDB again. A record made by a wrong match (for example the old search
for "Mr" that found "Mr. Grumble" for "Mr. Whiskers & Pip (2014)") must not be trusted
for ever: it only counts while the folder name agrees with it, or when you chose it.
"""

from __future__ import annotations

from mediakeep.models import Candidate, Identity, IdStatus, SourceItem

# Reasons that mark a match you made yourself (match page, manual edit, kind switch).
USER_REASONS = {
    "match chosen by user",
    "confirmed by user",
    "manual edit",
    "title chosen by user",
    "kind chosen by user",
    "library chosen by user",
}


def chosen_by_user(identity: Identity | None) -> bool:
    return identity is not None and any(r in USER_REASONS for r in identity.reasons)


def _words(text: str) -> list[str]:
    from mediakeep.identify.score import normalize_title

    return normalize_title(text).split()


def folder_agrees(folder_name: str, identity: Identity) -> bool:
    """True when the folder's own title and year fit the match.

    The year must be within one of the match's. The titles agree when one's words all
    appear in the other ("The Copper Kettle Gang" fits "The Copper Kettle Gang Movie"), so a
    folder keeping your own shorter title still counts.
    """
    from mediakeep.parse import _KNOWN_EXTS, parse_release_name

    has_ext = "." in folder_name and folder_name.rsplit(".", 1)[1].lower() in _KNOWN_EXTS
    parsed = parse_release_name(folder_name if has_ext else folder_name + ".x")
    if parsed.year is not None and identity.year is not None and abs(parsed.year - identity.year) > 1:
        return False
    mine = _words(parsed.title)
    if not mine:
        return False
    # Compare with TMDB's own title for that match: the remembered title may be a
    # shortened "keep your title" version (an old one could even be cut to "Mr").
    titles = [c.title for c in identity.candidates if c.tmdb_id == identity.tmdb_id and c.title]
    titles.append(identity.title or "")
    for title in titles:
        theirs = _words(title)
        if not theirs:
            continue
        short, long = (mine, theirs) if len(mine) <= len(theirs) else (theirs, mine)
        if set(short) == set(long):
            return True
        # A shorter title of your own still fits if it is most of TMDB's title
        # ("The Copper Kettle Gang" / "The Copper Kettle Gang Movie"), never one word of four.
        if set(short) <= set(long) and len(short) * 2 >= len(long):
            return True
    return False


# Added when you keep a match you picked even though the folder name says otherwise
# (an alternative title such as "The Lagoon" / "Fin Fighters"), so it isn't questioned again.
KEPT = "kept by user despite the name"


def item_name(item: SourceItem) -> str:
    """The name on disk the item is known by: its folder, or its file."""
    p = item.paths[0] if item.paths else item.root
    return p.name


def doubtful_pick(item: SourceItem, identity: Identity | None) -> bool:
    """A match you confirmed whose title or year doesn't fit the folder name, and that
    you haven't said to keep. The one-click button used to offer matches on the year
    alone, so some of these were confirmed by mistake ("Mr. Whiskers & Pip" -> "Mr. Grumble")."""
    if identity is None or not chosen_by_user(identity) or KEPT in identity.reasons:
        return False
    name = item_name(item)
    return bool(name) and not folder_agrees(name, identity)


def trusted_pick(identity: Identity | None, name: str) -> bool:
    """Is this match you picked still to be used as is?

    Yes when you kept it ("Keep this match", or a pick from the full list on the match
    page), or when it fits the name on disk. A pick that fits neither was most likely made
    with the old one-click button, which matched on the year alone: it is set aside and the
    item is matched again ("Mr. Tumble's Parrots" must not stay "Mr. Sparkle")."""
    if identity is None or not chosen_by_user(identity):
        return False
    if KEPT in identity.reasons:
        return True
    return bool(name) and folder_agrees(name, identity)


def candidate_fits(name: str, candidate: Candidate) -> bool:
    """Would this TMDB suggestion fit the name on disk (title words and year)?"""
    probe = Identity(
        item_key="",
        kind=candidate.kind,
        title=candidate.title,
        year=candidate.year,
        tmdb_id=candidate.tmdb_id,
        imdb_id=None,
        status=IdStatus.AUTO,
        confidence=0.0,
        reasons=[],
        candidates=[candidate],
    )
    return folder_agrees(name, probe)
