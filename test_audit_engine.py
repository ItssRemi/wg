"""test_audit_engine.py — Unit tests for the Waifugami audit engine.

No Discord/Red imports.  Run with:  python -m pytest test_audit_engine.py -v

Covers all 18 cases from SKILL.md plus regression tests for:
- Real JSONL pagination behaviour (one message, repeatedly edited)
- Duplicate page-update deduplication
- Parser failure isolation
- Complete reasons list (multi-reason cards)
- Series survivor invariant
- Cleanup blocking rules
"""

from __future__ import annotations

import pytest
from typing import Any, Dict, List, Optional, Set

from audit_engine import (
    KEEP, REVIEW, SELL, UNKNOWN,
    REASON_LOCKED, REASON_EVENT, REASON_SIGMA, REASON_OMEGA,
    REASON_SERIES_KEEP, REASON_SERIES_SELL, REASON_SERIES_UNREVIEWED,
    REASON_HIGH_STATS, REASON_HIGH_VALUE, REASON_NEAR_THRESHOLD, REASON_NO_STATS,
    ELIG_NON_EVENT, ELIG_LOW_SKILL, ELIG_LOW_LUCK,
    PROTECTED_SERIES_IDS,
    SKILL_PROTECT_THRESHOLD, LUCK_PROTECT_THRESHOLD,
    classify_cards, parse_list_page, HarvestStats, ParsedPage,
    assert_protected_series_invariant, select_series_survivor,
    _card_identity, _series_card_identity,
)


# ──────────────────────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────────────────────

def _card(
    local_id:     int         = 1,
    name:         str         = "Test Card",
    rarity:       str         = "α",
    skill:        Optional[float] = None,
    luck:         Optional[int]   = None,
    series_id:    Optional[int]   = None,
    favorite:     Optional[str]   = None,
    global_id:    Optional[int]   = None,
    waifu_id:     Optional[int]   = None,
    status_emoji: Optional[str]   = None,
) -> Dict[str, Any]:
    return {
        "local_id":     local_id,
        "global_id":    global_id,
        "waifu_id":     waifu_id,
        "name":         name,
        "rarity_symbol": rarity,
        "skill":        skill,
        "luck":         luck,
        "series_id":    str(series_id) if series_id is not None else None,
        "favorite":     favorite,
        "status_emoji": status_emoji,
        "status":       "active",
    }


def _classify_one(
    card:                    Dict[str, Any],
    session_event_cards:     Dict = None,
    persistent_event_cards:  Dict = None,
    persistent_sigma_cards:  Dict = None,
    persistent_series_reviews: Dict = None,
    locked_local_ids:        Set[int] = None,
) -> Dict[str, Any]:
    results = classify_cards(
        [card],
        session_event_cards      or {},
        persistent_event_cards   or {},
        persistent_sigma_cards   or {},
        persistent_series_reviews or {},
        locked_local_ids         or set(),
    )
    assert len(results) == 1
    return results[0]


def _event_store(card: Dict[str, Any]) -> Dict[str, Dict]:
    """Build a minimal event-card store for one card."""
    key = str(card["local_id"])
    return {key: {"name": card["name"], "rarity": card["rarity_symbol"]}}


