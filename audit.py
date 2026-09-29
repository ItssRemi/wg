"""audit.py — Waifugami List Audit & Cleanup Engine (bot mixin layer).

Pure classification logic lives in audit_engine.py (no bot imports there).
This file adds the AuditSession, AuditMixin, and all Red commands/listeners.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import discord
from discord import app_commands
from redbot.core import commands

from .audit_engine import (
    KEEP, REVIEW, SELL, UNKNOWN,
    REASON_LOCKED, REASON_EVENT, REASON_SIGMA, REASON_SERIES_LAST, REASON_SERIES_PROTECTED,
    REASON_HIGH_STATS, REASON_OMEGA, REASON_HIGH_VALUE,
    REASON_NEAR_THRESHOLD, REASON_NO_STATS,
    RARITY_DP, OMEGA_SHARDS_PER_CARD, OMEGA_SYMBOLS,
    HIGH_VALUE_REVIEW_RARITIES, PROTECTED_SERIES_IDS,
    SKILL_PROTECT_THRESHOLD, LUCK_PROTECT_THRESHOLD,
    RM_BATCH_SIZE, AUDIT_SESSION_TTL,
    LIST_TITLE_RE, LIST_ENTRY_RE, FINAL_PAGE_FIELD_NAME_RE,
    WAIFUGAMI_ID, V2_FLAG,
    classify_cards, _chunk, _is_locked, _skill, _luck,
    _rarity_symbol, _dp_for_card, _shards_for_card,
)

def _normalise_card_name(name: str) -> str:
    return " ".join(str(name).strip().casefold().split())


def _card_identity(name: str, rarity: str) -> str:
    return f"{_normalise_card_name(name)}|{str(rarity).strip().casefold()}"


def _card_snapshot(
    *,
    local_id: Optional[int],
    name: str,
    rarity: str,
    global_id: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "name": str(name).strip(),
        "rarity": str(rarity).strip().lower(),
        "last_seen_id": int(local_id) if local_id is not None else None,
        "global_id": int(global_id) if global_id is not None else None,
        "last_seen_at": time.time(),
    }

class AuditSession:
    """All mutable state for one user's in-progress audit."""

    __slots__ = (
        "user_id", "channel_id", "guild_id",
        "phase",               # "harvesting" | "classified" | "confirming" | "executing"
        "list_message_id",     # Waifugami's .l -event all message id
        "event_cards",         # Set[int] — accumulated across all pages
        "seen_pages",          # Set[int]
        "total_pages",         # int | None
        "classified",          # List[dict] — output of classify_cards()
        "sell_ids",            # List[int] — local_ids selected for removal
        "rarity_filter",       # Optional[str]
        "dupes_only",          # bool
        "guide_message_id",    # message we edit with progress
        "created",             # float (monotonic)
        "last_active",         # float (monotonic)
        "batch_queue",         # List[List[int]] — remaining batches to execute
        "current_batch",       # Optional[List[int]]
    )

    def __init__(self, user_id: int, channel_id: int, guild_id: int):
        self.user_id = user_id
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.phase = "harvesting"
        self.list_message_id: Optional[int] = None
        self.event_cards: Dict[str, Dict[str, Any]] = {}
        self.seen_pages: Set[int] = set()
        self.total_pages: Optional[int] = None
        self.classified: List[Dict[str, Any]] = []
        self.sell_ids: List[int] = []
        self.rarity_filter: Optional[str] = None
        self.dupes_only: bool = False
        self.guide_message_id: Optional[int] = None
        self.created = time.monotonic()
        self.last_active = time.monotonic()
        self.batch_queue: List[List[int]] = []
        self.current_batch: Optional[List[int]] = None

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def expired(self) -> bool:
        return (time.monotonic() - self.last_active) > AUDIT_SESSION_TTL


# ──────────────────────────────────────────────────────────────────────────────
# Mixin class — import and inherit in Waifugami
# ──────────────────────────────────────────────────────────────────────────────

