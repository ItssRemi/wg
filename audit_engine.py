"""audit_engine.py — Pure classification logic for the Waifugami audit engine.

No discord/redbot imports.  Import freely in tests and in audit.py.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

WAIFUGAMI_ID = 722418701852344391
V2_FLAG = 32768

# Series IDs whose last remaining representative must never be removed.
PROTECTED_SERIES_IDS: Set[int] = {
    1, 4, 5, 7, 19, 41, 65, 86, 88, 166, 239, 240, 371, 387, 394, 401, 499
}

# Rarity symbol → DP yield on removal.
RARITY_DP: Dict[str, int] = {
    "α": 15,
    "β": 20,
    "γ": 30,
    "δ": 40,
    "σ": 150,
    "ε": 300,
    "ζ": 7_500,
    "ω": 120_000,
}
OMEGA_SHARDS_PER_CARD = 20

# Rarity symbols considered high-value enough to always put in REVIEW first.
HIGH_VALUE_REVIEW_RARITIES: Set[str] = {"ζ", "ε"}
# Omega is always hard-protected.
OMEGA_SYMBOLS: Set[str] = {"ω"}

# Stats thresholds for automatic protection.
SKILL_PROTECT_THRESHOLD = 90.0
LUCK_PROTECT_THRESHOLD = 5

# Batch size for .rm commands.
RM_BATCH_SIZE = 30

# How long (seconds) an audit session stays alive without activity.
AUDIT_SESSION_TTL = 60 * 30  # 30 minutes

# Regex to detect the Waifugami list embed title for a specific user.
# We accept any user name (non-greedy) so we don't hardcode the owner.
LIST_TITLE_RE = re.compile(r"^(.+?)'s Waifus \(Page (\d+)\)$", re.I)

# List entry line:  "123 | 🎁 [δ] Character Name"
# The emoji prefix (🎁 🎄 💝 🕸 🏵 etc.) is optional — we capture the whole
# rarity bracket and the name that follows.
LIST_ENTRY_RE = re.compile(
    r"^(\d+)\s*\|\s*(?:\S+\s+)?\[([^\]]+)\]\s+(.+?)\s*$"
)

# Field name that marks the final page ("Final page?" field in the embed).
FINAL_PAGE_FIELD_NAME_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)

# Disposition labels
KEEP = "KEEP"
REVIEW = "REVIEW"
SELL = "SELL"
UNKNOWN = "UNKNOWN"

# Reason tags (for display)
REASON_LOCKED = "locked"
REASON_EVENT = "event card"
REASON_SERIES_LAST = "last series rep"
REASON_SERIES_PROTECTED = "protected series (excess)"
REASON_HIGH_STATS = "high stats"
REASON_OMEGA = "omega rarity"
REASON_HIGH_VALUE = "high rarity (review)"
REASON_NEAR_THRESHOLD = "near stat threshold"
REASON_NO_STATS = "stats unknown"


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────────────

def _rarity_symbol(card: Dict[str, Any]) -> str:
    return str(
        card.get("rarity_symbol") or card.get("type_symbol") or ""
    ).strip().lower()


def _dp_for_card(card: Dict[str, Any]) -> int:
    sym = _rarity_symbol(card)
    return RARITY_DP.get(sym, 0)


def _shards_for_card(card: Dict[str, Any]) -> int:
    sym = _rarity_symbol(card)
    return OMEGA_SHARDS_PER_CARD if sym in OMEGA_SYMBOLS else 0


def _is_locked(card: Dict[str, Any]) -> bool:
    """True only if the card carries Waifugami's actual lock flag (🔒).

    Plain emoji favorites (series tags, aesthetic markers, etc.) are NOT
    treated as protection — users apply those for organisation, not to
    signal that a card should be kept.
    """
    fav = str(card.get("favorite") or "").strip()
    return fav in {"🔒", "locked", "lock"}


def _skill(card: Dict[str, Any]) -> Optional[float]:
    v = card.get("skill")
    return float(v) if v is not None else None


def _luck(card: Dict[str, Any]) -> Optional[int]:
    v = card.get("luck")
    return int(v) if v is not None else None


def _chunk(items: List[Any], size: int):
    for i in range(0, len(items), size):
        yield items[i: i + size]


# ──────────────────────────────────────────────────────────────────────────────
# Core classification logic
# ──────────────────────────────────────────────────────────────────────────────

def _best_series_keeper(cards_in_series: List[Dict[str, Any]]) -> Optional[int]:
    """Pick the single local_id to keep from a protected series.

    Selection criteria (first applicable wins):
      1. The card that is locked (🔒).
      2. Among unlocked cards, the highest skill; ties broken by lowest local_id
         (lowest local_id = earliest acquired = safest default).
    Returns None if the list is empty.
    """
    if not cards_in_series:
        return None

    locked = [c for c in cards_in_series if _is_locked(c)]
    if locked:
        # If multiple are locked, keep all of them — locking is explicit intent.
        # This function only returns ONE id as the "minimum guaranteed keeper";
        # the caller treats every locked card as KEEP regardless.
        # Just return the highest-skill locked one for the label.
        best = max(locked, key=lambda c: (_skill(c) or 0.0, -(c.get("local_id") or 0)))
        return best.get("local_id")

    # No locks — pick best by skill, then lowest local_id as tiebreak.
    best = max(
        cards_in_series,
        key=lambda c: (_skill(c) or 0.0, -(c.get("local_id") or 999_999)),
    )
    return best.get("local_id")


def classify_cards(
    cards: List[Dict[str, Any]],
    session_event_cards: Dict[str, Dict[str, Any]],
    persistent_event_cards: Dict[str, Dict[str, Any]],
    locked_local_ids: Set[int],
) -> List[Dict[str, Any]]:
    """Return each card annotated with disposition + reasons.

    Parameters
    ----------
    cards:
        All active cards from the user's stored collection.
    event_local_ids:
        Local IDs harvested from `.l -event all` — these are current-event
        characters that must be kept.
    locked_local_ids:
        Local IDs the user has explicitly locked (learned from .l or .v).
        Also re-checks the stored card["favorite"] field for the 🔒 marker.

    Favorites that are NOT the 🔒 lock flag are intentionally ignored as
    protection criteria — users apply plain emoji favorites for series
    organisation, not to signal "keep this card".

    Returns a list of dicts, one per card, with keys:
        local_id, global_id, name, rarity_symbol, skill, luck,
        disposition (KEEP/REVIEW/SELL/UNKNOWN), reasons (list[str]),
        dp_yield (int), shard_yield (int), card (the raw stored dict).
    """
    # ── Pre-pass: determine the one local_id to keep per protected series ──
    # Group all cards that belong to a protected series, then pick exactly one
    # keeper per series (the highest-skill card, ties by lowest local_id).
    protected_series_cards: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for c in cards:
        sid = _series_id_for(c)
        if sid is not None and sid.isdigit():
            sid_int = int(sid)
            if sid_int in PROTECTED_SERIES_IDS:
                protected_series_cards[sid_int].append(c)

    # series_id_int → local_id that is the designated keeper
    series_keeper_lid: Dict[int, Optional[int]] = {
        sid_int: _best_series_keeper(group)
        for sid_int, group in protected_series_cards.items()
    }

    results = []
    for card in cards:
        local_id = card.get("local_id")
        global_id = card.get("global_id")
        sym = _rarity_symbol(card)
        skill_val = _skill(card)
        luck_val = _luck(card)
        sid = _series_id_for(card)
        sid_int = int(sid) if (sid is not None and sid.isdigit()) else None

        disposition: Optional[str] = None
        reasons: List[str] = []

        # ── 1. Hard lock (🔒 only — plain emoji favorites are NOT protection) ──
        if local_id in locked_local_ids or _is_locked(card):
            disposition = KEEP
            reasons.append(REASON_LOCKED)

        # ── 2. Current event card ──
        elif local_id is not None and int(local_id) in event_local_ids:
            disposition = KEEP
            reasons.append(REASON_EVENT)

        # ── 3. Omega — permanent hard keep ──
        elif sym in OMEGA_SYMBOLS:
            disposition = KEEP
            reasons.append(REASON_OMEGA)

        else:
            # ── 4. Protected series: exactly one card per series is kept ──
            if sid_int is not None and sid_int in PROTECTED_SERIES_IDS:
                keeper_lid = series_keeper_lid.get(sid_int)
                if local_id is not None and local_id == keeper_lid:
                    # This is the designated representative — keep it.
                    disposition = KEEP
                    reasons.append(REASON_SERIES_LAST)
                else:
                    # Excess card from a protected series — falls through to
                    # normal evaluation. Tag it so the user sees it in the
                    # sell list and understands why it's still removable.
                    reasons.append(REASON_SERIES_PROTECTED)

            # ── 5. High stats ──
            if disposition is None:
                if skill_val is not None and luck_val is not None:
                    if skill_val > SKILL_PROTECT_THRESHOLD and luck_val > LUCK_PROTECT_THRESHOLD:
                        disposition = KEEP
                        reasons.append(REASON_HIGH_STATS)

            # ── 6. High-value rarity → REVIEW before removing ──
            if disposition is None:
                if sym in HIGH_VALUE_REVIEW_RARITIES:
                    disposition = REVIEW
                    reasons.append(REASON_HIGH_VALUE)

            # ── 7. Unknown stats → UNKNOWN (never auto-removed) ──
            if disposition is None:
                if skill_val is None or luck_val is None:
                    disposition = UNKNOWN
                    reasons.append(REASON_NO_STATS)

            # ── 8. Near-threshold stats → REVIEW ──
            if disposition is None:
                near = (
                    skill_val > SKILL_PROTECT_THRESHOLD * 0.85
                    or luck_val >= LUCK_PROTECT_THRESHOLD
                )
                if near:
                    disposition = REVIEW
                    reasons.append(REASON_NEAR_THRESHOLD)

            # ── 9. Everything else is safe to sell ──
            if disposition is None:
                disposition = SELL

        dp = _dp_for_card(card)
        shards = _shards_for_card(card)

        results.append({
            "local_id": local_id,
            "global_id": global_id,
            "name": card.get("name", "Unknown"),
            "rarity_symbol": sym,
            "skill": skill_val,
            "luck": luck_val,
            "series_id": sid,
            "disposition": disposition,
            "reasons": reasons,
            "dp_yield": dp,
            "shard_yield": shards,
            "card": card,
        })

    return results


def _series_id_for(card: Dict[str, Any]) -> Optional[str]:
    """Extract a normalised series_id string from a stored card dict."""
    raw = card.get("series_id")
    if raw is None:
        # Some cards store waifu_id but not series_id; we cannot derive it
        # without the catalog, so callers must enrich before classifying.
        return None
    try:
        return str(int(raw))
    except (TypeError, ValueError):
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Session state
# ──────────────────────────────────────────────────────────────────────────────
