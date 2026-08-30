"""Small, dependency-free helpers for the unified card stream.

The API and the browser use the same card identity/dimension rules.  Keeping
the server-side rule in one place makes the returned count auditable and gives
tests a network-free seam.
"""


def card_identity(card):
    """Return a stable card identity, or ``None`` when none is available."""
    if not isinstance(card, dict):
        return None
    value = (card.get("id") or card.get("full_id") or
             card.get("official_url") or card.get("term") or
             card.get("title"))
    if value is None or str(value) == "":
        return None
    return (card.get("kind") or "", str(value))


def dedupe_cards(cards):
    """Drop repeated cards while preserving the source order."""
    out = []
    seen = set()
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        identity = card_identity(card)
        if identity is not None:
            if identity in seen:
                continue
            seen.add(identity)
        out.append(card)
    return out


def dimension_members(card, view):
    """Return unique dimensions for one card in the stream's filter order."""
    if view == "words":
        values = card.get("dims") if isinstance(card.get("dims"), list) else []
        values = values or [card.get("dimension") or "其他"]
    else:
        values = [card.get("dimension") or
                  ("模型与技术" if card.get("kind") == "model" else "其他")]
    members = []
    seen = set()
    for value in values:
        value = str(value or "其他")
        if value not in seen:
            seen.add(value)
            members.append(value)
    return members


def dimension_counts(cards, view):
    """Count unique cards per dimension; a cross-dimension word counts once per dimension."""
    counts = {}
    for card in cards:
        for dimension in dimension_members(card, view):
            counts[dimension] = counts.get(dimension, 0) + 1
    return counts


def dimension_list(cards, view, base_order):
    """Keep the configured dimension order and append observed unknown values."""
    order = list(base_order)
    for dimension in dimension_counts(cards, view):
        if dimension not in order:
            order.append(dimension)
    return order
