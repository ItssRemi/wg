"""audit.py — Waifugami List Audit & Cleanup Engine (bot mixin layer).
 
Pure classification logic lives in audit_engine.py (no bot imports there).
This file adds AuditSession, AuditMixin, all Red commands/listeners, and the
persistent JSONL audit log.
"""
 
from __future__ import annotations
 
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
 
import discord
from discord.http import Route
from redbot.core import commands, data_manager
 
from .audit_engine import (
    KEEP, REVIEW, SELL, UNKNOWN,
    REASON_LOCKED, REASON_EVENT, REASON_SIGMA, REASON_OMEGA,
    REASON_SERIES_KEEP, REASON_SERIES_SELL, REASON_SERIES_UNREVIEWED,
    REASON_HIGH_STATS, REASON_HIGH_VALUE, REASON_NEAR_THRESHOLD, REASON_NO_STATS,
    RARITY_DP, OMEGA_SHARDS_PER_CARD, OMEGA_SYMBOLS,
    HIGH_VALUE_REVIEW_RARITIES, PROTECTED_SERIES_IDS,
    SKILL_PROTECT_THRESHOLD, LUCK_PROTECT_THRESHOLD,
    RM_BATCH_SIZE, AUDIT_SESSION_TTL,
    LIST_TITLE_RE, LIST_ENTRY_RE, FINAL_PAGE_FIELD_NAME_RE,
    WAIFUGAMI_ID, V2_FLAG,
    classify_cards, _chunk, _is_locked, _skill, _luck,
    _rarity_symbol, _dp_for_card, _shards_for_card,
    _card_identity, _series_card_identity, _series_id_for,
    parse_list_page, ParsedPage, HarvestStats,
    assert_protected_series_invariant, select_series_survivor,
)
 
log = logging.getLogger("red.wg.audit")
 
 
def _card_snapshot(
    *,
    local_id:     Optional[int],
    name:         str,
    rarity:       str,
    status_emoji: Optional[str] = None,
    global_id:    Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "name":         str(name).strip(),
        "rarity":       str(rarity).strip().lower(),
        "status_emoji": str(status_emoji).strip() if status_emoji else None,
        "last_seen_id": int(local_id) if local_id is not None else None,
        "global_id":    int(global_id) if global_id is not None else None,
        "last_seen_at": time.time(),
    }
 
 
# ──────────────────────────────────────────────────────────────────────────────
# Audit session
# ──────────────────────────────────────────────────────────────────────────────
 
class AuditSession:
    """All mutable state for one user's in-progress audit."""
 
    __slots__ = (
        "session_id",
        "user_id", "channel_id", "guild_id",
        "phase",            # "harvesting"|"classified"|"confirming"|"executing"
        "list_message_id",  # Waifugami's .l -event all message id
        "event_cards",      # Dict[str, Dict] — local_id → snapshot
        "harvest_stats",    # HarvestStats — pagination/parse integrity
        "classified",       # List[dict] — output of classify_cards()
        "sell_ids",         # List[int] — local_ids selected for removal
        "rarity_filter",    # Optional[str]
        "dupes_only",       # bool
        "guide_message_id",
        "created",          # float (monotonic)
        "last_active",      # float (monotonic)
        "batch_queue",      # List[List[int]]
        "current_batch",    # Optional[List[int]]
        "series_review_series_id",
        "series_review_index",
    )
 
    def __init__(self, user_id: int, channel_id: int, guild_id: int):
        self.session_id       = f"{guild_id}_{user_id}_{int(time.time())}"
        self.user_id          = user_id
        self.channel_id       = channel_id
        self.guild_id         = guild_id
        self.phase            = "harvesting"
        self.list_message_id: Optional[int] = None
        self.event_cards:     Dict[str, Dict[str, Any]] = {}
        self.harvest_stats    = HarvestStats()
        self.classified:      List[Dict[str, Any]] = []
        self.sell_ids:        List[int] = []
        self.rarity_filter:   Optional[str] = None
        self.dupes_only:      bool = False
        self.guide_message_id: Optional[int] = None
        self.created          = time.monotonic()
        self.last_active      = time.monotonic()
        self.batch_queue:     List[List[int]] = []
        self.current_batch:   Optional[List[int]] = None
        self.series_review_series_id: Optional[int] = None
        self.series_review_index: int = 0
 
    def touch(self) -> None:
        self.last_active = time.monotonic()
 
    def expired(self) -> bool:
        return (time.monotonic() - self.last_active) > AUDIT_SESSION_TTL
 
 
# ──────────────────────────────────────────────────────────────────────────────
# JSONL audit log
# ──────────────────────────────────────────────────────────────────────────────
 