def _page(
    title:       str,
    description: str,
    fields:      List = None,
    message_id:  Optional[int] = None,
) -> ParsedPage:
    return parse_list_page(
        title              = title,
        description        = description,
        fields             = fields or [],
        source_message_id  = message_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Case 1 — Event card
# ──────────────────────────────────────────────────────────────────────────────

def test_case_1_event_card():
    card = _card(local_id=1, name="Kuki Shinobu", rarity="β", skill=30.0, luck=2)
    result = _classify_one(card, session_event_cards=_event_store(card))
    assert result["disposition"] == KEEP
    assert REASON_EVENT in result["reasons"]


# ──────────────────────────────────────────────────────────────────────────────
# Case 2 — Favorite / locked card
# ──────────────────────────────────────────────────────────────────────────────

def test_case_2_locked_card():
    card = _card(local_id=2, name="Rem", rarity="α", skill=10.0, luck=1, favorite="🔒")
    result = _classify_one(card)
    assert result["disposition"] == KEEP
    assert REASON_LOCKED in result["reasons"]


# ──────────────────────────────────────────────────────────────────────────────
# Case 3 — Tagged card
# Waifugami emoji tags are user-assigned and NOT protection signals.
# A tagged card without other protection reasons should SELL.
# ──────────────────────────────────────────────────────────────────────────────

def test_case_3_emoji_tag_is_not_protection():
    """Status emojis from the list view are user organisational tags, not locks."""
    card = _card(local_id=3, name="Echidna", rarity="β", skill=5.0, luck=1, status_emoji="🚮")
    result = _classify_one(card)
    # Low stats, no protection → SELL.  The tag does not protect.
    assert result["disposition"] == SELL
    assert REASON_EVENT not in result["reasons"]


# ──────────────────────────────────────────────────────────────────────────────
# Case 4 — Protected-series card (unreviewed)
# ──────────────────────────────────────────────────────────────────────────────

def test_case_4_protected_series_unreviewed():
    series_id = next(iter(PROTECTED_SERIES_IDS))
    card = _card(local_id=4, name="Emilia", rarity="δ", skill=50.0, luck=2, series_id=series_id)
    result = _classify_one(card)
    assert result["disposition"] == REVIEW
    assert REASON_SERIES_UNREVIEWED in result["reasons"]


def test_case_4_protected_series_decided_keep():
    series_id = next(iter(PROTECTED_SERIES_IDS))
    card = _card(local_id=4, name="Emilia", rarity="δ", skill=50.0, luck=2, series_id=series_id)
    identity = _series_card_identity(card)
    reviews = {str(series_id): {"status": "complete", "decisions": {
        identity: {"decision": "keep"}
    }}}
    result = _classify_one(card, persistent_series_reviews=reviews)
    assert result["disposition"] == KEEP
    assert REASON_SERIES_KEEP in result["reasons"]


def test_case_4_protected_series_decided_sell():
    series_id = next(iter(PROTECTED_SERIES_IDS))
    card = _card(local_id=4, name="Emilia", rarity="δ", skill=50.0, luck=2, series_id=series_id)
    identity = _series_card_identity(card)
    reviews = {str(series_id): {"status": "complete", "decisions": {
        identity: {"decision": "sell"}
    }}}
    result = _classify_one(card, persistent_series_reviews=reviews)
    assert result["disposition"] == SELL
    assert REASON_SERIES_SELL in result["reasons"]


# ──────────────────────────────────────────────────────────────────────────────
# Case 5 — High skill card
# ──────────────────────────────────────────────────────────────────────────────

def test_case_5_high_skill():
    card = _card(local_id=5, name="Kazuha", rarity="α", skill=95.0, luck=6)
    result = _classify_one(card)
    assert result["disposition"] == KEEP
    assert REASON_HIGH_STATS in result["reasons"]


def test_case_5_high_skill_only_not_enough():
    """Skill > threshold but luck NOT > threshold → NOT KEEP by stats."""
    card = _card(local_id=5, name="Kazuha", rarity="α", skill=95.0, luck=3)
    result = _classify_one(card)
    # luck=3 is not > LUCK_PROTECT_THRESHOLD(5), so high stats rule does not fire.
    # Near threshold fires because luck >= LUCK_NEAR_THRESHOLD(5)? No: luck=3 < 5.
    # But skill > 90*0.85 = 76.5, so near-threshold fires.
    assert result["disposition"] in (REVIEW,)  # near threshold
    assert REASON_HIGH_STATS not in result["reasons"]
    assert REASON_NEAR_THRESHOLD in result["reasons"]


# ──────────────────────────────────────────────────────────────────────────────
# Case 6 — High luck card
# ──────────────────────────────────────────────────────────────────────────────

def test_case_6_high_luck_at_threshold():
    """luck == LUCK_PROTECT_THRESHOLD → near-threshold, not SELL."""
    card = _card(local_id=6, name="Ram", rarity="β", skill=40.0, luck=5)
    result = _classify_one(card)
    assert result["disposition"] == REVIEW
    assert REASON_NEAR_THRESHOLD in result["reasons"]


def test_case_6_high_luck_above_threshold():
    """luck > threshold AND skill > threshold → KEEP."""
    card = _card(local_id=6, name="Ram", rarity="β", skill=91.0, luck=6)
    result = _classify_one(card)
    assert result["disposition"] == KEEP
    assert REASON_HIGH_STATS in result["reasons"]


# ──────────────────────────────────────────────────────────────────────────────
# Case 7 — Low skill/luck non-event card (SELL)
# ──────────────────────────────────────────────────────────────────────────────

def test_case_7_low_value_sell():
    card = _card(local_id=7, name="Nobody", rarity="α", skill=20.0, luck=1)
    result = _classify_one(card)
    assert result["disposition"] == SELL
    assert ELIG_NON_EVENT   in result["eligibility_reasons"]
    assert ELIG_LOW_SKILL   in result["eligibility_reasons"]
    assert ELIG_LOW_LUCK    in result["eligibility_reasons"]


# ──────────────────────────────────────────────────────────────────────────────
# Case 8 — Unknown skill
# ──────────────────────────────────────────────────────────────────────────────

def test_case_8_unknown_skill():
    card = _card(local_id=8, name="Mystery", rarity="γ", skill=None, luck=1)
    result = _classify_one(card)
    assert result["disposition"] == UNKNOWN
    assert REASON_NO_STATS in result["reasons"]


# ──────────────────────────────────────────────────────────────────────────────
# Case 9 — Unknown luck
# ──────────────────────────────────────────────────────────────────────────────

def test_case_9_unknown_luck():
    card = _card(local_id=9, name="Mystery", rarity="γ", skill=50.0, luck=None)
    result = _classify_one(card)
    assert result["disposition"] == UNKNOWN
    assert REASON_NO_STATS in result["reasons"]


# ──────────────────────────────────────────────────────────────────────────────
# Case 10 — Unknown event state
# ──────────────────────────────────────────────────────────────────────────────

def test_case_10_unknown_event_state():
    """A card with unknown stats whose event status is unknown → UNKNOWN, not SELL."""
    card = _card(local_id=10, name="Ambiguous", rarity="β", skill=None, luck=None)
    result = _classify_one(card)
    assert result["disposition"] in (UNKNOWN, REVIEW, KEEP)
    # Must NOT be SELL when event state is uncertain.
    assert result["disposition"] != SELL


# ──────────────────────────────────────────────────────────────────────────────
# Case 11 — Multiple protection reasons
# ──────────────────────────────────────────────────────────────────────────────

def test_case_11_multiple_reasons():
    """Event card AND locked → both reasons present."""
    card = _card(local_id=11, name="Kaeya", rarity="γ", skill=95.0, luck=6, favorite="🔒")
    result = _classify_one(
        card,
        session_event_cards = _event_store(card),
    )
    assert result["disposition"] == KEEP
    assert REASON_LOCKED    in result["reasons"]
    assert REASON_EVENT     in result["reasons"]
    assert REASON_HIGH_STATS in result["reasons"]
    assert len(result["reasons"]) >= 3


# ──────────────────────────────────────────────────────────────────────────────
# Case 12 — Sole remaining protected-series representative
# ──────────────────────────────────────────────────────────────────────────────

def test_case_12_sole_series_representative():
    """When only one card of a protected series exists it must be REVIEW, not SELL."""
    series_id = next(iter(PROTECTED_SERIES_IDS))
    card = _card(local_id=12, name="Subaru", rarity="γ", skill=20.0, luck=1, series_id=series_id)
    result = _classify_one(card)
    assert result["disposition"] == REVIEW
    assert REASON_SERIES_UNREVIEWED in result["reasons"]
    # Series invariant: at least one survivor must remain.
    results = [result]
    violations = assert_protected_series_invariant(results)
    # No violations because this card is REVIEW (not SELL).
    assert violations == []


# ──────────────────────────────────────────────────────────────────────────────
# Case 13 — Duplicate protected by another rule
# ──────────────────────────────────────────────────────────────────────────────

def test_case_13_duplicate_protected_by_event():
    """Two copies of same card: one is in event store → both survive independently."""
    card_a = _card(local_id=13, name="Rem", rarity="α", skill=20.0, luck=1)
    card_b = _card(local_id=14, name="Rem", rarity="α", skill=20.0, luck=1)
    event = _event_store(card_a)  # card_a is the event copy

    results = classify_cards([card_a, card_b], event, {}, {}, {}, set())
    by_lid = {r["local_id"]: r for r in results}

    # card_a (in event store by identity) → KEEP
    assert by_lid[13]["disposition"] == KEEP
    assert REASON_EVENT in by_lid[13]["reasons"]
    # card_b shares name+rarity → also KEEP (same event identity)
    assert by_lid[14]["disposition"] == KEEP
    assert REASON_EVENT in by_lid[14]["reasons"]


def test_case_13_duplicate_not_event():
    """Two copies, neither in event store, neither protected → both SELL."""
    card_a = _card(local_id=15, name="Generic", rarity="α", skill=10.0, luck=1)
    card_b = _card(local_id=16, name="Generic", rarity="α", skill=10.0, luck=1)
    results = classify_cards([card_a, card_b], {}, {}, {}, {}, set())
    for r in results:
        assert r["disposition"] == SELL


# ──────────────────────────────────────────────────────────────────────────────
# Cases 14/15/16 — Pagination (7 pages, 8 pages, >8 pages)
# ──────────────────────────────────────────────────────────────────────────────

def _make_page_fields(page_index: int, total_pages: int):
    return [(f"Page {page_index} of {total_pages}", "Final page?")]


def _make_page_desc(start_id: int, count: int = 20) -> str:
    lines = []
    for i in range(count):
        lines.append(f"{start_id + i} |  [α] Character {start_id + i}")
    return "\n".join(lines)


def _feed_pages(total_pages: int) -> HarvestStats:
    hs = HarvestStats()
    for idx in range(total_pages + 1):
        page = parse_list_page(
            title       = "TestUser's Waifus (Page 0)",
            description = _make_page_desc(idx * 20),
            fields      = _make_page_fields(idx, total_pages),
            source_message_id = 999,
        )
        hs.ingest(page)
    return hs


def test_case_14_pagination_7_pages():
    hs = _feed_pages(6)   # "Page 0 of 6" → 7 pages (0..6)
    assert hs.harvest_complete is True
    assert hs.expected_page_count == 7
    assert len(hs.seen_pages) == 7
    assert hs.pages_missing == []


def test_case_15_pagination_8_pages():
    hs = _feed_pages(7)   # "Page 0 of 7" → 8 pages (0..7)
    assert hs.harvest_complete is True
    assert hs.expected_page_count == 8
    assert len(hs.seen_pages) == 8


def test_case_16_pagination_more_than_8_pages():
    hs = _feed_pages(108)   # Real-world: "Page 0 of 108" → 109 pages
    assert hs.harvest_complete is True
    assert hs.expected_page_count == 109
    assert len(hs.seen_pages) == 109
    assert hs.pages_missing == []


# ──────────────────────────────────────────────────────────────────────────────
# Case 17 — Page containing an unparseable card
# ──────────────────────────────────────────────────────────────────────────────

def test_case_17_parse_failure_tracked_and_blocks_cleanup():
    title  = "TestUser's Waifus (Page 0)"
    desc   = (
        "15 |  [β] Kuki Shinobu\n"    # good
        "BAD LINE NO PIPE\n"           # not a card line (no pipe), not a failure
        "BADCARD | malformed rarity\n" # has pipe but no [rarity] → failure
        "17 |  [γ] Another Card\n"     # good
    )
    fields = [("Page 0 of 0", "Final page?")]
    page   = parse_list_page(title, desc, fields, source_message_id=1)

    assert len(page.entries) == 2
    assert page.parse_failures == 1
    assert len(page.failure_details) == 1
    assert page.failure_details[0]["reason"] == "regex_no_match"

    hs = HarvestStats()
    hs.ingest(page)
    assert hs.total_failures == 1
    assert hs.cleanup_blocked is True


# ──────────────────────────────────────────────────────────────────────────────
# Case 18 — Protected-series cleanup where one survivor must remain
# ──────────────────────────────────────────────────────────────────────────────

def test_case_18_series_invariant_no_violation():
    """KEEP/REVIEW/UNKNOWN cards count as survivors; series invariant passes."""
    series_id = 1   # Re:Zero — always in PROTECTED_SERIES_IDS
    card_keep = _card(local_id=18, name="Emilia", rarity="δ", skill=91.0, luck=6, series_id=series_id)
    card_sell = _card(local_id=19, name="Rem", rarity="α", skill=10.0, luck=1, series_id=series_id)

    identity_keep = _series_card_identity(card_keep)
    identity_sell = _series_card_identity(card_sell)

    reviews = {str(series_id): {"status": "complete", "decisions": {
        identity_keep: {"decision": "keep"},
        identity_sell: {"decision": "sell"},
    }}}

    results = classify_cards([card_keep, card_sell], {}, {}, {}, reviews, set())
    violations = assert_protected_series_invariant(results)
    assert violations == [], f"Unexpected violations: {violations}"

    by_lid = {r["local_id"]: r for r in results}
    assert by_lid[18]["disposition"] == KEEP
    assert by_lid[19]["disposition"] == SELL


def test_case_18_series_invariant_violation():
    """All cards in a protected series marked SELL → invariant violation."""
    series_id = 1
    card = _card(local_id=20, name="Felt", rarity="ε", skill=50.0, luck=2, series_id=series_id)
    identity = _series_card_identity(card)
    reviews = {str(series_id): {"status": "complete", "decisions": {
        identity: {"decision": "sell"},
    }}}

    results = classify_cards([card], {}, {}, {}, reviews, set())
    violations = assert_protected_series_invariant(results)
    assert any("1" in v for v in violations), f"Expected series 1 violation, got: {violations}"


# ──────────────────────────────────────────────────────────────────────────────
# Regression — real JSONL pagination behaviour
# One message, page 0 as new message, pages 1..N as edits of same message ID.
# ──────────────────────────────────────────────────────────────────────────────

def test_regression_jsonl_pagination_pattern():
    """Simulate the real Waifugami pattern: one message, repeatedly edited.

    Ground truth from _l_-event_all_preview.jsonl:
      msg_id=1555185742282752071
      Page 0 of 108  → on_message  (new message)
      Page 1 of 108  → on_message_edit
      Page 2 of 108  → on_message_edit
      Page 3 of 108  → on_message_edit
      Page 108 of 108 → on_message_edit
      Page 107 of 108 → on_message_edit  (can arrive out of order)
      Page 106 of 108 → on_message_edit

    We simulate ingesting pages 0..108 (total 109) and verify all are captured.
    """
    TOTAL_PAGES = 108
    hs = HarvestStats()

    # Page 0 comes first (new message)
    page0 = parse_list_page(
        title       = "itsremi's Waifus (Page 0)",
        description = _make_page_desc(0),
        fields      = [("Page 0 of 108", "Final page?")],
        source_message_id = 1555185742282752071,
    )
    assert hs.ingest(page0) is True  # new

    # Pages 1..108 arrive as edits (same message id)
    for idx in range(1, TOTAL_PAGES + 1):
        page = parse_list_page(
            title       = f"itsremi's Waifus (Page {idx})",
            description = _make_page_desc(idx * 20),
            fields      = [(f"Page {idx} of 108", "Final page?")],
            source_message_id = 1555185742282752071,
        )
        hs.ingest(page)

    assert hs.expected_page_count == 109
    assert len(hs.seen_pages) == 109
    assert hs.pages_missing == []
    assert hs.harvest_complete is True


def test_regression_duplicate_page_update_deduplication():
    """Both on_raw_message_edit and on_message_edit fire per page.

    on_raw_message_edit has no embed so it never reaches ingest.
    But if it somehow did (or on_message_edit fires twice), the
    seen_pages set must deduplicate.
    """
    hs = HarvestStats()
    page = parse_list_page(
        title       = "User's Waifus (Page 3)",
        description = _make_page_desc(60),
        fields      = [("Page 3 of 10", "Final page?")],
    )
    r1 = hs.ingest(page)   # first time: new
    r2 = hs.ingest(page)   # second time: duplicate
    assert r1 is True
    assert r2 is False
    assert hs.duplicate_page_updates == 1
    assert len(hs.seen_pages) == 1
    assert hs.total_parsed == 40   # 20 entries × 2 ingests... wait
    # NOTE: total_parsed counts ALL ingests including duplicates, because
    # the duplicate is still "source data" for counting purposes.
    # What matters is that dedup doesn't double-add event_cards in the mixin.


def test_regression_missing_page_blocks_cleanup():
    """If page 57 is never observed, harvest is not complete."""
    hs = HarvestStats()
    for idx in range(108 + 1):
        if idx == 57:
            continue   # skip page 57
        page = parse_list_page(
            title       = f"User's Waifus (Page {idx})",
            description = _make_page_desc(idx * 20),
            fields      = [(f"Page {idx} of 108", "Final page?")],
        )
        hs.ingest(page)

    assert hs.harvest_complete is False
    assert 57 in (hs.pages_missing or [])
    assert hs.cleanup_blocked is True


# ──────────────────────────────────────────────────────────────────────────────
# Harvest stats summary
# ──────────────────────────────────────────────────────────────────────────────

def test_harvest_stats_summary_lines():
    hs = _feed_pages(5)
    lines = hs.summary_lines()
    assert any("6/6" in l for l in lines), f"Expected 6/6 in: {lines}"


# ──────────────────────────────────────────────────────────────────────────────
# parse_list_page — real entry formats from JSONL logs
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line,expected_id,expected_rarity,expected_name,expected_emoji", [
    ("15 |  [β] Kuki Shinobu",              15,   "β", "Kuki Shinobu",      None),
    ("16 |  [β] Asada Shino",               16,   "β", "Asada Shino",       None),
    ("34 | 🎁 [δ] Tenma Tsukasa",           34,   "δ", "Tenma Tsukasa",     "🎁"),
    ("55 | 🏵 [γ] Kaeya",                   55,   "γ", "Kaeya",             "🏵"),
    ("89 | 🎄 [β] Akira Hayama",            89,   "β", "Akira Hayama",      "🎄"),
    ("140 | 💝 [α] Rumi Usagiyama",         140,  "α", "Rumi Usagiyama",    "💝"),
    ("8226 | 🕸 [γ] Ghislaine Dedoldia",   8226,  "γ", "Ghislaine Dedoldia","🕸"),
    ("267 | 🎤 [γ] Vestia Zeta",            267,  "γ", "Vestia Zeta",       "🎤"),
    ("3607 | 🔮 [δ] Osakabehime",           3607, "δ", "Osakabehime",       "🔮"),
    ("0 | 🚮 [β] Echidna",                  0,    "β", "Echidna",           "🚮"),
])
def test_parse_real_line_formats(line, expected_id, expected_rarity, expected_name, expected_emoji):
    page = parse_list_page(
        title       = "User's Waifus (Page 0)",
        description = line,
        fields      = [("Page 0 of 0", "Final page?")],
    )
    assert page.parse_failures == 0, f"Parse failure on: {line!r}"
    assert len(page.entries) == 1
    entry = page.entries[0]
    assert entry.local_id     == expected_id
    assert entry.rarity       == expected_rarity
    assert entry.name         == expected_name
    assert entry.status_emoji == expected_emoji


