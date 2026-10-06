"""audit_engine.py — Pure classification logic for the Waifugami audit engine.

No discord/redbot imports.  Import freely in tests and in audit.py.

Classification flow
────────────────────
For each card in the user's collection:

 1. Locked (🔒 flag only)               → KEEP
 2. Known event card (name|rarity)       → KEEP  [session or persistent]
 3. Known sigma card (persistent)        → KEEP  [explicit sigma store]
 4. Omega rarity                         → KEEP
 5. Protected series
      - series NOT yet reviewed          → REVIEW (needs user decision)
      - series reviewed, card kept       → KEEP
      - series reviewed, card sold       → SELL
      - series reviewed, no decision     → REVIEW
 6. High stats (Skill > 90 AND Luck > 5) → KEEP
 7. Zeta / Epsilon rarity               → REVIEW
 8. Stats unknown                        → UNKNOWN
 9. Near-threshold stats                 → REVIEW
10. Everything else                      → SELL

Every card result carries ALL applicable reasons, not just the first one
that matched.  Cleanup candidates carry explicit eligibility reasons too.

Primary safety invariant:
    if uncertain:
        preserve(card)
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

WAIFUGAMI_ID = 722418701852344391
V2_FLAG = 32768

# Series IDs that must always have at least one representative.
# Removal of cards from these series requires an explicit user review decision.
PROTECTED_SERIES_IDS: FrozenSet[int] = frozenset({
    1, 4, 5, 7, 19, 41, 65, 86, 88, 166, 239, 240, 371, 387, 394, 401, 499
})

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
HIGH_VALUE_REVIEW_RARITIES: FrozenSet[str] = frozenset({"ζ", "ε"})

# Omega is always a hard keep regardless of any other rule.
OMEGA_SYMBOLS: FrozenSet[str] = frozenset({"ω"})

# Stats thresholds.
# A card is protected by stats if Skill > threshold AND Luck > threshold.
SKILL_PROTECT_THRESHOLD = 90.0
LUCK_PROTECT_THRESHOLD = 5

# Near-threshold bands: if either stat is close, REVIEW not SELL.
SKILL_NEAR_THRESHOLD_FACTOR = 0.85   # Skill > 90 * 0.85 == > 76.5
LUCK_NEAR_THRESHOLD = LUCK_PROTECT_THRESHOLD   # Luck >= 5

# Maximum cards per .rm batch (index-shift safety).
RM_BATCH_SIZE = 20

# Audit session TTL in seconds (30 minutes of inactivity).
AUDIT_SESSION_TTL = 60 * 30

# ── Regexes used by the embed parser in audit.py ─────────────────────────────

# Matches Waifugami's list embed title: "{owner}'s Waifus (Page N)"
LIST_TITLE_RE = re.compile(r"^(.+?)'s Waifus \(Page (\d+)\)$", re.I)

# Matches a single list entry line:
#   "123 | 🎁 [δ] Character Name"   (with status emoji)
#   "15 |  [β] Kuki Shinobu"        (no status emoji — double space)
#
# Groups:
#   1 = local ID (digits)
#   2 = status emoji (may be None / empty — these are user-assigned tags)
#   3 = rarity symbol
#   4 = character name
#
# NOTE: Status emojis on the list view are user-assigned organisational tags
# (🚮 = trash, 💝 = love, 🏵 = rosette, etc.).  They are NOT system protection
# signals.  We capture them for logging purposes only.  Classification decisions
# are based on rarity, skill/luck from stored card data, and series membership.
LIST_ENTRY_RE = re.compile(
    r"^(\d+)\s*\|\s*(?:(\S+)\s+)?\[([^\]]+)\]\s+(.+?)\s*$"
)

# Matches the "Page N of M" footer field name.
FINAL_PAGE_FIELD_NAME_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)

# ── Disposition labels ────────────────────────────────────────────────────────
KEEP    = "KEEP"
REVIEW  = "REVIEW"
SELL    = "SELL"
UNKNOWN = "UNKNOWN"

# ── Protection reason tags ────────────────────────────────────────────────────
REASON_LOCKED             = "LOCKED"
REASON_EVENT              = "EVENT"
REASON_SIGMA              = "SIGMA"
REASON_OMEGA              = "OMEGA"
REASON_SERIES_KEEP        = "SERIES_KEEP"
REASON_SERIES_SELL        = "SERIES_SELL"
REASON_SERIES_UNREVIEWED  = "SERIES_UNREVIEWED"
REASON_SERIES_SURVIVOR    = "SERIES_SURVIVOR"
REASON_HIGH_STATS         = "HIGH_STATS"
REASON_HIGH_VALUE         = "HIGH_VALUE_RARITY"
REASON_NEAR_THRESHOLD     = "NEAR_THRESHOLD"
REASON_NO_STATS           = "UNKNOWN_STATS"
REASON_UNKNOWN_EVENT      = "UNKNOWN_EVENT_STATUS"

# ── Cleanup eligibility reasons (applied to SELL cards) ──────────────────────
ELIG_NON_EVENT            = "NON_EVENT"
ELIG_NOT_FAVORITED        = "NOT_FAVORITED"
ELIG_NOT_TAGGED           = "NOT_TAGGED"
ELIG_NOT_PROTECTED_SERIES = "NOT_PROTECTED_SERIES"
ELIG_LOW_SKILL            = "LOW_SKILL"
ELIG_LOW_LUCK             = "LOW_LUCK"

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

    Plain emoji favorites / user tags are organisational markers, NOT
    protection signals and are never used to infer lock status.
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
    same name and rarity share an identity; local_id distinguishes copies.
    """
    name   = _normalise_card_name(card.get("name", ""))
    rarity = str(
        card.get("rarity_symbol") or card.get("type_symbol") or ""
    ).strip().casefold()
    return f"{name}|{rarity}"