class AuditLog:
    """Append-only JSONL audit trail stored in the cog's data directory.
 
    One file per guild: <cog_data_path>/audit_log_<guild_id>.jsonl
 
    Logging failure NEVER blocks cleanup execution — failures are caught and
    logged to the Red logger only.
    """
 
    def __init__(self, cog_data_path: Path):
        self._base = cog_data_path
 
    def _log_path(self, guild_id: int) -> Path:
        return self._base / f"audit_log_{guild_id}.jsonl"
 
    def _append(self, guild_id: int, record: Dict[str, Any]) -> None:
        try:
            path = self._log_path(guild_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            log.error("AuditLog write failed (non-fatal): %s", exc)
 
    def record_audit_complete(
        self,
        *,
        session: "AuditSession",
        classified: List[Dict[str, Any]],
        guild_id: int,
    ) -> None:
        counts = {KEEP: 0, REVIEW: 0, SELL: 0, UNKNOWN: 0}
        for r in classified:
            counts[r["disposition"]] += 1
 
        hs = session.harvest_stats
        self._append(guild_id, {
            "event":            "audit_complete",
            "timestamp":        time.time(),
            "session_id":       session.session_id,
            "user_id":          session.user_id,
            "guild_id":         guild_id,
            "channel_id":       session.channel_id,
            "expected_pages":   hs.expected_page_count,
            "pages_harvested":  len(hs.seen_pages),
            "missing_pages":    hs.pages_missing or [],
            "duplicate_updates": hs.duplicate_page_updates,
            "source_lines":     hs.total_source_lines,
            "parsed_cards":     hs.total_parsed,
            "parse_failures":   hs.total_failures,
            "total_classified": len(classified),
            "keep_count":       counts[KEEP],
            "review_count":     counts[REVIEW],
            "sell_count":       counts[SELL],
            "unknown_count":    counts[UNKNOWN],
            "harvest_complete": hs.harvest_complete,
            "cleanup_blocked":  hs.cleanup_blocked,
        })
 
    def record_execution_intent(
        self,
        *,
        session: "AuditSession",
        targets: List[Dict[str, Any]],
        guild_id: int,
    ) -> None:
        """Written immediately before any .rm commands are sent."""
        cards_info = [
            {
                "local_id":    t.get("local_id"),
                "global_id":   t.get("global_id"),
                "name":        t.get("name"),
                "rarity":      t.get("rarity_symbol"),
                "series_id":   t.get("series_id"),
                "skill":       t.get("skill"),
                "luck":        t.get("luck"),
                "reasons":     t.get("reasons", []),
                "eligibility": t.get("eligibility_reasons", []),
            }
            for t in targets
        ]
        self._append(guild_id, {
            "event":      "removal_intended",
            "timestamp":  time.time(),
            "session_id": session.session_id,
            "user_id":    session.user_id,
            "guild_id":   guild_id,
            "channel_id": session.channel_id,
            "card_count": len(targets),
            "cards":      cards_info,
        })
 
    def record_removal_result(
        self,
        *,
        session: "AuditSession",
        local_id: int,
        status: str,                  # "removal_requested" | "revalidation_failed" | "removal_failed"
        name: Optional[str],
        rarity: Optional[str],
        global_id: Optional[int],
        dp_received: Optional[int],
        guild_id: int,
        failure_reason: Optional[str] = None,
    ) -> None:
        self._append(guild_id, {
            "event":          status,
            "timestamp":      time.time(),
            "session_id":     session.session_id,
            "user_id":        session.user_id,
            "guild_id":       guild_id,
            "local_id":       local_id,
            "global_id":      global_id,
            "name":           name,
            "rarity":         rarity,
            "dp_received":    dp_received,   # null if unknown
            "failure_reason": failure_reason,
        })
 
    def record_revalidation_failure(
        self,
        *,
        session: "AuditSession",
        local_id: int,
        audited_name: Optional[str],
        audited_rarity: Optional[str],
        live_name: Optional[str],
        live_rarity: Optional[str],
        guild_id: int,
    ) -> None:
        self._append(guild_id, {
            "event":          "revalidation_failed",
            "timestamp":      time.time(),
            "session_id":     session.session_id,
            "user_id":        session.user_id,
            "guild_id":       guild_id,
            "local_id":       local_id,
            "audited_name":   audited_name,
            "audited_rarity": audited_rarity,
            "live_name":      live_name,
            "live_rarity":    live_rarity,
        })
 
 
# ──────────────────────────────────────────────────────────────────────────────
# Mixin class
# ──────────────────────────────────────────────────────────────────────────────
 
class AuditMixin:
    """Mixin that adds the List Audit & Cleanup Engine to the Waifugami cog.
 
    Requirements from the host cog
    ────────────────────────────────
    self.bot                  — Red bot instance
    self.config               — per-user Config (cards, removed_cards keys)
    self._char_index          — Dict[str, dict] waifu_id → catalog entry
    self._series_index        — Dict[str, str] series_id → series name
    self._locks               — Dict[int, asyncio.Lock]
    self._send_channel_v2_components(channel, components)
    self._section_components(sections) → List[dict]
    WAIFUGAMI_ID, V2_FLAG
    """
 
    # ── Lifecycle ─────────────────────────────────────────────────────────
 
    def _audit_init(self) -> None:
        """Call from __init__ of the host cog."""
        self._audit_sessions: Dict[int, AuditSession] = {}
        try:
            cog_data_path = data_manager.cog_data_path(self)
        except Exception:  # noqa: BLE001
            cog_data_path = Path("./audit_data")
        self._audit_log = AuditLog(cog_data_path)
 
    # ── Utility ───────────────────────────────────────────────────────────
 
    def _audit_session(self, user_id: int) -> Optional[AuditSession]:
        session = self._audit_sessions.get(user_id)
        if session and session.expired():
            del self._audit_sessions[user_id]
            return None
        return session
 
    def _audit_cancel(self, user_id: int) -> None:
        self._audit_sessions.pop(user_id, None)
 
    async def _audit_active_cards(self, user_id: int) -> List[Dict[str, Any]]:
        cards = await self.config.user_from_id(user_id).cards()
        return [c for c in cards.values() if c.get("status") == "active"]
 
    async def _audit_protection(self, user_id: int) -> Dict[str, Any]:
        data = await self.config.user_from_id(user_id).audit_protection()
        if not isinstance(data, dict):
            data = {}
        data.setdefault("event_cards", {})
        data.setdefault("sigma_cards", {})
        data.setdefault("protected_series", {})
        return data
 
    async def _save_audit_protection(
        self, user_id: int, protection: Dict[str, Any]
    ) -> None:
        await self.config.user_from_id(user_id).audit_protection.set(protection)
 
    async def _persist_event_cards(
        self,
        user_id: int,
        cards: Dict[str, Dict[str, Any]],
    ) -> None:
        """Persist every physical event card seen during the scan."""
        if not cards:
            return
        protection = await self._audit_protection(user_id)
        event_cards = protection["event_cards"]
        sigma_cards = protection["sigma_cards"]
 
        for local_id_key, snapshot in cards.items():
            event_cards[str(local_id_key)] = dict(snapshot)
            if str(snapshot.get("rarity", "")).strip().lower() == "σ":
                sigma_key    = str(local_id_key)
                merged_sigma = dict(sigma_cards.get(sigma_key, {}))
                merged_sigma.update({
                    "name":         snapshot.get("name", "Unknown"),
                    "rarity":       snapshot.get("rarity", "σ"),
                    "last_seen_at": snapshot.get("last_seen_at", time.time()),
                })
                if snapshot.get("last_seen_id") is not None:
                    merged_sigma["last_seen_id"] = snapshot["last_seen_id"]
                if snapshot.get("global_id") is not None:
                    merged_sigma["global_id"] = snapshot["global_id"]
                sigma_cards[sigma_key] = merged_sigma
 
        protection["event_cards"] = event_cards
        protection["sigma_cards"] = sigma_cards
        await self._save_audit_protection(user_id, protection)
 
    def _enrich_with_catalog(
        self, cards: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Fill in series_id from the catalog when the stored card lacks it."""
        enriched = []
        for card in cards:
            c = dict(card)
            if c.get("series_id") is None:
                wid   = str(c.get("waifu_id") or "")
                entry = self._char_index.get(wid)
                if entry:
                    try:
                        c["series_id"] = str(int(entry.get("series_id")))
                    except (TypeError, ValueError):
                        pass
            enriched.append(c)
        return enriched
 
    # ── Persistent series review state ────────────────────────────────────
 
    async def _series_reviews(self, user_id: int) -> Dict[str, Any]:
        protection = await self._audit_protection(user_id)
        return protection.get("protected_series", {})
 
    async def _save_series_review(
        self, user_id: int, series_id: str, review: Dict[str, Any]
    ) -> None:
        protection = await self._audit_protection(user_id)
        protection["protected_series"][series_id] = review
        await self._save_audit_protection(user_id, protection)
 
    async def _record_series_decision(
        self,
        user_id: int,
        series_id: str,
        card: Dict[str, Any],
        decision: str,
    ) -> None:
        protection    = await self._audit_protection(user_id)
        series_reviews = protection["protected_series"]
        review = series_reviews.setdefault(str(series_id), {
            "status": "in_progress",
            "decisions": {},
        })
        review.setdefault("decisions", {})
        review.setdefault("status", "in_progress")
 
        identity = _series_card_identity(card)
        review["decisions"][identity] = {
            "decision":    decision,
            "name":        card.get("name", "Unknown"),
            "rarity":      _rarity_symbol(card),
            "last_seen_id": card.get("local_id"),
            "decided_at":  time.time(),
        }
        protection["protected_series"][str(series_id)] = review
        await self._save_audit_protection(user_id, protection)
 
    async def _mark_series_complete(self, user_id: int, series_id: str) -> None:
        protection = await self._audit_protection(user_id)
        review = protection["protected_series"].get(str(series_id), {})
        review["status"] = "complete"
        protection["protected_series"][str(series_id)] = review
        await self._save_audit_protection(user_id, protection)
 
    # ── Series review UI ──────────────────────────────────────────────────
 
    async def _audit_show_series_review(self, ctx: commands.Context) -> None:
        """Show the next protected-series card that needs a decision."""
        session = self._audit_session(ctx.author.id)
        if not session or not session.classified:
            await ctx.reply(
                "No active audit. Run `..wg audit start` or `..wg audit classify` first.",
                mention_author=False,
            )
            return
 
        raw_cards = await self._audit_active_cards(session.user_id)
        enriched  = self._enrich_with_catalog(raw_cards)
        protection = await self._audit_protection(session.user_id)
        reviews    = protection.get("protected_series", {})
 
        pending_by_series: Dict[int, List[Dict[str, Any]]] = {}
        for card in enriched:
            raw_sid = _series_id_for(card)
            if raw_sid is None:
                continue
            sid_int = int(raw_sid)
            if sid_int not in PROTECTED_SERIES_IDS:
                continue
            review    = reviews.get(str(sid_int), {})
            decisions = review.get("decisions", {})
            if _series_card_identity(card) in decisions:
                continue
            pending_by_series.setdefault(sid_int, []).append(card)
 
        if not pending_by_series:
            session.series_review_series_id = None
            session.series_review_index     = 0
            await ctx.reply(
                "All protected-series cards already have decisions.\n"
                "Run `..wg audit classify` again, then `..wg audit sell`.",
                mention_author=False,
            )
            return
 
        current = session.series_review_series_id
        if current is not None and current in pending_by_series:
            series_id = current
        else:
            series_id = sorted(pending_by_series)[0]
            session.series_review_series_id = series_id
            session.series_review_index     = 0
 
        cards = pending_by_series[series_id]
        if session.series_review_index >= len(cards):
            session.series_review_index = 0
        card = cards[session.series_review_index]
 
        series_name = (
            self._series_index.get(str(series_id))
            or self._series_index.get(series_id)
            or f"Series {series_id}"
        )
        await self._send_series_review_card(
            ctx.channel, session, series_id, series_name, card, len(cards)
        )
 
    async def _send_series_review_card(
        self,
        channel:      discord.abc.Messageable,
        session:      AuditSession,
        series_id:    int,
        series_name:  str,
        card:         Dict[str, Any],
        pending_count: int,
    ) -> None:
        skill = card.get("skill")
        luck  = card.get("luck")
        skill_text = f"{float(skill):.2f}" if skill is not None else "?"
        luck_text  = str(luck) if luck is not None else "?"
        content = (
            f"## Protected Series Review\n"
            f"**Series:** `{series_id}` {series_name}\n"
            f"**Card:** `{card.get('local_id', '?')}` "
            f"**{card.get('name', 'Unknown')}** "
            f"`[{_rarity_symbol(card).upper()}]`\n"
            f"Skill `{skill_text}` · Luck `{luck_text}`\n\n"
            f"-# `{pending_count}` undecided card(s) remain in this series.\n"
            f"-# You must keep at least one card from every protected series."
        )
        local_id = card.get("local_id")
        if local_id is None:
            return
        components = [
            {"type": 10, "content": content},
            {
                "type": 1,
                "components": [
                    {
                        "type": 2, "style": 3, "label": "Keep",
                        "custom_id": (
                            f"nebwg:auditseries:{session.user_id}:"
                            f"{series_id}:keep:{int(local_id)}"
                        ),
                    },
                    {
                        "type": 2, "style": 4, "label": "Sell",
                        "custom_id": (
                            f"nebwg:auditseries:{session.user_id}:"
                            f"{series_id}:sell:{int(local_id)}"
                        ),
                    },
                ],
            },
        ]
        await self._send_channel_v2_components(channel, components)
 
    @commands.Cog.listener("on_interaction")
    async def audit_on_interaction(self, interaction: discord.Interaction) -> None:
        """Handle protected-series Keep/Sell buttons."""
        data      = interaction.data or {}
        custom_id = str(data.get("custom_id") or "")
        match = re.fullmatch(
            r"nebwg:auditseries:(\d+):(\d+):(keep|sell):(\d+)",
            custom_id,
        )
        if not match:
            return
 
        user_id   = int(match.group(1))
        series_id = int(match.group(2))
        decision  = match.group(3)
        local_id  = int(match.group(4))
 
        if interaction.user.id != user_id:
            await interaction.response.send_message(
                "This audit review belongs to someone else.", ephemeral=True
            )
            return
 
        session = self._audit_session(user_id)
        if not session or not session.classified:
            await interaction.response.send_message(
                "This audit session has expired. Re-run the audit.", ephemeral=True
            )
            return
 
        await interaction.response.defer_update()
 
        raw_cards = await self._audit_active_cards(user_id)
        enriched  = self._enrich_with_catalog(raw_cards)
        card      = next(
            (c for c in enriched
             if c.get("local_id") is not None and int(c["local_id"]) == local_id),
            None,
        )
        if card is None:
            await interaction.followup.send(
                "That card is no longer present at this local ID. "
                "Re-run the audit before continuing.",
                ephemeral=True,
            )
            return
 
        actual_series = _series_id_for(card)
        if actual_series is None or int(actual_series) != series_id:
            await interaction.followup.send(
                "That card no longer belongs to the reviewed series. Re-run the audit.",
                ephemeral=True,
            )
            return
 
        # Safety: don't allow selling the last representative.
        if decision == "sell":
            series_cards = [
                c for c in enriched if _series_id_for(c) == str(series_id)
            ]
            protection = await self._audit_protection(user_id)
            review     = protection.get("protected_series", {}).get(str(series_id), {})
            decisions  = review.get("decisions", {})
 
            this_identity = _series_card_identity(card)
            keep_others = {
                _series_card_identity(c)
                for c in series_cards
                if _series_card_identity(c) != this_identity
                and decisions.get(_series_card_identity(c), {}).get("decision") == "keep"
            }
            undecided_others = {
                _series_card_identity(c)
                for c in series_cards
                if _series_card_identity(c) != this_identity
                and _series_card_identity(c) not in decisions
            }
            if not keep_others and not undecided_others:
                await interaction.followup.send(
                    "You cannot sell this card because it is the last remaining "
                    "representative of this protected series.",
                    ephemeral=True,
                )
                return
 
        await self._record_series_decision(user_id, str(series_id), card, decision)
        await self._audit_do_classify(session, interaction.channel)
 
        # Find remaining undecided cards.
        raw_cards  = await self._audit_active_cards(user_id)
        enriched   = self._enrich_with_catalog(raw_cards)
        protection = await self._audit_protection(user_id)
        reviews    = protection.get("protected_series", {})
 
        pending_by_series: Dict[int, List[Dict[str, Any]]] = {}
        for cand in enriched:
            sid = _series_id_for(cand)
            if sid is None:
                continue
            sid_int = int(sid)
            if sid_int not in PROTECTED_SERIES_IDS:
                continue
            rev = reviews.get(str(sid_int), {})
            if _series_card_identity(cand) not in rev.get("decisions", {}):
                pending_by_series.setdefault(sid_int, []).append(cand)
 
        if not pending_by_series:
            session.series_review_series_id = None
            session.series_review_index     = 0
            await interaction.followup.send(
                "Protected-series review complete.\n"
                "Run `..wg audit sell` to review the resulting removal list.",
                ephemeral=True,
            )
            return
 
        next_series = series_id if series_id in pending_by_series else sorted(pending_by_series)[0]
        session.series_review_series_id = next_series
        session.series_review_index     = 0
        next_cards  = pending_by_series[next_series]
        next_card   = next_cards[0]
        series_name = (
            self._series_index.get(str(next_series))
            or self._series_index.get(next_series)
            or f"Series {next_series}"
        )
 
        payload = {
            "flags": V2_FLAG,
            "allowed_mentions": {"parse": []},
            "components": [{"type": 17, "components": [
                {
                    "type": 10,
                    "content": (
                        f"## Protected Series Review\n"
                        f"**Series:** `{next_series}` {series_name}\n"
                        f"**Card:** `{next_card.get('local_id', '?')}` "
                        f"**{next_card.get('name', 'Unknown')}** "
                        f"`[{_rarity_symbol(next_card).upper()}]`\n"
                        f"Skill `{next_card.get('skill', '?')}` · "
                        f"Luck `{next_card.get('luck', '?')}`\n\n"
                        f"-# `{len(next_cards)}` undecided card(s) remain.\n"
                        f"-# You must keep at least one card from every protected series."
                    ),
                },
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2, "style": 3, "label": "Keep",
                            "custom_id": (
                                f"nebwg:auditseries:{user_id}:"
                                f"{next_series}:keep:{int(next_card['local_id'])}"
                            ),
                        },
                        {
                            "type": 2, "style": 4, "label": "Sell",
                            "custom_id": (
                                f"nebwg:auditseries:{user_id}:"
                                f"{next_series}:sell:{int(next_card['local_id'])}"
                            ),
                        },
                    ],
                },
            ]}],
        }
        route = Route(
            "PATCH",
            "/webhooks/{application_id}/{interaction_token}/messages/@original",
            application_id=self.bot.user.id,
            interaction_token=interaction.token,
        )
        await self.bot.http.request(route, json=payload)
 
    # ── Embed parsing ─────────────────────────────────────────────────────
 
    @staticmethod
    def _parse_event_list_embed(
        embed: discord.Embed,
        source_message_id: Optional[int] = None,
    ) -> ParsedPage:
        """Parse one page embed into a ParsedPage.
 
        This is the single parse entry point for Discord embed objects.
        It extracts title/description/fields and delegates to the pure
        parse_list_page() in audit_engine so it is independently testable.
        """
        fields = [(f.name or "", f.value or "") for f in (embed.fields or [])]
        return parse_list_page(
            title              = embed.title or "",
            description        = embed.description or "",
            fields             = fields,
            source_message_id  = source_message_id,
        )
 
    # ── Stage 1: harvest ──────────────────────────────────────────────────
 
    async def _audit_start(self, ctx: commands.Context) -> None:
        user_id = ctx.author.id
        self._audit_cancel(user_id)
        session = AuditSession(
            user_id    = user_id,
            channel_id = ctx.channel.id,
            guild_id   = ctx.guild.id,
        )
        self._audit_sessions[user_id] = session
        guide = await ctx.reply(
            "**Audit started.** Run `.l -event all` in this channel so I can "
            "harvest your event cards, then I will classify your full collection.\n"
            "-# If you have no event cards, just run `..wg audit classify` instead.",
            mention_author=False,
        )
        session.guide_message_id = guide.id
 
    async def _audit_collect_page(self, message: discord.Message) -> None:
        """Process one page of a `.l -event all` embed (new message OR edit).
 
        Called for:
        - The initial new message (page 0) via audit_on_new_message
        - Every subsequent page edit via audit_on_message_edit
 
        Deduplication: HarvestStats.ingest() tracks seen_pages and ignores
        duplicate page indices.  This handles the on_raw_message_edit /
        on_message_edit double-fire gracefully; raw edits have no embeds and
        never reach this method anyway.
 
        Completion fires only when every page in range(total_pages + 1) has
        been seen — not on "current == final", which would trigger on a single
        missed-then-retransmitted page.
        """
        if not message.embeds:
            return
 
        embed = message.embeds[0]
        page  = self._parse_event_list_embed(embed, source_message_id=message.id)
 
        if page.owner_name is None:
            return
 
        # Locate the matching harvesting session for this channel.
        session: Optional[AuditSession] = None
        for s in list(self._audit_sessions.values()):
            if s.channel_id != message.channel.id:
                continue
            if s.phase != "harvesting":
                continue
            if s.list_message_id is None or s.list_message_id == message.id:
                session = s
                break
 
        if session is None:
            return
 
        session.touch()
        session.list_message_id = message.id
 
        # Ingest into HarvestStats — returns True if page was new.
        is_new = session.harvest_stats.ingest(page)
 
        if is_new:
            for entry in page.entries:
                session.event_cards[str(entry.local_id)] = _card_snapshot(
                    local_id     = entry.local_id,
                    name         = entry.name,
                    rarity       = entry.rarity,
                    status_emoji = entry.status_emoji,
                )
            log.debug(
                "[WGA][session=%s][user=%s] page=%d source=%d parsed=%d failures=%d",
                session.session_id, session.user_id,
                page.page_index, page.source_lines,
                len(page.entries), page.parse_failures,
            )
            for fail in page.failure_details:
                log.warning(
                    "[WGA][session=%s] PARSE_FAILURE page=%d line=%r reason=%s",
                    session.session_id, fail["page"], fail["raw_line"], fail["reason"],
                )
        else:
            log.debug(
                "[WGA][session=%s] duplicate page=%d ignored",
                session.session_id, page.page_index,
            )
 
        hs    = session.harvest_stats
        all_seen = hs.harvest_complete
        channel  = self.bot.get_channel(session.channel_id)
 
        if all_seen:
            await self._persist_event_cards(session.user_id, session.event_cards)
            await self._audit_do_classify(session, channel)
        else:
            # Show live progress.
            if channel and session.guide_message_id:
                ep    = hs.expected_page_count
                sp    = len(hs.seen_pages)
                ep_str = str(ep) if ep is not None else "?"
                ep_max = (hs.expected_final_page or "?")
                try:
                    guide = channel.get_partial_message(session.guide_message_id)
                    await guide.edit(content=(
                        f"-# Harvesting event cards…\n"
                        f"Waifugami page `{page.page_index} of {ep_max}`\n"
                        f"Pages captured: `{sp}/{ep_str}`\n"
                        f"Cards this page: `{len(page.entries)}`\n"
                        f"Parse failures so far: `{hs.total_failures}`\n"
                        f"Cards marked preserve: `{len(session.event_cards)}`\n\n"
                        "Keep clicking ➡."
                    ))
                except discord.HTTPException:
                    pass
 
    async def _audit_event_protection(self, user_id: int) -> Tuple:
        protection = await self._audit_protection(user_id)
        return (
            protection.get("event_cards", {}),
            protection.get("sigma_cards", {}),
            protection.get("protected_series", {}),
        )
 
    # ── Stage 2: classify ─────────────────────────────────────────────────
 
    async def _audit_do_classify(
        self,
        session: AuditSession,
        channel: Optional[discord.abc.Messageable],
    ) -> None:
        session.phase = "classified"
 
        raw_cards = await self._audit_active_cards(session.user_id)
        enriched  = self._enrich_with_catalog(raw_cards)
 
        locked_local_ids: Set[int] = {
            int(c["local_id"])
            for c in enriched
            if c.get("local_id") is not None and _is_locked(c)
        }
 
        event_cards, sigma_cards, series_reviews = (
            await self._audit_event_protection(session.user_id)
        )
 
        classified = classify_cards(
            enriched,
            session.event_cards,
            event_cards,
            sigma_cards,
            series_reviews,
            locked_local_ids,
        )
        session.classified = classified
 
        # Log the completed audit.
        self._audit_log.record_audit_complete(
            session    = session,
            classified = classified,
            guild_id   = session.guild_id,
        )
 
        counts    = {KEEP: 0, REVIEW: 0, SELL: 0, UNKNOWN: 0}
        dp_total  = 0
        shard_total = 0
        for entry in classified:
            counts[entry["disposition"]] += 1
            if entry["disposition"] == SELL:
                dp_total    += entry["dp_yield"]
                shard_total += entry["shard_yield"]
 
        hs = session.harvest_stats
 
        # Block indicator
        if hs.cleanup_blocked:
            block_lines = ["⛔ **CLEANUP BLOCKED** — reasons:"]
            missing = hs.pages_missing
            if missing:
                block_lines.append(
                    f"  · Missing pages: `{', '.join(str(p) for p in missing[:10])}"
                    + (f" +{len(missing)-10} more`" if len(missing) > 10 else "`")
                )
            if hs.total_failures:
                block_lines.append(
                    f"  · `{hs.total_failures}` parse failure(s) — "
                    "re-run harvest after resolving"
                )
            if not hs.harvest_complete and hs.expected_page_count is None:
                block_lines.append("  · Harvest did not complete (unknown total pages)")
            block_status = "\n".join(block_lines)
        else:
            block_status = "🟢 **CLEANUP AVAILABLE** — harvest complete, no parse failures"
 
        harvest_summary = "\n".join(hs.summary_lines())
        persistent_count = len(event_cards)
 
        summary = (
            f"## Audit Report\n"
            f"-# `{len(classified)}` cards evaluated · "
            f"`{len(session.event_cards)}` event cards learned this scan · "
            f"`{persistent_count}` remembered permanently\n\n"
            f"{harvest_summary}\n\n"
            f"{block_status}\n\n"
            f"🟢 **KEEP** — `{counts[KEEP]}` cards protected\n"
            f"🟡 **REVIEW** — `{counts[REVIEW]}` cards need your attention\n"
            f"🔴 **SELL** — `{counts[SELL]}` cards ready for removal\n"
            f"⬛ **UNKNOWN** — `{counts[UNKNOWN]}` cards with missing stats\n\n"
            f"**Estimated yield from SELL candidates**\n"
            f"> `{dp_total:,}` DP"
            + (f" · `{shard_total}` Omega Shards" if shard_total else "")
        )
 
        if counts[REVIEW]:
            summary += (
                f"\n\n-# `{counts[REVIEW]}` REVIEW card(s) need decisions. "
                "Run `..wg audit series` to resolve protected-series cards, "
                "or `..wg audit review` to inspect all REVIEW/UNKNOWN cards."
            )
 
        if not hs.cleanup_blocked and counts[SELL]:
            summary += (
                f"\n\nRun `..wg audit sell` to preview removal candidates, "
                "then `..wg audit confirm` to execute."
            )
 
        if channel:
            sections = [summary]
            try:
                await self._send_channel_v2_components(
                    channel,
                    self._section_components(sections),
                )
            except discord.HTTPException as exc:
                log.error("[WGA] Failed to send audit report: %s", exc)
 
    # ── Stage 2b: sell candidates ─────────────────────────────────────────
 
    def _sell_candidates(self, session: AuditSession) -> List[Dict[str, Any]]:
        candidates = [
            e for e in session.classified
            if e["disposition"] == SELL and e["local_id"] is not None
        ]
        if session.rarity_filter:
            candidates = [
                e for e in candidates
                if e["rarity_symbol"] == session.rarity_filter
            ]
        if session.dupes_only:
            seen: Set[str] = set()
            dupes = []
            for e in candidates:
                ident = _card_identity(e["card"])
                if ident in seen:
                    dupes.append(e)
                else:
                    seen.add(ident)
            candidates = dupes
        return candidates
 
    async def _audit_show_sell(
        self,
        ctx: commands.Context,
        rarity_filter: Optional[str] = None,
        dupes_only:    bool = False,
    ) -> None:
        session = self._audit_session(ctx.author.id)
        if not session or session.phase not in ("classified", "confirming"):
            await ctx.reply(
                "No active audit. Run `..wg audit start` first.",
                mention_author=False,
            )
            return
 
        # Refuse to show sell candidates when cleanup is blocked.
        hs = session.harvest_stats
        if hs.cleanup_blocked:
            lines = ["⛔ **Cleanup blocked** — cannot show removal candidates."]
            missing = hs.pages_missing
            if missing:
                lines.append(
                    f"Missing pages: `{', '.join(str(p) for p in missing[:10])}`"
                )
            if hs.total_failures:
                lines.append(
                    f"`{hs.total_failures}` parse failure(s) exist. "
                    "Re-run `..wg audit start` and harvest again."
                )
            await ctx.reply("\n".join(lines), mention_author=False)
            return
 
        session.rarity_filter = rarity_filter
        session.dupes_only    = dupes_only
        candidates = self._sell_candidates(session)
 
        if not candidates:
            await ctx.reply(
                "No SELL candidates match the current filter.",
                mention_author=False,
            )
            return
 
        session.sell_ids = [int(e["local_id"]) for e in candidates if e["local_id"] is not None]
 
        from itertools import groupby
 
        sections     = []
        header_parts = ["## SELL Candidates"]
        if rarity_filter:
            header_parts.append(f" · filter: `[{rarity_filter}]`")
        if dupes_only:
            header_parts.append(" · duplicates only")
        header_parts.append(f"\n-# {len(candidates)} cards selected")
 
        dp_total    = sum(e["dp_yield"]    for e in candidates)
        shard_total = sum(e["shard_yield"] for e in candidates)
        header_parts.append(
            f"  ·  estimated `{dp_total:,}` DP"
            + (f"  ·  `{shard_total}` Omega Shards" if shard_total else "")
        )
        sections.append("".join(header_parts))
 
        DISPLAY_LIMIT = 60
        shown = candidates[:DISPLAY_LIMIT]
        for rarity_sym, group in groupby(shown, key=lambda e: e["rarity_symbol"]):
            group_list  = list(group)
            rarity_lines = [f"### [{rarity_sym.upper()}] — {len(group_list)} cards"]
            for entry in group_list:
                reasons_str = ", ".join(entry.get("eligibility_reasons") or entry.get("reasons") or [])
                skill_str   = f"{entry['skill']:.2f}" if entry["skill"] is not None else "?"
                luck_str    = str(entry["luck"]) if entry["luck"] is not None else "?"
                rarity_lines.append(
                    f"`{entry['local_id']}` **{entry['name']}** "
                    f"· Skill {skill_str} · Luck {luck_str}"
                    + (f"\n-# {reasons_str}" if reasons_str else "")
                )
            sections.append("\n".join(rarity_lines))
 
        if len(candidates) > DISPLAY_LIMIT:
            sections.append(
                f"-# … and {len(candidates) - DISPLAY_LIMIT} more (not shown). "
                "All are included in removal if you confirm."
            )
        sections.append(
            f"Run `..wg audit confirm` to remove these "
            f"{len(candidates)} cards and earn `{dp_total:,}` DP."
        )
 
        await self._send_channel_v2_components(
            ctx.channel,
            self._section_components(sections),
        )
 
    async def _audit_show_review(self, ctx: commands.Context) -> None:
        session = self._audit_session(ctx.author.id)
        if not session or not session.classified:
            await ctx.reply("No active audit.", mention_author=False)
            return
 
        entries = [e for e in session.classified if e["disposition"] in (REVIEW, UNKNOWN)]
        if not entries:
            await ctx.reply(
                "No REVIEW or UNKNOWN cards in this audit.", mention_author=False
            )
            return
 
        CARDS_PER_SECTION = 20
 
        def _fmt(entry: Dict[str, Any]) -> str:
            skill_str = f"{entry['skill']:.2f}" if entry["skill"] is not None else "?"
            luck_str  = str(entry["luck"]) if entry["luck"] is not None else "?"
            return (
                f"`{entry['local_id']}` **{entry['name']}** "
                f"`[{entry['rarity_symbol'].upper()}]`  "
                f"Skill {skill_str}  Luck {luck_str}  "
                f"-# {entry['disposition']} · {', '.join(entry['reasons'])}"
            )
 
        header   = (
            f"## Review & Unknown Cards\n"
            f"-# {len(entries)} cards require manual inspection"
        )
        sections = [header]
        chunk: List[str] = []
        for entry in entries:
            chunk.append(_fmt(entry))
            if len(chunk) == CARDS_PER_SECTION:
                sections.append("\n".join(chunk))
                chunk = []
        if chunk:
            sections.append("\n".join(chunk))
 
        await self._send_channel_v2_components(
            ctx.channel,
            self._section_components(sections),
        )
 
    # ── Stage 3: confirm & execute ────────────────────────────────────────
 
    async def _audit_confirm(
        self,
        ctx:          commands.Context,
        override_ids: Optional[List[int]] = None,
    ) -> None:
        """Validate, revalidate, and execute .rm removal batches."""
        session = self._audit_session(ctx.author.id)
        if not session or not session.classified:
            await ctx.reply(
                "No active audit to confirm. Run `..wg audit start` first.",
                mention_author=False,
            )
            return
 
        ids_to_remove = override_ids if override_ids is not None else session.sell_ids
 
        if not ids_to_remove:
            await ctx.reply(
                "No cards selected for removal. "
                "Run `..wg audit sell` to select candidates first.",
                mention_author=False,
            )
            return
 
        # Block on incomplete harvest or parse failures.
        hs = session.harvest_stats
        if hs.cleanup_blocked:
            parts = ["⛔ **Cannot execute removal — cleanup is blocked.**"]
            missing = hs.pages_missing
            if missing:
                parts.append(
                    f"Missing harvest pages: `{', '.join(str(p) for p in missing[:10])}`"
                )
            if hs.total_failures:
                parts.append(
                    f"`{hs.total_failures}` parse failure(s). Re-run the harvest."
                )
            await ctx.reply("\n".join(parts), mention_author=False)
            return
 
        # Session-level classification check (stale protection catch).
        classified_by_lid: Dict[int, Dict[str, Any]] = {
            int(e["local_id"]): e
            for e in session.classified
            if e["local_id"] is not None
        }
        protected_ids = [
            lid for lid in ids_to_remove
            if classified_by_lid.get(lid, {}).get("disposition") == KEEP
        ]
        if protected_ids:
            shown = ", ".join(str(x) for x in protected_ids[:10])
            await ctx.reply(
                f"⚠️ {len(protected_ids)} selected ID(s) are marked **KEEP** "
                f"(`{shown}{'…' if len(protected_ids) > 10 else ''}`). "
                "Aborting — please re-run `..wg audit sell` and try again.",
                mention_author=False,
            )
            return
 
        # ── Live revalidation ─────────────────────────────────────────────
        # Reload the live collection and verify that each target local_id
        # still corresponds to the card that was audited.
        await ctx.reply(
            f"Revalidating `{len(ids_to_remove)}` target(s) against your live collection…",
            mention_author=False,
        )
        raw_live  = await self._audit_active_cards(session.user_id)
        live_by_lid: Dict[int, Dict[str, Any]] = {
            int(c["local_id"]): c
            for c in raw_live
            if c.get("local_id") is not None
        }
 
        revalidation_failures: List[Tuple[int, str, str]] = []
        clean_ids: List[int] = []
 
        for lid in ids_to_remove:
            audited = classified_by_lid.get(lid)
            live    = live_by_lid.get(lid)
 
            if live is None:
                # Card no longer exists at this local_id — treat as failure.
                revalidation_failures.append((
                    lid,
                    f"no live card at local_id={lid}",
                    audited["name"] if audited else "Unknown",
                ))
                self._audit_log.record_revalidation_failure(
                    session       = session,
                    local_id      = lid,
                    audited_name  = audited["name"] if audited else None,
                    audited_rarity = audited["rarity_symbol"] if audited else None,
                    live_name     = None,
                    live_rarity   = None,
                    guild_id      = session.guild_id,
                )
                continue
 
            # Compare name + rarity using the same normalisation used by identity.
            audited_ident = _card_identity(audited["card"]) if audited else None
            live_ident    = _card_identity(live)
 
            if audited_ident is None or audited_ident != live_ident:
                revalidation_failures.append((
                    lid,
                    f"identity mismatch: audited={audited_ident!r} live={live_ident!r}",
                    audited["name"] if audited else "Unknown",
                ))
                self._audit_log.record_revalidation_failure(
                    session        = session,
                    local_id       = lid,
                    audited_name   = audited["name"] if audited else None,
                    audited_rarity = audited["rarity_symbol"] if audited else None,
                    live_name      = live.get("name"),
                    live_rarity    = _rarity_symbol(live),
                    guild_id       = session.guild_id,
                )
                continue
 
            clean_ids.append(lid)
 
        if revalidation_failures:
            lines = [
                f"⚠️ `{len(revalidation_failures)}` revalidation failure(s). "
                "These cards will NOT be removed:"
            ]
            for lid, reason, name in revalidation_failures[:10]:
                lines.append(f"  · `{lid}` {name} — {reason}")
            if len(revalidation_failures) > 10:
                lines.append(f"  … +{len(revalidation_failures) - 10} more")
 
            if not clean_ids:
                lines.append(
                    "\n⛔ No safe targets remain. Aborting. "
                    "Re-run `..wg audit start` to refresh."
                )
                await ctx.reply("\n".join(lines), mention_author=False)
                return
 
            lines.append(
                f"\nProceeding with the `{len(clean_ids)}` card(s) that did validate."
            )
            await ctx.reply("\n".join(lines), mention_author=False)
 
        if not clean_ids:
            await ctx.reply(
                "No valid targets remain after revalidation. "
                "Re-run `..wg audit start` to refresh.",
                mention_author=False,
            )
            return
 
        # Series invariant check on the clean subset.
        remaining_classified = [
            e for e in session.classified
            if e["local_id"] not in clean_ids or e["disposition"] != SELL
        ]
        violations = assert_protected_series_invariant(remaining_classified)
        if violations:
            vio_str = "\n".join(f"  · {v}" for v in violations)
            await ctx.reply(
                f"⛔ Protected-series invariant violated. Aborting.\n{vio_str}",
                mention_author=False,
            )
            return
 
        # Build target info for the audit log.
        targets = [classified_by_lid[lid] for lid in clean_ids if lid in classified_by_lid]
        self._audit_log.record_execution_intent(
            session  = session,
            targets  = targets,
            guild_id = session.guild_id,
        )
 
        all_ids_sorted_desc = sorted(set(clean_ids), reverse=True)
        batches = list(_chunk(all_ids_sorted_desc, RM_BATCH_SIZE))
 
        session.phase       = "executing"
        session.batch_queue = batches
        session.current_batch = None
 
        total    = len(clean_ids)
        n_batches = len(batches)
        await ctx.reply(
            f"**Removing {total} card(s) across {n_batches} batch(es) of ≤ {RM_BATCH_SIZE}.**\n"
            "-# Issuing `.rm` commands — do not touch your list until complete.",
            mention_author=False,
        )
        await self._audit_execute_next_batch(session, ctx.channel, classified_by_lid)
 
    async def _audit_execute_next_batch(
        self,
        session:        AuditSession,
        channel:        discord.abc.Messageable,
        classified_by_lid: Dict[int, Dict[str, Any]],
    ) -> None:
        if not session.batch_queue:
            await channel.send(
                "✅ **Audit removal complete.** All selected cards have been removed."
            )
            self._audit_cancel(session.user_id)
            return
 
        batch = session.batch_queue.pop(0)
        session.current_batch = batch
        session.touch()
 
        for local_id in batch:
            entry = classified_by_lid.get(local_id)
            try:
                await channel.send(f".rm {local_id}")
                self._audit_log.record_removal_result(
                    session     = session,
                    local_id    = local_id,
                    status      = "removal_requested",
                    name        = entry["name"] if entry else None,
                    rarity      = entry["rarity_symbol"] if entry else None,
                    global_id   = entry["global_id"] if entry else None,
                    dp_received = None,   # actual reward comes from Waifugami response
                    guild_id    = session.guild_id,
                )
                await asyncio.sleep(1.2)
            except discord.HTTPException as exc:
                self._audit_log.record_removal_result(
                    session        = session,
                    local_id       = local_id,
                    status         = "removal_failed",
                    name           = entry["name"] if entry else None,
                    rarity         = entry["rarity_symbol"] if entry else None,
                    global_id      = entry["global_id"] if entry else None,
                    dp_received    = None,
                    guild_id       = session.guild_id,
                    failure_reason = str(exc),
                )
                await channel.send(
                    f"⚠️ Failed to send `.rm {local_id}`: {exc}. "
                    "Stopping. Re-run `..wg audit confirm` to retry remaining."
                )
                session.phase = "classified"
                return
 
        remaining = len(session.batch_queue)
        if remaining:
            await channel.send(
                f"-# Batch done. {remaining} batch(es) remaining. Continuing in 3 s…"
            )
            await asyncio.sleep(3)
            await self._audit_execute_next_batch(session, channel, classified_by_lid)
        else:
            await channel.send(
                "✅ **Audit removal complete.** All selected cards have been removed."
            )
            self._audit_cancel(session.user_id)
 
    # ── Listener hooks ─────────────────────────────────────────────────────
    #
    # CRITICAL: Waifugami sends ONE message for page 0, then repeatedly
    # EDITS that same message for pages 1..N.  Therefore:
    #   - audit_on_new_message handles page 0
    #   - audit_on_message_edit handles pages 1+
    #
    # on_raw_message_edit always fires alongside on_message_edit but its
    # payload contains no embeds, so `if not after.embeds` returns False and
    # it exits immediately — no duplicate processing.
 
    async def audit_on_new_message(self, message: discord.Message) -> bool:
        """Capture the FIRST page of `.l -event all` (arrives as a new message).
 
        Returns True if the message was consumed by an active audit harvest.
        """
        if message.author.id != WAIFUGAMI_ID:
            return False
        if not message.embeds:
            return False
 
        page = self._parse_event_list_embed(message.embeds[0], message.id)
        if page.owner_name is None:
            return False
 
        for session in list(self._audit_sessions.values()):
            if (
                session.channel_id == message.channel.id
                and session.phase == "harvesting"
                and session.list_message_id is None
            ):
                await self._audit_collect_page(message)
                return True
 
        return False
 
    async def audit_on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> bool:
        """Capture pages 1..N of `.l -event all` (arrive as message edits).
 
        Returns True if the edit was consumed by an active audit harvest.
        """
        if after.author.id != WAIFUGAMI_ID:
            return False
        if not after.embeds:
            return False
 
        page = self._parse_event_list_embed(after.embeds[0], after.id)
        if page.owner_name is None:
            return False
 
        for session in list(self._audit_sessions.values()):
            if (
                session.channel_id == after.channel.id
                and session.phase == "harvesting"
                and (session.list_message_id is None or session.list_message_id == after.id)
            ):
                await self._audit_collect_page(after)
                return True
 
        return False
 
    # ── Commands ──────────────────────────────────────────────────────────
 
    @commands.group(name="wgaudit", aliases=["wga"])
    async def wgaudit(self, ctx: commands.Context) -> None:
        """List Audit & Cleanup Engine — identify and remove low-value cards.
 
        Workflow:
          1. ``..wg audit start``       — begin harvesting event cards
          2. Run `.l -event all` in the same channel and flip through ALL pages
          3. ``..wg audit sell``        — browse SELL candidates
          4. ``..wg audit confirm``     — execute `.rm` removals in safe batches
 
        Optional filters for step 3:
          ``..wg audit sell <rarity>``  — e.g. ``sell α``
          ``..wg audit sell dupes``     — duplicates only
          ``..wg audit review``         — inspect REVIEW / UNKNOWN cards
          ``..wg audit series``         — resolve protected-series decisions
 
        To skip event harvesting:
          ``..wg audit classify``
        """
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)
 
    @wgaudit.command(name="start")
    async def wgaudit_start(self, ctx: commands.Context) -> None:
        """Start an audit session and wait for you to run `.l -event all`."""
        if not await self.config.user(ctx.author).enabled():
            await ctx.reply(
                f"Enable card tracking first with `{ctx.clean_prefix}wgtrack enable`.",
                mention_author=False,
            )
            return
        await self._audit_start(ctx)
 
    @wgaudit.command(name="classify")
    async def wgaudit_classify(self, ctx: commands.Context) -> None:
        """Classify your collection immediately, without harvesting event cards."""
        if not await self.config.user(ctx.author).enabled():
            await ctx.reply(
                f"Enable card tracking first with `{ctx.clean_prefix}wgtrack enable`.",
                mention_author=False,
            )
            return
 
        user_id = ctx.author.id
        session = self._audit_sessions.get(user_id)
        if session is None:
            session = AuditSession(
                user_id    = user_id,
                channel_id = ctx.channel.id,
                guild_id   = ctx.guild.id,
            )
            self._audit_sessions[user_id] = session
 
        msg = await ctx.reply("Classifying your collection…", mention_author=False)
        session.guide_message_id = msg.id
        await self._audit_do_classify(session, ctx.channel)
 
    @wgaudit.command(name="sell")
    async def wgaudit_sell(self, ctx: commands.Context, *, args: str = "") -> None:
        """Show SELL candidates, optionally filtered.
 
        Examples::
 
            ..wg audit sell          — all SELL candidates
            ..wg audit sell α        — only Alpha cards
            ..wg audit sell dupes    — only duplicate cards
            ..wg audit sell α dupes  — Alpha duplicates
        """
        args_lower = args.lower().strip()
        dupes_only = "dupes" in args_lower or "duplicates" in args_lower
        rarity_map = {
            "a": "α", "alpha": "α",  "α": "α",
            "b": "β", "beta": "β",   "β": "β",
            "g": "γ", "gamma": "γ",  "γ": "γ",
            "d": "δ", "delta": "δ",  "δ": "δ",
            "s": "σ", "sigma": "σ",  "σ": "σ",
            "e": "ε", "epsilon": "ε","ε": "ε",
            "z": "ζ", "zeta": "ζ",   "ζ": "ζ",
            "o": "ω", "omega": "ω",  "ω": "ω",
        }
        rarity_filter: Optional[str] = None
        for token in args_lower.split():
            if token in rarity_map:
                rarity_filter = rarity_map[token]
                break
        await self._audit_show_sell(ctx, rarity_filter=rarity_filter, dupes_only=dupes_only)
 
    @wgaudit.command(name="review")
    async def wgaudit_review(self, ctx: commands.Context) -> None:
        """Show cards in the REVIEW or UNKNOWN category."""
        await self._audit_show_review(ctx)
 
    @wgaudit.command(name="series")
    async def wgaudit_series_review(self, ctx: commands.Context) -> None:
        """Walk through protected-series cards that need keep/sell decisions."""
        await self._audit_show_series_review(ctx)
 
    @wgaudit.command(name="confirm")
    async def wgaudit_confirm(
        self, ctx: commands.Context, *, ids_str: str = ""
    ) -> None:
        """Execute removal of the current SELL selection (or explicit IDs).
 
        Without arguments, removes all cards selected by ``..wg audit sell``.
        With arguments, removes only the specified local IDs::
 
            ..wg audit confirm 142 201 309
        """
        override: Optional[List[int]] = None
        if ids_str.strip():
            try:
                override = [int(x) for x in ids_str.split()]
            except ValueError:
                await ctx.reply(
                    "Could not parse the IDs. Use space-separated integers.",
                    mention_author=False,
                )
                return
        await self._audit_confirm(ctx, override_ids=override)
 
    @wgaudit.command(name="cancel")
    async def wgaudit_cancel(self, ctx: commands.Context) -> None:
        """Cancel the current audit session without removing anything."""
        if self._audit_session(ctx.author.id):
            self._audit_cancel(ctx.author.id)
            await ctx.reply("Audit session cancelled.", mention_author=False)
        else:
            await ctx.reply("No active audit session.", mention_author=False)
 
    @wgaudit.command(name="status")
    async def wgaudit_status(self, ctx: commands.Context) -> None:
        """Show the current state of your audit session."""
        session = self._audit_session(ctx.author.id)
        if not session:
            await ctx.reply("No active audit session.", mention_author=False)
            return
 
        counts = {KEEP: 0, REVIEW: 0, SELL: 0, UNKNOWN: 0}
        for entry in session.classified:
            counts[entry["disposition"]] += 1
 
        hs = session.harvest_stats
        lines = [
            f"**Phase:** `{session.phase}`",
            f"**Event cards learned this scan:** `{len(session.event_cards)}`",
            f"**Harvest:** `{len(hs.seen_pages)}/{hs.expected_page_count or '?'}` pages",
            f"**Parse failures:** `{hs.total_failures}`",
            f"**Cleanup blocked:** `{hs.cleanup_blocked}`",
            f"**Classified:** `{len(session.classified)}`",
            f"  · KEEP `{counts[KEEP]}`  REVIEW `{counts[REVIEW]}`"
            f"  SELL `{counts[SELL]}`  UNKNOWN `{counts[UNKNOWN]}`",
            f"**Current SELL selection:** `{len(session.sell_ids)}` cards",
        ]
        if session.rarity_filter:
            lines.append(f"**Rarity filter:** `[{session.rarity_filter}]`")
        if session.dupes_only:
            lines.append("**Duplicates only:** yes")
 
        await ctx.reply("\n".join(lines), mention_author=False)
 
    # ── wg subcommand aliases (registered by host cog) ───────────────────
 
    async def _wg_audit_dispatch(self, ctx: commands.Context) -> None:
        await ctx.send_help(self.wgaudit)