def test_parse_page_field_extracts_correct_numbers():
    """Page 108 of 108 → page_index=108, total_pages=108."""
    page = parse_list_page(
        title       = "User's Waifus (Page 108)",
        description = "",
        fields      = [("Page 108 of 108", "Final page?")],
    )
    assert page.page_index  == 108
    assert page.total_pages == 108
    assert page.expected_page_count == 109   # via property


def test_parse_page_0_of_108():
    """Page 0 of 108 is zero-based: expected_page_count must be 109."""
    page = parse_list_page(
        title       = "User's Waifus (Page 0)",
        description = "",
        fields      = [("Page 0 of 108", "Final page?")],
    )
    assert page.page_index  == 0
    assert page.total_pages == 108
    assert page.expected_page_count == 109


def test_parse_non_card_line_not_a_failure():
    """Lines without '|' are ignored silently, not counted as failures."""
    page = parse_list_page(
        title       = "User's Waifus (Page 0)",
        description = "15 |  [β] Kuki Shinobu\nJust a plain line\n17 |  [γ] Another",
        fields      = [("Page 0 of 0", "Final page?")],
    )
    assert len(page.entries) == 2
    assert page.parse_failures == 0


def test_parse_bad_card_line_is_failure():
    """Line with '|' but no valid rarity → parse failure."""
    page = parse_list_page(
        title       = "User's Waifus (Page 0)",
        description = "BADCARD | malformed no rarity",
        fields      = [("Page 0 of 0", "Final page?")],
    )
    assert page.parse_failures == 1
    assert page.entries == []


