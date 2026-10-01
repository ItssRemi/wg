"""audit_engine.py — Pure classification logic for the Waifugami audit engine.

No discord/redbot imports.  Import freely in tests and in audit.py.

Classification flow
────────────────────
For each card in the user's collection:

  1. Locked (🔒 flag only)           → KEEP
  2. Known event card (name|rarity)   → KEEP   [session or persistent]
  3. Known sigma card (persistent)    → KEEP   [explicit sigma store, separate
                                                from event_cards so intent is
                                                unambiguous even if the card
                                                later leaves the event list]
  4. Omega rarity                     → KEEP
  5. Protected series
       - series NOT yet reviewed      → REVIEW (needs user decision)
       - series reviewed, card kept   → KEEP
       - series reviewed, card sold   → SELL
       - series reviewed, no decision → REVIEW
  6. High stats (Skill>90 AND Luck>5) → KEEP
  7. Zeta / Epsilon rarity            → REVIEW
  8. Stats unknown                    → UNKNOWN
  9. Near-threshold stats             → REVIEW
 10. Everything else                  → SELL
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

# Series IDs that must always have at least one representative in the collection.
# Removal of cards from these series requires an explicit user review decision.
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

# Rarity symbols that go to REVIEW before any removal (high economic value).
HIGH_VALUE_REVIEW_RARITIES: Set[str] = {"ζ", "ε"}

# Omega is always a hard keep regardless of any other rule.
OMEGA_SYMBOLS: Set[str] = {"ω"}

# Stats thresholds: a card with Skill > threshold AND Luck > threshold is kept.
SKILL_PROTECT_THRESHOLD = 90.0
LUCK_PROTECT_THRESHOLD = 5

# Maximum cards per .rm batch (index-shift safety).
RM_BATCH_SIZE = 30

# Audit session TTL in seconds (30 minutes of inactivity).
AUDIT_SESSION_TTL = 60 * 30

# ── Regexes used by the embed parser in audit.py ──────────────────────────────

# Matches Waifugami's list embed title: "{owner}'s Waifus (Page N)"
LIST_TITLE_RE = re.compile(r"^(.+?)'s Waifus \(Page (\d+)\)$", re.I)

# Matches a single list entry line: "123 | 🎁 [δ] Character Name"
# The event-emoji prefix is optional.
LIST_ENTRY_RE = re.compile(
    # Matches:
    #   123 | 🎁 [δ] Name
    #   15 |  [β] Name
    #
    # Group 1 = local ID
    # Group 2 = status emoji, if present
    # Group 3 = rarity
    # Group 4 = character name
    r"^(\d+)\s*\|\s*(?:(\S+)\s+)?\[([^\]]+)\]\s+(.+?)\s*$"
)
# Matches the "Page N of M" footer field that signals pagination state.
FINAL_PAGE_FIELD_NAME_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)

# ── Disposition labels ────────────────────────────────────────────────────────
KEEP    = "KEEP"
REVIEW  = "REVIEW"
SELL    = "SELL"
UNKNOWN = "UNKNOWN"

# ── Reason tags (shown to the user next to each card) ────────────────────────
REASON_LOCKED           = "locked"
REASON_EVENT            = "event card"
REASON_SIGMA            = "sigma — permanent keep"
REASON_OMEGA            = "omega rarity"
REASON_SERIES_KEEP      = "series keep (decided)"
REASON_SERIES_SELL      = "series sell (decided)"
REASON_SERIES_UNREVIEWED = "series unreviewed — needs decision"
REASON_HIGH_STATS       = "high stats"
REASON_HIGH_VALUE       = "high rarity (review)"
REASON_NEAR_THRESHOLD   = "near stat threshold"
REASON_NO_STATS         = "stats unknown"


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────────────

def _rarity_symbol(card: Dict[str, Any]) -> str:
    return str(
        card.get("rarity_symbol") or card.get("type_symbol") or ""
    ).strip().lower()


def _dp_for_card(card: Dict[str, Any]) -> int:
    return RARITY_DP.get(_rarity_symbol(card), 0)


def _shards_for_card(card: Dict[str, Any]) -> int:
    return OMEGA_SHARDS_PER_CARD if _rarity_symbol(card) in OMEGA_SYMBOLS else 0


def _is_locked(card: Dict[str, Any]) -> bool:
    """True only when the card carries Waifugami's actual lock flag (🔒).

    Plain emoji favorites are organisational markers, NOT protection signals.
    """
    return str(card.get("favorite") or "").strip() in {"🔒", "locked", "lock"}


def _skill(card: Dict[str, Any]) -> Optional[float]:
    v = card.get("skill")
    return float(v) if v is not None else None


def _luck(card: Dict[str, Any]) -> Optional[int]:
    v = card.get("luck")
    return int(v) if v is not None else None


def _chunk(items: List[Any], size: int):
    for i in range(0, len(items), size):
        yield items[i: i + size]


def _normalise_card_name(name: str) -> str:
    return " ".join(str(name).strip().casefold().split())


def _card_identity(card: Dict[str, Any]) -> str:
    """Stable identity string: normalised_name|rarity_symbol.

    Used to match cards across list-ID shifts.  Two physical cards with the
    same name and rarity share an identity; local_id is used to distinguish
    individual copies when needed.
    """
    name = _normalise_card_name(card.get("name", ""))
    rarity = str(
        card.get("rarity_symbol") or card.get("type_symbol") or ""
    ).strip().casefold()
    return f"{name}|{rarity}"


def _event_identity_set(
    event_cards: Dict[str, Dict[str, Any]],
) -> Set[str]:
    """Build name+rarity identities from physical event-card records.

    Event-card storage is keyed by local list ID so duplicate physical cards
    are never collapsed. Classification still needs a name+rarity identity
    set so every active copy of a learned event card is protected.
    """
    identities: Set[str] = set()

    for snapshot in event_cards.values():
        if not isinstance(snapshot, dict):
            continue

        name = snapshot.get("name")
        rarity = snapshot.get("rarity")

        if name is None or rarity is None:
            continue

        identities.add(
            f"{_normalise_card_name(name)}|"
            f"{str(rarity).strip().casefold()}"
        )

    return identities


def _series_id_for(card: Dict[str, Any]) -> Optional[str]:
    """Normalised series_id string, or None if unavailable."""
    raw = card.get("series_id")
    if raw is None:
        return None
    try:
        return str(int(raw))
    except (TypeError, ValueError):
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Series review helpers (pure — no I/O)
# ──────────────────────────────────────────────────────────────────────────────

def series_review_decision(
    card: Dict[str, Any],
    series_review: Dict[str, Any],
) -> Optional[str]:
    """Look up the persisted decision for this card inside a series review.

    series_review is the value at protected_series[series_id_str].

    Returns "keep", "sell", or None (no decision recorded yet).
    """
    decisions = series_review.get("decisions", {})
    identity = _card_identity(card)
    entry = decisions.get(identity)
    if entry is None:
        return None
    return str(entry.get("decision", "")).lower() or None


# ──────────────────────────────────────────────────────────────────────────────
# Core classification
# ──────────────────────────────────────────────────────────────────────────────

def classify_cards(
    cards: List[Dict[str, Any]],
    session_event_cards: Dict[str, Dict[str, Any]],
    persistent_event_cards: Dict[str, Dict[str, Any]],
    persistent_sigma_cards: Dict[str, Dict[str, Any]],
    persistent_series_reviews: Dict[str, Dict[str, Any]],
    locked_local_ids: Set[int],
) -> List[Dict[str, Any]]:
    """Classify every card in the user's active collection.

    Parameters
    ----------
    cards:
        Active cards from the stored collection (enriched with series_id).
    session_event_cards:
        Cards seen during the current `.l -event all` scan:
        identity → snapshot dict.
    persistent_event_cards:
        All event cards ever seen across all scans:
        identity → snapshot dict.
    persistent_sigma_cards:
        Sigma cards seen in any past event scan, stored separately so the
        Sigma protection intent is unambiguous even when sigma cards leave
        the active event list.  identity → snapshot dict.
    persistent_series_reviews:
        User decisions for protected-series cards:
        series_id_str → { "status": str, "decisions": { identity → decision } }.
    locked_local_ids:
        Set of local_ids the user has explicitly locked (from .l or .v scans).
        _is_locked() also checks the stored card["favorite"] field directly.

    Returns a list of result dicts, one per card:
        local_id, global_id, name, rarity_symbol, skill, luck, series_id,
        disposition (KEEP/REVIEW/SELL/UNKNOWN), reasons (list[str]),
        dp_yield (int), shard_yield (int), card (raw stored dict).
    """
    # Event cards are stored individually by local list ID so duplicate
    # physical cards are never collapsed. Classification still protects
    # every active copy matching any learned event name+rarity.
    event_identities: Set[str] = (
        _event_identity_set(session_event_cards)
        | _event_identity_set(persistent_event_cards)
    )

    # Sigma identities come from their own dedicated store, NOT from
    # event_identities — this makes the protection intent explicit and keeps
    # it alive even if the card is no longer on the active event list.
    sigma_identities: Set[str] = _event_identity_set(
        persistent_sigma_cards
    )

    results: List[Dict[str, Any]] = []

    for card in cards:
        local_id   = card.get("local_id")
        global_id  = card.get("global_id")
        sym        = _rarity_symbol(card)
        skill_val  = _skill(card)
        luck_val   = _luck(card)
        sid        = _series_id_for(card)
        sid_int    = int(sid) if (sid is not None and sid.isdigit()) else None
        identity   = _card_identity(card)

        disposition: Optional[str] = None
        reasons: List[str] = []

        # ── 1. Hard lock (🔒 only) ───────────────────────────────────────────
        if local_id in locked_local_ids or _is_locked(card):
            disposition = KEEP
            reasons.append(REASON_LOCKED)

        # ── 2. Event card (session or persistent, matched by name+rarity) ────
        elif identity in event_identities:
            disposition = KEEP
            reasons.append(REASON_EVENT)

        # ── 3. Sigma — permanent keep from its own dedicated store ───────────
        elif identity in sigma_identities:
            disposition = KEEP
            reasons.append(REASON_SIGMA)

        # ── 4. Omega — hard keep ─────────────────────────────────────────────
        elif sym in OMEGA_SYMBOLS:
            disposition = KEEP
            reasons.append(REASON_OMEGA)

        else:
            # ── 5. Protected series — consult persisted user decisions ────────
            if sid_int is not None and sid_int in PROTECTED_SERIES_IDS:
                series_review = persistent_series_reviews.get(str(sid_int), {})
                decision = series_review_decision(card, series_review)

                if decision == "keep":
                    disposition = KEEP
                    reasons.append(REASON_SERIES_KEEP)
                elif decision == "sell":
                    # Explicit sell decision — falls through to normal
                    # evaluation below; tag it so the sell list is clear.
                    reasons.append(REASON_SERIES_SELL)
                else:
                    # No decision recorded yet → must review before removing.
                    disposition = REVIEW
                    reasons.append(REASON_SERIES_UNREVIEWED)

            # ── 6. High stats ─────────────────────────────────────────────────
            if disposition is None:
                if (
                    skill_val is not None and luck_val is not None
                    and skill_val > SKILL_PROTECT_THRESHOLD
                    and luck_val > LUCK_PROTECT_THRESHOLD
                ):
                    disposition = KEEP
                    reasons.append(REASON_HIGH_STATS)

            # ── 7. High-value rarity → REVIEW before removal ─────────────────
            if disposition is None and sym in HIGH_VALUE_REVIEW_RARITIES:
                disposition = REVIEW
                reasons.append(REASON_HIGH_VALUE)

            # ── 8. Unknown stats → UNKNOWN (never auto-removed) ──────────────
            if disposition is None and (skill_val is None or luck_val is None):
                disposition = UNKNOWN
                reasons.append(REASON_NO_STATS)

            # ── 9. Near-threshold stats → REVIEW ─────────────────────────────
            if disposition is None:
                if (
                    skill_val > SKILL_PROTECT_THRESHOLD * 0.85
                    or luck_val >= LUCK_PROTECT_THRESHOLD
                ):
                    disposition = REVIEW
                    reasons.append(REASON_NEAR_THRESHOLD)

            # ── 10. Safe to sell ──────────────────────────────────────────────
            if disposition is None:
                disposition = SELL

        results.append({
            "local_id":       local_id,
            "global_id":      global_id,
            "name":           card.get("name", "Unknown"),
            "rarity_symbol":  sym,
            "skill":          skill_val,
            "luck":           luck_val,
            "series_id":      sid,
            "disposition":    disposition,
            "reasons":        reasons,
            "dp_yield":       _dp_for_card(card),
            "shard_yield":    _shards_for_card(card),
            "card":           card,
        })

    return results


# ──────────────────────────────────────────────────────────────────────────────
# Session state (used by AuditMixin in audit.py)
# ──────────────────────────────────────────────────────────────────────────────
