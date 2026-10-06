# audit.py
 
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
    AUDIT_SESSION_TTL,
    LIST_TITLE_RE, LIST_ENTRY_RE, FINAL_PAGE_FIELD_NAME_RE,
    WAIFUGAMI_ID, V2_FLAG,
    classify_cards, _is_locked, _skill, _luck,
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
        "phase",            # "harvesting"|"classified"
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
        "harvested",            # bool — True only when a real harvest completed
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
        self.harvested:       bool = False
 
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
 
    def record_removal_result(
        self,
        *,
        session: "AuditSession",
        local_id: int,
        status: str,
        name: Optional[str],
        rarity: Optional[str],
        global_id: Optional[int],
        dp_received: Optional[int],
        guild_id: int,
        failure_reason: Optional[str] = None,
    ) -> None:
        """Record the result or queue state for one local card ID."""
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
            "dp_received":    dp_received,
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
        # Reject if another user's harvest is already active in this channel.
        for _existing in self._audit_sessions.values():
            if (
                _existing.channel_id == ctx.channel.id
                and _existing.phase == "harvesting"
                and _existing.user_id != user_id
            ):
                await ctx.reply(
                    "Another audit is already harvesting in this channel. "
                    "It must complete or be cancelled before starting a new one.",
                    mention_author=False,
                )
                return
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
 
        # Verify the embed's owner_name matches the audit user's Discord
        # account username (the unique handle Waifugami uses in the title).
        # We use discord.utils.find against member.name — the account username
        # field — not display_name or nick, which are non-unique.
        # If the guild is uncached or the member left, fall through and accept
        # by channel as before (fail-open is safer than dropping valid pages).
        guild = self.bot.get_guild(session.guild_id)
        if guild is not None:
            member = discord.utils.find(
                lambda m: m.name.casefold() == page.owner_name.casefold(),
                guild.members,
            )
            if member is not None and member.id != session.user_id:
                # List belongs to a different user — reject this page.
                log.debug(
                    "[WGA][session=%s] rejected page: owner_name=%r belongs to"
                    " user %d, not audit user %d",
                    session.session_id, page.owner_name,
                    member.id, session.user_id,
                )
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
            session.harvested = True
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
            # Protected-series review was removed (duplicates are handled by
            # the `list dupes` workflow). Always classify with no stored
            # series decisions: unreviewed protected-series cards stay REVIEW
            # and are never offered for sale.
            {},
        )
 
    # ── Stage 2: classify ─────────────────────────────────────────────────
 
    async def _audit_do_classify(
        self,
        session: AuditSession,
        channel: Optional[discord.abc.Messageable],
        send_report: bool = True,
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
        # Only block on harvest stats when a harvest was actually attempted.
        # ..wg audit classify skips harvesting and uses the persistent store.
        if session.harvested and hs.cleanup_blocked:
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
        elif session.harvested:
            block_status = "🟢 **CLEANUP AVAILABLE** — harvest complete, no parse failures"
        else:
            block_status = "🟢 **CLEANUP AVAILABLE** — using remembered event cards"
 
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
                "Run `..wg audit review` to inspect all REVIEW/UNKNOWN cards."
            )
 
        if not (session.harvested and hs.cleanup_blocked) and counts[SELL]:
            summary += (
                f"\n\nRun `..wg audit sell` to preview removal candidates, "
                "then `..wg audit confirm` to validate and queue them."
            )
 
        if channel and send_report:
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
        if session.harvested and hs.cleanup_blocked:
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
 
        # Discord's Components V2 displayable-text limit applies to the
        # whole message, so output is split across several messages.
        MAX_TEXT = 3000

        dp_total    = sum(e["dp_yield"]    for e in candidates)
        shard_total = sum(e["shard_yield"] for e in candidates)

        chunks: List[List[str]] = []
        current: List[str] = []
        current_len = 0

        for entry in candidates:
            reasons_str = ", ".join(
                entry.get("eligibility_reasons")
                or entry.get("reasons")
                or []
            )
            skill_str = (
                f"{entry['skill']:.2f}" if entry["skill"] is not None else "?"
            )
            luck_str = (
                str(entry["luck"]) if entry["luck"] is not None else "?"
            )

            line = (
                f"`{entry['local_id']}` **{entry['name']}** "
                f"· `[{entry['rarity_symbol'].upper()}]` "
                f"Skill {skill_str} · Luck {luck_str}"
            )
            if reasons_str:
                line += f"\n-# {reasons_str}"

            needed = len(line) + (1 if current else 0)

            if current and current_len + needed > MAX_TEXT:
                chunks.append(current)
                current = []
                current_len = 0

            current.append(line)
            current_len += needed

        if current:
            chunks.append(current)

        total_chunks = len(chunks)

        for index, chunk in enumerate(chunks, start=1):
            page_header = "## SELL Candidates"

            if rarity_filter:
                page_header += f" · filter: `[{rarity_filter}]`"

            if dupes_only:
                page_header += " · duplicates only"

            page_header += f"\n-# {len(candidates)} cards selected"
            page_header += (
                f"\n-# Estimated `{dp_total:,}` DP"
                + (f" · `{shard_total}` Omega Shards" if shard_total else "")
            )

            if total_chunks > 1:
                page_header += f"\n-# Page `{index}/{total_chunks}`"

            await self._send_channel_v2_components(
                ctx.channel,
                self._section_components([page_header, "\n".join(chunk)]),
            )

        await ctx.send(
            f"Run `..wg audit confirm` to validate and queue these "
            f"{len(candidates)} cards for the separate removal workflow."
        )

    async def _audit_show_review(self, ctx: commands.Context) -> None:
        session = self._audit_session(ctx.author.id)
        if not session or not session.classified:
            await ctx.reply("No active audit.", mention_author=False)
            return

        entries = [
            e for e in session.classified
            if e["disposition"] in (REVIEW, UNKNOWN)
        ]
        if not entries:
            await ctx.reply(
                "No REVIEW or UNKNOWN cards in this audit.",
                mention_author=False,
            )
            return

        def _fmt(entry: Dict[str, Any]) -> str:
            skill_str = (
                f"{entry['skill']:.2f}" if entry["skill"] is not None else "?"
            )
            luck_str = str(entry["luck"]) if entry["luck"] is not None else "?"
            reasons = ", ".join(entry.get("reasons") or [])

            return (
                f"`{entry['local_id']}` **{entry['name']}** "
                f"`[{entry['rarity_symbol'].upper()}]` "
                f"Skill {skill_str}  "
                f"Luck {luck_str}  "
                f"-# {entry['disposition']}"
                + (f" · {reasons}" if reasons else "")
            )

        # Discord's Components V2 displayable-text limit applies to the
        # entire message, not each type-10 component. Stay well below 4000
        # so headers and formatting cannot push us over.
        MAX_TEXT = 3000

        chunks: List[List[str]] = []
        current: List[str] = []
        current_len = 0

        for entry in entries:
            line = _fmt(entry)
            needed = len(line) + (1 if current else 0)

            if current and current_len + needed > MAX_TEXT:
                chunks.append(current)
                current = []
                current_len = 0

            current.append(line)
            current_len += needed

        if current:
            chunks.append(current)

        total_chunks = len(chunks)

        for index, chunk in enumerate(chunks, start=1):
            header = (
                "## Review & Unknown Cards\n"
                f"-# {len(entries)} cards require manual inspection"
            )
            if total_chunks > 1:
                header += f"\n-# Page `{index}/{total_chunks}`"

            await self._send_channel_v2_components(
                ctx.channel,
                self._section_components([header, "\n".join(chunk)]),
            )

    # ── Stage 3: confirm & queue ─────────────────────────────────────────
 
    async def _audit_confirm(
        self,
        ctx:          commands.Context,
        override_ids: Optional[List[int]] = None,
    ) -> None:
        """Validate and record safe removal IDs for the separate removal workflow."""
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
        # Only applies when a harvest was actually attempted this session.
        hs = session.harvest_stats
        if session.harvested and hs.cleanup_blocked:
            parts = ["⛔ **Cannot record removal IDs because cleanup is blocked.**"]
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
 
            # Compare identity. When both sides carry a global_id, use it as
            # the primary key because it is stable across local-list
            # reindexing. Fall back to the card identity when either side
            # lacks a global_id.

            audited_ident = (
                _card_identity(audited["card"])
                if audited
                else None
            )
            live_ident = _card_identity(live)

            audited_gid: Optional[int] = None
            live_gid: Optional[int] = None

            if audited:
                try:
                    _raw_gid = audited["card"].get("global_id")
                    if _raw_gid is not None:
                        audited_gid = int(_raw_gid)
                except (TypeError, ValueError):
                    pass

            try:
                _raw_gid = live.get("global_id")
                if _raw_gid is not None:
                    live_gid = int(_raw_gid)
            except (TypeError, ValueError):
                pass

            if audited_gid is not None and live_gid is not None:
                match = audited_gid == live_gid
            else:
                match = (
                    audited_ident is not None
                    and audited_ident == live_ident
                )

            if not match:
                revalidation_failures.append((
                    lid,
                    (
                        f"identity mismatch: "
                        f"audited={audited_ident!r} "
                        f"live={live_ident!r}"
                    ),
                    audited["name"] if audited else "Unknown",
                ))
                self._audit_log.record_revalidation_failure(
                    session=session,
                    local_id=lid,
                    audited_name=audited["name"] if audited else None,
                    audited_rarity=(
                        audited["rarity_symbol"]
                        if audited
                        else None
                    ),
                    live_name=live.get("name"),
                    live_rarity=_rarity_symbol(live),
                    guild_id=session.guild_id,
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
 
        # Record the validated local IDs for the separate removal workflow.
        #
        # The audit engine deliberately does NOT send `.rm`.
        # The separate removal workflow will consume these IDs in batches.
        clean_sell_ids = sorted(set(clean_ids))

        session.sell_ids = clean_sell_ids

        await self._audit_record_selected_ids(
            session,
            ctx.channel,
            clean_sell_ids,
            classified_by_lid,
        )
 
    async def _audit_record_selected_ids(
        self,
        session: AuditSession,
        channel: discord.abc.Messageable,
        local_ids: List[int],
        classified_by_lid: Dict[int, Dict[str, Any]],
    ) -> None:

        clean_ids = sorted(
            {
                int(local_id)
                for local_id in local_ids
                if int(local_id) in classified_by_lid
            }
        )

        if not clean_ids:
            await channel.send(
                "No validated local IDs remain to record."
            )
            return

        protection = await self._audit_protection(session.user_id)

        pending_tag_ids = protection.setdefault("pending_tag_ids", [])

        existing = {
            int(value)
            for value in pending_tag_ids
            if str(value).isdigit()
        }

        existing.update(clean_ids)

        protection["pending_tag_ids"] = sorted(existing)

        await self._save_audit_protection(
            session.user_id,
            protection,
        )

        for local_id in clean_ids:
            entry = classified_by_lid.get(local_id)

            self._audit_log.record_removal_result(
                session=session,
                local_id=local_id,
                status="queued_for_removal",
                name=entry["name"] if entry else None,
                rarity=entry["rarity_symbol"] if entry else None,
                global_id=entry["global_id"] if entry else None,
                dp_received=None,
                guild_id=session.guild_id,
            )

        session.batch_queue.clear()
        session.current_batch = None
        session.phase = "classified"
        session.touch()

        await channel.send(
            f"✅ **Audit selection recorded.**\n"
            f"{len(clean_ids)} local ID(s) queued for the separate "
            f"removal workflow.\n\n"
            f"-# No `.rm` commands were sent."
        )
 
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
        """Capture Waifugami list page 0 (arrives as a new message).

        Returns True if the message was consumed by an active harvest.
        """
        if message.author.id != WAIFUGAMI_ID:
            return False
        if not message.embeds:
            return False

        page = self._parse_event_list_embed(message.embeds[0], message.id)
        if page.owner_name is None:
            return False

        # Normal `.l -event all` harvest.
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
        """Capture pages 1..N of a Waifugami list (arrive as message edits).

        Returns True if the edit was consumed by an active audit.
        """
        if after.author.id != WAIFUGAMI_ID:
            return False
        if not after.embeds:
            return False

        page = self._parse_event_list_embed(after.embeds[0], after.id)
        if page.owner_name is None:
            return False

        # Normal event harvest.
        for session in list(self._audit_sessions.values()):
            if (
                session.channel_id == after.channel.id
                and session.phase == "harvesting"
                and (
                    session.list_message_id is None
                    or session.list_message_id == after.id
                )
            ):
                await self._audit_collect_page(after)
                return True

        return False

    # ── Commands ──────────────────────────────────────────────────────────
 
    @commands.group(name="wgaudit", aliases=["wga"])
    async def wgaudit(self, ctx: commands.Context) -> None:
        """List Audit & Cleanup Engine — identify and remove low-value cards.
 
        Workflow:
          1. ``..wg audit start``       - begin harvesting event cards
          2. Run `.l -event all` in the same channel and flip through ALL pages
          3. ``..wg audit sell``        - browse SELL candidates
          4. ``..wg audit confirm``     - validate and queue approved local IDs

        Optional filters for step 3:
          ``..wg audit sell <rarity>``  - e.g. ``sell α``
          ``..wg audit sell dupes``     - duplicates only
          ``..wg audit review``         - inspect REVIEW / UNKNOWN cards

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
 
    @wgaudit.command(name="confirm")
    async def wgaudit_confirm(
        self, ctx: commands.Context, *, ids_str: str = ""
    ) -> None:
        """Validate the current SELL selection and queue its local IDs.

        Without arguments, validates all cards selected by
        ``..wg audit sell``.

        With arguments, validates only the specified local IDs::

            ..wg audit confirm 142 201 309

        No `.rm` commands are sent by the audit engine.
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