# ──────────────────────────────────────────────────────────────────────────────
# select_series_survivor
# ──────────────────────────────────────────────────────────────────────────────

def test_survivor_prefers_high_skill():
    a = _card(local_id=1, name="A", rarity="α", skill=20.0, luck=1)
    b = _card(local_id=2, name="B", rarity="β", skill=95.0, luck=1)
    c = _card(local_id=3, name="C", rarity="γ", skill=40.0, luck=7)
    survivor = select_series_survivor([a, b, c])
    # b has skill 95 — highest → wins
    assert survivor["local_id"] == 2


def test_survivor_prefers_locked_over_skill():
    a = _card(local_id=1, name="A", rarity="α", skill=95.0, luck=6)
    b = _card(local_id=2, name="B", rarity="β", skill=20.0, luck=1, favorite="🔒")
    survivor = select_series_survivor([a, b])
    assert survivor["local_id"] == 2   # locked wins


def test_survivor_stable_tiebreak():
    a = _card(local_id=10, name="Same", rarity="α", skill=30.0, luck=2)
    b = _card(local_id=5,  name="Same", rarity="α", skill=30.0, luck=2)
    survivor = select_series_survivor([a, b])
    assert survivor["local_id"] == 5   # lower local_id = stable tie-break


def test_survivor_empty_list():
    assert select_series_survivor([]) is None