class AuditMixin:
    """Mixin that adds the List Audit & Cleanup Engine to the Waifugami cog.

    Requirements from the host cog
    ────────────────────────────────
    self.bot                — Red bot instance
    self.config             — per-user Config (cards, removed_cards keys)
    self._char_index        — Dict[str, dict]  waifu_id → catalog entry
    self._series_index      — Dict[str, str]   series_id → series name
    self._locks             — Dict[int, asyncio.Lock]
    self._send_channel_v2_components(channel, components, ref_id)
    self._section_components(sections) → List[dict]
    WAIFUGAMI_ID            — int
    V2_FLAG                 — int

    The mixin stores its own session dict:
        self._audit_sessions: Dict[int, AuditSession]   user_id → session
    """

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def _audit_init(self) -> None:
        """Call from __init__ of the host cog."""
        self._audit_sessions: Dict[int, AuditSession] = {}

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
        self,
        user_id: int,
        protection: Dict[str, Any],
    ) -> None:
        await self.config.user_from_id(user_id).audit_protection.set(protection)

    async def _persist_event_cards(
        self,
        user_id: int,
        cards: Dict[str, Dict[str, Any]],
    ) -> None:
        if not cards:
            return

        protection = await self._audit_protection(user_id)
        event_cards = protection["event_cards"]
        sigma_cards = protection["sigma_cards"]

        for identity, snapshot in cards.items():
            existing = event_cards.get(identity)

            if existing:
                merged = dict(existing)

                if snapshot.get("last_seen_id") is not None:
                    merged["last_seen_id"] = snapshot["last_seen_id"]

                if snapshot.get("global_id") is not None:
                    merged["global_id"] = snapshot["global_id"]

                merged["name"] = snapshot["name"]
                merged["rarity"] = snapshot["rarity"]
                merged["last_seen_at"] = snapshot["last_seen_at"]

                event_cards[identity] = merged
            else:
                event_cards[identity] = dict(snapshot)

            if snapshot["rarity"] == "σ":
                sigma_cards[identity] = dict(event_cards[identity])

        protection["event_cards"] = event_cards
        protection["sigma_cards"] = sigma_cards

        await self._save_audit_protection(user_id, protection)

    def _enrich_with_catalog(self, cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Fill in series_id from the catalog when the stored card lacks it."""
        enriched = []
        for card in cards:
            c = dict(card)
            if c.get("series_id") is None:
                wid = str(c.get("waifu_id") or "")
                entry = self._char_index.get(wid)
                if entry:
                    try:
                        c["series_id"] = str(int(entry.get("series_id")))
                    except (TypeError, ValueError):
                        pass
            enriched.append(c)
        return enriched

    # ── Embed parsing (event list pages) ─────────────────────────────────

    @staticmethod
    def _parse_event_list_page(
        embed: discord.Embed,
    ) -> Tuple[Optional[str], int, int, List[Tuple[int, str, str]]]:
        """Parse one page of `.l -event all` output.

        Returns (owner_name, page_index, total_pages, entries)
        where entries = [(local_id, rarity_symbol, name), ...]
        """
        title = embed.title or ""
        m = LIST_TITLE_RE.match(title)
        if not m:
            return None, 0, 0, []

        owner_name = m.group(1)
        page_index: int = 0
        total_pages: int = 0

        for field in embed.fields:
            fm = FINAL_PAGE_FIELD_NAME_RE.search(field.name or "")
            if fm:
                page_index = int(fm.group(1))
                total_pages = int(fm.group(2))
                break

        entries: List[Tuple[int, str, str]] = []
        for line in (embed.description or "").splitlines():
            lm = LIST_ENTRY_RE.match(line.strip())
            if lm:
                local_id = int(lm.group(1))
                rarity = lm.group(2).strip()
                name = lm.group(3).strip()
                entries.append((local_id, rarity, name))

        return owner_name, page_index, total_pages, entries

    # ── Stage 1: harvest ──────────────────────────────────────────────────

    async def _audit_start(
        self, ctx: commands.Context
    ) -> None:
        """Begin the audit. The user needs to run `.l -event all` themselves;
        we instruct them and wait for the embed to appear in this channel."""
        user_id = ctx.author.id

        # Cancel any previous session.
        self._audit_cancel(user_id)

        session = AuditSession(
            user_id=user_id,
            channel_id=ctx.channel.id,
            guild_id=ctx.guild.id,
        )
        self._audit_sessions[user_id] = session

        guide = await ctx.reply(
            "**Audit started.** Run `.l -event all` in this channel so I can "
            "harvest your event cards, then I will classify your full collection.\n"
            "-# If you don't have any event cards, just run `.wg audit classify` instead.",
            mention_author=False,
        )
        session.guide_message_id = guide.id

    async def _audit_collect_event_page(
        self, message: discord.Message
    ) -> None:
        """Called from on_message_edit when a Waifugami list embed updates.

        Accumulates event-card local_ids across every page of `.l -event all`.
        Advances to classification automatically once the last page is seen.
        """
        if not message.embeds:
            return
        embed = message.embeds[0]
        owner_name, page_index, total_pages, entries = self._parse_event_list_page(embed)
        if owner_name is None:
            return

        # Find the session whose list_message_id matches, or whose channel
        # matches and is still in the harvesting phase.
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

        for local_id, rarity, name in entries:
            identity = _card_identity(name, rarity)

            session.event_cards[identity] = _card_snapshot(
                local_id=local_id,
                name=name,
                rarity=rarity,
            )
        session.seen_pages.add(page_index)

        if total_pages > 0:
            session.total_pages = total_pages

        # Check if we have seen all pages.
        all_seen = (
            session.total_pages is not None
            and session.seen_pages == set(range(session.total_pages + 1))
        )

        channel = self.bot.get_channel(session.channel_id)

        if all_seen or (total_pages > 0 and page_index == total_pages):
            await self._persist_event_cards(
                session.user_id,
                session.event_cards,
            )
            await self._audit_do_classify(session, channel)
        else:
            # Update the guide message with live progress.
            if channel and session.guide_message_id:
                try:
                    guide = channel.get_partial_message(session.guide_message_id)
                    await guide.edit(
                        content=(
                            f"-# Harvesting event cards… "
                            f"{len(session.event_cards)} found so far "
                            f"(page {page_index} of {total_pages or '?'}). "
                            "Keep clicking ➡."
                        )
                    )
                except discord.HTTPException:
                    pass

    async def _audit_event_protection(
        self,
        user_id: int,
    ) -> Dict[str, Dict[str, Any]]:
        protection = await self._audit_protection(user_id)
        return protection.get("event_cards", {})

    # ── Stage 1b: classify ────────────────────────────────────────────────

    async def _audit_do_classify(
        self,
        session: AuditSession,
        channel: Optional[discord.abc.Messageable],
    ) -> None:
        """Classify the full collection and post the Stage 1 report."""
        session.phase = "classified"

        raw_cards = await self._audit_active_cards(session.user_id)
        enriched = self._enrich_with_catalog(raw_cards)

        # Build locked set: local_ids of cards marked locked in storage.
        locked_local_ids: Set[int] = {
            int(c["local_id"])
            for c in enriched
            if c.get("local_id") is not None and _is_locked(c)
        }

        event_cards = await self._audit_event_protection(session.user_id)

        classified = classify_cards(
            enriched,
            session.event_cards,
            event_cards,
            locked_local_ids,
        )

        # Summarise by disposition.
        counts = {KEEP: 0, REVIEW: 0, SELL: 0, UNKNOWN: 0}
        dp_total = 0
        shard_total = 0
        for entry in classified:
            counts[entry["disposition"]] += 1
            if entry["disposition"] == SELL:
                dp_total += entry["dp_yield"]
                shard_total += entry["shard_yield"]

        persistent_event_count = len(event_cards)

        summary = (
            f"## Audit Report\n"
            f"-# {len(classified)} cards evaluated  ·  "
            f"{len(session.event_cards)} event cards learned this scan  ·  "
            f"{persistent_event_count} remembered permanently\n\n"
            f"🟢 **KEEP** — `{counts[KEEP]}` cards protected\n"
            f"🟡 **REVIEW** — `{counts[REVIEW]}` cards need your attention\n"
            f"🔴 **SELL** — `{counts[SELL]}` cards ready for removal\n"
            f"⬛ **UNKNOWN** — `{counts[UNKNOWN]}` cards with missing stats\n\n"
            f"**Estimated yield from SELL candidates**\n"
            f"> `{dp_total:,}` DP"
            + (f"  ·  `{shard_total}` Omega Shards" if shard_total else "")
            + f"\n\n"
            f"Use `..wg audit sell` to see the removal list.\n"
            f"Use `..wg audit review` to inspect REVIEW cards.\n"
            f"Use `..wg audit sell dupes` to limit to duplicates only.\n"
            f"Use `..wg audit sell <rarity>` to filter (e.g. `sell α`).\n"
            f"Use `..wg audit confirm` to execute removal after reviewing."
        )

        if channel:
            await self._send_channel_v2_components(
                channel,
                self._section_components([summary]),
                reference_message_id=None,
            )

    # ── Stage 2: browse sell / review candidates ──────────────────────────

    def _sell_candidates(self, session: AuditSession) -> List[Dict[str, Any]]:
        """Return classified entries that are SELL, respecting active filters."""
        entries = [e for e in session.classified if e["disposition"] == SELL]

        if session.rarity_filter:
            entries = [
                e for e in entries
                if e["rarity_symbol"] == session.rarity_filter.lower().strip()
            ]

        if session.dupes_only:
            # Group by (name, rarity_symbol); include only those with count > 1.
            from collections import Counter
            key_counts: Counter = Counter(
                (e["name"].casefold(), e["rarity_symbol"]) for e in entries
            )
            entries = [
                e for e in entries
                if key_counts[(e["name"].casefold(), e["rarity_symbol"])] > 1
            ]

        return sorted(entries, key=lambda e: (e["rarity_symbol"], e["local_id"] or 0))

    async def _audit_show_sell(
        self,
        ctx: commands.Context,
        rarity_filter: Optional[str] = None,
        dupes_only: bool = False,
    ) -> None:
        session = self._audit_session(ctx.author.id)
        if not session or session.phase not in ("classified", "confirming"):
            await ctx.reply(
                "No active audit. Run `..wg audit start` first.",
                mention_author=False,
            )
            return

        session.rarity_filter = rarity_filter
        session.dupes_only = dupes_only
        candidates = self._sell_candidates(session)

        if not candidates:
            await ctx.reply(
                "No SELL candidates match the current filter.",
                mention_author=False,
            )
            return

        # Update sell_ids to match the current filter selection.
        session.sell_ids = [
            int(e["local_id"]) for e in candidates if e["local_id"] is not None
        ]

        # Build a display embed — one text component per rarity group, up to
        # 30 entries shown; rest summarised.
        from itertools import groupby

        sections: List[str] = []
        header_parts = ["## SELL Candidates"]
        if rarity_filter:
            header_parts.append(f" · filter: `[{rarity_filter}]`")
        if dupes_only:
            header_parts.append(" · duplicates only")
        header_parts.append(f"\n-# {len(candidates)} cards selected")

        dp_total = sum(e["dp_yield"] for e in candidates)
        shard_total = sum(e["shard_yield"] for e in candidates)
        header_parts.append(
            f"  ·  estimated `{dp_total:,}` DP"
            + (f"  ·  `{shard_total}` Omega Shards" if shard_total else "")
        )
        sections.append("".join(header_parts))

        DISPLAY_LIMIT = 60
        shown = candidates[:DISPLAY_LIMIT]
        for rarity_sym, group in groupby(shown, key=lambda e: e["rarity_symbol"]):
            group_list = list(group)
            rarity_lines = [f"### [{rarity_sym.upper()}] — {len(group_list)} cards"]
            for entry in group_list:
                reasons_str = ", ".join(entry["reasons"]) if entry["reasons"] else ""
                skill_str = f"{entry['skill']:.2f}" if entry["skill"] is not None else "?"
                luck_str = str(entry["luck"]) if entry["luck"] is not None else "?"
                rarity_lines.append(
                    f"`{entry['local_id']}` **{entry['name']}** "
                    f"· Skill {skill_str} · Luck {luck_str}"
                    + (f"\n-# {reasons_str}" if reasons_str else "")
                )
            sections.append("\n".join(rarity_lines))

        if len(candidates) > DISPLAY_LIMIT:
            sections.append(
                f"-# … and {len(candidates) - DISPLAY_LIMIT} more (not shown). "
                "All are included in the removal if you confirm."
            )

        sections.append(
            "Run `..wg audit confirm` to remove these "
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
            await ctx.reply("No REVIEW or UNKNOWN cards in this audit.", mention_author=False)
            return

        # Discord allows at most 40 components per message.
        # Pack card lines into chunks of 20 per section so we never exceed it.
        CARDS_PER_SECTION = 20

        def _fmt(entry: Dict[str, Any]) -> str:
            skill_str = f"{entry['skill']:.2f}" if entry["skill"] is not None else "?"
            luck_str = str(entry["luck"]) if entry["luck"] is not None else "?"
            return (
                f"`{entry['local_id']}` **{entry['name']}** "
                f"`[{entry['rarity_symbol'].upper()}]`  "
                f"Skill {skill_str}  Luck {luck_str}  "
                f"-# {entry['disposition']} · {', '.join(entry['reasons'])}"
            )

        header = (
            f"## Review & Unknown Cards\n"
            f"-# {len(entries)} cards require manual inspection"
        )
        sections: List[str] = [header]

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
        ctx: commands.Context,
        override_ids: Optional[List[int]] = None,
    ) -> None:
        """Build removal batches and issue .rm commands."""
        session = self._audit_session(ctx.author.id)
        if not session or not session.classified:
            await ctx.reply(
                "No active audit to confirm. Run `..wg audit start` first.",
                mention_author=False,
            )
            return

        ids_to_remove = (
            override_ids
            if override_ids is not None
            else session.sell_ids
        )

        if not ids_to_remove:
            await ctx.reply(
                "No cards selected for removal. "
                "Run `..wg audit sell` to select candidates first.",
                mention_author=False,
            )
            return

        # Validate: make sure none of the IDs slipped into a protected
        # disposition since the audit was run (e.g. user re-ran in between).
        classified_by_lid: Dict[int, str] = {
            int(e["local_id"]): e["disposition"]
            for e in session.classified
            if e["local_id"] is not None
        }
        protected_ids = [
            lid for lid in ids_to_remove
            if classified_by_lid.get(lid) == KEEP
        ]
        if protected_ids:
            await ctx.reply(
                f"⚠️ {len(protected_ids)} of the selected IDs are marked **KEEP** "
                f"(`{', '.join(str(x) for x in protected_ids[:10])}{'…' if len(protected_ids) > 10 else ''}`). "
                "Aborting — please re-run `..wg audit sell` and try again.",
                mention_author=False,
            )
            return

        # Build batches (≤ 30, descending order within each batch).
        all_ids_sorted_desc = sorted(set(ids_to_remove), reverse=True)
        batches = list(_chunk(all_ids_sorted_desc, RM_BATCH_SIZE))

        session.phase = "executing"
        session.batch_queue = batches
        session.current_batch = None

        total = len(ids_to_remove)
        n_batches = len(batches)
        await ctx.reply(
            f"**Removing {total} cards across {n_batches} batch(es) of ≤ {RM_BATCH_SIZE}.**\n"
            "-# Issuing `.rm` commands now — do not touch your list until complete.",
            mention_author=False,
        )

        await self._audit_execute_next_batch(session, ctx.channel)

    async def _audit_execute_next_batch(
        self,
        session: AuditSession,
        channel: discord.abc.Messageable,
    ) -> None:
        """Pop the next batch from the queue and issue .rm commands for it."""
        if not session.batch_queue:
            await channel.send(
                "✅ **Audit removal complete.** All selected cards have been removed."
            )
            self._audit_cancel(session.user_id)
            return

        batch = session.batch_queue.pop(0)
        session.current_batch = batch
        session.touch()

        # Issue one .rm per local_id, descending (already sorted by confirm).
        for local_id in batch:
            try:
                await channel.send(f".rm {local_id}")
                # Small delay to avoid rate-limiting and give Waifugami time
                # to process each removal before the next.
                await asyncio.sleep(1.2)
            except discord.HTTPException as exc:
                await channel.send(
                    f"⚠️ Failed to send `.rm {local_id}`: {exc}. "
                    "Stopping. Re-run `..wg audit confirm` to retry remaining."
                )
                session.phase = "classified"
                return

        remaining = len(session.batch_queue)
        if remaining:
            await channel.send(
                f"-# Batch done. {remaining} batch(es) remaining. "
                "Continuing in 3 s…"
            )
            await asyncio.sleep(3)
            await self._audit_execute_next_batch(session, channel)
        else:
            await channel.send(
                "✅ **Audit removal complete.** All selected cards have been removed."
            )
            self._audit_cancel(session.user_id)

    # ── Listener hook — call this from on_message_edit_cards ──────────────

    async def audit_on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> bool:
        """Return True if the edit was consumed by an active audit session.

        Call this near the top of on_message_edit_cards so normal scan
        logic doesn't also try to process the event list pages.
        """
        if after.author.id != WAIFUGAMI_ID:
            return False
        if not after.embeds:
            return False

        embed = after.embeds[0]
        owner_name, page_index, total_pages, entries = self._parse_event_list_page(embed)
        if owner_name is None:
            return False

        # Find a matching harvesting session in this channel.
        for session in list(self._audit_sessions.values()):
            if (
                session.channel_id == after.channel.id
                and session.phase == "harvesting"
                and (session.list_message_id is None or session.list_message_id == after.id)
            ):
                await self._audit_collect_event_page(after)
                return True

        return False

    # ──────────────────────────────────────────────────────────────────────
    # Commands
    # ──────────────────────────────────────────────────────────────────────

    @commands.group(name="wgaudit", aliases=["wga"])
    async def wgaudit(self, ctx: commands.Context) -> None:
        """List Audit & Cleanup Engine — identify and remove low-value cards.

        Workflow:
          1. `..wg audit start`        — begin harvesting event cards
          2. Run `.l -event all` in the same channel and flip through all pages
          3. `..wg audit sell`         — browse SELL candidates
          4. `..wg audit confirm`      — execute `.rm` removals in safe batches

        Optional filters for step 3:
          `..wg audit sell <rarity>`   — e.g. `sell α`
          `..wg audit sell dupes`      — duplicates only
          `..wg audit review`          — inspect REVIEW / UNKNOWN cards

        To skip event harvesting (no event cards):
          `..wg audit classify`
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
        """Classify your collection immediately, without harvesting event cards.

        Use this if you have no event cards or want to skip that step.
        """
        if not await self.config.user(ctx.author).enabled():
            await ctx.reply(
                f"Enable card tracking first with `{ctx.clean_prefix}wgtrack enable`.",
                mention_author=False,
            )
            return

        user_id = ctx.author.id
        # Re-use existing session if one is harvesting, otherwise make a fresh one.
        session = self._audit_sessions.get(user_id)
        if session is None:
            session = AuditSession(
                user_id=user_id,
                channel_id=ctx.channel.id,
                guild_id=ctx.guild.id,
            )
            self._audit_sessions[user_id] = session

        msg = await ctx.reply("Classifying your collection…", mention_author=False)
        session.guide_message_id = msg.id
        await self._audit_do_classify(session, ctx.channel)

    @wgaudit.command(name="sell")
    async def wgaudit_sell(
        self, ctx: commands.Context, *, args: str = ""
    ) -> None:
        """Show SELL candidates, optionally filtered.

        Examples:
          ..wg audit sell          — all SELL candidates
          ..wg audit sell α        — only Alpha cards
          ..wg audit sell dupes    — only duplicate cards
          ..wg audit sell α dupes  — Alpha duplicates
        """
        args_lower = args.lower().strip()
        dupes_only = "dupes" in args_lower or "duplicates" in args_lower
        # Extract a rarity symbol if present (Greek letter or ASCII equivalent).
        rarity_map = {
            "a": "α", "alpha": "α", "α": "α",
            "b": "β", "beta": "β", "β": "β",
            "g": "γ", "gamma": "γ", "γ": "γ",
            "d": "δ", "delta": "δ", "δ": "δ",
            "s": "σ", "sigma": "σ", "σ": "σ",
            "e": "ε", "epsilon": "ε", "ε": "ε",
            "z": "ζ", "zeta": "ζ", "ζ": "ζ",
            "o": "ω", "omega": "ω", "ω": "ω",
        }
        rarity_filter: Optional[str] = None
        for token in args_lower.split():
            if token in rarity_map:
                rarity_filter = rarity_map[token]
                break

        await self._audit_show_sell(
            ctx, rarity_filter=rarity_filter, dupes_only=dupes_only
        )

    @wgaudit.command(name="review")
    async def wgaudit_review(self, ctx: commands.Context) -> None:
        """Show cards in the REVIEW or UNKNOWN category."""
        await self._audit_show_review(ctx)

    @wgaudit.command(name="confirm")
    async def wgaudit_confirm(
        self, ctx: commands.Context, *, ids_str: str = ""
    ) -> None:
        """Execute removal of the current SELL selection (or explicit IDs).

        Without arguments, removes all cards selected by `..wg audit sell`.
        With arguments, removes only the specified local IDs:
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

        lines = [
            f"**Phase:** `{session.phase}`",
            f"**Event cards learned this scan:** `{len(session.event_cards)}`",
            f"**Classified:** `{len(session.classified)}`",
            f"  · KEEP `{counts[KEEP]}`  REVIEW `{counts[REVIEW]}`  SELL `{counts[SELL]}`  UNKNOWN `{counts[UNKNOWN]}`",
            f"**Current SELL selection:** `{len(session.sell_ids)}` cards",
        ]
        if session.rarity_filter:
            lines.append(f"**Rarity filter:** `[{session.rarity_filter}]`")
        if session.dupes_only:
            lines.append("**Duplicates only:** yes")

        await ctx.reply("\n".join(lines), mention_author=False)

    # ── wg subcommand aliases ─────────────────────────────────────────────

    # These are registered by the host cog's wg group via the mixin.
    # They forward to the wgaudit group's callbacks so `..wg audit` also works.

    async def _wg_audit_dispatch(self, ctx: commands.Context) -> None:
        """Root forwarder — show help when called bare."""
        await ctx.send_help(self.wgaudit)