def _series_card_identity(card: Dict[str, Any]) -> str:
    """Stable physical-card identity for protected-series review."""
    global_id = card.get("global_id")
    if global_id is not None:
        try:
            return f"global:{int(global_id)}"
        except (TypeError, ValueError):
            pass

    local_id = card.get("local_id")
    if local_id is not None:
        try:
            return f"local:{int(local_id)}"
        except (TypeError, ValueError):
            pass

    return f"card:{_card_identity(card)}"


def _event_identity_set(
    event_cards: Dict[str, Dict[str, Any]],
) -> Set[str]:
    """Build name+rarity identity set from physical event-card records.

    Event-card storage is keyed by local list ID so duplicate physical
    cards are never collapsed.  Classification still protects every active
    copy of a learned event card.
    """
    identities: Set[str] = set()
    for snapshot in event_cards.values():
        if not isinstance(snapshot, dict):
            continue
        name   = snapshot.get("name")
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
    identity  = _series_card_identity(card)
    entry     = decisions.get(identity)
    if entry is None:
        return None
    return str(entry.get("decision", "")).lower() or None


# ──────────────────────────────────────────────────────────────────────────────
# Harvest page parsing
# ──────────────────────────────────────────────────────────────────────────────

class ParsedPage:
    """Result of parsing one list embed page."""
    __slots__ = (
        "owner_name",
        "page_index",
        "total_pages",
        "entries",          # List[ParsedEntry]
        "source_lines",     # int — non-empty lines in description
        "parse_failures",   # int — lines that look like cards but failed
        "failure_details",  # List[dict] — debug info per failure
    )

    def __init__(
        self,
        owner_name: Optional[str],
        page_index: int,
        total_pages: int,
        entries: List["ParsedEntry"],
        source_lines: int,
        parse_failures: int,
        failure_details: List[Dict[str, Any]],
    ):
        self.owner_name     = owner_name
        self.page_index     = page_index
        self.total_pages    = total_pages
        self.entries        = entries
        self.source_lines   = source_lines
        self.parse_failures = parse_failures
        self.failure_details = failure_details