# ──────────────────────────────────────────────────────────────────────────────
# assert_protected_series_invariant
# ──────────────────────────────────────────────────────────────────────────────

def test_invariant_all_protected_series_have_survivor():
    """If no card from a protected series is in the results, that's a violation."""
    # Only include series 1 with a SELL card — all other protected series are absent
    results = [{
        "local_id": 1, "global_id": None, "name": "Rem", "rarity_symbol": "α",
        "series_id": "1", "disposition": SELL, "reasons": [], "eligibility_reasons": [],
        "skill": 10.0, "luck": 1, "dp_yield": 15, "shard_yield": 0, "card": {},
    }]
    violations = assert_protected_series_invariant(results)
    # Series 1 has no survivor → violation
    assert any("1" in v for v in violations)


def test_invariant_passes_when_review_card_present():
    """A REVIEW card counts as a survivor."""
    results = [{
        "local_id": 1, "global_id": None, "name": "Rem", "rarity_symbol": "δ",
        "series_id": "1", "disposition": REVIEW, "reasons": [], "eligibility_reasons": [],
        "skill": 50.0, "luck": 3, "dp_yield": 40, "shard_yield": 0, "card": {},
    }]
    # All other protected series are absent from results — only check series 1
    # (other series aren't in results so they still show as violations)
    violations = assert_protected_series_invariant(results)
    # Series 1 has a REVIEW card → no violation for series 1
    assert not any("Series 1 would have NO" in v for v in violations)


