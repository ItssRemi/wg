from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import discord
from redbot.core import Config, commands
from redbot.core.data_manager import bundled_data_path, cog_data_path

from .catalog import ItemCatalog, OutcomeCatalog, normalize_name
from .parser import (
    ADVENTURES,
    COMPASS_EMOJI_IDS,
    DANK_MEMER_ID,
    NAME_TO_KEY,
    RewardParser,
    aggregate_rewards,
    classify_ending,
    collect_custom_ids,
    compass_attribution,
    embed_author_name,
    fields_to_map,
    infer_adventure_key,
    is_result_state,
    json_hash,
    message_timestamp,
    multiset_difference,
    parse_iso,
    parse_start_custom_id,
    payload_is_dank,
    selected_component,
    split_completed_event_replay,
    strip_markdown,
)
from .storage import AdventureDB, dumps, loads

log = logging.getLogger("red.nebula.AdventureTracker")

DURATION_RE = re.compile(r"(?:next|for the next)\s+(\d+)\s*(minute|hour|day)s?", re.IGNORECASE)


NOTHING_HAPPENED_PHRASES = (
    "nothing ventured, nothing gained - or lost, in this case.",
    "seems like a typical day in your story - nothing's changed.",
    "nothing interesting happened.",
    "just a footnote in your adventure saga - carry on!",
    "it's just another moment in time, nothing to see here.",
    "all's quiet on the adventure front - for now.",
    "despite the intrigue, your situation remains unaffected.",
)

# Matches custom and animated Discord emoji formats, including legacy
# emoji names containing mixed case.
ADVENTURE_REWARD_PATTERN = re.compile(
    r"-\s*\d+\s+"
    r"(?:<a?:[A-Za-z0-9_]+:\d+>|:[A-Za-z0-9_]+:)"
    r"\s+.+",
    re.IGNORECASE,
)

ADVENTURE_END_PHRASES = (
    "your adventure has ended.",
)


def classify_adventure_outcome(
    result_text: str = "",
    ending_type: Optional[str] = None,
) -> str:
    """Classify the player-side outcome independently of story text."""
    result = " ".join(str(result_text or "").casefold().split())

    if not result:
        return "unclassified"

    if ending_type:
        return "premature_end"

    if any(phrase in result for phrase in ADVENTURE_END_PHRASES):
        return "premature_end"

    # The final hyphen-separated segment contains the player-side outcome.
    # Story text before that segment must not affect classification.
    parts = re.split(r"\s+-\s+", result)
    player_outcome = parts[-1].strip()

    if any(
        phrase in player_outcome
        for phrase in NOTHING_HAPPENED_PHRASES
    ):
        return "nothing_happened"

    if ADVENTURE_REWARD_PATTERN.search(player_outcome):
        return "reward"

    return "unclassified"

class _AdventureStatsNavButton(discord.ui.Button):
    """One of the owner-locked navigation controls on the stats menu."""

    def __init__(self, pager: "_AdventureStatsView", action: str, *, disabled: bool = False):
        emoji_data = {
            "previous": ("ArrowLeftui", 1516920524998049803),
            "refresh": ("Refreshui", 1516920773971808358),
            "next": ("ArrowRightui", 1516920674172665866),
        }
        name, emoji_id = emoji_data[action]
        super().__init__(
            style=discord.ButtonStyle.secondary,
            custom_id=f"advstats:{pager.owner_id}:{pager.target_id}:{pager.page}:{action}",
            emoji=discord.PartialEmoji(name=name, id=emoji_id),
            disabled=disabled,
        )
        self.pager = pager
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.pager.navigate(interaction, self.action)