class ParsedEntry:
    """One successfully parsed card line from a list page."""
    __slots__ = ("local_id", "status_emoji", "rarity", "name")

    def __init__(
        self,
        local_id: int,
        status_emoji: Optional[str],
        rarity: str,
        name: str,
    ):
        self.local_id     = local_id
        self.status_emoji = status_emoji
        self.rarity       = rarity
        self.name         = name


def parse_list_page(
    title: str,
    description: str,
    fields: List[Tuple[str, str]],    # [(name, value), ...]
    source_message_id: Optional[int] = None,
) -> ParsedPage:
    """Parse one page of `.l -event all` output (pure, no Discord objects).

    Parameters
    ----------
    title       : embed.title
    description : embed.description  (may be None/empty)
    fields      : [(field.name, field.value), ...]
    source_message_id : for failure debug records

    Returns
    -------
    ParsedPage — always returns a result; failures are recorded in
    failure_details and parse_failures, never silently dropped.

    Page numbering
    --------------
    Waifugami sends "Page X of Y" where X is 0-based.
    So "Page 0 of 108" means pages 0..108 inclusive → 109 pages total.
    total_pages is the Y value (the maximum index).
    expected_count = total_pages + 1.
    """
    # Title check
    tm = LIST_TITLE_RE.match(title or "")
    if not tm:
        return ParsedPage(
            owner_name=None,
            page_index=0,
            total_pages=0,
            entries=[],
            source_lines=0,
            parse_failures=0,
            failure_details=[],
        )

    owner_name  = tm.group(1)
    page_index  = 0
    total_pages = 0

    for field_name, _field_value in fields:
        fm = FINAL_PAGE_FIELD_NAME_RE.search(field_name or "")
        if fm:
            page_index  = int(fm.group(1))
            total_pages = int(fm.group(2))
            break

    entries:        List[ParsedEntry]        = []
    failure_details: List[Dict[str, Any]]   = []
    source_lines    = 0

    for line in (description or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        source_lines += 1

        lm = LIST_ENTRY_RE.match(raw)
        if lm:
            entries.append(ParsedEntry(
                local_id     = int(lm.group(1)),
                status_emoji = (lm.group(2) or "").strip() or None,
                rarity       = lm.group(3).strip(),
                name         = lm.group(4).strip(),
            ))
        elif "|" in raw:
            # Looks like a card line but did not match — record as failure.
            failure_details.append({
                "page":       page_index,
                "raw_line":   raw,
                "reason":     "regex_no_match",
                "message_id": source_message_id,
            })
        # Lines without "|" are non-card lines (messages, etc.) — not failures.

    return ParsedPage(
        owner_name     = owner_name,
        page_index     = page_index,
        total_pages    = total_pages,
        entries        = entries,
        source_lines   = source_lines,
        parse_failures = len(failure_details),
        failure_details = failure_details,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Harvest state (pure, storable)
# ──────────────────────────────────────────────────────────────────────────────

class HarvestStats:
    """Tracks pagination and parsing integrity across all pages."""
    __slots__ = (
        "seen_pages",           # Set[int]   — 0-based page indices observed
        "expected_final_page",  # Optional[int] — Y from "Page X of Y"
        "duplicate_page_updates", # int
        "total_source_lines",   # int
        "total_parsed",         # int
        "total_failures",       # int
        "failure_details",      # List[dict]
    )

    def __init__(self):
        self.seen_pages:            Set[int]          = set()
        self.expected_final_page:   Optional[int]     = None
        self.duplicate_page_updates: int              = 0
        self.total_source_lines:    int               = 0
        self.total_parsed:          int               = 0
        self.total_failures:        int               = 0
        self.failure_details:       List[Dict[str, Any]] = []

    def ingest(self, page: ParsedPage) -> bool:
        """Record one parsed page.  Returns True if this page was new."""
        if page.owner_name is None:
            return False
        if self.expected_final_page is None:
            self.expected_final_page = page.total_pages
        # Update expected count if a later page reveals a larger total
        # (can happen if total_pages wasn't known on page 0).
        if page.total_pages > (self.expected_final_page or 0):
            self.expected_final_page = page.total_pages

        is_new = page.page_index not in self.seen_pages
        if is_new:
            self.seen_pages.add(page.page_index)
        else:
            self.duplicate_page_updates += 1

        self.total_source_lines += page.source_lines
        self.total_parsed       += len(page.entries)
        self.total_failures     += page.parse_failures
        self.failure_details.extend(page.failure_details)
        return is_new

    @property
    def expected_page_count(self) -> Optional[int]:
        if self.expected_final_page is None:
            return None
        return self.expected_final_page + 1

    @property
    def pages_missing(self) -> Optional[List[int]]:
        """Returns sorted list of missing page indices, or None if unknown."""
        if self.expected_final_page is None:
            return None
        expected = set(range(self.expected_final_page + 1))
        missing  = sorted(expected - self.seen_pages)
        return missing

    @property
    def harvest_complete(self) -> bool:
        """True only when every page from 0..expected_final_page is present."""
        missing = self.pages_missing
        return missing is not None and len(missing) == 0

    @property
    def cleanup_blocked(self) -> bool:
        """True when any condition prevents safe destructive execution."""
        if not self.harvest_complete:
            return True
        if self.total_failures > 0:
            return True
        return False

    def summary_lines(self) -> List[str]:
        """Human-readable harvest summary for the audit report."""
        ep = self.expected_page_count
        sp = len(self.seen_pages)
        mp = self.pages_missing or []

        lines = [
            f"**Harvest:** `{sp}/{ep if ep is not None else '?'}` pages",
            f"**Source lines:** `{self.total_source_lines}`",
            f"**Parsed:** `{self.total_parsed}`",
            f"**Parse failures:** `{self.total_failures}`",
        ]
        if self.duplicate_page_updates:
            lines.append(
                f"**Duplicate page updates (deduped):** `{self.duplicate_page_updates}`"
            )
        if mp:
            shown = mp[:10]
            extra = len(mp) - 10
            missing_str = ", ".join(str(p) for p in shown)
            if extra > 0:
                missing_str += f" … +{extra}"
            lines.append(f"**Missing pages:** `{missing_str}`")
        return lines


# ──────────────────────────────────────────────────────────────────────────────
# Core classification
# ──────────────────────────────────────────────────────────────────────────────

def classify_cards(
    cards:                       List[Dict[str, Any]],
    session_event_cards:         Dict[str, Dict[str, Any]],
    persistent_event_cards:      Dict[str, Dict[str, Any]],
    persistent_sigma_cards:      Dict[str, Dict[str, Any]],
    persistent_series_reviews:   Dict[str, Dict[str, Any]],
    locked_local_ids:            Set[int],
) -> List[Dict[str, Any]]:
    """Classify every card in the user's active collection.

    Parameters
    ----------
    cards:
        Active cards from the stored collection (enriched with series_id).
    session_event_cards:
        Cards seen during the current `.l -event all` scan:
        local_id_str → snapshot dict.
    persistent_event_cards:
        All event cards ever seen across all scans:
        local_id_str → snapshot dict.
    persistent_sigma_cards:
        Sigma cards in their own dedicated store (always-keep, intent explicit).
        local_id_str → snapshot dict.
    persistent_series_reviews:
        User decisions for protected-series cards:
        series_id_str → { "status": str, "decisions": { identity → decision } }.
    locked_local_ids:
        Local IDs the user has explicitly locked (from .l or .v scans).
        _is_locked() also checks the stored card["favorite"] field directly.

    Returns a list of result dicts, one per card:

        local_id, global_id, name, rarity_symbol, skill, luck, series_id,
        status_emoji,
        disposition (KEEP/REVIEW/SELL/UNKNOWN),
        reasons (list[str])    — ALL applicable reasons, not just first match,
        eligibility_reasons (list[str]) — why a SELL card is eligible,
        dp_yield (int), shard_yield (int), card (raw stored dict)

    Safety invariant: if uncertain, disposition is REVIEW or UNKNOWN, never SELL.
    """
    event_identities: Set[str] = (
        _event_identity_set(session_event_cards)
        | _event_identity_set(persistent_event_cards)
    )

    sigma_identities: Set[str] = _event_identity_set(persistent_sigma_cards)

    results: List[Dict[str, Any]] = []

    for card in cards:
        local_id  = card.get("local_id")
        global_id = card.get("global_id")
        sym       = _rarity_symbol(card)
        skill_val = _skill(card)
        luck_val  = _luck(card)
        sid       = _series_id_for(card)
        sid_int   = int(sid) if (sid is not None and sid.isdigit()) else None
        identity  = _card_identity(card)
        status_emoji = card.get("status_emoji")

        disposition:        Optional[str]  = None
        reasons:            List[str]      = []
        eligibility_reasons: List[str]    = []

        # ── 1. Hard lock ─────────────────────────────────────────────────────
        is_explicitly_locked = (
            (local_id is not None and local_id in locked_local_ids)
            or _is_locked(card)
        )
        if is_explicitly_locked:
            disposition = KEEP
            reasons.append(REASON_LOCKED)

        # ── 2. Event card ────────────────────────────────────────────────────
        if identity in event_identities:
            if disposition is None:
                disposition = KEEP
            reasons.append(REASON_EVENT)

        # ── 3. Sigma — permanent keep ─────────────────────────────────────────
        if identity in sigma_identities:
            if disposition is None:
                disposition = KEEP
            reasons.append(REASON_SIGMA)

        # ── 4. Omega — hard keep ─────────────────────────────────────────────
        if sym in OMEGA_SYMBOLS:
            if disposition is None:
                disposition = KEEP
            reasons.append(REASON_OMEGA)

        # ── 5. Protected series ───────────────────────────────────────────────
        is_protected_series_card = (
            sid_int is not None and sid_int in PROTECTED_SERIES_IDS
        )
        series_decision: Optional[str] = None
        if is_protected_series_card:
            series_review  = persistent_series_reviews.get(str(sid_int), {})
            series_decision = series_review_decision(card, series_review)

            if series_decision == "keep":
                if disposition is None:
                    disposition = KEEP
                reasons.append(REASON_SERIES_KEEP)
            elif series_decision == "sell":
                # Only mark SELL here if no higher-priority protection applies.
                if disposition is None:
                    disposition = SELL
                reasons.append(REASON_SERIES_SELL)
            else:
                # No decision recorded yet — must review.
                if disposition is None:
                    disposition = REVIEW
                reasons.append(REASON_SERIES_UNREVIEWED)

        # ── 6. High stats ─────────────────────────────────────────────────────
        if skill_val is not None and luck_val is not None:
            if (
                skill_val > SKILL_PROTECT_THRESHOLD
                and luck_val > LUCK_PROTECT_THRESHOLD
            ):
                if disposition is None:
                    disposition = KEEP
                reasons.append(REASON_HIGH_STATS)

        # ── 7. High-value rarity → REVIEW before removal ─────────────────────
        if sym in HIGH_VALUE_REVIEW_RARITIES:
            if disposition is None:
                disposition = REVIEW
            reasons.append(REASON_HIGH_VALUE)

        # ── 8. Unknown stats → UNKNOWN ────────────────────────────────────────
        if skill_val is None or luck_val is None:
            if disposition is None:
                disposition = UNKNOWN
            reasons.append(REASON_NO_STATS)

        # ── 9. Near-threshold stats → REVIEW ─────────────────────────────────
        if disposition is None and skill_val is not None and luck_val is not None:
            if (
                skill_val > SKILL_PROTECT_THRESHOLD * SKILL_NEAR_THRESHOLD_FACTOR
                or luck_val >= LUCK_NEAR_THRESHOLD
            ):
                disposition = REVIEW
                reasons.append(REASON_NEAR_THRESHOLD)

        # ── 10. Safe to sell ──────────────────────────────────────────────────
        if disposition is None:
            disposition = SELL
            # Record ALL eligibility reasons explicitly.
            if identity not in event_identities:
                eligibility_reasons.append(ELIG_NON_EVENT)
            if not _is_locked(card):
                eligibility_reasons.append(ELIG_NOT_FAVORITED)
            eligibility_reasons.append(ELIG_NOT_TAGGED)
            if not is_protected_series_card:
                eligibility_reasons.append(ELIG_NOT_PROTECTED_SERIES)
            if skill_val is not None and skill_val <= SKILL_PROTECT_THRESHOLD:
                eligibility_reasons.append(ELIG_LOW_SKILL)
            if luck_val is not None and luck_val <= LUCK_PROTECT_THRESHOLD:
                eligibility_reasons.append(ELIG_LOW_LUCK)

        # Safety net: disposition must always be set by this point.
        assert disposition is not None, (
            f"BUG: no disposition assigned for card local_id={local_id}"
        )

        results.append({
            "local_id":          local_id,
            "global_id":         global_id,
            "name":              card.get("name", "Unknown"),
            "rarity_symbol":     sym,
            "skill":             skill_val,
            "luck":              luck_val,
            "series_id":         sid,
            "status_emoji":      status_emoji,
            "disposition":       disposition,
            "reasons":           reasons,
            "eligibility_reasons": eligibility_reasons,
            "dp_yield":          _dp_for_card(card),
            "shard_yield":       _shards_for_card(card),
            "card":              card,
        })

    return results


def assert_protected_series_invariant(
    results: List[Dict[str, Any]],
) -> List[str]:
    """Verify that every protected series has at least one surviving card.

    Returns a list of violation strings (empty = all good).
    Call this before executing any destructive batch.
    """
    surviving: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for r in results:
        if r["disposition"] in (KEEP, REVIEW, UNKNOWN):
            sid = r.get("series_id")
            if sid is not None:
                try:
                    sid_int = int(sid)
                except (ValueError, TypeError):
                    continue
                if sid_int in PROTECTED_SERIES_IDS:
                    surviving[sid_int].append(r)

    # Only flag series that actually appear in the audited collection.
    # A protected series the user has no cards from is not a violation.
    series_represented: Set[int] = set()
    for r in results:
        sid = r.get("series_id")
        if sid is not None:
            try:
                sid_int = int(sid)
            except (ValueError, TypeError):
                continue
            if sid_int in PROTECTED_SERIES_IDS:
                series_represented.add(sid_int)

    violations = []
    for sid in series_represented:
        if not surviving.get(sid):
            violations.append(
                f"Protected series {sid} would have NO surviving representative"
            )
    return violations


def select_series_survivor(
    series_cards: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Deterministically select the best card to keep for a protected series.

    Priority (higher is better):
      1. Locked
      2. Favorited (any favorite marker)
      3. Event card identity
      4. Highest skill
      5. Highest luck
      6. Lowest local_id (stable tie-breaker)

    Returns None if the list is empty.
    """
    if not series_cards:
        return None

    def _score(card: Dict[str, Any]) -> Tuple:
        skill = _skill(card)
        luck  = _luck(card)
        return (
            -int(_is_locked(card)),                      # 0 = locked, 1 = not
            -int(bool(card.get("favorite"))),            # 0 = fav, 1 = not
            -(skill or 0.0),                             # higher skill better
            -(luck or 0),                                # higher luck better
            card.get("local_id") or 999_999_999,        # lower ID = older = tie-break
        )

    return min(series_cards, key=_score)