# ──────────────────────────────────────────────────────────────────────────────
# Omega and Sigma cards
# ──────────────────────────────────────────────────────────────────────────────

def test_omega_always_keep():
    card = _card(local_id=100, name="Legend", rarity="ω", skill=5.0, luck=0)
    result = _classify_one(card)
    assert result["disposition"] == KEEP
    assert REASON_OMEGA in result["reasons"]


def test_sigma_card_from_store():
    card = _card(local_id=101, name="Sigma Card", rarity="σ", skill=50.0, luck=2)
    sigma_store = {"101": {"name": "Sigma Card", "rarity": "σ"}}
    result = _classify_one(card, persistent_sigma_cards=sigma_store)
    assert result["disposition"] == KEEP
    assert REASON_SIGMA in result["reasons"]


# ──────────────────────────────────────────────────────────────────────────────
# Locked local IDs set
# ──────────────────────────────────────────────────────────────────────────────

def test_locked_by_local_id_set():
    card = _card(local_id=200, name="Card", rarity="α", skill=10.0, luck=1)
    result = _classify_one(card, locked_local_ids={200})
    assert result["disposition"] == KEEP
    assert REASON_LOCKED in result["reasons"]


# ──────────────────────────────────────────────────────────────────────────────
# Eligibility reasons must be present on SELL cards
# ──────────────────────────────────────────────────────────────────────────────