class _AdventureStatsAdventureSelect(discord.ui.Select):
    """Switches the single-adventure detail view without leaving page three."""

    def __init__(self, pager: "_AdventureStatsView"):
        options: List[discord.SelectOption] = []
        for row in pager.stats.get("by_adventure", []):
            key = str(row.get("adventure_key") or "")
            if not key:
                continue
            name = str(row.get("adventure_name") or key or "Unknown Adventure")
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=key,
                    default=key == pager.selected_adventure,
                    emoji=pager.cog._adventure_partial_emoji(key),
                )
            )

        super().__init__(
            custom_id=f"advstats:{pager.owner_id}:{pager.target_id}:details:adventure",
            placeholder="Choose an adventure...",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.pager = pager

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = str(self.values[0]) if self.values else ""
        await self.pager.select_adventure(interaction, selected)


class _AdventureStatsView(discord.ui.LayoutView):
    """Five-page Components V2 personal adventure statistics menu."""

    PAGE_COUNT = 5

    def __init__(
        self,
        cog: "AdventureTracker",
        *,
        owner_id: int,
        target_id: int,
        display_name: str,
        utc_day: str,
        page: int,
        stats: Dict[str, Any],
        selected_adventure: str = "",
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.target_id = int(target_id)
        self.display_name = display_name
        self.utc_day = utc_day
        self.stats = stats
        self.page_count = self.PAGE_COUNT
        self.page = max(0, min(int(page), self.page_count - 1))

        available = [
            str(row.get("adventure_key") or "")
            for row in stats.get("by_adventure", [])
            if str(row.get("adventure_key") or "")
        ]
        self.selected_adventure = (
            selected_adventure if selected_adventure in available else (available[0] if available else "")
        )

        sections = cog._stats_page_text_sections(
            stats,
            display_name,
            page=self.page,
            selected_adventure=self.selected_adventure,
        )
        children: List[Any] = []
        for index, section in enumerate(sections):
            if index:
                children.append(discord.ui.Separator())
            children.append(discord.ui.TextDisplay(str(section)))

        if self.page == 2 and available:
            children.append(discord.ui.Separator())
            children.append(discord.ui.ActionRow(_AdventureStatsAdventureSelect(self)))

        children.append(discord.ui.Separator())
        children.append(
            discord.ui.ActionRow(
                _AdventureStatsNavButton(self, "previous", disabled=self.page == 0),
                _AdventureStatsNavButton(self, "refresh"),
                _AdventureStatsNavButton(self, "next", disabled=self.page == self.page_count - 1),
            )
        )
        children.append(discord.ui.TextDisplay(f"-# Page {self.page + 1} of {self.page_count}"))
        self.add_item(discord.ui.Container(*children))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the person who opened these adventure stats can use the controls.",
            ephemeral=True,
        )
        return False

    async def _refresh_view(
        self,
        interaction: discord.Interaction,
        *,
        page: int,
        selected_adventure: str,
    ) -> None:
        utc_day = datetime.now(timezone.utc).date().isoformat()
        display_name = self.display_name
        guild = interaction.guild
        if guild is not None:
            member = guild.get_member(self.target_id)
            if member is not None:
                display_name = discord.utils.escape_markdown(member.display_name)

        stats = await self.cog.db.user_stats(self.target_id, utc_day)
        new_view = _AdventureStatsView(
            self.cog,
            owner_id=self.owner_id,
            target_id=self.target_id,
            display_name=display_name,
            utc_day=utc_day,
            page=page,
            stats=stats,
            selected_adventure=selected_adventure,
        )
        await interaction.edit_original_response(
            view=new_view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def navigate(self, interaction: discord.Interaction, action: str) -> None:
        next_page = self.page
        if action == "previous":
            next_page = max(0, self.page - 1)
        elif action == "next":
            next_page = min(self.page_count - 1, self.page + 1)

        await interaction.response.defer()
        try:
            await self._refresh_view(
                interaction,
                page=next_page,
                selected_adventure=self.selected_adventure,
            )
        except Exception:
            log.exception("Failed to refresh adventure stats menu")
            await interaction.followup.send(
                "The adventure stats menu could not be refreshed.",
                ephemeral=True,
            )

    async def select_adventure(self, interaction: discord.Interaction, adventure_key: str) -> None:
        await interaction.response.defer()
        try:
            await self._refresh_view(
                interaction,
                page=2,
                selected_adventure=adventure_key,
            )
        except Exception:
            log.exception("Failed to switch adventure detail selection")
            await interaction.followup.send(
                "The selected adventure details could not be loaded.",
                ephemeral=True,
            )


class _AdventureLeaderboardNavButton(discord.ui.Button):
    """Owner-locked navigation control for the two-page global leaderboard."""

    def __init__(self, pager: "_AdventureLeaderboardView", action: str, *, disabled: bool = False):
        emoji_data = {
            "previous": ("ArrowLeftui", 1516920524998049803),
            "refresh": ("Refreshui", 1516920773971808358),
            "next": ("ArrowRightui", 1516920674172665866),
        }
        name, emoji_id = emoji_data[action]
        super().__init__(
            style=discord.ButtonStyle.secondary,
            custom_id=f"advlb:{pager.owner_id}:{pager.page}:{action}",
            emoji=discord.PartialEmoji(name=name, id=emoji_id),
            disabled=disabled,
        )
        self.pager = pager
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.pager.navigate(interaction, self.action)


class _AdventureLeaderboardView(discord.ui.LayoutView):
    """Two-page Components V2 leaderboard: net value and market value."""

    METRICS = ("net", "market")

    def __init__(
        self,
        cog: "AdventureTracker",
        *,
        owner_id: int,
        utc_day: str,
        page: int,
        rows_by_metric: Dict[str, List[Dict[str, Any]]],
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.owner_id = int(owner_id)
        self.utc_day = utc_day
        self.page_count = len(self.METRICS)
        self.page = max(0, min(int(page), self.page_count - 1))
        self.rows_by_metric = rows_by_metric

        metric = self.METRICS[self.page]
        rows = rows_by_metric.get(metric, [])
        sections = cog._leaderboard_page_sections(utc_day, metric, rows)

        children: List[Any] = []
        for index, section in enumerate(sections):
            if index:
                children.append(discord.ui.Separator())
            children.append(discord.ui.TextDisplay(section))

        children.append(discord.ui.Separator())
        children.append(
            discord.ui.ActionRow(
                _AdventureLeaderboardNavButton(self, "previous", disabled=self.page == 0),
                _AdventureLeaderboardNavButton(self, "refresh"),
                _AdventureLeaderboardNavButton(
                    self,
                    "next",
                    disabled=self.page == self.page_count - 1,
                ),
            )
        )
        children.append(discord.ui.TextDisplay(f"-# Page {self.page + 1} of {self.page_count}"))
        self.add_item(discord.ui.Container(*children))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the person who opened this adventure leaderboard can use the page controls.",
            ephemeral=True,
        )
        return False

    async def navigate(self, interaction: discord.Interaction, action: str) -> None:
        next_page = self.page
        if action == "previous":
            next_page = max(0, self.page - 1)
        elif action == "next":
            next_page = min(self.page_count - 1, self.page + 1)

        await interaction.response.defer()
        try:
            utc_day = datetime.now(timezone.utc).date().isoformat()
            net_rows, market_rows = await asyncio.gather(
                self.cog.db.leaderboard(utc_day, "net", 10, 0),
                self.cog.db.leaderboard(utc_day, "market", 10, 0),
            )
            new_view = _AdventureLeaderboardView(
                self.cog,
                owner_id=self.owner_id,
                utc_day=utc_day,
                page=next_page,
                rows_by_metric={"net": net_rows, "market": market_rows},
            )
            await interaction.edit_original_response(
                view=new_view,
                allowed_mentions=discord.AllowedMentions(users=False),
            )
        except Exception:
            log.exception("Failed to refresh adventure leaderboard menu")
            await interaction.followup.send(
                "The adventure leaderboard could not be refreshed.",
                ephemeral=True,
            )


class AdventureTracker(commands.Cog):
    """Tracks Dank Memer adventures with authoritative final-summary accounting."""

    __author__ = "Nebula"
    __version__ = "0.1.14"

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xA6D3_2026_0714, force_registration=True)
        self.config.register_guild(enabled=True, channels=[])

        data_dir = bundled_data_path(self)
        self.items = ItemCatalog(data_dir / "dankitems.json", data_dir / "emoji_map.csv")
        self.outcomes = OutcomeCatalog(data_dir / "nodes")
        self.reward_parser = RewardParser(self.items)
        self.db = AdventureDB(cog_data_path(self) / "adventuretracker.sqlite3")
        self._ready = asyncio.Event()
        self._init_task = asyncio.create_task(self._initialize())
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def _initialize(self) -> None:
        await self.db.initialize()
        backfilled = 0
        try:
            backfilled = await self._backfill_luck_observations()
        except Exception:
            # Luck backfilling is optional collection work. A malformed or old
            # historical row must never prevent the cog commands from becoming
            # ready. Live tracking can continue and the failed rows can be
            # retried after the parser is corrected.
            log.exception("Adventure luck backfill failed; continuing without blocking commands")
        self._ready.set()
        log.info(
            "AdventureTracker ready: %s (luck observations backfilled=%s)",
            self.db.path,
            backfilled,
        )

    def cog_unload(self) -> None:
        for task in (self._init_task, self._maintenance_task):
            if task and not task.done():
                task.cancel()


    @staticmethod
    def _item_identity(item: Dict[str, Any]) -> str:
        return str(
            item.get("item_key")
            or item.get("item_id")
            or normalize_name(str(item.get("name") or ""))
        )

    def _luck_observation_payload(
        self,
        *,
        node_event_id: int,
        run_message_id: int,
        guild_id: Optional[int],
        user_id: int,
        adventure_key: str,
        selected_custom_id: str,
        selected_label: str,
        occurred_at: str,
        source: str,
        result_text: str,
        active_items_known: bool,
        active_items_before: Sequence[Dict[str, Any]],
        item_losses: Sequence[Dict[str, Any]],
        lose_all: bool,
    ) -> Dict[str, Any]:
        contributors = self.reward_parser.parse_luck_contributions(result_text)
        active_before = [dict(item) for item in active_items_before]
        losses = [dict(item) for item in item_losses]
        active_after = [] if lose_all else multiset_difference(active_before, losses)

        conflicts: List[Dict[str, Any]] = []
        if active_items_known:
            active_counts: Dict[str, int] = {}
            for item in active_before:
                identity = self._item_identity(item)
                active_counts[identity] = active_counts.get(identity, 0) + int(item.get("quantity") or 1)
            for contributor in contributors:
                identity = self._item_identity(contributor)
                if active_counts.get(identity, 0) <= 0:
                    conflicts.append({
                        "type": "luck_contributor_not_active",
                        "item_key": contributor.get("item_key"),
                        "name": contributor.get("name"),
                    })
            remaining = dict(active_counts)
            for loss in losses:
                identity = self._item_identity(loss)
                quantity = int(loss.get("quantity") or 1)
                if remaining.get(identity, 0) < quantity:
                    conflicts.append({
                        "type": "lost_item_not_active",
                        "item_key": loss.get("item_key"),
                        "name": loss.get("name"),
                        "quantity": quantity,
                    })
                remaining[identity] = max(0, remaining.get(identity, 0) - quantity)

        total = sum(
            (float(item.get("luck_percent") or 0) for item in contributors),
            0.0,
        )
        total_luck: int | float = int(total) if total.is_integer() else total
        return {
            "node_event_id": int(node_event_id),
            "run_message_id": int(run_message_id),
            "guild_id": guild_id,
            "user_id": int(user_id),
            "adventure_key": adventure_key,
            "interaction_key": self.outcomes.normalize_custom_id(selected_custom_id),
            "option_label": selected_label,
            "occurred_at": occurred_at,
            "source": source,
            "active_items_known": bool(active_items_known),
            "active_items_before": active_before,
            "active_items_after": active_after,
            "contributors": contributors,
            "total_luck_percent": total_luck,
            "item_losses": losses,
            "state_conflicts": conflicts,
            "result_text": result_text,
        }

    async def _backfill_luck_observations(self) -> int:
        rows = await self.db.luck_backfill_rows()
        if not rows:
            return 0
        written = 0
        current_run: Optional[int] = None
        active_items: List[Dict[str, Any]] = []
        state_known = False
        for row in rows:
            run_message_id = int(row["run_message_id"] or 0)
            if run_message_id != current_run:
                current_run = run_message_id
                state_known = bool(row.get("selected_items_observed"))
                active_items = loads(row.get("selected_items_json"), []) if state_known else []
            losses = loads(row.get("item_losses_json"), [])
            observation = self._luck_observation_payload(
                node_event_id=int(row["node_event_id"]),
                run_message_id=run_message_id,
                guild_id=row.get("guild_id"),
                user_id=int(row["user_id"]),
                adventure_key=str(row.get("adventure_key") or ""),
                selected_custom_id=str(row.get("selected_custom_id") or ""),
                selected_label=str(row.get("selected_label") or ""),
                occurred_at=str(row.get("occurred_at") or ""),
                source="backfill",
                result_text=str(row.get("result_text") or ""),
                active_items_known=state_known,
                active_items_before=active_items,
                item_losses=losses,
                lose_all=bool(row.get("lose_all")),
            )
            await self.db.add_luck_observation(observation)
            active_items = [dict(item) for item in observation["active_items_after"]]
            written += 1
        return written

    async def _maintenance_loop(self) -> None:
        try:
            await self._ready.wait()
            while True:
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
                marked = await self.db.mark_stale_runs(cutoff.isoformat())
                if marked:
                    log.info("Marked %s abandoned adventure run(s) incomplete", marked)
                await asyncio.sleep(900)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("AdventureTracker maintenance loop failed")

    async def _wait_ready(self) -> None:
        await self._ready.wait()

    # ---------------------------------------------------------------------
    # Discord payload conversion
    # ---------------------------------------------------------------------

    @staticmethod
    def _component_to_dict(component: Any) -> Dict[str, Any]:
        if isinstance(component, dict):
            return component
        if hasattr(component, "to_dict"):
            try:
                return component.to_dict()
            except Exception:
                pass
        result: Dict[str, Any] = {}
        for attr in ("type", "style", "disabled", "custom_id", "label", "id"):
            value = getattr(component, attr, None)
            if value is not None:
                if hasattr(value, "value"):
                    value = value.value
                result[attr] = value
        emoji = getattr(component, "emoji", None)
        if emoji is not None:
            result["emoji"] = {
                "id": getattr(emoji, "id", None),
                "name": getattr(emoji, "name", None),
                "animated": getattr(emoji, "animated", False),
            }
        children = getattr(component, "children", None) or getattr(component, "components", None)
        if children:
            result["components"] = [AdventureTracker._component_to_dict(c) for c in children]
        return result

    @classmethod
    def _message_to_payload(cls, message: discord.Message) -> Dict[str, Any]:
        embeds = []
        for embed in getattr(message, "embeds", []) or []:
            try:
                embeds.append(embed.to_dict())
            except Exception:
                continue
        components = [cls._component_to_dict(c) for c in (getattr(message, "components", []) or [])]
        payload: Dict[str, Any] = {
            "id": str(message.id),
            "channel_id": str(message.channel.id),
            "guild_id": str(message.guild.id) if message.guild else None,
            "author": {"id": str(message.author.id)},
            "timestamp": message.created_at.isoformat() if message.created_at else None,
            "edited_timestamp": message.edited_at.isoformat() if message.edited_at else None,
            "webhook_id": str(message.webhook_id) if getattr(message, "webhook_id", None) else None,
            "application_id": str(message.application_id) if getattr(message, "application_id", None) else None,
            "embeds": embeds,
            "components": components,
            "content": message.content or "",
        }
        interaction = getattr(message, "interaction_metadata", None)
        user = getattr(interaction, "user", None)
        if user:
            payload["interaction_metadata"] = {"user": {"id": str(user.id)}}
        return payload

    @staticmethod
    def _meaningful_state(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "embeds": payload.get("embeds") or [],
            "components": payload.get("components") or [],
            "content": payload.get("content") or "",
        }

    async def _guild_enabled(self, guild_id: Optional[int], channel_id: Optional[int]) -> bool:
        if guild_id is None:
            return False
        guild_conf = self.config.guild_from_id(guild_id)
        if not await guild_conf.enabled():
            return False
        channels = await guild_conf.channels()
        return not channels or int(channel_id or 0) in {int(x) for x in channels}

    # ---------------------------------------------------------------------
    # Listeners
    # ---------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.id != DANK_MEMER_ID:
            return
        await self._process_payload(self._message_to_payload(message), source="on_message")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if after.author.id != DANK_MEMER_ID:
            return
        await self._process_payload(self._message_to_payload(after), source="on_message_edit")

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        data = dict(getattr(payload, "data", {}) or {})
        if not data or not (data.get("embeds") or data.get("components")):
            return
        data.setdefault("id", str(payload.message_id))
        data.setdefault("channel_id", str(payload.channel_id))
        if payload.guild_id:
            data.setdefault("guild_id", str(payload.guild_id))
        await self._process_payload(data, source="on_raw_message_edit")

    # ---------------------------------------------------------------------
    # Core processing
    # ---------------------------------------------------------------------

    async def _process_payload(self, payload: Dict[str, Any], source: str = "live") -> None:
        await self._wait_ready()
        if not payload_is_dank(payload):
            return
        try:
            source_message_id = int(payload.get("id"))
            channel_id = int(payload.get("channel_id"))
            guild_id = int(payload.get("guild_id")) if payload.get("guild_id") else None
        except (TypeError, ValueError):
            return
        if not await self._guild_enabled(guild_id, channel_id):
            return

        state_hash = json_hash(self._meaningful_state(payload))
        if not await self.db.claim_state(source_message_id, state_hash, datetime.now(timezone.utc).isoformat()):
            return

        custom_ids = collect_custom_ids(payload.get("components") or [])
        descriptions = [str(e.get("description") or "") for e in (payload.get("embeds") or [])]
        combined_description = "\n".join(x for x in descriptions if x)
        completed_event_replay, continuation_prompt = split_completed_event_replay(
            combined_description
        )
        event_time = message_timestamp(payload)
        event_iso = event_time.isoformat()

        # Explicit timed item uses are tracked independently from adventures.
        if await self._process_effect_use(
            payload, source_message_id, guild_id, channel_id, custom_ids, combined_description, event_time
        ):
            return

        message_id = await self.db.resolve_message_id(source_message_id)
        run = await self.db.get_run(message_id)
        existing_key = str(run.get("adventure_key") or "") if run else ""
        adventure_key = infer_adventure_key(payload, existing_key)

        # Final summary must be processed before start/prompt detection.
        summary_embed = next(
            (e for e in (payload.get("embeds") or []) if embed_author_name(e).casefold() == "adventure summary"),
            None,
        )
        if summary_embed is not None:
            if run is None:
                summary_fields = fields_to_map(summary_embed)
                summary_key = NAME_TO_KEY.get(normalize_name(summary_fields.get("Name", "")), adventure_key)
                summary_user_id = self._extract_user_id(payload, custom_ids)
                if summary_user_id is not None and summary_key:
                    candidate = await self.db.find_resumable_run(summary_user_id, summary_key)
                    if candidate is not None:
                        message_id = int(candidate["message_id"])
                        run = candidate
                        await self.db.link_message_alias(
                            source_message_id, message_id, summary_user_id, summary_key, event_iso,
                            "summary_continuation",
                        )
                        log.info(
                            "Linked resumed adventure summary alias=%s canonical=%s user=%s key=%s",
                            source_message_id, message_id, summary_user_id, summary_key,
                        )
            await self._finalize_summary(payload, summary_embed, run, message_id, guild_id, channel_id, event_time)
            return

        start = None
        for cid in custom_ids:
            start = parse_start_custom_id(cid)
            if start:
                break
        if start:
            key = start["adventure_key"]
            meta = ADVENTURES.get(key, {})
            selected_items = self.items.selected_items_from_ids(start["item_ids"])
            await self.db.upsert_pending_run(
                {
                    "message_id": message_id,
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "user_id": start["user_id"],
                    "adventure_key": key,
                    "adventure_name": meta.get("name", key),
                    "last_seen_at": event_iso,
                    "selected_item_ids": start["item_ids"],
                    "selected_items": selected_items,
                    "ticket_quantity": int(meta.get("tickets", 0)),
                    "max_interactions": meta.get("max"),
                }
            )
            return

        if not adventure_key and not run:
            return

        # Adventure catalogue and backpack-selection messages are not runs.
        # A run becomes active only after Dank Memer renders the live adventure
        # message with progress/next or an actual option component.
        live_prefixes = (
            "adventure-option:",
            "adventure-leading:",
            "adventure-progress:",
            "adventure-next:",
        )
        is_live_adventure = any(cid.startswith(live_prefixes) for cid in custom_ids)
        if not is_live_adventure and not run:
            return

        user_id = self._extract_user_id(payload, custom_ids)
        if user_id is None and run:
            user_id = int(run["user_id"])
        if user_id is None:
            return
        meta = ADVENTURES.get(adventure_key, {})

        # Running /adventure during an unfinished run renders the current prompt
        # in a brand-new Discord message. Link that physical message to the
        # original canonical run so the ticket, backpack, prior nodes, Compass
        # procs, and final summary remain one adventure. This also works when the
        # user resumes in another guild in Nebula's network.
        if run is None and is_live_adventure and adventure_key:
            candidate = await self.db.find_resumable_run(
                user_id, adventure_key, continuation_prompt
            )
            if candidate is not None:
                message_id = int(candidate["message_id"])
                run = candidate
                alias_reason = (
                    "completed_event_replay"
                    if completed_event_replay
                    else "live_prompt_continuation"
                )
                await self.db.link_message_alias(
                    source_message_id, message_id, user_id, adventure_key, event_iso,
                    alias_reason,
                )
                log.info(
                    "Linked resumed adventure message alias=%s canonical=%s user=%s key=%s reason=%s",
                    source_message_id, message_id, user_id, adventure_key, alias_reason,
                )

        await self.db.ensure_active_run(
            {
                "message_id": message_id,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "user_id": user_id,
                "adventure_key": adventure_key,
                "adventure_name": meta.get("name", adventure_key),
                "started_at": event_iso,
                "started_utc_day": event_time.date().isoformat(),
                "last_seen_at": event_iso,
                "ticket_quantity": int(meta.get("tickets", 0)),
                "max_interactions": meta.get("max"),
            }
        )
        run = await self.db.get_run(message_id)
        if run:
            ticket = self.items.resolve_key("adventureticket")
            if ticket:
                await self.db.ensure_start_ticket_ledger(run, self.items.snapshot(ticket, 1))

        # Dank can replay the prior prompt after its choice has already paid out
        # when the user runs /adventure before clicking Next. The replacement
        # notice contains no new reward data. Preserve the original recorded
        # node result, keep this message as an alias, and wait for its Next
        # button to advance to the actual next prompt.
        if completed_event_replay:
            existing_prompt = str((run or {}).get("current_prompt_text") or "").strip()
            existing_is_replay, _ = split_completed_event_replay(existing_prompt)
            if continuation_prompt and (not existing_prompt or existing_is_replay):
                await self.db.update_prompt(
                    message_id, continuation_prompt, payload, event_iso
                )
            log.info(
                "Ignored completed-event replay message=%s canonical=%s user=%s key=%s",
                source_message_id, message_id, user_id, adventure_key,
            )
            return

        if not is_result_state(payload):
            prompt = combined_description.strip()
            if prompt:
                await self.db.update_prompt(message_id, prompt, payload, event_iso)
            return

        await self._process_node_result(payload, run or {}, message_id, user_id, adventure_key, event_time, source)

    @staticmethod
    def _extract_user_id(payload: Dict[str, Any], custom_ids: Sequence[str]) -> Optional[int]:
        from .parser import extract_user_id
        return extract_user_id(payload, custom_ids)

    async def _process_effect_use(
        self,
        payload: Dict[str, Any],
        message_id: int,
        guild_id: Optional[int],
        channel_id: int,
        custom_ids: Sequence[str],
        description: str,
        event_time: datetime,
    ) -> bool:
        user_id = self._extract_user_id(payload, custom_ids)
        item_id: Optional[int] = None
        for cid in custom_ids:
            parts = cid.split(":")
            if len(parts) >= 3 and parts[0] == "use-again":
                try:
                    item_id = int(parts[2])
                except ValueError:
                    continue
                break
        lowered = description.casefold()
        if item_id is None:
            if "equipped your handy dandy compass" in lowered:
                item_id = 236
            elif "tentacled temptation" in lowered:
                item_id = 271
        if user_id is None or item_id not in {236, 271}:
            return False

        if item_id == 236:
            item = self.items.resolve_item_id(236)
            expires = event_time + timedelta(hours=1)
            await self.db.add_compass_session(
                {
                    "session_message_id": message_id,
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "user_id": user_id,
                    "used_at": event_time.isoformat(),
                    "expires_at": expires.isoformat(),
                    "explicit_observed": True,
                    "cost_quantity": 1,
                    "cost_net": item.net_value if item else 0,
                    "cost_market": item.market_value if item else 0,
                    "raw": payload,
                }
            )
            return True

        expires = event_time + timedelta(hours=3)
        await self.db.add_effect_session(
            {
                "session_message_id": message_id,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "user_id": user_id,
                "effect_key": "tentacled_temptation",
                "used_at": event_time.isoformat(),
                "expires_at": expires.isoformat(),
                "raw": payload,
            }
        )
        return True

    async def _process_node_result(
        self,
        payload: Dict[str, Any],
        run: Dict[str, Any],
        message_id: int,
        user_id: int,
        adventure_key: str,
        event_time: datetime,
        source: str = "live",
    ) -> None:
        descriptions = [str(e.get("description") or "") for e in (payload.get("embeds") or [])]
        result_text = "\n".join(x for x in descriptions if x).strip()
        if not result_text:
            return
        selected_cid, selected_label = selected_component(payload.get("components") or [])
        prompt_text = str(run.get("current_prompt_text") or "")
        ending_type, fatal_end, lose_all = classify_ending(result_text)
        story_text, outcome_text = split_adventure_result(result_text)
        outcome_classification = classify_adventure_outcome(outcome_text)
        if outcome_classification == "unclassified":
            log.warning(
                "Unclassified adventure outcome | "
                "run=%s | message=%s | adventure=%s | "
                "choice=%r | prompt=%r | result=%r",
                message_id,
                message_id,
                adventure_key,
                selected_label,
                prompt_text,
                result_text,
            )
        item_losses = self.reward_parser.parse_item_losses(result_text)
        parsed_rewards = self.reward_parser.parse(result_text, adventure_key, source="live")
        compass_marker = any(emoji_id in COMPASS_EMOJI_IDS for emoji_id in self._all_emoji_ids(result_text))

        attribution = {
            "proc": compass_marker,
            "doubled_components": [],
            "bonus_net": 0,
            "bonus_market": 0,
            "bonus_coins": 0,
        }
        if compass_marker:
            record = self.outcomes.find(adventure_key, selected_cid, prompt_text, selected_label)
            candidates: List[Dict[str, Any]] = []
            if record:
                for field in ("outcome", "outcome_2"):
                    text = record.get(field)
                    if text:
                        expected = self.reward_parser.parse(str(text), adventure_key, source="expected")
                        candidates.append(compass_attribution(parsed_rewards, expected))
            if candidates:
                attribution = max(candidates, key=lambda x: (len(x["doubled_components"]), x["bonus_net"] + x["bonus_market"]))
                attribution["proc"] = True

        event_hash = json_hash(
            {
                "selected": self.outcomes.normalize_custom_id(selected_cid),
                "label": selected_label,
                "result": result_text,
            }
        )
        event = {
            "event_hash": event_hash,
            "occurred_at": event_time.isoformat(),
            "selected_custom_id": selected_cid,
            "selected_label": selected_label,
            "prompt_text": prompt_text,
            "result_text": result_text,
            "ending_type": ending_type,
            "outcome_classification": outcome_classification,
            "fatal_end": fatal_end,
            "lose_all": lose_all,
            "item_losses": item_losses,
            "parsed_rewards": parsed_rewards,
            "compass_marker": compass_marker,
            "compass_proc": bool(attribution.get("proc")),
            "doubled_components": attribution.get("doubled_components", []),
            "compass_bonus_net": attribution.get("bonus_net", 0),
            "compass_bonus_market": attribution.get("bonus_market", 0),
            "compass_bonus_coins": attribution.get("bonus_coins", 0),
            "raw": payload,
        }
        node_id = await self.db.add_node_event(message_id, event)
        if node_id:
            state_known = bool(run.get("selected_items_observed"))
            selected_items = loads(run.get("selected_items_json"), []) if state_known else []
            prior_losses = loads(run.get("node_item_losses_json"), []) if state_known else []
            active_before = multiset_difference(selected_items, prior_losses) if state_known else []
            observation = self._luck_observation_payload(
                node_event_id=node_id,
                run_message_id=message_id,
                guild_id=run.get("guild_id"),
                user_id=user_id,
                adventure_key=adventure_key,
                selected_custom_id=selected_cid,
                selected_label=selected_label,
                occurred_at=event_time.isoformat(),
                source=source,
                result_text=result_text,
                active_items_known=state_known,
                active_items_before=active_before,
                item_losses=item_losses,
                lose_all=lose_all,
            )
            await self.db.add_luck_observation(observation)
            if observation["state_conflicts"]:
                log.warning(
                    "Adventure luck state conflict run=%s node=%s conflicts=%s",
                    message_id,
                    node_id,
                    observation["state_conflicts"],
                )
        if node_id and attribution.get("doubled_components"):
            session = await self.db.find_active_compass_session(user_id, event_time.isoformat())
            await self.db.add_compass_bonus(
                int(session["session_message_id"]) if session else None,
                message_id,
                node_id,
                user_id,
                event_time.isoformat(),
                attribution["doubled_components"],
            )

    @staticmethod
    def _all_emoji_ids(text: str) -> List[int]:
        from .catalog import extract_emoji_ids
        return extract_emoji_ids(text)

    async def _finalize_summary(
        self,
        payload: Dict[str, Any],
        summary_embed: Dict[str, Any],
        run: Optional[Dict[str, Any]],
        message_id: int,
        guild_id: Optional[int],
        channel_id: int,
        event_time: datetime,
    ) -> None:
        fields = fields_to_map(summary_embed)
        adventure_name = fields.get("Name", "Unknown Adventure")
        adventure_key = NAME_TO_KEY.get(normalize_name(adventure_name), str((run or {}).get("adventure_key") or ""))
        meta = ADVENTURES.get(adventure_key, {})
        custom_ids = collect_custom_ids(payload.get("components") or [])
        user_id = self._extract_user_id(payload, custom_ids)
        if user_id is None and run:
            user_id = int(run["user_id"])
        if user_id is None:
            return

        await self.db.ensure_active_run(
            {
                "message_id": message_id,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "user_id": user_id,
                "adventure_key": adventure_key,
                "adventure_name": adventure_name,
                "started_at": (run or {}).get("started_at") or event_time.isoformat(),
                "started_utc_day": ((run or {}).get("started_at") or event_time.isoformat())[:10],
                "last_seen_at": event_time.isoformat(),
                "ticket_quantity": int((run or {}).get("ticket_quantity") or meta.get("tickets", 0)),
                "max_interactions": (run or {}).get("max_interactions") or meta.get("max"),
            }
        )
        run = await self.db.get_run(message_id) or {}
        ticket = self.items.resolve_key("adventureticket")
        if ticket:
            await self.db.ensure_start_ticket_ledger(run, self.items.snapshot(ticket, 1))

        try:
            interactions = int(str(fields.get("Interactions", "0")).replace(",", ""))
        except ValueError:
            interactions = 0
        max_interactions = int(run.get("max_interactions") or meta.get("max") or 0)
        premature = bool(run.get("premature_end_seen"))
        lose_all = bool(run.get("lose_all_seen"))
        ending_type = str(run.get("ending_type") or "")

        if premature:
            status = "lost"
            completion_reason = ending_type or "premature_end"
        elif max_interactions and interactions == max_interactions:
            status = "won"
            completion_reason = "maximum_interactions_without_fatal_end"
            ending_type = "normal_completion"
        elif max_interactions and interactions < max_interactions:
            status = "lost"
            completion_reason = "summary_below_maximum"
            ending_type = ending_type or "premature_end_unobserved"
        else:
            status = "unknown"
            completion_reason = "invalid_or_unverified_interaction_count"
            ending_type = ending_type or "unknown"

        selected_items = loads(run.get("selected_items_json"), [])
        returned_items = self.reward_parser.parse_backpack(fields.get("Backpack", ""))
        if selected_items:
            backpack_losses = multiset_difference(selected_items, returned_items)
        else:
            backpack_losses = loads(run.get("node_item_losses_json"), [])
        if lose_all and selected_items:
            backpack_losses = selected_items

        rewards = self.reward_parser.parse(fields.get("Rewards", ""), adventure_key, source="summary")
        aggregate = aggregate_rewards(rewards)
        node_events = await self.db.node_events_for_run(message_id)
        forfeited_rewards: List[Dict[str, Any]] = []
        if lose_all:
            for node in node_events:
                forfeited_rewards.extend(loads(node.get("parsed_rewards_json"), []))

        ticket_quantity = int(run.get("ticket_quantity") or meta.get("tickets", 0))
        ticket_net_unit = ticket.net_value if ticket else 0
        ticket_market_unit = ticket.market_value if ticket else 0
        ticket_cost_net = ticket_quantity * ticket_net_unit
        ticket_cost_market = ticket_quantity * ticket_market_unit
        backpack_loss_net = sum(int(x.get("net_total") or 0) for x in backpack_losses)
        backpack_loss_market = sum(int(x.get("market_total") or 0) for x in backpack_losses)
        profit_net = int(aggregate["gross_net"]) - ticket_cost_net - backpack_loss_net
        profit_market = int(aggregate["gross_market"]) - ticket_cost_market - backpack_loss_market
        completion_percent = (100.0 * interactions / max_interactions) if max_interactions else None

        final = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "adventure_key": adventure_key,
            "adventure_name": adventure_name,
            "completed_at": event_time.isoformat(),
            "completed_utc_day": event_time.date().isoformat(),
            "returned_items": returned_items,
            "backpack_losses": backpack_losses,
            "rewards": aggregate["tracked"],
            "ignored_rewards": aggregate["ignored"],
            "unknown_rewards": aggregate["unknown"],
            "multipliers": aggregate["multipliers"],
            "forfeited_rewards": forfeited_rewards,
            "ticket_quantity": ticket_quantity,
            "max_interactions": max_interactions,
            "interactions": interactions,
            "status": status,
            "completion_reason": completion_reason,
            "ending_type": ending_type,
            "ending_text": run.get("ending_text"),
            "premature_end_seen": premature,
            "lose_all_seen": lose_all,
            "completion_percent": completion_percent,
            "coins_gained": aggregate["coins"],
            "fish_tokens_gained": aggregate["fish_tokens"],
            "skin_fragments_gained": aggregate["skin_fragments"],
            "gross_reward_net": aggregate["gross_net"],
            "gross_reward_market": aggregate["gross_market"],
            "ticket_cost_net": ticket_cost_net,
            "ticket_cost_market": ticket_cost_market,
            "backpack_loss_net": backpack_loss_net,
            "backpack_loss_market": backpack_loss_market,
            "profit_net": profit_net,
            "profit_market": profit_market,
            "raw_summary": payload,
        }

        ledger: List[Dict[str, Any]] = []
        for reward in rewards:
            if reward.get("kind") == "coins":
                amount = int(reward.get("amount") or 0)
                ledger.append(
                    {
                        "kind": "coins_reward", "direction": 1, "name": "Coins", "quantity": amount,
                        "amount": amount, "net_total": amount, "market_total": amount,
                        "reason": "adventure_reward",
                    }
                )
            elif reward.get("kind") == "item" and reward.get("tracked"):
                ledger.append(
                    {
                        "kind": "item_reward", "direction": 1, "item_key": reward.get("item_key"),
                        "name": reward.get("name"), "quantity": reward.get("quantity", 0),
                        "net_unit": reward.get("net_unit", 0), "market_unit": reward.get("market_unit", 0),
                        "net_total": reward.get("net_total", 0), "market_total": reward.get("market_total", 0),
                        "reason": "adventure_reward",
                    }
                )
            elif reward.get("kind") in {"fish_tokens", "skin_fragments"}:
                ledger.append(
                    {
                        "kind": reward["kind"], "direction": 1, "name": reward.get("name"),
                        "quantity": reward.get("quantity", 0), "reason": "adventure_reward",
                    }
                )
        for loss in backpack_losses:
            ledger.append(
                {
                    "kind": "backpack_loss", "direction": -1, "item_key": loss.get("item_key"),
                    "name": loss.get("name"), "quantity": loss.get("quantity", 0),
                    "net_unit": loss.get("net_unit", 0), "market_unit": loss.get("market_unit", 0),
                    "net_total": -int(loss.get("net_total", 0)),
                    "market_total": -int(loss.get("market_total", 0)),
                    "reason": ending_type if lose_all else "backpack_item_not_returned",
                }
            )
        await self.db.finalize_run(message_id, final, ledger)
        log.info(
            "Finalized adventure message=%s user=%s key=%s status=%s interactions=%s/%s net=%s market=%s",
            message_id, user_id, adventure_key, status, interactions, max_interactions, profit_net, profit_market,
        )

    # ---------------------------------------------------------------------
    # Commands
    # ---------------------------------------------------------------------

    @commands.hybrid_group(name="adventure", invoke_without_command=True)
    @commands.guild_only()
    async def adventure(self, ctx: commands.Context) -> None:
        """Adventure statistics and leaderboard."""
        await ctx.send_help(ctx.command)

    @adventure.command(name="stats")
    @commands.guild_only()
    async def adventure_stats(self, ctx: commands.Context, user: Optional[discord.Member] = None) -> None:
        """Show globally aggregated UTC adventure statistics for yourself or another member."""
        await self._wait_ready()
        user = user or ctx.author
        utc_day = datetime.now(timezone.utc).date().isoformat()
        stats = await self.db.user_stats(user.id, utc_day)
        display_name = discord.utils.escape_markdown(user.display_name)

        layout_cls = getattr(discord.ui, "LayoutView", None)
        if layout_cls is None:
            # The bot currently runs a Components V2 capable build, but retain a
            # readable fallback for older installations.
            sections = self._stats_page_text_sections(stats, display_name, page=0)
            await self._send_v2_sections(ctx, sections)
            return

        view = _AdventureStatsView(
            self,
            owner_id=ctx.author.id,
            target_id=user.id,
            display_name=display_name,
            utc_day=utc_day,
            page=0,
            stats=stats,
        )
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    @adventure.command(name="leaderboard", aliases=["lb"])
    @commands.guild_only()
    async def adventure_leaderboard(self, ctx: commands.Context, metric: str = "net") -> None:
        """Show the two-page global UTC leaderboard for net and market profit."""
        await self._wait_ready()
        metric = metric.casefold().replace("-", "").replace("_", "")
        if metric in {"market", "marketvalue", "marketprofit"}:
            start_page = 1
        elif metric in {"net", "netvalue", "netprofit"}:
            start_page = 0
        else:
            await ctx.send("Metric must be either `net` or `market`.")
            return

        utc_day = datetime.now(timezone.utc).date().isoformat()
        net_rows, market_rows = await asyncio.gather(
            self.db.leaderboard(utc_day, "net", 10, 0),
            self.db.leaderboard(utc_day, "market", 10, 0),
        )
        rows_by_metric = {"net": net_rows, "market": market_rows}

        layout_cls = getattr(discord.ui, "LayoutView", None)
        if layout_cls is None:
            selected_metric = "market" if start_page else "net"
            sections = self._leaderboard_page_sections(
                utc_day,
                selected_metric,
                rows_by_metric[selected_metric],
            )
            await self._send_v2_sections(ctx, sections)
            return

        view = _AdventureLeaderboardView(
            self,
            owner_id=ctx.author.id,
            utc_day=utc_day,
            page=start_page,
            rows_by_metric=rows_by_metric,
        )
        await ctx.send(
            view=view,
            allowed_mentions=discord.AllowedMentions(users=False),
        )

    @adventure.command(name="trackerstatus")
    @commands.is_owner()
    async def adventure_tracker_status(self, ctx: commands.Context) -> None:
        """Show collector/database health."""
        await self._wait_ready()
        counts = await self.db.db_counts()
        lines = [
            "### Adventure Tracker Status",
            f"Database: `{self.db.path}`",
            f"Runs: **{counts['runs']}**・Finalized: **{counts['finalized']}**",
            f"Node events: **{counts['nodes']}**・Ledger entries: **{counts['ledger']}**",
            f"Compass sessions: **{counts['compass_sessions']}**",
            f"Luck observations: **{counts['luck_observations']}**・Contributing items: **{counts['luck_sources']}**",
            f"Known backpack snapshots: **{counts['luck_known']}**・State conflicts: **{counts['luck_conflicts']}**",
            f"Unknown rewards awaiting mapping: **{counts['unknown_rewards']}**",
        ]
        await ctx.send("\n".join(lines))

    @adventure.command(name="enable")
    @commands.admin_or_permissions(manage_guild=True)
    async def adventure_enable(self, ctx: commands.Context, enabled: bool) -> None:
        """Enable or disable collection in this server."""
        await self.config.guild(ctx.guild).enabled.set(enabled)
        await ctx.send(f"Adventure tracking is now **{'enabled' if enabled else 'disabled'}**.")

    @adventure.command(name="channels")
    @commands.admin_or_permissions(manage_guild=True)
    async def adventure_channels(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        """Limit tracking to one channel. Run without a channel to track all channels."""
        ids = [channel.id] if channel else []
        await self.config.guild(ctx.guild).channels.set(ids)
        if channel:
            await ctx.send(f"Adventure tracking is limited to {channel.mention}.")
        else:
            await ctx.send("Adventure tracking now watches all channels in this server.")

    @adventure.command(name="importlog")
    @commands.is_owner()
    async def adventure_import_log(self, ctx: commands.Context) -> None:
        """Replay attached Nebula JSONL logs through the collector."""
        await self._wait_ready()
        attachments = [a for a in ctx.message.attachments if a.filename.casefold().endswith(".jsonl")]
        if not attachments:
            await ctx.send("Attach one or more `.jsonl` files to this command.")
            return
        processed = errors = 0
        async with ctx.typing():
            for attachment in attachments:
                raw = await attachment.read()
                for line in raw.decode("utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        for payload in self._extract_payloads_from_log_record(record):
                            await self._process_payload(payload, source="importlog")
                            processed += 1
                    except Exception:
                        errors += 1
                        log.exception("Failed importing a JSONL record from %s", attachment.filename)
        await ctx.send(f"Replay complete. Payloads examined: **{processed}** • Errors: **{errors}**")

    @staticmethod
    def _extract_payloads_from_log_record(record: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        outer = record.get("payload") or {}
        # Hydrated listener records.
        for key in ("before", "after", "message"):
            value = outer.get(key)
            if isinstance(value, dict):
                yield AdventureTracker._hydrated_log_message_to_payload(value)
        # Raw listener records.
        raw_wrapper = outer.get("payload")
        if isinstance(raw_wrapper, dict):
            data = raw_wrapper.get("data")
            if isinstance(data, dict):
                payload = dict(data)
                payload.setdefault("id", str(raw_wrapper.get("message_id") or ""))
                payload.setdefault("channel_id", str(raw_wrapper.get("channel_id") or ""))
                if raw_wrapper.get("guild_id"):
                    payload.setdefault("guild_id", str(raw_wrapper.get("guild_id")))
                yield payload

    @staticmethod
    def _hydrated_log_message_to_payload(message: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "id": str(message.get("id") or ""),
            "channel_id": str(message.get("channel_id") or ""),
            "guild_id": str(message.get("guild_id") or "") if message.get("guild_id") else None,
            "author": {"id": str((message.get("author") or {}).get("id") or "")},
            "timestamp": message.get("created_at"),
            "edited_timestamp": message.get("edited_at"),
            "webhook_id": str(message.get("webhook_id") or "") if message.get("webhook_id") else None,
            "application_id": str(message.get("application_id") or "") if message.get("application_id") else None,
            "embeds": message.get("embeds") or [],
            "components": message.get("components") or [],
            "content": message.get("content") or "",
        }
        interaction = message.get("interaction_metadata") or message.get("interaction") or {}
        if interaction:
            payload["interaction_metadata"] = interaction
        return payload

    def _leaderboard_page_sections(
        self,
        utc_day: str,
        metric: str,
        rows: Sequence[Dict[str, Any]],
    ) -> List[str]:
        title = "Market Value Profit" if metric == "market" else "Net Value Profit"
        header = f"### Today's Adventure Leaderboard\n-# Global・{utc_day} UTC・{title}"

        if not rows:
            body = "No finalized adventures have been recorded today."
            return [header, body]

        value_key = "profit_market" if metric == "market" else "profit_net"
        lines: List[str] = []
        for index, row in enumerate(rows[:10], 1):
            user_id = int(row.get("user_id") or 0)
            value = self._money(row.get(value_key, 0))
            won = int(row.get("won") or 0)
            lost = int(row.get("lost") or 0)
            lines.append(f"`{index:>2}.` <@{user_id}>・{value}・`{won}W {lost}D`")
        return [header, "\n".join(lines)]

    @staticmethod
    def _money(value: Any) -> str:
        """Format value without a leading plus sign, while retaining losses."""
        number = int(value or 0)
        sign = "- " if number < 0 else ""
        return f"{sign}⏣ {abs(number):,}"

    @staticmethod
    def _profit_emoji(value: Any) -> str:
        number = int(value or 0)
        if number > 0:
            return "<:buff:1521473960620068894>"
        if number < 0:
            return "<:debuff:1521473958246092830>"
        return "<:ReplyCont:1509731665436606625>"

    @staticmethod
    def _signed_money(value: Any) -> str:
        """Signed format retained for ranked leaderboard values."""
        number = int(value or 0)
        return f"{'+' if number >= 0 else '-'}⏣ {abs(number):,}"

    @staticmethod
    def _adventure_emoji(adventure_key: str) -> str:
        return {
            "space": "<:ShootingStar:1507484202982047937>",
            "west": "<:CowBoyHat:1507483886203048048>",
            "halloween": "<:ScaryMask:1507484194782052572>",
            "holiday": "<:ChristmasTree:1507483869346136225>",
            "museum": "<:RobbersWishlist:1507484175492448256>",
            "brazil": "<:caipirinha:1507483845631676479>",
            "vacation": "<a:BluesPlane:1507483809669714153>",
            "fishing": "<:HammerheadShark:1526275588405592134>",
            "down-under": "<:KoalaPet:1526353256937427076>",
        }.get(adventure_key, "<:AdventureTicket:1507483744062406787>")

    @staticmethod
    def _format_duration(seconds: Any) -> str:
        total = max(0, int(seconds or 0))
        days, remainder = divmod(total, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds_left = divmod(remainder, 60)
        parts: List[str] = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}min")
        if seconds_left and not parts:
            parts.append(f"{seconds_left}s")
        return " ".join(parts) if parts else "0m"

    @staticmethod
    def _format_multiplier_strength(multiplier_type: str, strength: Any) -> str:
        value = str(strength or "?").strip()
        if multiplier_type in {"luck", "coin"} and value.endswith("%") and not value.startswith(("+", "-")):
            return f"+{value}"
        return value

    @staticmethod
    def _multiplier_emoji(multiplier_type: str) -> str:
        return {
            "luck": "<:LuckyMultiplier:1526343431608336496>",
            "xp": "<:XPMultiplier:1526343474746757301>",
            "coin": "<:CoinMultiplier:1526343452244049942>",
        }.get(multiplier_type, "<:buff:1521473960620068894>")

    @staticmethod
    def _loss_money(value: Any) -> str:
        number = abs(int(value or 0))
        return f"- ⏣ {number:,}" if number > 0 else "⏣ 0"

    @staticmethod
    def _gain_value(value: Any) -> str:
        number = max(0, int(value or 0))
        money = f"⏣ {number:,}"
        if number > 0:
            return f"<:buff:1521473960620068894> {money}"
        return money

    @staticmethod
    def _cost_value(value: Any) -> str:
        number = abs(int(value or 0))
        if number > 0:
            return f"<:debuff:1521473958246092830> - ⏣ {number:,}"
        return "⏣ 0"

    @staticmethod
    def _adventure_partial_emoji(adventure_key: str) -> discord.PartialEmoji:
        name, emoji_id, animated = {
            "space": ("ShootingStar", 1507484202982047937, False),
            "west": ("CowBoyHat", 1507483886203048048, False),
            "halloween": ("ScaryMask", 1507484194782052572, False),
            "holiday": ("ChristmasTree", 1507483869346136225, False),
            "museum": ("RobbersWishlist", 1507484175492448256, False),
            "brazil": ("caipirinha", 1507483845631676479, False),
            "vacation": ("BluesPlane", 1507483809669714153, True),
            "fishing": ("HammerheadShark", 1526275588405592134, False),
            "down-under": ("KoalaPet", 1526353256937427076, False),
        }.get(adventure_key, ("AdventureTicket", 1507483744062406787, False))
        return discord.PartialEmoji(name=name, id=emoji_id, animated=animated)

    @staticmethod
    def _percent(numerator: Any, denominator: Any) -> str:
        top = int(numerator or 0)
        bottom = int(denominator or 0)
        if bottom <= 0:
            return "0%"
        text = f"{100.0 * top / bottom:.1f}".rstrip("0").rstrip(".")
        return f"{text}%"

    @staticmethod
    def _decimal(value: Any, places: int = 1) -> str:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            number = 0.0
        return f"{number:.{places}f}".rstrip("0").rstrip(".")

    @staticmethod
    def _metric_profit_emoji(value: Any, metric: str) -> str:
        number = int(value or 0)
        if number > 0:
            return "<:buff:1521473960620068894>"
        if number < 0:
            return "<:debuff:1521473958246092830>"
        if metric == "market":
            return "<:MarketThumbnail:1526360219746177076>"
        return "<:Coins:1526353276012990556>"

    @staticmethod
    def _roi_emoji(value: Any) -> str:
        number = int(value or 0)
        if number > 0:
            return "<:largeBuff:1521473959617630279>"
        if number < 0:
            return "<:largeDebuff:1521473955754414281>"
        return "<:ReplyCont:1509731665436606625>"

    def _item_contributor_line(self, item: Dict[str, Any]) -> str:
        item_name = str(item.get("name") or "Unknown Item")
        item_emoji = self.items.global_emoji(item_name)
        quantity = int(item.get("quantity") or 0)
        emoji_prefix = f"{item_emoji} " if item_emoji else ""
        return f"{quantity} {emoji_prefix}{item_name} ({self._money(item.get('net_total', 0))})"

    def _reward_contributor_line(self, reward: Dict[str, Any]) -> str:
        if str(reward.get("kind") or "") == "coins":
            return (
                "<:Coins:1526353276012990556> Coins "
                f"({self._money(reward.get('net_total', 0))})"
            )
        return self._item_contributor_line(reward)

    @staticmethod
    def _ranked_reward_entries(
        item_rewards: Sequence[Dict[str, Any]], coins: Any
    ) -> List[Dict[str, Any]]:
        rewards = [dict(entry) for entry in item_rewards if isinstance(entry, dict)]
        coin_total = max(0, int(coins or 0))
        if coin_total > 0:
            rewards.append(
                {
                    "kind": "coins",
                    "item_key": "coins",
                    "name": "Coins",
                    "quantity": coin_total,
                    "net_total": coin_total,
                    "market_total": coin_total,
                }
            )
        rewards.sort(
            key=lambda entry: (
                -int(entry.get("net_total") or 0),
                0 if str(entry.get("kind") or "") == "coins" else 1,
                str(entry.get("name") or "").casefold(),
            )
        )
        return rewards

    def _loss_contributor_line(self, item: Dict[str, Any]) -> str:
        item_name = str(item.get("name") or "Unknown Item")
        item_emoji = self.items.global_emoji(item_name)
        quantity = int(item.get("quantity") or 0)
        emoji_prefix = f"{item_emoji} " if item_emoji else ""
        return f"- {quantity} {emoji_prefix}{item_name} ({self._money(item.get('net_total', 0))})"

    @staticmethod
    def _branched_rows(entries: Sequence[str]) -> List[str]:
        output: List[str] = []
        for index, entry in enumerate(entries):
            branch = (
                "<:Reply:1509731664249749535>"
                if index == len(entries) - 1
                else "<:ReplyCont:1509731665436606625>"
            )
            output.append(f"-# {branch} {entry}")
        return output

    def _multiplier_lines(self, stats: Dict[str, Any]) -> List[str]:
        multiplier_groups: Dict[str, List[str]] = {}
        for multiplier in stats.get("multipliers", []):
            multiplier_type = str(multiplier.get("multiplier_type") or "unknown")
            strength = self._format_multiplier_strength(multiplier_type, multiplier.get("strength"))
            duration = self._format_duration(multiplier.get("duration_seconds", 0))
            count = int(multiplier.get("count") or 0)
            multiplier_groups.setdefault(multiplier_type, []).append(
                f"{strength} for {duration} `x{count}`"
            )
        return [
            f"{self._multiplier_emoji(multiplier_type)} {', '.join(entries)}"
            for multiplier_type, entries in multiplier_groups.items()
        ]

    @staticmethod
    def _selected_adventure_row(
        stats: Dict[str, Any], selected_adventure: str
    ) -> Optional[Dict[str, Any]]:
        rows = [row for row in stats.get("by_adventure", []) if isinstance(row, dict)]
        for row in rows:
            if str(row.get("adventure_key") or "") == selected_adventure:
                return row
        return rows[0] if rows else None

    def _detail_loss_items(self, row: Dict[str, Any]) -> List[Dict[str, Any]]:
        raw_losses = row.get("loss_items")
        loss_items = [
            dict(entry)
            for entry in (raw_losses if isinstance(raw_losses, list) else [])
            if isinstance(entry, dict)
        ]

        has_ticket = any(str(entry.get("kind") or "") == "ticket_cost" for entry in loss_items)
        ticket_net = int(row.get("ticket_net") or 0)
        if not has_ticket and ticket_net > 0:
            loss_items.append(
                {
                    "kind": "ticket_cost",
                    "item_key": "adventureticket",
                    "name": "Adventure Ticket",
                    "quantity": int(row.get("tickets") or 0),
                    "net_total": ticket_net,
                    "market_total": int(row.get("ticket_market") or 0),
                }
            )

        loss_items.sort(
            key=lambda entry: (
                1 if str(entry.get("kind") or "") == "ticket_cost" else 0,
                -int(entry.get("net_total") or 0),
                str(entry.get("name") or "").casefold(),
            )
        )
        return loss_items

    def _page1_overview_sections(self, stats: Dict[str, Any], display_name: str) -> List[str]:
        s = stats.get("summary") or {}
        completed = int(s.get("completed") or 0)
        won = int(s.get("won") or 0)
        sections = [
            (
                f"### {display_name} Adventure Overview\n"
                f"-# Completed: **{completed}**・Won: **{won}**\n"
                f"-# Win Rate: **{self._percent(won, completed)}**"
            ),
            (
                "-# Net Value\n"
                f"<:Coins:1526353276012990556> Profit: **{self._money(s.get('profit_net', 0))}**\n"
                f"<:ReplyCont:1509731665436606625> Gross Rewards: {self._gain_value(s.get('gross_net', 0))}\n"
                f"<:ReplyCont:1509731665436606625> Adventure Tickets: {self._cost_value(s.get('ticket_net', 0))}\n"
                f"<:Reply:1509731664249749535> Backpack Losses: {self._cost_value(s.get('backpack_net', 0))}\n\n"
                "-# Market Value\n"
                f"<:MarketThumbnail:1526360219746177076> Profit: **{self._money(s.get('profit_market', 0))}**\n"
                f"<:ReplyCont:1509731665436606625> Gross Rewards: {self._gain_value(s.get('gross_market', 0))}\n"
                f"<:ReplyCont:1509731665436606625> Adventure Tickets: {self._cost_value(s.get('ticket_market', 0))}\n"
                f"<:Reply:1509731664249749535> Backpack Losses: {self._cost_value(s.get('backpack_market', 0))}"
            ),
        ]

        multiplier_lines = self._multiplier_lines(stats)
        if multiplier_lines:
            sections.append("-# Multipliers\n" + "\n".join(multiplier_lines))

        sections.append(
            "-# Currencies\n"
            f"<:FishToken:1526274581654933624> {int(s.get('fish_tokens') or 0)} Fish Tokens\n"
            f"<:SkinFragments:1526274599736573982> {int(s.get('skin_fragments') or 0)} Skin Fragments"
        )
        return sections

    def _page2_performance_sections(self, stats: Dict[str, Any], display_name: str) -> List[str]:
        s = stats.get("summary") or {}
        completed = int(s.get("completed") or 0)
        won = int(s.get("won") or 0)
        died = int(s.get("died") or 0)
        sections = [
            (
                f"### {display_name} Adventure Performance\n"
                f"-# Completed: **{completed}**・Won: **{won}**・Died: **{died}**\n"
                "-# Ranked by Net Value Profit"
            )
        ]

        rows = [row for row in stats.get("by_adventure", []) if isinstance(row, dict)]
        if not rows:
            sections.append("No finalized adventures have been recorded today.")
            return sections

        for row in rows:
            played = int(row.get("played") or 0)
            row_won = int(row.get("won") or 0)
            row_died = int(row.get("died") or 0)
            key = str(row.get("adventure_key") or "")
            emoji = self._adventure_emoji(key)
            name = str(row.get("adventure_name") or key or "Unknown Adventure")
            profit_net = int(row.get("profit_net") or 0)
            profit_market = int(row.get("profit_market") or 0)
            sections.append(
                "\n".join(
                    [
                        f"{emoji} **{name}** ({played})",
                        f"-# Won: **{row_won}**・Died: **{row_died}**・Win Rate: **{self._percent(row_won, played)}**",
                        f"{self._metric_profit_emoji(profit_net, 'net')} Net Profit: **{self._money(profit_net)}**",
                        f"-# Gain: {self._money(row.get('gain_net', 0))}・Loss: {self._loss_money(row.get('loss_net', 0))}",
                        f"{self._metric_profit_emoji(profit_market, 'market')} Market Profit: **{self._money(profit_market)}**",
                        f"-# Gain: {self._money(row.get('gain_market', 0))}・Loss: {self._loss_money(row.get('loss_market', 0))}",
                    ]
                )
            )
        return sections

    def _page3_detail_sections(
        self,
        stats: Dict[str, Any],
        selected_adventure: str,
    ) -> List[str]:
        row = self._selected_adventure_row(stats, selected_adventure)
        if row is None:
            return [
                "### Adventure Details\n-# No finalized adventures have been recorded today.",
                "Complete an adventure to populate this page.",
            ]

        played = int(row.get("played") or 0)
        won = int(row.get("won") or 0)
        died = int(row.get("died") or 0)
        key = str(row.get("adventure_key") or "")
        emoji = self._adventure_emoji(key)
        name = str(row.get("adventure_name") or key or "Unknown Adventure")
        max_interactions = int(row.get("max_interactions") or 0)
        avg_interactions = self._decimal(row.get("avg_interactions"), 1)
        average_text = f"{avg_interactions}/{max_interactions}" if max_interactions else avg_interactions

        sections = [
            (
                f"### {emoji} **{name}**\n"
                f"-# Completed: **{played}**・Won: **{won}**・Died: **{died}**\n"
                f"-# Win Rate: **{self._percent(won, played)}**・Average Interactions: **{average_text}**"
            ),
            (
                "-# Net Value\n"
                f"<:Coins:1526353276012990556> Profit: **{self._money(row.get('profit_net', 0))}**\n"
                f"<:ReplyCont:1509731665436606625> Gross Rewards: {self._gain_value(row.get('gain_net', 0))}\n"
                f"<:ReplyCont:1509731665436606625> Adventure Tickets: {self._cost_value(row.get('ticket_net', 0))}\n"
                f"<:Reply:1509731664249749535> Backpack Losses: {self._cost_value(row.get('backpack_net', 0))}\n\n"
                "-# Market Value\n"
                f"<:MarketThumbnail:1526360219746177076> Profit: **{self._money(row.get('profit_market', 0))}**\n"
                f"<:ReplyCont:1509731665436606625> Gross Rewards: {self._gain_value(row.get('gain_market', 0))}\n"
                f"<:ReplyCont:1509731665436606625> Adventure Tickets: {self._cost_value(row.get('ticket_market', 0))}\n"
                f"<:Reply:1509731664249749535> Backpack Losses: {self._cost_value(row.get('backpack_market', 0))}"
            ),
        ]

        detail_parts: List[str] = []
        raw_top_items = row.get("top_items")
        top_items = [
            entry
            for entry in (raw_top_items if isinstance(raw_top_items, list) else [])
            if isinstance(entry, dict)
        ]
        ranked_rewards = self._ranked_reward_entries(
            top_items, row.get("coins_gained", 0)
        )
        reward_lines = self._branched_rows(
            [self._reward_contributor_line(reward) for reward in ranked_rewards[:3]]
        )
        detail_parts.append(
            "-# Top Rewards\n"
            + ("\n".join(reward_lines) if reward_lines else "No retained rewards.")
        )

        loss_items = self._detail_loss_items(row)
        loss_lines = self._branched_rows(
            [self._loss_contributor_line(item) for item in loss_items]
        )
        detail_parts.append(
            "-# Items Lost\n"
            + ("\n".join(loss_lines) if loss_lines else "No items were lost.")
        )

        other_lines: List[str] = []
        fish_tokens = int(row.get("fish_tokens") or 0)
        skin_fragments = int(row.get("skin_fragments") or 0)
        if fish_tokens:
            other_lines.append(f"<:FishToken:1526274581654933624> {fish_tokens} Fish Tokens")
        if skin_fragments:
            other_lines.append(
                f"<:SkinFragments:1526274599736573982> {skin_fragments} Skin Fragments"
            )
        if other_lines:
            detail_parts.append("-# Other Rewards\n" + "\n".join(other_lines))

        sections.append("\n\n".join(detail_parts))
        return sections

    def _page4_rewards_sections(self, stats: Dict[str, Any], display_name: str) -> List[str]:
        s = stats.get("summary") or {}
        gained = [row for row in stats.get("gained", []) if isinstance(row, dict)]
        lost = [row for row in stats.get("lost", []) if isinstance(row, dict)]
        retained_count = sum(int(row.get("quantity") or 0) for row in gained)
        lost_count = sum(int(row.get("quantity") or 0) for row in lost)

        ranked_rewards = self._ranked_reward_entries(gained, s.get("coins", 0))
        reward_lines = self._branched_rows(
            [self._reward_contributor_line(reward) for reward in ranked_rewards[:5]]
        )
        loss_lines = self._branched_rows(
            [self._loss_contributor_line(item) for item in lost[:5]]
        )
        items_earned_market = sum(int(row.get("market_total") or 0) for row in gained)

        return [
            (
                f"### {display_name} Adventure Rewards\n"
                f"-# Items Retained: **{retained_count}**・Items Lost: **{lost_count}**\n"
                "-# Ranked by Net Value"
            ),
            "-# Top Rewards\n"
            + ("\n".join(reward_lines) if reward_lines else "No retained rewards."),
            "-# Backpack Losses\n"
            + ("\n".join(loss_lines) if loss_lines else "No backpack items were lost."),
            (
                "-# Reward Totals\n"
                f"<:Coins:1526353276012990556> Coins Earned: {self._money(s.get('coins', 0))}\n"
                f"<:MarketThumbnail:1526360219746177076> Items Earned: {self._money(items_earned_market)}\n"
                f"<:FishToken:1526274581654933624> {int(s.get('fish_tokens') or 0)} Fish Tokens\n"
                f"<:SkinFragments:1526274599736573982> {int(s.get('skin_fragments') or 0)} Skin Fragments"
            ),
        ]

    def _page5_compass_sections(self, stats: Dict[str, Any]) -> List[str]:
        compass = stats.get("compass") or {}
        used = int(compass.get("used") or 0)
        procs = int(compass.get("procs") or 0)
        components = int(compass.get("components") or 0)
        bonus_net = int(compass.get("bonus_net") or 0)
        bonus_market = int(compass.get("bonus_market") or 0)
        cost_net = int(compass.get("cost_net") or 0)
        cost_market = int(compass.get("cost_market") or 0)
        roi_net = bonus_net - cost_net
        roi_market = bonus_market - cost_market
        return [
            (
                "### <a:Compass:1507483877252534383> Adventure Compass\n"
                f"-# Compasses Used: **{used}**\n"
                f"-# Proc Interactions: **{procs}**・Doubled Rewards: **{components}**"
            ),
            (
                "-# Bonus Value\n"
                f"<:Coins:1526353276012990556> Net: {self._money(bonus_net)}\n"
                f"<:MarketThumbnail:1526360219746177076> Market: {self._money(bonus_market)}\n\n"
                "-# Compass Cost\n"
                f"Net: {self._cost_value(cost_net)}\n"
                f"Market: {self._cost_value(cost_market)}\n\n"
                "-# ROI\n"
                f"{self._roi_emoji(roi_net)} Net: **{self._money(roi_net)}**\n"
                f"{self._roi_emoji(roi_market)} Market: **{self._money(roi_market)}**"
            ),
        ]

    def _stats_page_text_sections(
        self,
        stats: Dict[str, Any],
        display_name: str,
        *,
        page: int,
        selected_adventure: str = "",
    ) -> List[str]:
        index = max(0, min(int(page), 4))
        if index == 0:
            return self._page1_overview_sections(stats, display_name)
        if index == 1:
            return self._page2_performance_sections(stats, display_name)
        if index == 2:
            return self._page3_detail_sections(stats, selected_adventure)
        if index == 3:
            return self._page4_rewards_sections(stats, display_name)
        return self._page5_compass_sections(stats)

    async def _send_v2_sections(self, ctx: commands.Context, sections: Sequence[str]) -> None:
        """Send Components V2 text displays separated by native V2 separators."""
        clean_sections = [str(section).strip() for section in sections if str(section).strip()]
        content_fallback = "\n\n⸻⸻⸻⸻⸻\n\n".join(clean_sections)
        layout_cls = getattr(discord.ui, "LayoutView", None)
        container_cls = getattr(discord.ui, "Container", None)
        text_cls = getattr(discord.ui, "TextDisplay", None)
        separator_cls = getattr(discord.ui, "Separator", None)
        if layout_cls is None or container_cls is None or text_cls is None:
            log.error("discord.py Components V2 classes are unavailable; sending text fallback")
            await ctx.send(content_fallback, allowed_mentions=discord.AllowedMentions.none())
            return

        children: List[Any] = []
        for index, section in enumerate(clean_sections):
            if index and separator_cls is not None:
                children.append(separator_cls())
            children.append(text_cls(section))

        # Older Components V2 builds can send TextDisplay but may not expose
        # Separator yet. In that case retain one V2 container and use a quiet
        # textual divider between sections.
        if separator_cls is None:
            children = [text_cls(content_fallback)]

        view = layout_cls(timeout=None)
        view.add_item(container_cls(*children))
        await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    async def _send_v2_text(self, ctx: commands.Context, content: str) -> None:
        """Compatibility wrapper for callers that only need one text display."""
        await self._send_v2_sections(ctx, [content])