def test_sell_cards_have_eligibility_reasons():
    card = _card(local_id=300, name="Low Value", rarity="α", skill=15.0, luck=1)
    result = _classify_one(card)
    assert result["disposition"] == SELL
    assert len(result["eligibility_reasons"]) > 0
    assert ELIG_NON_EVENT in result["eligibility_reasons"]


# ──────────────────────────────────────────────────────────────────────────────
# High-value rarity (ζ, ε) → REVIEW, not SELL
# ──────────────────────────────────────────────────────────────────────────────

def test_zeta_always_review():
    card = _card(local_id=400, name="Julius", rarity="ζ", skill=5.0, luck=0)
    result = _classify_one(card)
    assert result["disposition"] == REVIEW
    assert REASON_HIGH_VALUE in result["reasons"]


def test_epsilon_always_review():
    card = _card(local_id=401, name="Blade", rarity="ε", skill=5.0, luck=0)
    result = _classify_one(card)
    assert result["disposition"] == REVIEW
    assert REASON_HIGH_VALUE in result["reasons"]


# ──────────────────────────────────────────────────────────────────────────────
# Safety: classify_cards never produces None disposition
# ──────────────────────────────────────────────────────────────────────────────

def test_all_dispositions_are_set():
    """Every card in a batch must have a non-None disposition."""
    cards = [
        _card(local_id=i, name=f"Card {i}", rarity="α", skill=None, luck=None)
        for i in range(20)
    ]
    results = classify_cards(cards, {}, {}, {}, {}, set())
    for r in results:
        assert r["disposition"] is not None
        assert r["disposition"] in (KEEP, REVIEW, SELL, UNKNOWN)