"""Waifugami — merged card tracking, team building, and spawn listener cog.

This cog is a structural merge of the former ``WaifugamiCards`` and
``WaifugamiListener`` cogs. The merge is intentionally behaviour-preserving:
almost every original function body is unchanged, and every previously
existing command still works exactly as before, under its original name.

What changed:
    * Both cogs now live in one Cog class (``Waifugami``) so they load and
      reload together and can be discovered from a single ``[p]help wg``.
    * Every command is now reachable as a subcommand of the top-level
      ``wg`` group (``[p]wg status``, ``[p]wg watch``, ``[p]wg tieralert``,
      etc.), in addition to its original standalone name/alias, which is
      kept working unchanged.
    * The card-tracking toggle group that used to be the bare ``wg`` command
      (aliases ``wgtrack`` / ``waifugamitrack``) is now primarily reached as
      ``wg track`` (or the unchanged legacy ``wgtrack`` / ``waifugamitrack``).
    * Reply-triggered utility commands (``wgupdate``, ``wgscan``) and the
      right-click "Name" context menu are left exactly as they were, since
      nesting them under ``wg`` would not make them any easier to use.
    * ``/wgcards`` and ``/wgcd`` remain standalone slash commands (Discord's
      slash-command tree is separate from the prefix command tree, so they
      are unaffected by this refactor).

See ``[p]help wg`` for the full merged command list.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import re
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.http import Route
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.data_manager import cog_data_path


log = logging.getLogger("red.nebula.Waifugami")

from .audit import AuditMixin  # noqa: E402  (after constants are defined above)

# ============================================================
# Constants — card tracking (from WaifugamiCards)
# ============================================================
WAIFUGAMI_ID = 722418701852344391
V2_FLAG = 32768
PAGE_SIZE = 5
SCAN_BATCH_SIZE = 5
SCAN_SESSION_TTL = 30 * 60
ARROW_LEFT_ID = "1498458221105643602"
REFRESH_ID = "1498458217477570560"
ARROW_RIGHT_ID = "1498458218668621854"
TEAM_ANALYSE_EMOJI_ID = 1503413262623047811
TEAM_ANALYSE_EMOJI = "<a:nebby_pat:1503413262623047811>"

ELEMENT_OPTIONS = (
    ("pure:fire", "🔥"),
    ("pure:droplet", "💧"),
    ("pure:zap", "⚡"),
    ("pure:star_of_david", "✡️"),
    ("pure:high_brightness", "🔆"),
    ("physical:fire", "⚔️🔥"),
    ("physical:droplet", "⚔️💧"),
    ("physical:zap", "⚔️⚡"),
    ("physical:star_of_david", "⚔️✡️"),
    ("physical:high_brightness", "⚔️🔆"),
)
ELEMENT_LABELS = dict(ELEMENT_OPTIONS)
SORT_OPTIONS = (
    ("local_id", "Local ID"),
    ("type", "Type"),
    ("global_id", "Global ID"),
    ("skill_luck", "Skill + Luck"),
    ("skill", "Skill"),
    ("luck", "Luck"),
)
SORT_LABELS = dict(SORT_OPTIONS)

CARD_LINE_PATTERNS = {
    "owner_id": re.compile(r"Claimed by <@(\d+)>", re.I),
    "local_id": re.compile(r"Local ID:\s*(\d+)", re.I),
    "global_id": re.compile(r"Global ID:\s*(\d+)", re.I),
    "waifu_id": re.compile(r"Waifu ID:\s*(\d+)", re.I),
    "level": re.compile(r"Level:\s*(\d+)", re.I),
    "hp": re.compile(r":heart:\s*HP:\s*(\d+)", re.I),
    "atk": re.compile(r":crossed_swords:\s*ATK:\s*(\d+)", re.I),
    "phr": re.compile(r":shield:\s*PHR:\s*(\d+)%", re.I),
    "mgr": re.compile(r":fleur_de_lis:\s*MGR:\s*(\d+)%", re.I),
    "luck": re.compile(r":four_leaf_clover:\s*Luck:\s*\+?(\d+)", re.I),
    "skill": re.compile(r"Total Skill:\s*([0-9]+(?:\.[0-9]+)?)", re.I),
    "favorite": re.compile(r"Favorite:\s*(\S+)", re.I),
}
TYPE_RE = re.compile(r"Type:\s*([^\n(]+?)\s*\(([^)]+)\)", re.I)
MAG_RE = re.compile(r":([a-zA-Z0-9_]+):\s*MAG:\s*(\d+)", re.I)

CLAIM_RE = re.compile(
    r"Congrats,\s*<@(\d+)>\s+you claimed a\s*\[([^\]]+)]\s*(.+?)!\s*$", re.I
)
EVENT_OPEN_RE = re.compile(
    r"Nice\s*<@(\d+)>!\s*You opened an event chest and got a\s*\*\*\[([^\]]+)]\s*(.+?)\*\*!",
    re.I,
)
ZETA_OPEN_RE = re.compile(
    r"Nice\s*<@(\d+)>!\s*You got a\s*\*\*\[([^\]]+)]\s*(.+?)\*\*!"
    r"(?P<celebration>\s*🎉)?(?:\s*Pity\s*\[(\d+)\s*/\s*(\d+)])?",
    re.I,
)
REMOVE_PROMPT_RE = re.compile(r"Do you really want to remove (.+?)\?", re.I)
TRADE_CONFIRMED_TITLE = "trade confirmed"
TRADE_PARTICIPANT_BLOCK_RE = re.compile(
    r"\*\*(Host|Invited)\s*<@!?(\d+)>\*\*[^\n]*\n(.*?)(?=\*\*(?:Host|Invited)|\Z)",
    re.S | re.I,
)
INFO_NOT_FOUND_RE = re.compile(r"Something went wrong trying to info the character", re.I)
WL_SESSION_TTL = SCAN_SESSION_TTL
WL_PAGE_SIZE = 15  # rows per page in the wishlist V2 view
# Matches lines like "5272 | Kafka ((630) Wishlist)" from a Kazuha orderwl DM dump
KAZUHA_ORDERWL_LINE_RE = re.compile(r"^(\d+)\s*\|\s*(.+?)\s*\(\((\d+)\)\s*Wishlist\)\s*$", re.M)
SKILL_LIST_TITLE_RE = re.compile(r"^.+?'s Waifus \(Page (\d+)\)$", re.I)
SKILL_LIST_ID_RE = re.compile(r"^(\d+)\s*\|", re.M)
VIEW_COMMAND_RE = re.compile(r"\.v\s+((?:\d+\s*){1,5})$", re.I)
SKILL_LIST_ENTRY_RE = re.compile(r"^(\d+)\s*\|\s*(.+?)\s*$", re.M)
DAILY_COMMAND_RE = re.compile(r"\.daily\s*$", re.I)
DAILYGACHA_COMMAND_RE = re.compile(r"\.dailygacha\s*$", re.I)
DAILY_NOT_READY_RE = re.compile(r"Daily is not available right now!", re.I)
DAILYGACHA_NOT_READY_RE = re.compile(r"Dailygacha is not available right now!", re.I)
COOLDOWN_RE = re.compile(r"You still have to wait(?:\s+for)?\s+\*\*(\d+):([0-9]{1,2}):([0-9]{1,2})(?:\s*hours)?\*\*", re.I)
TEAM_COMMAND_RE = re.compile(r"\.team\s+([1-4])\s*$", re.I)
TEAM_MEMBER_RE = re.compile(r"^\s*(.+?)\s*\[(\d+)]\s*$")
EMPTY_POSITION_RE = re.compile(r"This is position\s*([1-4])", re.I)
TEAM_POWER_RE = re.compile(r"Total team power:\s*([0-9]+(?:\.[0-9]+)?)", re.I)
TEAM_MEMBER_EMOJI_RE = re.compile(
    r"^(?:(?:<a?:[A-Za-z0-9_]+:\d+>)|(?::[A-Za-z0-9_]+:))\s*(.*)$"
)
ADVENTURE_BOARD_RE = re.compile(
    r"\*\*Questboard\*\*\s*\n\*\*([A-Za-z0-9]+):\*\*\s*(.+?)\s*\n([^\n]+)",
    re.I,
)
ADVENTURE_REFRESH_RE = re.compile(
    r"\*\*(\d+):(\d{1,2})\*\* minutes left until next quest", re.I
)
ADVENTURE_PROFIT_RE = re.compile(
    r"Total earned profit from adventures\*\*\s*\n(\d+)", re.I
)
ADVENTURE_START_RE = re.compile(r"successfully assigned to the quest!", re.I)
ADVENTURE_OUTCOME_RE = re.compile(
    r"^On turn\s+(\d+)\s+(player|enemy) wins!\s+([A-Za-z0-9]+) quest!$", re.I
)
ADVENTURE_COMMAND_RE = re.compile(r"\.ad\s*$", re.I)
ADVENTURE_SEND_RE = re.compile(r"\.sad\s+([1-4])\s*$", re.I)
ADVENTURE_END_RE = re.compile(r"\.ead\s*$", re.I)
ADVENTURE_PENDING_TTL = 30
ADVENTURE_REMAINING_RE = re.compile(
    r"You still have to wait\s+\*\*(\d+):(\d{1,2}):(\d{1,2})\*\*", re.I
)
TEAM_ADD_RE = re.compile(r"\.teamadd\s+(\d+)\s+([1-4])\s+([1-4])\s*$", re.I)
TEAM_ADD_SUCCESS_RE = re.compile(
    r"managed to add the character to the team", re.I
)
TEAM_MUTATION_TTL = 30

QUEST_RANK_INFO = {
    "F": {"minutes": 10, "dp": 20},
    "E": {"minutes": 60, "dp": 150},
    "C": {"minutes": 120, "dp": 400},
    "B": {"minutes": 180, "dp": 750},
    "A": {"minutes": 240, "dp": 1200},
    "S": {"minutes": 300, "dp": 1750},
    "SS": {"minutes": 360, "dp": 2400},
    "EX": {"minutes": 420, "dp": 3150},
    "LEGENDARY": {"minutes": 480, "dp": 4000},
    "100DAYS": {"minutes": 144000, "dp": 500000},
}
QUEST_WAIT_SECONDS = 15 * 60

QUEST_SYMBOLS = {
    "🔥": "pure:fire", "💧": "pure:droplet", "⚡": "pure:zap",
    "✡": "pure:star_of_david", "🔆": "pure:high_brightness",
    "⚔🔥": "physical:fire", "⚔💧": "physical:droplet",
    "⚔⚡": "physical:zap", "⚔✡": "physical:star_of_david",
    "⚔🔆": "physical:high_brightness",
}

ROLE_INFO = {
    "pure:fire": ("🔥", "Offensive Self Buffer"),
    "pure:droplet": ("💧", "Team Defence Support"),
    "pure:zap": ("⚡", "Turn Support"),
    "pure:star_of_david": ("✡️", "Control and Debuffer"),
    "pure:high_brightness": ("🔆", "Healer and Cleanser"),
    "physical:fire": ("⚔️🔥", "Scaling Physical DPS"),
    "physical:droplet": ("⚔️💧", "Scaling Physical Tank"),
    "physical:zap": ("⚔️⚡", "Critical Physical DPS"),
    "physical:star_of_david": ("⚔️✡️", "Life Steal Physical DPS"),
    "physical:high_brightness": ("⚔️🔆", "Self Cleanser"),
}
PHYSICAL_DPS = "physical_dps"
RECOMMENDED_COMPOSITIONS = (
    (PHYSICAL_DPS, PHYSICAL_DPS, PHYSICAL_DPS, "pure:high_brightness"),
    (PHYSICAL_DPS, PHYSICAL_DPS, "pure:zap", "pure:high_brightness"),
    (PHYSICAL_DPS, PHYSICAL_DPS, "pure:high_brightness", "pure:star_of_david"),
    (PHYSICAL_DPS, "pure:zap", "pure:high_brightness", "pure:star_of_david"),
)
COMPOSITION_NAMES = (
    "Maximum Damage",
    "Damage and Turn Support",
    "Damage and Control",
    "Balanced Support",
)

# ============================================================
# Constants — spawn listener / tier alerts (from WaifugamiListener)
# ============================================================
LINE_ID_NAME_RE = re.compile(r"^\s*(\d+)\s*\|\s*(.+?)\s*$", re.MULTILINE)
SERIES_TITLE_RE = re.compile(r"^\s*\((\d+)\)\s*(.+?)\s*$")
SERIES_ID_IN_TITLE_RE = re.compile(r"\((\d+)\)")
# `.i <id>` character-info embed title, e.g. "(2734) Eizen"
INFO_EMBED_TITLE_RE = re.compile(r"^\((\d+)\)\s+(.+)$")
PAGE_HINT_RE = re.compile(r"`?\s*Page\s+(\d+)\s*(?:of|/)\s*(\d+)\s*`?", re.IGNORECASE)
CLAIMED_BY_RE = re.compile(r"\(Claimed by\s+<@!?(\d+)>\)", re.IGNORECASE)
WAFU_HASH_PRIMARY_RE = re.compile(
    r"/catalog/[a-f0-9]{32}/([a-f0-9]{32})/images/",
    re.IGNORECASE,
)
WAFU_HASH_FALLBACK_RE = re.compile(r"/([a-f0-9]{32})/images/", re.IGNORECASE)

WG_TIER_ALERTS = {
    "zeta": {
        "markers": {"a9321b18df8f7556.png"},
        "label": "Zeta tier spawn detected",
        "color": 12135852,
    },
    "epsilon": {
        "markers": {"4c36c6e97f7a22e5.png"},
        "label": "Epsilon tier spawn detected",
        "color": 6235533,
    },
    "sigma": {
        "markers": {"2f5a061b4088f24b.png"},
        "label": "Sigma tier spawn detected",
        "color": 15293728,
    },
    "fake": {
        "markers": {"faker.gif"},
        "label": "Fake tier spawn detected",
        "color": 10794234,
    },
}

# ============================================================
# Module helpers — card tracking (from WaifugamiCards)
# ============================================================
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def first_match(pattern: re.Pattern, text: str, cast=str):
    match = pattern.search(text)
    return cast(match.group(1)) if match else None


# ============================================================
# Module helpers — spawn listener (from WaifugamiListener)
# ============================================================
def chunk(items: List[Any], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def md5_hash(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()

class WGTrackedView(discord.ui.View):
    def __init__(
        self,
        *,
        invoker_user_id: int,
        pages: List[discord.Embed],
        timeout: int = 180,
    ):
        super().__init__(timeout=timeout)
        self.invoker_user_id = invoker_user_id
        self.pages = pages
        self.page_index = 0
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.first.disabled = self.page_index <= 0
        self.prev.disabled = self.page_index <= 0
        self.next.disabled = self.page_index >= (len(self.pages) - 1)
        self.last.disabled = self.page_index >= (len(self.pages) - 1)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_user_id:
            await interaction.response.send_message("This paginator is not for you.", ephemeral=True)
            return False
        return True

    async def _edit(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page_index], view=self)

    @discord.ui.button(emoji="⏪", style=discord.ButtonStyle.primary)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page_index = 0
        await self._edit(interaction)

    @discord.ui.button(emoji="⬅", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page_index = max(0, self.page_index - 1)
        await self._edit(interaction)

    @discord.ui.button(emoji="➡", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page_index = min(len(self.pages) - 1, self.page_index + 1)
        await self._edit(interaction)

    @discord.ui.button(emoji="⏩", style=discord.ButtonStyle.primary)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page_index = len(self.pages) - 1
        await self._edit(interaction)


class Waifugami(AuditMixin, commands.Cog):
    """Waifugami card tracking, team building, and spawn assistant.

    Run ``[p]help wg`` to see every Waifugami command in one place.

    Feature areas:
      - passive card tracking, team analysis, and the Components V2
        card browser (``/wgcards``, ``wg track``, ``wg cd``)
      - public spawn name assistance and the "Name" message context menu
      - seasonal event role pings and manual event-tier overrides
      - owner/admin catalog updates from ``.sc`` series list replies
      - user completion tracking, watched series, and tier alert DMs
    """

    EVENT_MIN_ID = 75500
    EVENT_MAX_ID = 77400
    EVENT_MIN_DIGITS = 5
    CACHE_CAP = 20000

    DEFAULT_SPAWN_CHANNEL_IDS = [
        1497616138518138918,
        1277381307420381236,
        1177357195822780497,
    ]
    DEFAULT_WAIFUGAMI_BOT_ID = 722418701852344391
    DEFAULT_EVENT_ROLE_ID = 1497616131567915153

    # ------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------

    def __init__(self, bot: Red):
        self.bot = bot

        # ---- card tracking config & state (from WaifugamiCards) ----
        self.config = Config.get_conf(self, identifier=915_2026_0807, force_registration=True)
        self.config.register_user(
            enabled=False,
            cards={},
            removed_cards={},
            pending_acquisitions=[],
            collection_max_local=None,
            collection_state="unknown",
            teams={},
            active_team_id=None,
            pending_questboard={},
            pending_dispatch={},
            active_adventure={},
            quest_wait_reminder={},
            adventure_reminder={},
            cooldowns={},
            cooldown_reminders={},
        )
        self._pending_removals: Dict[int, Deque[Dict[str, Any]]] = defaultdict(deque)
        self._removal_prompts: Dict[int, Dict[str, Any]] = {}
        self._locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._browser_elements: Dict[int, set[str]] = {}
        self._browser_sorts: Dict[int, str] = {}
        self._browser_team_mode: set[int] = set()
        self._pending_team_commands: Dict[int, Deque[Dict[str, Any]]] = defaultdict(deque)
        self._pending_team_adds: Dict[int, Deque[Dict[str, Any]]] = defaultdict(deque)
        self._team_previews: Dict[int, Dict[str, Any]] = {}
        self._scan_sessions: Dict[int, Dict[str, Any]] = {}
        self._scan_list_owners: Dict[int, int] = {}
        self._pending_ad_commands: Dict[int, Deque[Dict[str, Any]]] = defaultdict(deque)
        self._pending_dispatches: Dict[int, Deque[Dict[str, Any]]] = defaultdict(deque)
        self._pending_end_commands: Dict[int, Deque[Dict[str, Any]]] = defaultdict(deque)
        self._pending_cooldown_commands: Dict[int, Deque[Dict[str, Any]]] = defaultdict(deque)
        self._questboards: Dict[int, Dict[str, Any]] = {}
        self._reminder_tasks: Dict[Tuple[str, int], asyncio.Task] = {}
        self._restore_reminders_task: Optional[asyncio.Task] = None
        # Trade-confirmation dedup (bounded, matches CACHE_CAP idiom)
        self._trade_confirmed_seen: Deque[int] = deque(maxlen=200)
        # WL learn sessions: user_id -> session dict
        self._active_wl_sessions: Dict[int, Dict[str, Any]] = {}

        # ---- spawn listener config & state (from WaifugamiListener) ----
        base = Path(__file__).parent
        self.base_path = base
        self.map_path = base / "waifu_hash_map.json"
        self.series_catalog_path = base / "series_catalog.json"
        self.waifus_path = base / "waifus.json"
        self.old_lookup_path = base / "waifugami_catalog_lookup.json"
        self.event_overrides_path = base / "wgevent_overrides.json"

        self.image_id_re = re.compile(r"/images/(\d+)(?:/|$)")

        self.new_map: Dict[str, Dict[str, Any]] = {}
        self.old_map: Dict[str, str] = {}
        self.series_catalog: Dict[str, Dict[str, Any]] = {}
        self._series_index: Dict[str, str] = {}
        self._char_index: Dict[str, Dict[str, Any]] = {}

        self.event_overrides: Dict[str, Any] = {"ids": {}, "ranges": []}

        self._event_seen: Dict[int, bool] = {}
        self._announced: Dict[int, str] = {}
        self._tracking_alerted: Dict[int, str] = {}
        self._tier_alerted: Dict[int, str] = {}
        self._msg_order = deque()

        self._active_completion_scans: Dict[int, Dict[str, Any]] = {}
        self._active_series_updates: Dict[int, Dict[str, Any]] = {}

        self.listener_config: Config = Config.get_conf(
            self,
            identifier=421900777,
            force_registration=True,
        )
        self.listener_config.register_global(
            show_series=True,
            debug_channel_id=None,
            spawn_channel_id=None,
            spawn_channel_ids=[],
            waifugami_bot_id=self.DEFAULT_WAIFUGAMI_BOT_ID,
            event_role_id=self.DEFAULT_EVENT_ROLE_ID,
            event_channel_ids=[],
            auto_remove_on_hit=True,
            tracked_users_by_series={},
            series_watchers_by_series={},
            tier_alert_user_ids=[],
            tier_alert_user_ids_by_tier={},
            # ---- split completion/tracking architecture ----
            completion_users_by_series={},
            character_tracked_users_by_series={},
            completion_schema_migrated=False,
            # ---- .i <id> catalog learning ----
            wishlist_by_waifu_id={},
        )
        self.listener_config.register_user(
            wanted_by_series={},
            watched_series=[],
            # ---- split completion/tracking architecture ----
            # completion_missing_by_series: what the user still needs, kept in
            # sync (added AND removed) by `.wgscan`. Independent of...
            completion_missing_by_series={},
            # tracked_characters_by_series: characters the user explicitly
            # asked to be told about via `wgtrackid`, regardless of whether
            # they've since obtained them.
            tracked_characters_by_series={},
        )

        self._name_context_menu = app_commands.ContextMenu(
            name="Name",
            callback=self.context_name,
        )

        # ---- audit engine (AuditMixin) ----
        self._audit_init()

    async def cog_load(self) -> None:
        # card tracking
        self._restore_reminders_task = asyncio.create_task(
            self._restore_persistent_reminders()
        )
        # spawn listener
        self._load_event_overrides()
        self._load_catalogs()
        self.bot.tree.add_command(self._name_context_menu, override=True)
        await self._migrate_completion_tracking_schema()
        log.info("Waifugami cog loaded (card tracking + spawn listener merged)")

    async def _migrate_completion_tracking_schema(self) -> None:
        """One-time migration off the old conflated ``wanted_by_series``.

        Historically, `.wgscan`-derived "still missing" data and explicit
        `wgtrackid` subscriptions were the same field, so we can't tell
        which entries were which. To avoid silently dropping anyone's
        explicit tracking, every existing entry is copied into *both* new
        fields — `completion_missing_by_series` (which the next `.wgscan`
        will correct down to the true state) and `tracked_characters_by_series`
        (which is left alone, exactly matching pre-migration behaviour,
        until the user explicitly untracks something).
        """
        if await self.listener_config.completion_schema_migrated():
            return

        try:
            all_users = await self.listener_config.all_users()
        except Exception:
            log.exception("Waifugami: failed to enumerate users for completion-schema migration")
            all_users = {}

        migrated_users = 0
        for user_id, data in (all_users or {}).items():
            wanted = data.get("wanted_by_series") if isinstance(data, dict) else None
            if not isinstance(wanted, dict) or not wanted:
                continue
            user_conf = self.listener_config.user_from_id(user_id)
            existing_missing = await user_conf.completion_missing_by_series()
            existing_tracked = await user_conf.tracked_characters_by_series()
            if not isinstance(existing_missing, dict):
                existing_missing = {}
            if not isinstance(existing_tracked, dict):
                existing_tracked = {}
            existing_missing.update(wanted)
            existing_tracked.update(wanted)
            await user_conf.completion_missing_by_series.set(existing_missing)
            await user_conf.tracked_characters_by_series.set(existing_tracked)
            migrated_users += 1

        legacy_reverse = await self.listener_config.tracked_users_by_series()
        if isinstance(legacy_reverse, dict) and legacy_reverse:
            await self.listener_config.completion_users_by_series.set(dict(legacy_reverse))
            await self.listener_config.character_tracked_users_by_series.set(dict(legacy_reverse))

        await self.listener_config.completion_schema_migrated.set(True)
        log.info(
            "Waifugami: migrated completion/tracking schema for %d user(s)", migrated_users
        )

    async def cog_unload(self) -> None:
        # card tracking
        if self._restore_reminders_task:
            self._restore_reminders_task.cancel()
        for task in self._reminder_tasks.values():
            task.cancel()
        self._reminder_tasks.clear()
        # spawn listener
        self.bot.tree.remove_command("Name", type=discord.AppCommandType.message)

    # ------------------------------------------------------------
    # Card tracking (formerly WaifugamiCards)
    # ------------------------------------------------------------

    async def _restore_persistent_reminders(self) -> None:
        await self.bot.wait_until_red_ready()
        try:
            users = await self.config.all_users()
            for raw_user_id, data in users.items():
                if not data.get("enabled"):
                    continue
                user_id = int(raw_user_id)
                for kind, field in (
                    ("quest_wait", "quest_wait_reminder"),
                    ("adventure", "adventure_reminder"),
                ):
                    reminder = data.get(field) or {}
                    if reminder.get("deadline") and reminder.get("channel_id"):
                        self._schedule_reminder(kind, user_id, reminder)
                for kind, reminder in (data.get("cooldown_reminders") or {}).items():
                    if reminder.get("deadline") and reminder.get("channel_id"):
                        self._schedule_reminder(kind, user_id, reminder)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Could not restore Waifugami reminder tasks")

    def _cancel_reminder_task(self, kind: str, user_id: int) -> None:
        task = self._reminder_tasks.pop((kind, user_id), None)
        if task and task is not asyncio.current_task():
            task.cancel()

    def _schedule_reminder(
        self, kind: str, user_id: int, reminder: Dict[str, Any]
    ) -> None:
        self._cancel_reminder_task(kind, user_id)
        self._reminder_tasks[(kind, user_id)] = asyncio.create_task(
            self._reminder_worker(kind, user_id, dict(reminder))
        )

    async def _reminder_worker(
        self, kind: str, user_id: int, reminder: Dict[str, Any]
    ) -> None:
        try:
            delay = max(0, int(reminder["deadline"]) - int(time.time()))
            await asyncio.sleep(delay)
            channel = self.bot.get_channel(int(reminder["channel_id"]))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(reminder["channel_id"]))
                except discord.HTTPException:
                    channel = None
            if channel is not None:
                content = {
                    "adventure": f"<@{user_id}>, your adventure has ended.",
                    "quest_wait": f"<@{user_id}>, it is time to run `.ad`.",
                    "daily": f"<@{user_id}>, your `.daily` is ready.",
                    "dailygacha": f"<@{user_id}>, your `.dailygacha` is ready.",
                }.get(kind, f"<@{user_id}>, your Waifugami cooldown is ready.")
                await self._send_reminder_ping(channel, user_id, content)
            user_conf = self.config.user_from_id(user_id)
            if kind == "adventure":
                await user_conf.adventure_reminder.clear()
            elif kind == "quest_wait":
                await user_conf.quest_wait_reminder.clear()
            else:
                async with user_conf.cooldown_reminders() as reminders:
                    reminders.pop(kind, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Waifugami %s reminder failed for user %s", kind, user_id)
        finally:
            key = (kind, user_id)
            if self._reminder_tasks.get(key) is asyncio.current_task():
                self._reminder_tasks.pop(key, None)

    # ---------------------------- Parsing ----------------------------

    def _parse_card_embed(self, embed: discord.Embed) -> Optional[Dict[str, Any]]:
        description = embed.description or ""
        global_id = first_match(CARD_LINE_PATTERNS["global_id"], description, int)
        owner_id = first_match(CARD_LINE_PATTERNS["owner_id"], description, int)
        local_id = first_match(CARD_LINE_PATTERNS["local_id"], description, int)
        if global_id is None or owner_id is None or local_id is None:
            return None

        card: Dict[str, Any] = {
            "global_id": global_id,
            "owner_id": owner_id,
            "local_id": local_id,
            "local_id_source": "observed",
            "name": embed.title or "Unknown character",
            "image_url": embed.image.url if embed.image else None,
            "last_seen": utc_now(),
            "status": "active",
        }
        integer_fields = ("waifu_id", "level", "hp", "atk", "phr", "mgr", "luck")
        for field in integer_fields:
            card[field] = first_match(CARD_LINE_PATTERNS[field], description, int)
        card["skill"] = first_match(CARD_LINE_PATTERNS["skill"], description, float)
        card["favorite"] = first_match(CARD_LINE_PATTERNS["favorite"], description)

        type_match = TYPE_RE.search(description)
        if type_match:
            card["type"] = type_match.group(1).strip()
            card["type_symbol"] = type_match.group(2).strip()
            card["rarity"] = card["type"]
            card["rarity_symbol"] = card["type_symbol"]
        mag_match = MAG_RE.search(description)
        if mag_match:
            card["mag_element"] = mag_match.group(1).lower()
            card["mag"] = int(mag_match.group(2))
        card["combat_subtype"] = (
            "physical"
            if card.get("atk") is not None
            and card.get("mag") is not None
            and card["atk"] > card["mag"]
            else "pure"
        )
        card["element_key"] = self._element_key(card)
        card["skill_luck_score"] = (
            float(card.get("skill") or 0) + int(card.get("luck") or 0)
        )
        return card

    def _parse_acquisition(self, content: str) -> Optional[Dict[str, Any]]:
        match = EVENT_OPEN_RE.search(content)
        if match:
            return self._new_acquisition(match.group(1), match.group(2), match.group(3), "event_chest")

        match = ZETA_OPEN_RE.search(content)
        if match:
            item = self._new_acquisition(match.group(1), match.group(2), match.group(3), "zeta_chest")
            item["event_pity_reward"] = bool(match.group("celebration"))
            item["pity"] = int(match.group(5)) if match.group(5) else None
            item["pity_limit"] = int(match.group(6)) if match.group(6) else None
            return item

        match = CLAIM_RE.search(content)
        if match:
            return self._new_acquisition(match.group(1), match.group(2), match.group(3), "claim")
        return None

    @staticmethod
    def _embed_text(embed: discord.Embed) -> str:
        parts = [embed.title or "", embed.description or ""]
        for field in embed.fields:
            parts.extend((field.name or "", field.value or ""))
        if embed.footer and embed.footer.text:
            parts.append(embed.footer.text)
        return "\n".join(parts)

    def _parse_team_embed(self, embed: discord.Embed) -> Optional[Dict[str, Any]]:
        text = self._embed_text(embed)
        power_match = TEAM_POWER_RE.search(text)
        if not power_match:
            return None
        positions: List[Dict[str, Any]] = []
        occupied: List[Tuple[str, int]] = []
        empty_positions = {int(value) for value in EMPTY_POSITION_RE.findall(text)}
        for raw_line in text.splitlines():
            match = TEAM_MEMBER_RE.match(raw_line.strip())
            if match and not match.group(1).casefold().startswith("team"):
                occupied.append((match.group(1).strip(), int(match.group(2))))
        occupied_iter = iter(occupied)
        for position in range(1, 5):
            if position in empty_positions:
                positions.append({"position": position, "empty": True})
            else:
                member = next(occupied_iter, None)
                if member:
                    raw_name = member[0]
                    emoji_match = TEAM_MEMBER_EMOJI_RE.match(raw_name)
                    character_name = emoji_match.group(1).strip() if emoji_match else raw_name
                    positions.append({
                        "position": position, "empty": False,
                        "name": character_name,
                        "local_id": member[1],
                    })
                else:
                    positions.append({"position": position, "empty": True})
        return {
            "name": (embed.title or "Team").strip(),
            "total_power": float(power_match.group(1)),
            "positions": positions,
            "observed_at": utc_now(),
        }

    @staticmethod
    def _quest_element_key(token: str) -> Optional[str]:
        """Normalize Discord's optional variation selector in quest symbols."""
        return QUEST_SYMBOLS.get(token.replace("\ufe0f", "").strip())

    def _parse_questboard(self, embed: discord.Embed) -> Optional[Dict[str, Any]]:
        description = embed.description or ""
        if (embed.title or "").casefold() != "welcome adventurer":
            return None
        match = ADVENTURE_BOARD_RE.search(description)
        if not match:
            return None
        element_keys = [
            key for key in (
                self._quest_element_key(token) for token in match.group(3).split()
            ) if key
        ]
        if len(element_keys) != 4:
            return None
        refresh = ADVENTURE_REFRESH_RE.search(description)
        rank = match.group(1).upper()
        # NOTE: previously this looked for the literal text "**Team 1-4**
        # is adventuring!", but real team names are whatever the player
        # named them (e.g. "**[⋆˚]** is adventuring!"), so that regex never
        # actually matched and `.ad` would keep offering team-building
        # advice even while a team was already out on an active quest.
        # The presence/absence of "No teams are out on an adventure
        # currently" is the reliable signal, so derive both fields from it.
        teams_out = "No teams are out on an adventure currently" not in description
        return {
            "state": "current_adventure" if teams_out else "new_quest",
            "rank": rank,
            "quest_name": match.group(2).strip(),
            "enemy_elements": element_keys,
            "enemy_symbols": [ELEMENT_LABELS.get(key, "?") for key in element_keys],
            "element_meaning": "enemy_composition",
            "meaning_source": "community_adventure_document",
            "minutes_until_refresh": (
                int(refresh.group(1)) + int(refresh.group(2)) / 60 if refresh else None
            ),
            "displayed_profit_dp": first_match(ADVENTURE_PROFIT_RE, description, int),
            "teams_out": teams_out,
            "documented": QUEST_RANK_INFO.get(rank, {}),
            "observed_at": utc_now(),
        }

    def _parse_adventure_outcome(self, embed: discord.Embed) -> Optional[Dict[str, Any]]:
        match = ADVENTURE_OUTCOME_RE.fullmatch((embed.title or "").strip())
        if not match:
            return None
        description = embed.description or ""
        characters: List[Dict[str, Any]] = []
        blocks = re.split(r"\n\n(?=(?::skull:\s*)?\*\*)", description)
        stat_patterns = {
            "damage_taken": r"Damage taken:\s*(-?\d+)",
            "damage_dealt": r"Damage dealt:\s*(-?\d+)",
            "crits": r"Crits hit:\s*(\d+)",
            "misses": r"Misses:\s*(\d+)",
            "dodges": r"Dodges:\s*(\d+)",
            "buffs_given": r"Buffs given:\s*(\d+)",
            "debuffs_given": r"Debuffs given:\s*(\d+)",
            "ally_healing": r"Heal amount:\s*(\d+)",
            "self_healing": r"Self heal amount:\s*(\d+)",
            "debuffs_cleansed": r"Debuffs cleansed:\s*(\d+)",
            "self_cleanses": r"Cleansed self:\s*(\d+)",
        }
        for block in blocks:
            heading = re.match(
                r"(?P<dead>:skull:\s*)?\*\*(?P<name>.+?)\*\*\s+(?P<symbols>[^\n]+)",
                block.strip(), re.I,
            )
            health = re.search(
                r"Health:\s*\*\*(-?\d+) HP\*\* left of \*\*(\d+) HP\*\*", block, re.I
            )
            if not heading or not health:
                continue
            character: Dict[str, Any] = {
                "name": heading.group("name").strip(),
                "symbols": heading.group("symbols").strip(),
                "ending_hp": int(health.group(1)),
                "maximum_hp": int(health.group(2)),
                "dead": bool(heading.group("dead")) or int(health.group(1)) <= 0,
            }
            for key, pattern in stat_patterns.items():
                value = re.search(pattern, block, re.I)
                if value:
                    character[key] = int(value.group(1))
            characters.append(character)

        xp = {
            name.strip(): int(value)
            for name, value in re.findall(r"^(.+?) gained \*\*(\d+) ExP\*\*!$", description, re.M)
        }
        for character in characters:
            if character["name"] in xp:
                character["xp"] = xp[character["name"]]

        def total(field: str) -> int:
            return sum(int(character.get(field, 0)) for character in characters)

        maximum_hp = total("maximum_hp")
        remaining_hp = sum(max(0, int(character["ending_hp"])) for character in characters)
        winner = match.group(2).casefold()
        return {
            "winner": winner,
            "reported_side": "player" if winner == "player" else "enemy",
            "turns": int(match.group(1)),
            "rank": match.group(3).upper(),
            "characters": characters,
            "observed": {"xp_by_character": xp},
            "derived": {
                "survivors": sum(not character["dead"] for character in characters),
                "remaining_hp": remaining_hp,
                "maximum_hp": maximum_hp,
                "remaining_hp_percent": round(100 * remaining_hp / maximum_hp, 2) if maximum_hp else None,
                "damage_dealt": total("damage_dealt"),
                "damage_taken": total("damage_taken"),
                "ally_healing": total("ally_healing"),
                "self_healing": total("self_healing"),
                "crits": total("crits"),
                "misses": total("misses"),
                "dodges": total("dodges"),
                "xp": sum(xp.values()),
            },
            "observed_at": utc_now(),
        }

    @staticmethod
    def _new_acquisition(owner: str, symbol: str, name: str, source: str) -> Dict[str, Any]:
        return {
            "owner_id": int(owner),
            "name": name.strip(),
            "type_symbol": symbol.strip(),
            "source": source,
            "acquired_at": utc_now(),
            "status": "awaiting_card_details",
        }

    # ---------------------------- Storage ----------------------------

    async def _tracking_enabled(self, user_id: int) -> bool:
        return await self.config.user_from_id(user_id).enabled()

    async def _store_card(self, card: Dict[str, Any]) -> None:
        owner_id = card["owner_id"]
        if not await self._tracking_enabled(owner_id):
            return
        async with self._locks[owner_id]:
            user_conf = self.config.user_from_id(owner_id)
            acquisition = await self._pop_matching_acquisition(owner_id, card)
            if acquisition:
                card["acquisition"] = acquisition
            async with user_conf.cards() as cards:
                key = str(card["global_id"])
                existing = cards.get(key, {})
                history = existing.get("local_id_history", [])
                old_local = existing.get("local_id")
                if old_local is not None and old_local != card["local_id"]:
                    history.append({"local_id": old_local, "ended_at": utc_now()})
                    history = history[-20:]
                merged = {**existing, **card, "local_id_history": history}
                cards[key] = merged

            max_local = await user_conf.collection_max_local()
            if max_local is None or card["local_id"] > max_local:
                await user_conf.collection_max_local.set(card["local_id"])
        log.info(
            "Stored Waifugami card global=%s local=%s owner=%s name=%s",
            card["global_id"], card["local_id"], owner_id, card["name"],
        )

    async def _pop_matching_acquisition(
        self, owner_id: int, card: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        user_conf = self.config.user_from_id(owner_id)
        async with user_conf.pending_acquisitions() as pending:
            candidates = [
                (index, item)
                for index, item in enumerate(pending)
                if item.get("name", "").casefold() == card.get("name", "").casefold()
                and item.get("type_symbol") == card.get("type_symbol")
            ]
            if candidates:
                index, acquisition = candidates[-1]
                pending.pop(index)
                acquisition["status"] = "completed"
                acquisition["completed_at"] = utc_now()
                return acquisition
        return None

    async def _store_acquisition(self, acquisition: Dict[str, Any]) -> None:
        owner_id = acquisition["owner_id"]
        if not await self._tracking_enabled(owner_id):
            return
        user_conf = self.config.user_from_id(owner_id)
        async with user_conf.pending_acquisitions() as pending:
            pending.append(acquisition)
            del pending[:-50]

    async def _apply_removal(self, pending: Dict[str, Any]) -> None:
        user_id = pending["user_id"]
        if not await self._tracking_enabled(user_id):
            return
        removed_local = pending.get("local_id")
        async with self._locks[user_id]:
            user_conf = self.config.user_from_id(user_id)
            removed_record = None
            async with user_conf.cards() as cards:
                if removed_local is None:
                    active = [c for c in cards.values() if c.get("status") == "active" and c.get("local_id") is not None]
                    if active:
                        removed_local = max(c["local_id"] for c in active)
                for key, card in list(cards.items()):
                    local_id = card.get("local_id")
                    if local_id == removed_local and card.get("status") == "active":
                        removed_record = dict(card)
                        removed_record.update({"removed_at": utc_now(), "removal_source": pending["command"]})
                        card["status"] = "removed"
                        card["local_id"] = None
                        card["removed_at"] = removed_record["removed_at"]
                    elif removed_local is not None and local_id is not None and local_id > removed_local:
                        card.setdefault("local_id_history", []).append({"local_id": local_id, "ended_at": utc_now()})
                        card["local_id"] = local_id - 1
                        card["local_id_source"] = "calculated_removal"

            if removed_record:
                async with user_conf.removed_cards() as removed:
                    removed[str(removed_record["global_id"])] = removed_record
            max_local = await user_conf.collection_max_local()
            if max_local is not None:
                await user_conf.collection_max_local.set(max(-1, max_local - 1))
            if removed_local is None:
                await user_conf.collection_state.set("uncertain")
        log.info("Applied Waifugami removal owner=%s local=%s", user_id, removed_local)

    # ---------------------------- Trades ----------------------------

    @staticmethod
    def _parse_trade_confirmed_embed(
        embed: discord.Embed,
    ) -> Optional[Dict[int, List[Dict[str, Any]]]]:
        """Parse a "Trade confirmed" embed into {user_id: [offered items]}.

        Returns None for any title other than "Trade confirmed"
        (pending, abort, refused) so callers never act on non-confirmed states.
        Each item line is "<local_id> | [<rarity>] <name>"; a side that offered
        nothing renders as the literal word "none" and yields an empty list.
        """
        title = (embed.title or "").strip().casefold()
        if title != TRADE_CONFIRMED_TITLE:
            return None

        description = embed.description or ""
        result: Dict[int, List[Dict[str, Any]]] = {}
        for _role, user_id_str, block in TRADE_PARTICIPANT_BLOCK_RE.findall(description):
            user_id = int(user_id_str)
            items: List[Dict[str, Any]] = []
            for local_id_str, raw_entry in SKILL_LIST_ENTRY_RE.findall(block):
                m = re.search(r"\[([^\]]+)\]\s*(.+)$", raw_entry.strip())
                if not m:
                    continue
                items.append({
                    "local_id": int(local_id_str),
                    "rarity_symbol": m.group(1).strip(),
                    "name": m.group(2).strip().strip("*_`~ "),
                })
            result[user_id] = items

        if len(result) < 2:
            return None
        return result

    async def _remove_traded_items(self, user_id: int, items: List[Dict[str, Any]]) -> None:
        """Remove this user's outgoing traded cards via the existing _apply_removal path.

        Cross-checks name+rarity against the stored record before acting.
        Multiple items are processed sequentially in descending local_id order
        so that shift-down from an earlier removal never invalidates a later one.
        """
        if not items:
            return
        if not await self._tracking_enabled(user_id):
            return
        user_conf = self.config.user_from_id(user_id)
        for item in sorted(items, key=lambda e: e["local_id"], reverse=True):
            local_id = item["local_id"]
            cards = await user_conf.cards()
            match = next(
                (c for c in cards.values()
                 if c.get("local_id") == local_id and c.get("status") == "active"),
                None,
            )
            if match is None:
                log.warning(
                    "Trade removal skipped: owner=%s local_id=%s no matching active card",
                    user_id, local_id,
                )
                continue
            stored_name = str(match.get("name") or "").strip().casefold()
            traded_name = item["name"].strip().casefold()
            stored_rarity = str(
                match.get("rarity_symbol") or match.get("type_symbol") or ""
            ).strip().casefold()
            traded_rarity = item["rarity_symbol"].strip().casefold()
            if stored_name != traded_name or (
                stored_rarity and traded_rarity and stored_rarity != traded_rarity
            ):
                log.warning(
                    "Trade removal skipped: owner=%s local_id=%s stored=%r/%r traded=%r/%r mismatch",
                    user_id, local_id,
                    match.get("name"), stored_rarity, item["name"], traded_rarity,
                )
                continue
            await self._apply_removal({"user_id": user_id, "local_id": local_id, "command": "trade"})

    async def _handle_trade_confirmed(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        """React to a Trade pending -> Trade confirmed embed edit.

        Guards on the before->after title transition so the duplicate
        dispatch event (before.title already == "Trade confirmed") is
        a no-op without needing extra state.
        """
        if after.author.id != WAIFUGAMI_ID or not after.embeds:
            return
        before_title = (before.embeds[0].title if before.embeds else "") or ""
        if before_title.strip().casefold() == TRADE_CONFIRMED_TITLE:
            return
        if after.id in self._trade_confirmed_seen:
            return
        parsed = self._parse_trade_confirmed_embed(after.embeds[0])
        if not parsed:
            return
        self._trade_confirmed_seen.append(after.id)
        log.info("Trade confirmed msg=%s participants=%s", after.id, list(parsed.keys()))
        for uid, items in parsed.items():
            await self._remove_traded_items(uid, items)

    # ---------------------------- Adventures ----------------------------

    def _adventure_file(self, user_id: int) -> Path:
        path = cog_data_path(self) / "adventures"
        path.mkdir(parents=True, exist_ok=True)
        return path / f"{user_id}.jsonl"

    async def _append_adventure(self, user_id: int, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        path = self._adventure_file(user_id)

        def write() -> None:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)

        await asyncio.to_thread(write)

    @staticmethod
    def _pop_recent(queue: Deque[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        while queue and time.monotonic() - queue[0]["created"] > ADVENTURE_PENDING_TTL:
            queue.popleft()
        return queue.popleft() if queue else None

    async def _bind_questboard(
        self, message: discord.Message, quest: Dict[str, Any]
    ) -> None:
        pending = self._pop_recent(self._pending_ad_commands[message.channel.id])
        if not pending or not await self._tracking_enabled(pending["user_id"]):
            return
        if quest.get("teams_out"):
            await self.config.user_from_id(pending["user_id"]).pending_questboard.clear()
            log.info(".ad returned current adventure for owner=%s; no new quest guidance", pending["user_id"])
            return
        quest.update({
            "user_id": pending["user_id"],
            "guild_id": message.guild.id,
            "channel_id": message.channel.id,
            "command_message_id": pending["message_id"],
            "board_message_id": message.id,
        })
        self._questboards[message.id] = quest
        await self.config.user_from_id(pending["user_id"]).pending_questboard.set(quest)
        try:
            await message.add_reaction(TEAM_ANALYSE_EMOJI)
        except discord.HTTPException:
            log.exception("Could not add quest analysis reaction to message %s", message.id)
        try:
            await self._send_quest_wait_button(message.channel, pending["user_id"], message.id)
        except discord.HTTPException:
            log.exception("Could not send quest wait button for message %s", message.id)

    async def _queue_adventure_dispatch(
        self, message: discord.Message, team_id: int
    ) -> None:
        user_id = message.author.id
        user_conf = self.config.user_from_id(user_id)
        quest = await user_conf.pending_questboard()
        teams = await user_conf.teams()
        team = teams.get(str(team_id), {})
        resolved, missing = await self._resolve_team(user_id, team) if team else ([], [])
        snapshot = dict(team)
        if team:
            snapshot["positions"] = resolved
            snapshot["missing_local_ids"] = missing
        pending = {
            "user_id": user_id,
            "team_id": team_id,
            "guild_id": message.guild.id,
            "channel_id": message.channel.id,
            "command_message_id": message.id,
            "attempted_at": utc_now(),
            "created": time.monotonic(),
            "quest": quest,
            "team_snapshot": snapshot,
        }
        self._pending_dispatches[message.channel.id].append(pending)
        await user_conf.pending_dispatch.set({key: value for key, value in pending.items() if key != "created"})

    async def _confirm_adventure_start(self, message: discord.Message) -> None:
        pending = self._pop_recent(self._pending_dispatches[message.channel.id])
        if not pending or not await self._tracking_enabled(pending["user_id"]):
            return
        user_conf = self.config.user_from_id(pending["user_id"])
        self._cancel_reminder_task("quest_wait", pending["user_id"])
        await user_conf.quest_wait_reminder.clear()
        record = {key: value for key, value in pending.items() if key != "created"}
        record.update({
            "adventure_id": f"{message.guild.id}:{pending['user_id']}:{message.id}",
            "confirmation_message_id": message.id,
            "confirmed_at": utc_now(),
            "status": "active",
        })
        rank = str((pending.get("quest") or {}).get("rank") or "").upper()
        rank_info = QUEST_RANK_INFO.get(rank)
        if rank_info:
            deadline = int(time.time()) + int(rank_info["minutes"]) * 60
            reminder = {
                "deadline": deadline,
                "channel_id": message.channel.id,
                "guild_id": message.guild.id,
                "source_message_id": message.id,
                "rank": rank,
                "source": "documented_rank_duration",
                "created_at": utc_now(),
            }
            record["reminder"] = reminder
        await user_conf.active_adventure.set(record)
        await user_conf.pending_dispatch.clear()
        if rank_info:
            await user_conf.adventure_reminder.set(reminder)
            self._schedule_reminder("adventure", pending["user_id"], reminder)
            async with user_conf.cooldowns() as cooldowns:
                cooldowns["adventure"] = {
                    "ready_at": deadline,
                    "channel_id": message.channel.id,
                    "source_message_id": message.id,
                    "source": "documented_rank_duration",
                    "observed_at": utc_now(),
                }
            await self._send_channel_v2_text(
                message.channel,
                f"I will remind you <t:{deadline}:R>.",
                message.id,
            )
        log.info(
            "Started Waifugami adventure owner=%s team=%s rank=%s",
            pending["user_id"], pending["team_id"], pending.get("quest", {}).get("rank"),
        )

    async def _complete_adventure(
        self, message: discord.Message, outcome: Dict[str, Any]
    ) -> None:
        pending = self._pop_recent(self._pending_end_commands[message.channel.id])
        if not pending or not await self._tracking_enabled(pending["user_id"]):
            return
        user_id = pending["user_id"]
        user_conf = self.config.user_from_id(user_id)
        active = await user_conf.active_adventure()
        if not active:
            active = {
                "adventure_id": f"historical:{message.guild.id}:{user_id}:{message.id}",
                "guild_id": message.guild.id,
                "channel_id": message.channel.id,
                "user_id": user_id,
                "status": "outcome_only",
            }
        record = dict(active)
        record.update({
            "status": "completed",
            "end_command_message_id": pending["message_id"],
            "outcome_message_id": message.id,
            "completed_at": utc_now(),
            "outcome": outcome,
        })
        await self._append_adventure(user_id, record)
        self._cancel_reminder_task("adventure", user_id)
        await user_conf.adventure_reminder.clear()
        await user_conf.active_adventure.clear()
        async with user_conf.cooldowns() as cooldowns:
            cooldowns.pop("adventure", None)
        log.info(
            "Completed Waifugami adventure owner=%s winner=%s rank=%s turns=%s",
            user_id, outcome["winner"], outcome["rank"], outcome["turns"],
        )

    async def _correct_adventure_reminder(
        self, message: discord.Message, remaining_seconds: int
    ) -> None:
        pending = self._pop_recent(self._pending_end_commands[message.channel.id])
        if not pending or not await self._tracking_enabled(pending["user_id"]):
            return
        user_id = pending["user_id"]
        user_conf = self.config.user_from_id(user_id)
        active = await user_conf.active_adventure()
        if not active:
            # Adventure was started before the bot began tracking (e.g. bot
            # restarted mid-adventure).  Synthesise a minimal record so that
            # .wgcd shows the remaining time instead of "Not on an adventure".
            active = {
                "adventure_id": f"recovered:{message.guild.id}:{user_id}:{message.id}",
                "guild_id": message.guild.id,
                "channel_id": message.channel.id,
                "user_id": user_id,
                "status": "active",
                "source": "recovered_from_ead_response",
                "recovered_at": utc_now(),
            }
        deadline = int(time.time()) + remaining_seconds
        reminder = {
            "deadline": deadline,
            "channel_id": message.channel.id,
            "guild_id": message.guild.id,
            "source_message_id": message.id,
            "rank": str((active.get("quest") or {}).get("rank") or "").upper(),
            "source": "waifugami_remaining_time",
            "remaining_seconds_observed": remaining_seconds,
            "corrected_at": utc_now(),
        }
        active["reminder"] = reminder
        await user_conf.active_adventure.set(active)
        await user_conf.adventure_reminder.set(reminder)
        self._schedule_reminder("adventure", user_id, reminder)
        async with user_conf.cooldowns() as cooldowns:
            cooldowns["adventure"] = {
                "ready_at": deadline,
                "channel_id": message.channel.id,
                "source_message_id": message.id,
                "source": "waifugami_remaining_time",
                "observed_at": utc_now(),
            }
        await self._send_channel_v2_text(
            message.channel,
            f"I will remind you <t:{deadline}:R>.",
            message.id,
        )
        log.info(
            "Corrected Waifugami adventure reminder owner=%s remaining=%s",
            user_id, remaining_seconds,
        )

    async def _resolve_team_add_response(self, message: discord.Message) -> None:
        """Apply a queued team mutation only after Waifugami confirms success."""
        queue = self._pending_team_adds[message.channel.id]
        while queue and time.monotonic() - queue[0]["created"] > TEAM_MUTATION_TTL:
            queue.popleft()
        if not queue:
            return

        pending = queue.popleft()
        if not TEAM_ADD_SUCCESS_RE.search(message.content or ""):
            log.info(
                "Waifugami teamadd was not confirmed owner=%s team=%s position=%s response=%r",
                pending["user_id"], pending["team_id"], pending["position"],
                (message.content or "")[:200],
            )
            return
        if not await self._tracking_enabled(pending["user_id"]):
            return

        user_conf = self.config.user_from_id(pending["user_id"])
        cards = await user_conf.cards()
        card = next(
            (
                item for item in cards.values()
                if item.get("status") == "active"
                and item.get("local_id") is not None
                and int(item.get("local_id")) == pending["local_id"]
            ),
            None,
        )
        async with self._locks[pending["user_id"]]:
            async with user_conf.teams() as teams:
                team = dict(teams.get(str(pending["team_id"])) or {})
                positions = [dict(slot) for slot in team.get("positions", [])]
                by_position = {
                    int(slot.get("position", 0)): slot
                    for slot in positions if slot.get("position") is not None
                }
                slot: Dict[str, Any] = {
                    "position": pending["position"],
                    "empty": False,
                    "local_id": pending["local_id"],
                    "name": card.get("name", "Unknown character") if card else "Unknown character",
                }
                by_position[pending["position"]] = slot
                team.update({
                    "team_id": pending["team_id"],
                    "user_id": pending["user_id"],
                    "positions": [
                        by_position.get(position, {"position": position, "empty": True})
                        for position in range(1, 5)
                    ],
                    "observed_at": utc_now(),
                    "source": "confirmed_teamadd",
                    "last_mutation": {
                        "command_message_id": pending["message_id"],
                        "confirmation_message_id": message.id,
                        "local_id": pending["local_id"],
                        "position": pending["position"],
                        "confirmed_at": utc_now(),
                    },
                })
                known_cards = []
                for stored_slot in team["positions"]:
                    stored = next(
                        (
                            item for item in cards.values()
                            if item.get("status") == "active"
                            and item.get("local_id") is not None
                            and stored_slot.get("local_id") is not None
                            and int(item.get("local_id")) == int(stored_slot.get("local_id"))
                        ),
                        None,
                    )
                    if stored:
                        known_cards.append(stored)
                if len(known_cards) == 4 and all(card.get("skill") is not None for card in known_cards):
                    team["total_power"] = sum(float(card["skill"]) for card in known_cards)
                    team["power_source"] = "calculated_from_confirmed_teamadd"
                else:
                    team["total_power"] = None
                    team["power_source"] = "unknown_after_confirmed_teamadd"
                teams[str(pending["team_id"])] = team

        for preview_id, preview in list(self._team_previews.items()):
            if (
                int(preview.get("user_id", 0)) == pending["user_id"]
                and int(preview.get("team_id", 0)) == pending["team_id"]
            ):
                refreshed = dict(team)
                refreshed["message_id"] = preview_id
                self._team_previews[preview_id] = refreshed

        await user_conf.active_team_id.set(pending["team_id"])
        log.info(
            "Applied confirmed Waifugami teamadd owner=%s team=%s position=%s local=%s",
            pending["user_id"], pending["team_id"], pending["position"], pending["local_id"],
        )

    # ---------------------------- Cooldowns ----------------------------

    @staticmethod
    def _cooldown_seconds_from_text(text: str) -> Optional[int]:
        match = COOLDOWN_RE.search(text)
        if not match:
            return None
        return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))

    def _pop_cooldown_command(self, channel_id: int, kind: str) -> Optional[Dict[str, Any]]:
        queue = self._pending_cooldown_commands[channel_id]
        now = time.monotonic()
        while queue and now - queue[0]["created"] > ADVENTURE_PENDING_TTL:
            queue.popleft()
        for index, pending in enumerate(queue):
            if pending.get("kind") == kind:
                queue.rotate(-index)
                item = queue.popleft()
                queue.rotate(index)
                return item
        return None

    async def _set_cooldown(
        self, user_id: int, kind: str, ready_at: int, channel_id: int,
        source: str, source_message_id: int,
    ) -> None:
        user_conf = self.config.user_from_id(user_id)
        record = {
            "ready_at": int(ready_at),
            "channel_id": int(channel_id),
            "source_message_id": int(source_message_id),
            "source": source,
            "observed_at": utc_now(),
        }
        async with user_conf.cooldowns() as cooldowns:
            cooldowns[kind] = record
        if ready_at > int(time.time()):
            reminder = {
                "deadline": int(ready_at),
                "channel_id": int(channel_id),
                "source_message_id": int(source_message_id),
                "created_at": utc_now(),
            }
            async with user_conf.cooldown_reminders() as reminders:
                reminders[kind] = reminder
            self._schedule_reminder(kind, user_id, reminder)
        else:
            async with user_conf.cooldown_reminders() as reminders:
                reminders.pop(kind, None)
            self._cancel_reminder_task(kind, user_id)

    async def _observe_cooldown_response(self, message: discord.Message) -> None:
        embeds = message.embeds
        if not embeds:
            return
        for embed in embeds:
            title = (embed.title or "").strip()
            description = embed.description or ""
            text = f"{title}\n{description}"
            kind: Optional[str] = None
            if DAILY_NOT_READY_RE.search(title):
                kind = "daily"
            elif DAILYGACHA_NOT_READY_RE.search(title):
                kind = "dailygacha"
            if kind is None:
                if title.casefold() == "your daily prayers have been answered!":
                    kind = "daily"
                elif title.casefold() == "character gacha" and any(
                    (field.name or "").casefold() == "roll type" and (field.value or "").casefold() == "daily"
                    for field in embed.fields
                ):
                    kind = "dailygacha"
            if kind is None:
                continue
            pending = self._pop_cooldown_command(message.channel.id, kind)
            if not pending or not await self._tracking_enabled(pending["user_id"]):
                continue
            remaining = self._cooldown_seconds_from_text(text)
            if remaining is not None:
                ready_at = int(time.time()) + remaining
                source = "waifugami_remaining_time"
            else:
                fallback = 24 * 3600
                ready_at = int(time.time()) + fallback
                source = "documented_cooldown_fallback"
            await self._set_cooldown(
                pending["user_id"], kind, ready_at, message.channel.id, source, message.id
            )
            log.info("Updated Waifugami %s cooldown owner=%s ready_at=%s source=%s", kind, pending["user_id"], ready_at, source)

    @app_commands.command(name="wgcd", description="Show your Waifugami cooldowns")
    async def wgcd_slash(self, interaction: discord.Interaction) -> None:
        if not await self._tracking_enabled(interaction.user.id):
            await interaction.response.send_message("Enable tracking first with `/wg track enable` or `..wgtrack enable`.", ephemeral=True)
            return
        await self._respond_v2_text(interaction, await self._cooldown_text(interaction.user.id))

    @commands.command(name="wgcd")
    async def wgcd_prefix(self, ctx: commands.Context) -> None:
        if not await self._tracking_enabled(ctx.author.id):
            await self._send_v2_text(ctx, f"Enable tracking first with `{ctx.clean_prefix}wgtrack enable`.")
            return
        await self._send_v2_text(ctx, await self._cooldown_text(ctx.author.id))

    async def _cooldown_text(self, user_id: int) -> str:
        user_conf = self.config.user_from_id(user_id)
        cooldowns = await user_conf.cooldowns()
        active_adventure = await user_conf.active_adventure()
        now = int(time.time())
        lines = ["### COOLDOWNS"]
        for kind, label in (("adventure", ".ead       "), ("daily", ".daily     "), ("dailygacha", ".dailygacha")):
            record = cooldowns.get(kind) or {}
            ready_at = int(record.get("ready_at") or 0)
            if kind == "adventure" and not active_adventure:
                # No team is currently out on an adventure at all, distinct from
                # "an adventure just finished and is ready to be collected".
                status = "-# Not on an adventure"
            elif not ready_at or ready_at <= now:
                status = "**Ready**"
            else:
                status = f"<t:{ready_at}:R>"
            lines.append(f"`{label}` {status}")
        return "\n".join(lines)

    # ---------------------------- Listeners ----------------------------

    @commands.Cog.listener(name="on_message")
    async def on_message_cards(self, message: discord.Message) -> None:
        if not message.guild:
            return

        if not message.author.bot:
            list_trigger = (message.content or "").strip().casefold()
            if list_trigger in {"list", "list v", "list new"}:
                if await self._try_list_action_from_reply(message, list_trigger):
                    return
            await self._observe_user_command(message)
            return
        if message.author.id != WAIFUGAMI_ID:
            return

        # .i <id> embeds may arrive in card-tracking channels that are NOT
        # configured as spawn channels, so the spawn listener's _learn_from_info_embed
        # call would never see them. Learn here too; the function is idempotent.
        await self._learn_from_info_embed(message)

        if self._pending_team_adds[message.channel.id]:
            await self._resolve_team_add_response(message)

        if ADVENTURE_START_RE.search(message.content or ""):
            await self._confirm_adventure_start(message)

        await self._observe_cooldown_response(message)

        for embed in message.embeds:
            quest = self._parse_questboard(embed)
            if quest:
                await self._bind_questboard(message, quest)
            remaining = ADVENTURE_REMAINING_RE.search(embed.description or "")
            if remaining:
                remaining_seconds = (
                    int(remaining.group(1)) * 3600
                    + int(remaining.group(2)) * 60
                    + int(remaining.group(3))
                )
                await self._correct_adventure_reminder(message, remaining_seconds)
            outcome = self._parse_adventure_outcome(embed)
            if outcome:
                await self._complete_adventure(message, outcome)

        observed_cards: List[Dict[str, Any]] = []
        for embed in message.embeds:
            card = self._parse_card_embed(embed)
            if card:
                observed_cards.append(card)
                card["observation"] = {
                    "guild_id": message.guild.id,
                    "channel_id": message.channel.id,
                    "message_id": message.id,
                    "observed_at": card["last_seen"],
                }
                await self._store_card(card)

            team = self._parse_team_embed(embed)
            if team:
                await self._bind_team_preview(message, team)

        if observed_cards:
            await self._confirm_scan_batch(message, observed_cards)

        acquisition = self._parse_acquisition(message.content or "")
        if acquisition:
            await self._store_acquisition(acquisition)

        await self._bind_removal_prompt(message)

    async def _observe_user_command(self, message: discord.Message) -> None:
        content = (message.content or "").strip()
        team_add_match = TEAM_ADD_RE.fullmatch(content)
        if team_add_match and await self._tracking_enabled(message.author.id):
            queue = self._pending_team_adds[message.channel.id]
            queue.append({
                "user_id": message.author.id,
                "local_id": int(team_add_match.group(1)),
                "team_id": int(team_add_match.group(2)),
                "position": int(team_add_match.group(3)),
                "message_id": message.id,
                "created": time.monotonic(),
            })
            while len(queue) > 20:
                queue.popleft()
            return

        if DAILY_COMMAND_RE.fullmatch(content) and await self._tracking_enabled(message.author.id):
            queue = self._pending_cooldown_commands[message.channel.id]
            queue.append({"user_id": message.author.id, "kind": "daily", "message_id": message.id, "created": time.monotonic()})
            return

        if DAILYGACHA_COMMAND_RE.fullmatch(content) and await self._tracking_enabled(message.author.id):
            queue = self._pending_cooldown_commands[message.channel.id]
            queue.append({"user_id": message.author.id, "kind": "dailygacha", "message_id": message.id, "created": time.monotonic()})
            return

        if ADVENTURE_COMMAND_RE.fullmatch(content) and await self._tracking_enabled(message.author.id):
            queue = self._pending_ad_commands[message.channel.id]
            queue.append({
                "user_id": message.author.id,
                "message_id": message.id,
                "created": time.monotonic(),
            })
            return

        dispatch_match = ADVENTURE_SEND_RE.fullmatch(content)
        if dispatch_match and await self._tracking_enabled(message.author.id):
            await self._queue_adventure_dispatch(message, int(dispatch_match.group(1)))
            return

        if ADVENTURE_END_RE.fullmatch(content) and await self._tracking_enabled(message.author.id):
            self._pending_end_commands[message.channel.id].append({
                "user_id": message.author.id,
                "message_id": message.id,
                "created": time.monotonic(),
            })
            return

        view_match = VIEW_COMMAND_RE.fullmatch(content)
        if view_match:
            await self._lock_scan_from_view(
                message, [int(value) for value in view_match.group(1).split()]
            )
            return

        team_match = TEAM_COMMAND_RE.fullmatch(content)
        if team_match and await self._tracking_enabled(message.author.id):
            queue = self._pending_team_commands[message.channel.id]
            queue.append({
                "user_id": message.author.id,
                "team_id": int(team_match.group(1)),
                "created": time.monotonic(),
            })
            while len(queue) > 10:
                queue.popleft()
            return
        match = re.fullmatch(r"\.rm\s+(\d+)", content, re.I)
        if match and await self._tracking_enabled(message.author.id):
            self._pending_removals[message.channel.id].append({
                "user_id": message.author.id,
                "local_id": int(match.group(1)),
                "command": "rm",
                "created": time.monotonic(),
            })
            return
        if re.fullmatch(r"\.rml", content, re.I) and await self._tracking_enabled(message.author.id):
            self._pending_removals[message.channel.id].append({
                "user_id": message.author.id,
                "local_id": None,
                "command": "rml",
                "created": time.monotonic(),
            })

    async def _bind_team_preview(self, message: discord.Message, team: Dict[str, Any]) -> None:
        queue = self._pending_team_commands[message.channel.id]
        while queue and time.monotonic() - queue[0]["created"] > 30:
            queue.popleft()
        if not queue:
            return
        pending = queue.popleft()
        team.update({
            "team_id": pending["team_id"],
            "user_id": pending["user_id"],
            "message_id": message.id,
            "channel_id": message.channel.id,
        })
        self._team_previews[message.id] = team
        user_conf = self.config.user_from_id(pending["user_id"])
        async with user_conf.teams() as teams:
            teams[str(team["team_id"])] = team
        try:
            await message.add_reaction(TEAM_ANALYSE_EMOJI)
        except discord.HTTPException:
            log.exception("Could not add the team analysis reaction to message %s", message.id)
    async def _bind_removal_prompt(self, message: discord.Message) -> None:
        if not message.embeds or not self._pending_removals[message.channel.id]:
            return
        title = message.embeds[0].title or ""
        match = REMOVE_PROMPT_RE.search(title)
        if not match:
            return
        queue = self._pending_removals[message.channel.id]
        while queue and time.monotonic() - queue[0]["created"] > 30:
            queue.popleft()
        if not queue:
            return
        pending = queue.popleft()
        pending["character_name"] = match.group(1)
        pending["prompt_message_id"] = message.id
        self._removal_prompts[message.id] = pending

    @commands.Cog.listener(name="on_message_edit")
    async def on_message_edit_cards(self, before: discord.Message, after: discord.Message) -> None:
        if after.author.id != WAIFUGAMI_ID:
            return

        # ---- audit engine: consume .l -event all pages ----
        if await self.audit_on_message_edit(before, after):
            return
        # ---- end audit hook ----

        if after.id in self._scan_list_owners:
            await self._collect_scan_page(after)

        if after.id in self._removal_prompts and after.embeds:
            title = after.embeds[0].title or ""
            if title.casefold() == "character successfully removed!":
                pending = self._removal_prompts.pop(after.id)
                await self._apply_removal(pending)
            elif "cancel" in title.casefold():
                self._removal_prompts.pop(after.id, None)

        await self._handle_trade_confirmed(before, after)

    # ---------------------------- Guided list scanning ----------------------------

    @staticmethod
    def _parse_skill_list(embed: discord.Embed) -> Optional[List[int]]:
        if not SKILL_LIST_TITLE_RE.fullmatch(embed.title or ""):
            return None
        local_ids = [int(value) for value in SKILL_LIST_ID_RE.findall(embed.description or "")]
        return local_ids or None

    @staticmethod
    def _scan_batches(local_ids: List[int]) -> List[List[int]]:
        return [
            local_ids[index:index + SCAN_BATCH_SIZE]
            for index in range(0, len(local_ids), SCAN_BATCH_SIZE)
        ]

    async def _try_list_action_from_reply(
        self, message: discord.Message, trigger: str
    ) -> bool:
        reference = message.reference
        if not reference or not reference.message_id:
            return False
        list_message = reference.resolved
        if not isinstance(list_message, discord.Message):
            try:
                list_message = await message.channel.fetch_message(reference.message_id)
            except discord.HTTPException:
                return False
        if list_message.author.id != WAIFUGAMI_ID or not list_message.embeds:
            return False
        if not self._parse_skill_list(list_message.embeds[0]):
            return False
        if trigger == "list":
            await self._send_favorite_list(message, list_message)
            return True
        if trigger == "list new":
            if not await self._tracking_enabled(message.author.id):
                await message.reply("Run `..wgtrack enable` first.", mention_author=False)
                return True
            await self._start_scan_session(message, list_message, mode="new")
            return True
        if not await self._tracking_enabled(message.author.id):
            await message.reply("Run `..wgtrack enable` first.", mention_author=False)
            return True
        await self._start_scan_session(message, list_message)
        return True

    async def _new_ids_from_list(
        self, user_id: int, embed: discord.Embed
    ) -> List[int]:
        """Return local IDs whose character name + rarity is not known."""
        entries = SKILL_LIST_ENTRY_RE.findall(embed.description or "")
        if not entries:
            return []

        user_conf = self.config.user_from_id(user_id)
        cards = await user_conf.cards()
        removed = await user_conf.removed_cards()

        def identity_from_card(card: Dict[str, Any]) -> Optional[str]:
            name = str(card.get("name") or "").strip().casefold()
            rarity_symbol = str(
                card.get("rarity_symbol") or card.get("type_symbol") or ""
            ).strip().casefold()
            if not name or not rarity_symbol:
                return None
            return f"{name}|{rarity_symbol}"

        known_identities = {
            identity
            for card in list(cards.values()) + list(removed.values())
            for identity in [identity_from_card(card)]
            if identity
        }

        result: List[int] = []
        seen_identities: set[str] = set()
        for raw_id, raw_entry in entries:
            # List entries look like:
            #   🔮 [δ] Osakabehime
            # The favorite emoji is irrelevant; name + rarity is the identity.
            rarity_match = re.search(r"\[([^\]]+)\]\s*(.+)$", raw_entry.strip())
            if not rarity_match:
                continue
            rarity_symbol = rarity_match.group(1).strip().casefold()
            name = rarity_match.group(2).strip().strip("*_`~ ")
            if not name or not rarity_symbol:
                continue

            identity = f"{name.casefold()}|{rarity_symbol}"
            if identity in known_identities or identity in seen_identities:
                continue
            seen_identities.add(identity)
            result.append(int(raw_id))
        return result

    async def _send_favorite_list(
        self, trigger: discord.Message, list_message: discord.Message
    ) -> None:
        local_ids = self._parse_skill_list(list_message.embeds[0])
        if not local_ids:
            return
        command = ".fav " + " ".join(map(str, local_ids)) + " "
        await trigger.reply(f"`{command}`", mention_author=False)

    async def _start_scan_session(
        self, trigger: discord.Message, list_message: discord.Message,
        *, mode: str = "all"
    ) -> None:
        if list_message.author.id != WAIFUGAMI_ID or not list_message.embeds:
            return
        local_ids = self._parse_skill_list(list_message.embeds[0])
        if not local_ids:
            return

        if mode == "new":
            local_ids = await self._new_ids_from_list(
                trigger.author.id, list_message.embeds[0]
            )

        self._finish_scan_session(trigger.author.id)
        response = await trigger.reply("Preparing the list...", mention_author=False)
        session = {
            "user_id": trigger.author.id,
            "channel_id": trigger.channel.id,
            "list_message_id": list_message.id,
            "guide_message_id": response.id,
            "seen_ids": list(dict.fromkeys(local_ids)),
            "remaining_ids": list(dict.fromkeys(local_ids)),
            "phase": "collecting",
            "pending_ids": [],
            "created": time.monotonic(),
            "mode": mode,
        }
        self._scan_sessions[trigger.author.id] = session
        self._scan_list_owners[list_message.id] = trigger.author.id
        await self._render_scan_session(session)

    async def _collect_scan_page(self, message: discord.Message) -> None:
        user_id = self._scan_list_owners.get(message.id)
        session = self._scan_sessions.get(user_id) if user_id is not None else None
        if not session or session.get("phase") != "collecting" or not message.embeds:
            return
        if time.monotonic() - session["created"] > SCAN_SESSION_TTL:
            self._finish_scan_session(user_id)
            return
        local_ids = self._parse_skill_list(message.embeds[0])
        if not local_ids:
            return

        if session.get("mode") == "new":
            local_ids = await self._new_ids_from_list(
                user_id, message.embeds[0]
            )

        changed = False
        for local_id in local_ids:
            if local_id not in session["seen_ids"]:
                session["seen_ids"].append(local_id)
                session["remaining_ids"].append(local_id)
                changed = True
        if changed:
            await self._render_scan_session(session)

    async def _lock_scan_from_view(
        self, message: discord.Message, sent_ids: List[int]
    ) -> None:
        session = self._scan_sessions.get(message.author.id)
        if not session or session.get("channel_id") != message.channel.id:
            return
        if time.monotonic() - session["created"] > SCAN_SESSION_TTL:
            self._finish_scan_session(message.author.id)
            return
        batches = self._scan_batches(session["remaining_ids"])
        if session["phase"] == "collecting":
            if sent_ids not in batches:
                return
            session["phase"] = "confirming"
        elif session["phase"] == "guided":
            if not batches or sent_ids != batches[0]:
                return
            session["phase"] = "confirming"
        else:
            return
        session["pending_ids"] = list(sent_ids)
        session["command_message_id"] = message.id
        session["command_sent_at"] = time.monotonic()

    async def _confirm_scan_batch(
        self, message: discord.Message, observed_cards: List[Dict[str, Any]]
    ) -> None:
        owners = {int(card["owner_id"]) for card in observed_cards}
        if len(owners) != 1:
            return
        user_id = next(iter(owners))
        session = self._scan_sessions.get(user_id)
        if not session or session.get("phase") != "confirming":
            return
        if session.get("channel_id") != message.channel.id:
            return
        sent_at = session.get("command_sent_at")
        if not sent_at or time.monotonic() - sent_at > 60:
            return
        expected = set(session.get("pending_ids", []))
        observed = {int(card["local_id"]) for card in observed_cards}
        if not expected or not expected.issubset(observed):
            return

        session["remaining_ids"] = [
            local_id for local_id in session["remaining_ids"] if local_id not in expected
        ]
        session["pending_ids"] = []
        session["phase"] = "guided"
        if session["remaining_ids"]:
            await self._send_next_scan_message(session)
        else:
            await self._send_next_scan_message(session, complete=True)
            self._finish_scan_session(user_id)

    async def _send_next_scan_message(
        self, session: Dict[str, Any], *, complete: bool = False
    ) -> None:
        channel = self.bot.get_channel(session["channel_id"])
        if channel is None:
            return
        if complete:
            content = "Perfect, I have them."
        else:
            batches = self._scan_batches(session["remaining_ids"])
            batch = batches[0] if batches else []
            content = (
                f"`.v {' '.join(map(str, batch))}`"
                if batch else "Perfect, I have them."
            )
        try:
            guide = await channel.send(content)
        except discord.HTTPException:
            log.exception(
                "Could not send the next Waifugami scan guide for user %s",
                session["user_id"],
            )
            return
        session["guide_message_id"] = guide.id

    async def _render_scan_session(
        self, session: Dict[str, Any]
    ) -> None:
        channel = self.bot.get_channel(session["channel_id"])
        if channel is None:
            return
        batches = self._scan_batches(session["remaining_ids"])
        if session.get("mode") == "new":
            if batches:
                content = f"`.v {' '.join(map(str, batches[0]))}`"
            else:
                content = "No new cards found on the pages scanned yet. Flip to another page."
        else:
            content = "\n".join(
                f"`.v {' '.join(map(str, batch))}`" for batch in batches
            )
            if not content:
                content = "Flip to another page."

        try:
            guide = await channel.fetch_message(session["guide_message_id"])
            await guide.edit(content=content)
        except discord.HTTPException:
            self._finish_scan_session(session["user_id"])

    def _finish_scan_session(self, user_id: int) -> None:
        session = self._scan_sessions.pop(user_id, None)
        if session:
            self._scan_list_owners.pop(session.get("list_message_id"), None)

    # ---------------------------- WL learn session ----------------------------

    async def _wl_remaining(
        self, filter_series_id: Optional[str] = None
    ) -> List[Tuple[str, str]]:
        """Return [(waifu_id, name), ...] for catalog chars with no stored Wishlist."""
        store = await self.listener_config.wishlist_by_waifu_id()
        if not isinstance(store, dict):
            store = {}
        result: List[Tuple[str, str]] = []
        for cid, entry in self._char_index.items():
            if not isinstance(entry, dict):
                continue
            if filter_series_id is not None:
                sid = str(int(entry.get("series_id") or 0))
                if sid != filter_series_id:
                    continue
            if cid not in store:
                name = str(entry.get("name") or f"ID {cid}")
                result.append((cid, name))
        result.sort(key=lambda t: int(t[0]))
        return result

    async def _start_wl_session(
        self, ctx: commands.Context, filter_series_id: Optional[str] = None
    ) -> None:
        """Start or restart a WL learn session for ctx.author."""
        user_id = ctx.author.id
        remaining = await self._wl_remaining(filter_series_id)
        if not remaining:
            label = f"series {filter_series_id}" if filter_series_id else "the catalog"
            await ctx.reply(f"All Wishlist values for {label} are already learned! Nothing to do.")
            return

        store = await self.listener_config.wishlist_by_waifu_id()
        total_catalog = len([c for c in self._char_index if filter_series_id is None or
                              str(int((self._char_index[c] or {}).get("series_id") or 0)) == filter_series_id])
        already = total_catalog - len(remaining)

        session: Dict[str, Any] = {
            "user_id": user_id,
            "channel_id": ctx.channel.id,
            "filter_series_id": filter_series_id,
            "queue": [(cid, name) for cid, name in remaining],
            "total": len(remaining),
            "done": 0,
            "created": time.monotonic(),
            "guide_message_id": None,
        }
        self._active_wl_sessions[user_id] = session

        if filter_series_id is not None:
            series_name = self._series_index.get(filter_series_id, f"Series {filter_series_id}")
            series_label = f"({filter_series_id}) {series_name}"
        else:
            series_label = "All Series"
        header = (
            f"Series Session Started\n"
            f"-# {series_label}\n"
            f"-# Known: {already}\n"
            f"-# Remaining: {len(remaining)}\n"
            f"-# Type `cancel` to stop.\n"
            f"⸻\n"
        )
        guide = await ctx.reply(header + self._wl_next_prompt(session), mention_author=False)
        session["guide_message_id"] = guide.id

    def _wl_next_prompt(self, session: Dict[str, Any]) -> str:
        """Format the next `.i <id>` instruction line."""
        if not session["queue"]:
            return ""
        cid, name = session["queue"][0]
        done = session["done"]
        total = session["total"]
        current = done + 1
        percent = int(current / total * 100) if total else 0
        char_entry = self._char_index.get(str(cid)) or {}
        sid = str(int(char_entry.get("series_id") or 0)) if char_entry.get("series_id") is not None else None
        if sid is not None:
            series_name = self._series_index.get(sid, f"Series {sid}")
            series_label = f"({sid}) {series_name}"
        else:
            series_label = f"({cid})"
        return f"-# {percent}%・{current}/{total}・{name}・{series_label}\n`.i {cid}`"

    async def _wl_advance(self, user_id: int, *, learned: bool, waifu_id: Optional[str] = None) -> None:
        """Advance the WL session for user_id after one .i result arrives.

        learned=True  → the embed was captured and Wishlist stored.
        learned=False → Waifugami returned "Something went wrong" (catalog gap
                        we queued because _char_index had it, but Waifugami
                        disagrees; skip silently).
        """
        session = self._active_wl_sessions.get(user_id)
        if not session:
            return
        if time.monotonic() - session["created"] > WL_SESSION_TTL:
            self._active_wl_sessions.pop(user_id, None)
            return

        queue = session["queue"]
        if not queue:
            return

        # Only advance if the id that just came back matches what we asked for.
        expected_id = queue[0][0]
        if waifu_id is not None and str(waifu_id) != str(expected_id):
            return  # irrelevant .i embed; don't advance

        queue.pop(0)
        session["done"] += 1

        channel = self.bot.get_channel(session["channel_id"])
        if channel is None:
            self._active_wl_sessions.pop(user_id, None)
            return

        if not queue:
            # Session complete
            total = session["total"]
            self._active_wl_sessions.pop(user_id, None)
            try:
                await channel.send(
                    f"✅ **Wishlist learning complete!** Processed {total} characters."
                )
            except discord.HTTPException:
                pass
            return

        # Send the next prompt as a new message (cheap, keeps a clear history).
        try:
            await channel.send(self._wl_next_prompt(session))
        except discord.HTTPException:
            self._active_wl_sessions.pop(user_id, None)

    def _finish_wl_session(self, user_id: int) -> None:
        self._active_wl_sessions.pop(user_id, None)

    @staticmethod
    def _quest_pressure(quest: Dict[str, Any]) -> Dict[str, Any]:
        elements = list(quest.get("enemy_elements") or [])
        physical = sum(key.startswith("physical:") for key in elements)
        return {
            "physical": physical,
            "magical": len(elements) - physical,
            "control": sum(key.endswith(":star_of_david") for key in elements),
            "healing": sum(key.endswith(":high_brightness") for key in elements),
            "turn_support": sum(key == "pure:zap" for key in elements),
            "defence": sum(key.endswith(":droplet") for key in elements),
        }

    def _quest_card_score(
        self, card: Dict[str, Any], pressure: Dict[str, Any], rank: str
    ) -> float:
        """Score documented matchup qualities without pretending to simulate combat."""
        physical_ratio = pressure["physical"] / 4
        magical_ratio = pressure["magical"] / 4
        rank_order = ("F", "E", "C", "B", "A", "S", "SS", "EX", "LEGENDARY", "100DAYS")
        rank_index = rank_order.index(rank) if rank in rank_order else 0
        key = self._element_key(card)
        score = float(card.get("skill") or 0)
        score += int(card.get("luck") or 0) * (0.65 + rank_index * 0.06)
        score += float(card.get("phr") or 0) * physical_ratio * 0.08
        score += float(card.get("mgr") or 0) * magical_ratio * 0.08
        score += min(int(card.get("level") or 0), 100) * 0.015
        if pressure["control"]:
            if key == "pure:high_brightness":
                score += 5.0
            elif key == "physical:high_brightness":
                score += 3.0
        if pressure["healing"] and key in {
            "physical:fire", "physical:zap", "physical:star_of_david", "pure:fire"
        }:
            score += 1.5
        if pressure["turn_support"] and key in {"pure:droplet", "pure:high_brightness"}:
            score += 1.5
        return score

    async def _quest_history_summary(
        self, user_id: int, quest: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        path = self._adventure_file(user_id)
        if not path.exists():
            return None
        wanted = self._quest_pressure(quest)

        def read() -> List[Dict[str, Any]]:
            records: List[Dict[str, Any]] = []
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                return []
            return records[-500:]

        comparable = []
        for record in await asyncio.to_thread(read):
            recorded_quest = record.get("quest") or {}
            if recorded_quest.get("rank") != quest.get("rank"):
                continue
            observed = self._quest_pressure(recorded_quest)
            if (observed["physical"], observed["magical"]) == (
                wanted["physical"], wanted["magical"]
            ):
                comparable.append(record)
        if len(comparable) < 5:
            return None
        outcomes = [record.get("outcome") or {} for record in comparable]
        turns = [int(outcome["turns"]) for outcome in outcomes if outcome.get("turns") is not None]
        survivors = [
            int((outcome.get("derived") or {})["survivors"])
            for outcome in outcomes
            if outcome.get("reported_side") == "player"
            and (outcome.get("derived") or {}).get("survivors") is not None
        ]
        return {
            "count": len(outcomes),
            "wins": sum(outcome.get("winner") == "player" for outcome in outcomes),
            "average_turns": sum(turns) / len(turns) if turns else None,
            "average_survivors": sum(survivors) / len(survivors) if survivors else None,
        }

    async def _quest_recommendation_components(
        self, user_id: int, quest: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        cards = [
            card for card in await self._active_cards(user_id)
            if card.get("local_id") is not None and self._element_key(card) in ROLE_INFO
        ]
        pressure = self._quest_pressure(quest)
        rank = str(quest.get("rank") or "?").upper()
        pools: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for card in cards:
            pools[self._composition_key(self._element_key(card))].append(card)
        for pool in pools.values():
            pool.sort(
                key=lambda card: (
                    -self._quest_card_score(card, pressure, rank),
                    -float(card.get("skill") or 0),
                    -int(card.get("luck") or 0),
                )
            )

        builds: List[Tuple[float, int, List[Dict[str, Any]]]] = []
        for index, composition in enumerate(RECOMMENDED_COMPOSITIONS):
            needed = Counter(composition)
            if any(len(pools.get(role, [])) < count for role, count in needed.items()):
                continue
            option_groups = [
                list(itertools.combinations(pools[role][:12], count))
                for role, count in needed.items()
            ]
            for selected_groups in itertools.product(*option_groups):
                selected = [card for group in selected_groups for card in group]
                score = sum(self._quest_card_score(card, pressure, rank) for card in selected)
                builds.append((score, index, selected))

        enemy_symbols = "  ".join(quest.get("enemy_symbols") or [])
        sections = [
            f"## {rank} Quest Matchup\n"
            f"**Enemy team:** {enemy_symbols}\n"
            f"-# Physical pressure: {pressure['physical']}・Magic pressure: {pressure['magical']}"
        ]
        if not builds:
            sections.append(
                "### No Complete Recommended Team Yet\n"
                "I cannot build a quest-specific team from the cards I have learned.\n\n"
                "Run `.l -orderby skill` and continue the guided scan."
            )
            return self._section_components(sections)

        _, composition_index, chosen = max(builds, key=lambda item: item[0])
        active_team_id = await self.config.user_from_id(user_id).active_team_id()
        target_team_id = int(active_team_id or 1)
        teams = await self.config.user_from_id(user_id).teams()
        target_team = teams.get(str(target_team_id), {
            "team_id": target_team_id,
            "positions": [{"position": value, "empty": True} for value in range(1, 5)],
        })
        positions, _ = await self._resolve_team(user_id, target_team)
        assignment = self._assign_recommended_cards(positions, chosen)
        total_power = sum(float(card.get("skill") or 0) for card in chosen)
        total_luck = sum(int(card.get("luck") or 0) for card in chosen)
        lines = [
            f"### Recommended Build・{COMPOSITION_NAMES[composition_index]}",
            self._quiet_composition_role_lines(RECOMMENDED_COMPOSITIONS[composition_index]),
        ]
        for position, card in assignment:
            symbol = ROLE_INFO[self._element_key(card)][0]
            lines.append(
                f"{position}. **{card.get('name', 'Unknown')}**・`{card.get('local_id')}`\n"
                f"-# `{symbol}`・Skill {self._format_skill(card.get('skill'))}・"
                f"Luck {card.get('luck', '?')}・PHR {card.get('phr', '?')}%・MGR {card.get('mgr', '?')}%"
            )
        lines.append(
            f"\n**Power:** {self._format_skill(total_power)}・**Combined Luck:** {total_luck}"
        )
        sections.append("\n".join(lines))

        reasons = []
        reasons.append("PHR is weighted more heavily." if pressure["physical"] > pressure["magical"] else
                       "MGR is weighted more heavily." if pressure["magical"] > pressure["physical"] else
                       "PHR and MGR are weighted evenly.")
        if pressure["control"]:
            reasons.append("Enemy control increases the value of cleansing.")
        if pressure["healing"]:
            reasons.append("Enemy healing increases the value of sustained damage.")
        if pressure["turn_support"]:
            reasons.append("Enemy extra turns increase the value of survival support.")
        reasons.append("Pure ⚡ utility is counted once because extra-turn support does not stack.")
        sections.append("### Why This Build\n" + "\n".join(f"- {reason}" for reason in reasons))

        current_ids = {
            int(slot["position"]): int(slot["card"]["global_id"])
            for slot in positions if slot.get("card") and slot["card"].get("global_id") is not None
        }
        changes = [
            f"`.teamadd {card.get('local_id')} {target_team_id} {position}`"
            for position, card in assignment
            if card.get("global_id") is None or current_ids.get(position) != int(card.get("global_id"))
        ]
        if changes:
            sections.append(
                f"### Changes For Team {target_team_id}\n" + "\n".join(f"> {change}" for change in changes)
            )
        else:
            sections.append(f"### Team {target_team_id} already matches this recommendation.")

        history = await self._quest_history_summary(user_id, quest)
        if history:
            average_turns = (
                f"{history['average_turns']:.1f}" if history["average_turns"] is not None else "?"
            )
            average_survivors = (
                f"{history['average_survivors']:.1f}"
                if history["average_survivors"] is not None else "?"
            )
            sections.append(
                f"### Similar Recorded Quests\n"
                f"**Samples:** {history['count']}・**Win rate:** {100 * history['wins'] / history['count']:.1f}%\n"
                f"-# Average turns: {average_turns}・"
                f"Average player survivors after wins: {average_survivors}"
            )
        else:
            sections.append("-# Historical matchup statistics appear after 5 comparable completed quests.")
        return self._section_components(sections)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Search the owner's learned collection for the best team build."""
        if payload.user_id == self.bot.user.id or payload.emoji.id != TEAM_ANALYSE_EMOJI_ID:
            return
        team = self._team_previews.get(payload.message_id)
        if not await self._tracking_enabled(payload.user_id):
            return
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except discord.HTTPException:
                return
        if team is not None and int(team.get("user_id", 0)) == payload.user_id:
            await self.config.user_from_id(payload.user_id).active_team_id.set(team["team_id"])
            components = await self._team_analysis_components(payload.user_id, team)
        else:
            quest = self._questboards.get(payload.message_id)
            if quest is None:
                stored = await self.config.user_from_id(payload.user_id).pending_questboard()
                if int(stored.get("board_message_id", 0)) == payload.message_id:
                    quest = stored
            if quest is None or int(quest.get("user_id", 0)) != payload.user_id:
                return
            components = await self._quest_recommendation_components(payload.user_id, quest)
        await self._send_channel_v2_components(channel, components, payload.message_id)

        try:
            message = await channel.fetch_message(payload.message_id)
            member = payload.member or channel.guild.get_member(payload.user_id)
            if member:
                await message.remove_reaction(payload.emoji, member)
        except (discord.HTTPException, AttributeError):
            pass

    # ---------------------------- Commands ----------------------------

    @commands.group(name="wgtrack", aliases=["waifugamitrack"])
    async def card_tracking_group(self, ctx: commands.Context) -> None:
        """Configure Waifugami card tracking."""
        if ctx.invoked_subcommand is None:
            enabled = await self.config.user(ctx.author).enabled()
            cards = await self.config.user(ctx.author).cards()
            active = sum(1 for card in cards.values() if card.get("status") == "active")
            await ctx.send(f"Waifugami tracking is **{'enabled' if enabled else 'disabled'}**. I have {active:,} active cards stored.")

    @card_tracking_group.command(name="enable")
    async def wgtrack_enable(self, ctx: commands.Context) -> None:
        await self.config.user(ctx.author).enabled.set(True)
        await ctx.send("Now run `.l -orderby skill`")

    @card_tracking_group.command(name="disable")
    async def wgtrack_disable(self, ctx: commands.Context) -> None:
        user_conf = self.config.user(ctx.author)
        await user_conf.enabled.set(False)
        await user_conf.quest_wait_reminder.clear()
        await user_conf.adventure_reminder.clear()
        await user_conf.cooldown_reminders.clear()
        self._cancel_reminder_task("quest_wait", ctx.author.id)
        self._cancel_reminder_task("adventure", ctx.author.id)
        for kind in ("daily", "dailygacha"):
            self._cancel_reminder_task(kind, ctx.author.id)
        self._finish_scan_session(ctx.author.id)
        self._clear_adventure_queues(ctx.author.id)
        await ctx.send("Waifugami tracking disabled. Your stored data was kept and pending reminders were cancelled.")

    @card_tracking_group.command(name="clear")
    async def wgtrack_clear(self, ctx: commands.Context) -> None:
        """Delete all of your stored Waifugami tracking data."""
        await self.config.user(ctx.author).clear()
        self._cancel_reminder_task("quest_wait", ctx.author.id)
        self._cancel_reminder_task("adventure", ctx.author.id)
        for kind in ("daily", "dailygacha"):
            self._cancel_reminder_task(kind, ctx.author.id)
        self._finish_scan_session(ctx.author.id)
        self._clear_adventure_queues(ctx.author.id)
        path = self._adventure_file(ctx.author.id)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError:
            log.exception("Could not delete adventure history for user %s", ctx.author.id)
        await ctx.send("Your stored Waifugami card and adventure data has been cleared.")

    def _clear_adventure_queues(self, user_id: int) -> None:
        for queues in (
            self._pending_ad_commands,
            self._pending_dispatches,
            self._pending_end_commands,
            self._pending_team_adds,
            self._pending_cooldown_commands,
        ):
            for channel_id, queue in list(queues.items()):
                queues[channel_id] = deque(
                    item for item in queue if int(item.get("user_id", 0)) != user_id
                )
        for message_id, quest in list(self._questboards.items()):
            if int(quest.get("user_id", 0)) == user_id:
                self._questboards.pop(message_id, None)

    @app_commands.command(name="wgcards", description="Open your Waifugami card browser")
    async def wgcards(self, interaction: discord.Interaction) -> None:
        """Open the Components V2 browser as a visible channel response."""
        if not await self.config.user(interaction.user).enabled():
            await interaction.response.send_message(
                "Enable tracking first with your bot prefix followed by `wgtrack enable` (or `wg track enable`).",
                ephemeral=True,
            )
            return
        await self._send_browser(interaction, page=0)

    async def _send_v2_text(self, ctx: commands.Context, content: str) -> None:
        payload = {
            "flags": V2_FLAG,
            "allowed_mentions": {"parse": []},
            "components": [{"type": 17, "components": [{"type": 10, "content": content}]},],
        }
        if ctx.message.reference and ctx.message.reference.message_id:
            payload["message_reference"] = {
                "message_id": str(ctx.message.reference.message_id),
                "channel_id": str(ctx.channel.id),
                "guild_id": str(ctx.guild.id),
                "fail_if_not_exists": False,
            }
        route = Route("POST", "/channels/{channel_id}/messages", channel_id=ctx.channel.id)
        await self.bot.http.request(route, json=payload)

    async def _send_channel_v2_text(
        self, channel: Any, content: str, reference_message_id: Optional[int] = None
    ) -> None:
        await self._send_channel_v2_components(
            channel, [{"type": 10, "content": content}], reference_message_id
        )

    async def _send_channel_v2_components(
        self,
        channel: Any,
        components: List[Dict[str, Any]],
        reference_message_id: Optional[int] = None,
    ) -> Any:
        if not components:
            components = [{
                "type": 10,
                "content": "Nothing to display.",
            }]

        # Discord's Components V2 total component limit is 40.
        # The type 17 container counts as one, so it can contain at most
        # 39 child components.
        MAX_CHILD_COMPONENTS = 39

        chunks = [
            components[i:i + MAX_CHILD_COMPONENTS]
            for i in range(0, len(components), MAX_CHILD_COMPONENTS)
        ]

        log.debug(
            "Sending V2 components: %s total child components, %s messages required",
            len(components),
            len(chunks),
        )

        route = Route(
            "POST",
            "/channels/{channel_id}/messages",
            channel_id=channel.id,
        )

        responses = []

        for index, chunk in enumerate(chunks):
            payload: Dict[str, Any] = {
                "flags": V2_FLAG,
                "allowed_mentions": {"parse": []},
                "components": [{
                    "type": 17,
                    "components": chunk,
                }],
            }

            if reference_message_id:
                payload["message_reference"] = {
                    "message_id": str(reference_message_id),
                    "channel_id": str(channel.id),
                    "guild_id": str(channel.guild.id),
                    "fail_if_not_exists": False,
                }

            response = await self.bot.http.request(route, json=payload)
            responses.append(response)

        return responses[-1] if responses else None

    async def _send_quest_wait_button(
        self, channel: Any, user_id: int, board_message_id: int
    ) -> Any:
        payload: Dict[str, Any] = {
            "flags": V2_FLAG,
            "allowed_mentions": {"parse": []},
            "components": [{
                "type": 1,
                "components": [{
                    "type": 2,
                    "style": 2,
                    "label": "Remind in 15 min",
                    "emoji": {"name": "🔔"},
                    "custom_id": f"nebwg:questwait:{user_id}:{board_message_id}",
                }],
            }],
            "message_reference": {
                "message_id": str(board_message_id),
                "channel_id": str(channel.id),
                "guild_id": str(channel.guild.id),
                "fail_if_not_exists": False,
            },
        }
        route = Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id)
        return await self.bot.http.request(route, json=payload)

    async def _send_reminder_ping(
        self, channel: Any, user_id: int, content: str
    ) -> Any:
        payload = {
            "flags": V2_FLAG,
            "allowed_mentions": {"users": [str(user_id)]},
            "components": [{"type": 17, "components": [{"type": 10, "content": content}]}],
        }
        route = Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id)
        return await self.bot.http.request(route, json=payload)

    async def _confirm_quest_wait_reminder(
        self, interaction: discord.Interaction, user_id: int, board_message_id: int
    ) -> None:
        deadline = int(time.time()) + QUEST_WAIT_SECONDS
        reminder = {
            "deadline": deadline,
            "channel_id": interaction.channel_id,
            "guild_id": interaction.guild_id,
            "board_message_id": board_message_id,
            "confirmation_message_id": interaction.message.id,
            "source": "questboard_button",
            "created_at": utc_now(),
        }
        await self.config.user_from_id(user_id).quest_wait_reminder.set(reminder)
        self._schedule_reminder("quest_wait", user_id, reminder)
        payload = {
            "allowed_mentions": {"parse": []},
            "components": [{
                "type": 17,
                "components": [{
                    "type": 10,
                    "content": f"I will remind you to run `.ad` <t:{deadline}:R>.",
                }],
            }],
        }
        route = Route(
            "POST",
            "/interactions/{interaction_id}/{interaction_token}/callback",
            interaction_id=interaction.id,
            interaction_token=interaction.token,
        )
        await self.bot.http.request(route, json={"type": 7, "data": payload})

    async def _respond_v2_text(
        self, interaction: discord.Interaction, content: str, ephemeral: bool = False
    ) -> None:
        payload = {
            "flags": V2_FLAG | (64 if ephemeral else 0),
            "allowed_mentions": {"parse": []},
            "components": [{"type": 17, "components": [{"type": 10, "content": content}]},],
        }
        route = Route(
            "POST",
            "/interactions/{interaction_id}/{interaction_token}/callback",
            interaction_id=interaction.id,
            interaction_token=interaction.token,
        )
        await self.bot.http.request(route, json={"type": 4, "data": payload})

    @staticmethod
    def _composition_key(element_key: str) -> str:
        if element_key in {"physical:fire", "physical:zap", "physical:star_of_david"}:
            return PHYSICAL_DPS
        return element_key

    @staticmethod
    def _counter_distance(left: Iterable[str], right: Iterable[str]) -> int:
        a, b = Counter(left), Counter(right)
        return sum((a - b).values()) + sum((b - a).values())

    async def _resolve_team(self, user_id: int, team: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[int]]:
        cards = await self._active_cards(user_id)
        by_local = {card.get("local_id"): card for card in cards}
        resolved, missing = [], []
        for slot in team.get("positions", []):
            item = dict(slot)
            if not item.get("empty"):
                card = by_local.get(item.get("local_id"))
                if card:
                    item["card"] = card
                else:
                    missing.append(int(item["local_id"]))
            resolved.append(item)
        return resolved, missing

    async def _team_analysis_components(
        self, user_id: int, team: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        positions, missing = await self._resolve_team(user_id, team)
        lines = ["## Team Overview"]
        known_keys: List[str] = []
        for slot in positions:
            number = slot["position"]
            if slot.get("empty"):
                lines.append(f"{number}. **Empty**")
                continue
            member_name = self._team_member_name(slot)
            card = slot.get("card")
            if not card:
                lines.append(f"{number}. **{member_name}**・`{slot['local_id']}`・Card details not learned yet")
                continue
            key = self._element_key(card)
            known_keys.append(self._composition_key(key))
            symbol, role = ROLE_INFO.get(key, ("❔", "Unknown role"))
            if key == "pure:high_brightness":
                role = "Healer / Cleanser"
            lines.append(
                f"{number}. **{member_name}**・`{slot['local_id']}`・`{symbol}`・{role}"
            )
        lines.append(f"**Total Power:** {self._format_skill(team.get('total_power'))}")
        sections = ["\n".join(lines)]
        if missing:
            ids = " ".join(str(value) for value in missing)
            sections.append(
                f"### More Card Details Needed\n"
                f"Run `.v {ids}`, then react with {TEAM_ANALYSE_EMOJI} again."
            )
            return self._section_components(sections)
        occupied_count = sum(not slot.get("empty") for slot in positions)
        if occupied_count < 4:
            sections.append("-# Empty positions will be filled before any current card is replaced.")
        current_analysis: Optional[str] = None
        if len(known_keys) == 4:
            exact_index = next((i for i, comp in enumerate(RECOMMENDED_COMPOSITIONS) if Counter(comp) == Counter(known_keys)), None)
            if exact_index is not None:
                current_analysis = (
                    f"### Your Current Team Already Works\n"
                    f"-# **{COMPOSITION_NAMES[exact_index]}**\n"
                    f"{self._quiet_composition_role_lines(RECOMMENDED_COMPOSITIONS[exact_index], healer_slash=True)}\n"
                    f"-# {self._composition_explanation(exact_index)}"
                )
            else:
                distances = [self._counter_distance(known_keys, comp) for comp in RECOMMENDED_COMPOSITIONS]
                closest = min(range(len(distances)), key=distances.__getitem__)
                difference = self._composition_difference_plain(
                    known_keys, RECOMMENDED_COMPOSITIONS[closest]
                )
                current_analysis = (
                    f"### What Your Current Team Is Missing\n"
                    f"-# The closest recommended setup is **{COMPOSITION_NAMES[closest]}**:\n"
                    f"{self._quiet_composition_role_lines(RECOMMENDED_COMPOSITIONS[closest])}\n"
                    f"{difference}"
                )
        recommendation = await self._collection_recommendation_sections(user_id, team, positions)
        if current_analysis and recommendation:
            recommendation.insert(1, "__SOFT_DIVIDER__")
            recommendation.insert(2, current_analysis)
        elif current_analysis:
            recommendation.append(current_analysis)
        sections.extend(recommendation)
        return self._section_components(sections)

    async def _collection_recommendation_sections(
        self, user_id: int, team: Dict[str, Any], positions: List[Dict[str, Any]]
    ) -> List[str]:
        """Build every documented composition from all fully learned active cards."""
        cards = [
            card for card in await self._active_cards(user_id)
            if card.get("local_id") is not None and self._element_key(card) in ROLE_INFO
        ]
        pools: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for card in cards:
            pools[self._composition_key(self._element_key(card))].append(card)
        for pool in pools.values():
            pool.sort(key=lambda card: (
                -float(card.get("skill") or 0),
                -int(card.get("luck") or 0),
                int(card.get("local_id") or 0),
            ))

        builds: List[Tuple[float, int, int, List[Dict[str, Any]]]] = []
        for index, composition in enumerate(RECOMMENDED_COMPOSITIONS):
            needed = Counter(composition)
            if any(len(pools.get(role, [])) < count for role, count in needed.items()):
                continue
            chosen: List[Dict[str, Any]] = []
            for role, count in needed.items():
                chosen.extend(pools[role][:count])
            builds.append((
                sum(float(card.get("skill") or 0) for card in chosen),
                sum(int(card.get("luck") or 0) for card in chosen),
                index,
                chosen,
            ))

        if not builds:
            available = Counter(self._composition_key(self._element_key(card)) for card in cards)
            closest = min(
                range(len(RECOMMENDED_COMPOSITIONS)),
                key=lambda index: self._counter_distance(
                    available.elements(), RECOMMENDED_COMPOSITIONS[index]
                ),
            )
            missing = list((Counter(RECOMMENDED_COMPOSITIONS[closest]) - available).elements())
            result = ["### No Complete Recommended Team Yet\nI cannot build one from the cards I have learned."]
            if missing:
                result[0] += f"\n\n**Still needed**\n{self._composition_role_lines(missing)}"
            result.append(
                "Run `.l -orderby skill`."
            )
            return result

        power, luck, index, chosen = max(builds, key=lambda item: (item[0], item[1]))
        assignment = self._assign_recommended_cards(positions, chosen)
        current_ids = {
            int(slot["position"]): int(slot["card"]["global_id"])
            for slot in positions
            if slot.get("card") and slot["card"].get("global_id") is not None
        }
        current_power = float(team.get("total_power") or 0)
        power_change = power - current_power
        result = [
            f"### Best Team From Your Known Cards\n"
            f"-# **{COMPOSITION_NAMES[index]}**\n"
            f"{self._quiet_composition_role_lines(RECOMMENDED_COMPOSITIONS[index])}\n"
            f"-# {self._composition_explanation(index)}"
        ]
        card_lines = []
        for position, card in assignment:
            key = self._element_key(card)
            symbol, role = ROLE_INFO[key]
            compact_role = {
                "physical:fire": "Physical DPS",
                "physical:zap": "Physical DPS",
                "pure:high_brightness": "Healer/Cleanser",
            }.get(key, role)
            card_lines.append(
                f"{position}. **{card.get('name', 'Unknown')}**・`{card.get('local_id')}`\n"
                f"-# `{symbol}`・**Skill:** {self._format_skill(card.get('skill'))}・"
                f"**Luck:** {card.get('luck', '?')}・{compact_role}"
            )
        card_lines.extend([
            f"\n**Power:** {self._format_skill(power)}・{power_change:+.2f} from current",
            f"**Combined Luck:** {luck}",
        ])
        result.append("\n".join(card_lines))
        changes = [
            f"`.teamadd {card.get('local_id')} {team['team_id']} {position}`"
            for position, card in assignment
            if card.get("global_id") is None or current_ids.get(position) != int(card.get("global_id"))
        ]
        if changes:
            result.append(
                f"### Changes To Make\n"
                f"Replace {len(changes)} of your 4 current cards:\n"
                + "\n".join(f"> {change}" for change in changes)
            )
        else:
            result.append("### Current team is already the strongest recommended team possible.")
        alternatives = sorted(builds, key=lambda item: (item[0], item[1]), reverse=True)[1:4]
        if alternatives:
            option_sections: List[str] = []
            for other_power, other_luck, other_index, other_cards in alternatives:
                card_details = "\n".join(
                    f"-# `{ROLE_INFO[self._element_key(card)][0]}` **{card.get('name', 'Unknown')}** "
                    f"`{card.get('local_id')}`・**Skill:** {self._format_skill(card.get('skill'))}・"
                    f"**Luck:** {card.get('luck', '?')}"
                    for card in other_cards
                )
                option_sections.append(
                    f"-# **{COMPOSITION_NAMES[other_index]}**・**Power:** "
                    f"{self._format_skill(other_power)}・**Luck:** {other_luck}\n"
                    f"{card_details}"
                )
            result.append("### Other Teams You Can Build\n" + option_sections[0])
            for option in option_sections[1:]:
                result.extend(("__SOFT_DIVIDER__", option))
        return result

    @staticmethod
    def _assign_recommended_cards(
        positions: List[Dict[str, Any]], chosen: List[Dict[str, Any]]
    ) -> List[Tuple[int, Dict[str, Any]]]:
        """Keep selected current cards in place where possible."""
        current = {int(slot["position"]): slot.get("card") for slot in positions}
        best: Optional[Tuple[int, Tuple[int, ...], List[Tuple[int, Dict[str, Any]]]]] = None
        for permutation in itertools.permutations(chosen):
            assignment = [(position, permutation[position - 1]) for position in range(1, 5)]
            changes = sum(
                1 for position, card in assignment
                if not current.get(position)
                or current[position].get("global_id") != card.get("global_id")
            )
            local_order = tuple(int(card.get("local_id") or 0) for _, card in assignment)
            candidate = (changes, local_order, assignment)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        return best[2] if best else []

    @staticmethod
    def _section_components(sections: Iterable[str]) -> List[Dict[str, Any]]:
        """Turn readable sections into a Components V2 container body."""
        components: List[Dict[str, Any]] = []
        boundary_supplied = False
        for section in sections:
            if not section:
                continue
            if section == "__SOFT_DIVIDER__":
                components.append({"type": 14, "divider": False, "spacing": 1})
                boundary_supplied = True
                continue
            if components and not boundary_supplied:
                components.append({"type": 14})
            components.append({"type": 10, "content": section})
            boundary_supplied = False
        return components

    @classmethod
    def _quiet_composition_role_lines(
        cls, composition: Iterable[str], healer_slash: bool = False
    ) -> str:
        """Render role counts as deliberately quiet supporting text."""
        return cls._composition_role_lines(composition, healer_slash=healer_slash)

    @staticmethod
    def _team_member_name(slot: Dict[str, Any]) -> str:
        """Return the character name without Waifugami's decorative shortcode."""
        raw_name = str(slot.get("name") or "Unknown").strip()
        # Waifugami assigns decorative custom emojis to team positions, not cards.
        # Clean generically at the final boundary as well as during parsing so
        # previously stored teams cannot bring an old position emoji back.
        raw_name = re.sub(
            r"^(?:(?:\*\*|__|`)\s*)*"
            r"(?:(?:<a?:[A-Za-z0-9_]+:\d+>)|(?::[A-Za-z0-9_]+:))\s*",
            "",
            raw_name,
        )
        return raw_name.strip(" *_`").strip() or "Unknown"

    @staticmethod
    def _composition_role_lines(
        composition: Iterable[str], healer_slash: bool = False
    ) -> str:
        """Describe a composition as role counts instead of repeated emoji alternatives."""
        counts = Counter(composition)
        order = (
            PHYSICAL_DPS,
            "pure:zap",
            "pure:high_brightness",
            "pure:star_of_david",
        )
        labels = {
            PHYSICAL_DPS: "Physical DPS (`⚔️🔥` or `⚔️⚡`)",
            "pure:zap": "`⚡` Turn Support",
            "pure:high_brightness": (
                "`🔆` Healer / Cleanser" if healer_slash
                else "`🔆` Healer and Cleanser"
            ),
            "pure:star_of_david": "`✡️` Control and Debuffer",
        }
        lines = []
        for role in order:
            count = counts.pop(role, 0)
            if count:
                lines.append(f"> -# **{count}** × {labels[role]}")
        for role, count in counts.items():
            symbol, label = ROLE_INFO.get(role, ("❔", "Unknown role"))
            lines.append(f"> -# **{count}** × `{symbol}` {label}")
        return "\n".join(lines)

    def _composition_difference_plain(
        self, actual: Iterable[str], target: Iterable[str]
    ) -> str:
        actual_count, target_count = Counter(actual), Counter(target)
        missing = list((target_count - actual_count).elements())
        extra = list((actual_count - target_count).elements())
        lines = []
        if missing:
            lines.append(f"**You need**\n{self._composition_role_lines(missing)}")
        if extra:
            lines.append(f"**Cards to replace**\n{self._composition_role_lines(extra)}")
        return "\n\n".join(lines) or "No changes needed."

    @staticmethod
    def _composition_symbols(composition: Iterable[str]) -> str:
        symbols = {PHYSICAL_DPS: "⚔️🔥/⚔️⚡"}
        symbols.update({key: value[0] for key, value in ROLE_INFO.items()})
        return "  ".join(symbols.get(value, "❔") for value in composition)

    @staticmethod
    def _composition_explanation(index: int) -> str:
        return (
            "Three physical damage dealers apply maximum pressure while `🔆` supplies healing and cleansing."
            if index == 0 else
            "Two physical damage dealers are supported by extra team turns from `⚡` and healing plus cleansing from `🔆`."
            if index == 1 else
            "Two physical damage dealers provide pressure, `🔆` sustains the team, and `✡️` weakens and disrupts enemies."
            if index == 2 else
            "The physical damage dealer gains extra opportunities from `⚡`, `✡️` controls enemies, and `🔆` sustains and cleanses the team."
        )

    def _composition_difference(self, actual: Iterable[str], target: Iterable[str]) -> List[str]:
        actual_count, target_count = Counter(actual), Counter(target)
        missing = list((target_count - actual_count).elements())
        extra = list((actual_count - target_count).elements())
        result = []
        if missing:
            result.append(f"**Missing:** {self._composition_symbols(missing)}")
        if extra:
            result.append(f"**Would replace:** {self._composition_symbols(extra)}")
        return result

    # ---------------------------- Components V2 ----------------------------

    async def _active_cards(
        self,
        user_id: int,
        elements: Optional[set[str]] = None,
        sort_by: str = "skill_luck",
    ) -> List[Dict[str, Any]]:
        cards = await self.config.user_from_id(user_id).cards()
        result = [card for card in cards.values() if card.get("status") == "active" and card.get("skill") is not None]
        if elements:
            result = [card for card in result if self._element_key(card) in elements]

        combined = lambda c: (
                -(float(c.get("skill") or 0) + int(c.get("luck") or 0)),
                -float(c.get("skill") or 0),
                -int(c.get("luck") or 0),
                c.get("name", "").casefold(),
            )
        if sort_by == "local_id":
            key = lambda c: (c.get("local_id") is None, c.get("local_id") or 0, *combined(c))
        elif sort_by == "global_id":
            key = lambda c: (c.get("global_id") is None, c.get("global_id") or 0, *combined(c))
        elif sort_by == "type":
            key = lambda c: (
                str(c.get("type") or c.get("type_symbol") or "").casefold(),
                *combined(c),
            )
        elif sort_by == "skill":
            key = lambda c: (-float(c.get("skill") or 0), -int(c.get("luck") or 0), c.get("name", "").casefold())
        elif sort_by == "luck":
            key = lambda c: (-int(c.get("luck") or 0), -float(c.get("skill") or 0), c.get("name", "").casefold())
        else:
            key = combined
        return sorted(result, key=key)

    @staticmethod
    def _element_key(card: Dict[str, Any]) -> str:
        element = str(card.get("mag_element") or "").lower()
        if element == "sunny":
            element = "high_brightness"
        atk = card.get("atk")
        mag = card.get("mag")
        subtype = "physical" if atk is not None and mag is not None and int(atk) > int(mag) else "pure"
        return f"{subtype}:{element}"

    @staticmethod
    def _format_skill(value: Any) -> str:
        if value is None:
            return "?"
        return f"{float(value):.2f}".rstrip("0").rstrip(".")

    def _browser_payload(
        self,
        user_id: int,
        cards: List[Dict[str, Any]],
        page: int,
        total_count: int,
        selected_elements: set[str],
        sort_by: str,
        team_mode: bool = False,
        ephemeral: bool = False,
    ) -> Dict[str, Any]:
        page_count = max(1, (len(cards) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        visible = cards[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        lines = ["## Card List" + ("・Compare Cards" if team_mode else "")]
        if not visible:
            lines.append("No fully observed cards yet. Use `.v` or `.latest` and I will store their statistics.")
        for card in visible:
            local = card.get("local_id")
            local_text = f"`{local}`" if local is not None else "`ID unknown`"
            element = ELEMENT_LABELS.get(self._element_key(card), "❔")
            uncertain = " ⚠️" if card.get("local_id_source") == "unconfirmed" else ""
            lines.append(
                f"**{card.get('name', 'Unknown')}**・{local_text}{uncertain}\n"
                f"> {element}・**Skill:** {self._format_skill(card.get('skill'))}・**Luck:** {card.get('luck', '?')}"
            )
        content = "\n".join(lines)
        return {
            "flags": V2_FLAG | (64 if ephemeral else 0),
            "allowed_mentions": {"parse": []},
            "components": [{
                "type": 17,
                "components": [
                    {"type": 10, "content": content},
                    *([{
                        "type": 1,
                        "components": [{
                            "type": 3,
                            "custom_id": f"nebwg:{user_id}:candidate",
                            "placeholder": "Compare a visible card with your team",
                            "min_values": 1,
                            "max_values": 1,
                            "options": [
                                {
                                    "label": str(card.get("name", "Unknown"))[:80],
                                    "description": (
                                        f"ID {card.get('local_id', '?')}・"
                                        f"{ROLE_INFO.get(self._element_key(card), ('?', 'Unknown role'))[1]}"
                                    )[:100],
                                    "value": str(card.get("global_id")),
                                }
                                for card in visible
                            ],
                        }],
                    }] if team_mode and visible else []),
                    {
                        "type": 1,
                        "components": [{
                            "type": 3,
                            "custom_id": f"nebwg:{user_id}:elements",
                            "placeholder": "Filter Elements",
                            "min_values": 1,
                            "max_values": len(ELEMENT_OPTIONS) + 1,
                            "options": [
                                {
                                    "label": "All Elements",
                                    "value": "all",
                                    "default": not selected_elements,
                                },
                                *[
                                    {
                                        "label": label,
                                        "value": value,
                                        "default": value in selected_elements,
                                    }
                                    for value, label in ELEMENT_OPTIONS
                                ],
                            ],
                        }],
                    },
                    {
                        "type": 1,
                        "components": [{
                            "type": 3,
                            "custom_id": f"nebwg:{user_id}:sort",
                            "placeholder": "Sort by",
                            "min_values": 1,
                            "max_values": 1,
                            "options": [
                                {
                                    "label": label,
                                    "value": value,
                                    "default": value == sort_by,
                                }
                                for value, label in SORT_OPTIONS
                            ],
                        }],
                    },
                    {"type": 14},
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 2, "style": 2,
                                "emoji": {"id": ARROW_LEFT_ID, "name": "ArrowLeftui"},
                                "custom_id": f"nebwg:{user_id}:page:{page - 1}",
                                "disabled": page <= 0,
                            },
                            {
                                "type": 2, "style": 2,
                                "emoji": {"id": REFRESH_ID, "name": "Refreshui"},
                                "custom_id": f"nebwg:{user_id}:refresh:{page}",
                            },
                            {
                                "type": 2, "style": 2,
                                "emoji": {"id": ARROW_RIGHT_ID, "name": "ArrowRightui"},
                                "custom_id": f"nebwg:{user_id}:page:{page + 1}",
                                "disabled": page >= page_count - 1,
                            },
                            {
                                "type": 2, "style": 1 if team_mode else 2,
                                "label": "Compare Cards" if not team_mode else "Exit Compare Cards",
                                "custom_id": f"nebwg:{user_id}:teammode:{page}",
                            },
                            ],
                    },
                    {
                        "type": 10,
                        "content": (
                            f"-# Page {page + 1} of {page_count}・Cards: {len(cards):,}"
                            f"・Sort by: {SORT_LABELS.get(sort_by, 'Skill + Luck')}"
                        ),
                    },
                ],
            }],
        }

    async def _send_browser(self, interaction: discord.Interaction, page: int) -> None:
        user_id = interaction.user.id
        selected_elements: set[str] = set()
        sort_by = "skill_luck"
        cards = await self._active_cards(user_id, selected_elements, sort_by)
        payload = self._browser_payload(
            user_id, cards, page, len(cards), selected_elements, sort_by,
            team_mode=False, ephemeral=False
        )
        route = Route(
            "POST",
            "/interactions/{interaction_id}/{interaction_token}/callback",
            interaction_id=interaction.id,
            interaction_token=interaction.token,
        )
        await self.bot.http.request(route, json={"type": 4, "data": payload})

    async def _update_browser(self, interaction: discord.Interaction, user_id: int, page: int) -> None:
        message_id = interaction.message.id
        selected_elements = self._browser_elements.get(message_id, set())
        sort_by = self._browser_sorts.get(message_id, "skill_luck")
        team_mode = message_id in self._browser_team_mode
        all_cards = await self._active_cards(user_id, sort_by=sort_by)
        cards = await self._active_cards(user_id, selected_elements, sort_by)
        payload = self._browser_payload(
            user_id, cards, page, len(all_cards), selected_elements, sort_by,
            team_mode=team_mode
        )
        payload.pop("flags", None)
        route = Route(
            "POST",
            "/interactions/{interaction_id}/{interaction_token}/callback",
            interaction_id=interaction.id,
            interaction_token=interaction.token,
        )
        await self.bot.http.request(route, json={"type": 7, "data": payload})

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        data = interaction.data or {}
        custom_id = data.get("custom_id", "")
        quest_wait_match = re.fullmatch(r"nebwg:questwait:(\d+):(\d+)", custom_id)
        if quest_wait_match:
            owner_id = int(quest_wait_match.group(1))
            board_message_id = int(quest_wait_match.group(2))
            if interaction.user.id != owner_id:
                await interaction.response.send_message(
                    "This reminder button belongs to someone else.", ephemeral=True
                )
                return
            if not await self._tracking_enabled(owner_id):
                await interaction.response.send_message(
                    "Waifugami tracking is no longer enabled.", ephemeral=True
                )
                return
            await self._confirm_quest_wait_reminder(
                interaction, owner_id, board_message_id
            )
            return
        team_mode_match = re.fullmatch(r"nebwg:(\d+):teammode:(\d+)", custom_id)
        if team_mode_match:
            owner_id, page = int(team_mode_match.group(1)), int(team_mode_match.group(2))
            if interaction.user.id != owner_id:
                await interaction.response.send_message("This card browser belongs to someone else.", ephemeral=True)
                return
            active_team = await self.config.user_from_id(owner_id).active_team_id()
            if active_team is None:
                await interaction.response.send_message(
                    f"Run `.team <1 to 4>`, then react to Waifugami's preview with {TEAM_ANALYSE_EMOJI} first.",
                    ephemeral=True,
                )
                return
            if interaction.message.id in self._browser_team_mode:
                self._browser_team_mode.discard(interaction.message.id)
            else:
                self._browser_team_mode.add(interaction.message.id)
            await self._update_browser(interaction, owner_id, page)
            return
        candidate_match = re.fullmatch(r"nebwg:(\d+):candidate", custom_id)
        if candidate_match:
            owner_id = int(candidate_match.group(1))
            if interaction.user.id != owner_id:
                await interaction.response.send_message("This card browser belongs to someone else.", ephemeral=True)
                return
            values = data.get("values", [])
            if not values:
                await interaction.response.send_message("No card was selected.", ephemeral=True)
                return
            cards = await self.config.user_from_id(owner_id).cards()
            candidate = cards.get(str(values[0]))
            active_team_id = await self.config.user_from_id(owner_id).active_team_id()
            teams = await self.config.user_from_id(owner_id).teams()
            team = teams.get(str(active_team_id))
            if not candidate or not team:
                await interaction.response.send_message("That card or active team is no longer available.", ephemeral=True)
                return
            await self._respond_v2_text(
                interaction,
                await self._candidate_comparison_text(owner_id, team, candidate),
                ephemeral=True,
            )
            return
        element_match = re.fullmatch(r"nebwg:(\d+):elements", custom_id)
        if element_match:
            owner_id = int(element_match.group(1))
            if interaction.user.id != owner_id:
                await interaction.response.send_message(
                    "This card browser belongs to someone else.", ephemeral=True
                )
                return
            values = set(data.get("values", []))
            self._browser_elements[interaction.message.id] = (
                set() if "all" in values else values
            )
            await self._update_browser(interaction, owner_id, page=0)
            return
        sort_match = re.fullmatch(r"nebwg:(\d+):sort", custom_id)
        if sort_match:
            owner_id = int(sort_match.group(1))
            if interaction.user.id != owner_id:
                await interaction.response.send_message(
                    "This card browser belongs to someone else.", ephemeral=True
                )
                return
            values = data.get("values", [])
            self._browser_sorts[interaction.message.id] = (
                values[0] if values and values[0] in SORT_LABELS else "skill_luck"
            )
            await self._update_browser(interaction, owner_id, page=0)
            return
        match = re.fullmatch(r"nebwg:(\d+):(page|refresh):(-?\d+)", custom_id)
        if not match:
            # ---- wishlist V2 interactions ----
            await self._handle_wl_interaction(interaction, custom_id)
            return
        owner_id, action, page_text = int(match.group(1)), match.group(2), match.group(3)
        if interaction.user.id != owner_id:
            await interaction.response.send_message("This card browser belongs to someone else.", ephemeral=True)
            return
        page = int(page_text)
        await self._update_browser(interaction, owner_id, page)

    async def _handle_wl_interaction(
        self, interaction: discord.Interaction, custom_id: str
    ) -> None:
        """Handle all nebwl: wishlist V2 interactions."""
        # Select-menu view switch: nebwl:<uid>:view
        view_match = re.fullmatch(r"nebwl:(\d+):view", custom_id)
        if view_match:
            owner_id = int(view_match.group(1))
            if interaction.user.id != owner_id:
                await interaction.response.send_message(
                    "This wishlist belongs to someone else.", ephemeral=True
                )
                return
            data = interaction.data or {}
            values = data.get("values", [])
            new_view = values[0] if values else "wishlist"
            if new_view not in ("wishlist", "series_known", "series_unknown"):
                new_view = "wishlist"
            store = await self.listener_config.wishlist_by_waifu_id()
            if not isinstance(store, dict):
                store = {}
            payload = self._wl_v2_payload(owner_id, new_view, 0, store)
            payload.pop("flags", None)
            route = Route(
                "POST",
                "/interactions/{interaction_id}/{interaction_token}/callback",
                interaction_id=interaction.id,
                interaction_token=interaction.token,
            )
            await self.bot.http.request(route, json={"type": 7, "data": payload})
            return

        # Sort toggle: nebwl:<uid>:<view>:sort_toggle:<page>:<new_sort>
        sort_match = re.fullmatch(
            r"nebwl:(\d+):(series_known|series_unknown):sort_toggle:(\d+):(wishlist|series|chars)",
            custom_id,
        )
        if sort_match:
            owner_id = int(sort_match.group(1))
            if interaction.user.id != owner_id:
                await interaction.response.send_message(
                    "This wishlist belongs to someone else.", ephemeral=True
                )
                return
            wl_view = sort_match.group(2)
            current_page = int(sort_match.group(3))
            new_sort = sort_match.group(4)
            store = await self.listener_config.wishlist_by_waifu_id()
            if not isinstance(store, dict):
                store = {}
            payload = self._wl_v2_payload(owner_id, wl_view, current_page, store, new_sort)
            payload.pop("flags", None)
            route = Route(
                "POST",
                "/interactions/{interaction_id}/{interaction_token}/callback",
                interaction_id=interaction.id,
                interaction_token=interaction.token,
            )
            await self.bot.http.request(route, json={"type": 7, "data": payload})
            return

        # Pagination: nebwl:<uid>:<view>:<action>:<current_page>:<sort_key>
        page_match = re.fullmatch(
            r"nebwl:(\d+):(wishlist|series_known|series_unknown):(first|prev|next|last):(\d+):(wishlist|series|chars)",
            custom_id,
        )
        if not page_match:
            return
        owner_id = int(page_match.group(1))
        if interaction.user.id != owner_id:
            await interaction.response.send_message(
                "This wishlist belongs to someone else.", ephemeral=True
            )
            return
        wl_view = page_match.group(2)
        action = page_match.group(3)
        current_page = int(page_match.group(4))
        sort_key = page_match.group(5)

        store = await self.listener_config.wishlist_by_waifu_id()
        if not isinstance(store, dict):
            store = {}

        # Compute total pages to safely resolve first/last
        if wl_view == "wishlist":
            total = len(self._wl_wishlist_rows(store))
        else:
            known_only = (wl_view == "series_known")
            total = len(self._wl_series_rows(store, known_only=known_only))
        total_pages = max(1, (total + WL_PAGE_SIZE - 1) // WL_PAGE_SIZE)

        if action == "first":
            new_page = 0
        elif action == "prev":
            new_page = max(0, current_page - 1)
        elif action == "next":
            new_page = min(total_pages - 1, current_page + 1)
        else:  # last
            new_page = total_pages - 1

        payload = self._wl_v2_payload(owner_id, wl_view, new_page, store, sort_key)
        payload.pop("flags", None)
        route = Route(
            "POST",
            "/interactions/{interaction_id}/{interaction_token}/callback",
            interaction_id=interaction.id,
            interaction_token=interaction.token,
        )
        await self.bot.http.request(route, json={"type": 7, "data": payload})

    async def _candidate_comparison_text(
        self, user_id: int, team: Dict[str, Any], candidate: Dict[str, Any]
    ) -> str:
        positions, missing = await self._resolve_team(user_id, team)
        key = self._composition_key(self._element_key(candidate))
        symbol, role = ROLE_INFO.get(self._element_key(candidate), ("❔", "Unknown role"))
        header = [
            f"### {candidate.get('name', 'Unknown')}・`{candidate.get('local_id', '?')}`",
            f"{symbol}・{role}",
            f"**Skill:** {self._format_skill(candidate.get('skill'))}・**Luck:** {candidate.get('luck', '?')}",
        ]
        if missing:
            header.append("\nI cannot compare replacements until the current team members are known.")
            header.append(f"Run `.v {' '.join(str(value) for value in missing)}`.")
            return "\n".join(header)
        empty = next((slot for slot in positions if slot.get("empty")), None)
        if empty:
            best_position = int(empty["position"])
            current = [
                self._composition_key(self._element_key(slot["card"]))
                for slot in positions if slot.get("card")
            ] + [key]
            verdict = "Fills an empty position"
        else:
            choices = []
            for slot in positions:
                resulting = [
                    key if other["position"] == slot["position"]
                    else self._composition_key(self._element_key(other["card"]))
                    for other in positions
                ]
                distance = min(
                    self._counter_distance(resulting, comp)
                    for comp in RECOMMENDED_COMPOSITIONS
                )
                replaced_skill = float(slot["card"].get("skill") or 0)
                choices.append((distance, replaced_skill, slot, resulting))
            _, _, chosen, current = min(choices, key=lambda value: (value[0], value[1]))
            best_position = int(chosen["position"])
            verdict = f"Best replacement: {chosen.get('name', 'Unknown')}・Position {best_position}"
        closest = min(
            range(len(RECOMMENDED_COMPOSITIONS)),
            key=lambda index: self._counter_distance(current, RECOMMENDED_COMPOSITIONS[index]),
        )
        exact = Counter(current) == Counter(RECOMMENDED_COMPOSITIONS[closest]) and len(current) == 4
        header.extend([
            f"\n**{verdict}**",
            ("✅ Creates " if exact else "Closest result: ")
            + f"{self._composition_symbols(RECOMMENDED_COMPOSITIONS[closest])}・{COMPOSITION_NAMES[closest]}",
            f"\nTeam {team['team_id']}・Position {best_position}・{symbol} {role}",
            f"`.teamadd {candidate.get('local_id')} {team['team_id']} {best_position}`",
        ])
        return "\n".join(header)

    # ------------------------------------------------------------
    # Spawn listener / catalog / tier alerts (formerly WaifugamiListener)
    # ------------------------------------------------------------

    # ------------------------------------------------------------
    # Catalog files
    # ------------------------------------------------------------

    def _load_json_file(self, path: Path, fallback: Any) -> Any:
        if not path.exists():
            return fallback
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("Failed reading %s", path)
            return fallback

    def _write_json_file(self, path: Path, data: Any) -> None:
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _load_catalogs(self) -> None:
        loaded_map = self._load_json_file(self.map_path, {})
        self.new_map = loaded_map if isinstance(loaded_map, dict) else {}

        loaded_old = self._load_json_file(self.old_lookup_path, {})
        self.old_map = loaded_old if isinstance(loaded_old, dict) else {}

        loaded_catalog = self._load_json_file(self.series_catalog_path, {})
        if isinstance(loaded_catalog, dict) and loaded_catalog:
            self.series_catalog = self._normalise_series_catalog(loaded_catalog)
            self.new_map = self._build_hash_map_from_series_catalog(self.series_catalog)
        else:
            self.series_catalog = self._build_series_catalog_from_hash_map(self.new_map)
            if self.series_catalog:
                self._save_catalog_files()

        self._rebuild_indexes()

    def _normalise_series_catalog(self, data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        catalog: Dict[str, Dict[str, Any]] = {}
        for sid_raw, entry in data.items():
            if not isinstance(entry, dict):
                continue
            try:
                sid = str(int(entry.get("series_id", sid_raw)))
            except Exception:
                continue
            series_name = str(entry.get("series") or entry.get("name") or f"Series {sid}").strip()
            chars_raw = entry.get("characters") or {}
            if not isinstance(chars_raw, dict):
                continue
            chars: Dict[str, str] = {}
            for cid_raw, name_raw in chars_raw.items():
                try:
                    cid = str(int(cid_raw))
                except Exception:
                    continue
                name = str(name_raw or "").strip()
                if name:
                    chars[cid] = name
            catalog[sid] = {
                "series_id": int(sid),
                "series": series_name,
                "characters": dict(sorted(chars.items(), key=lambda t: int(t[0]))),
                "updated_at": entry.get("updated_at"),
                "source_message_id": entry.get("source_message_id"),
            }
        return catalog

    def _build_series_catalog_from_hash_map(self, source: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        catalog: Dict[str, Dict[str, Any]] = {}
        for entry in source.values():
            if not isinstance(entry, dict):
                continue
            try:
                cid = str(int(entry.get("id")))
                sid = str(int(entry.get("series_id")))
            except Exception:
                continue
            name = str(entry.get("name") or "").strip()
            series = str(entry.get("series") or f"Series {sid}").strip()
            if not name:
                continue
            target = catalog.setdefault(
                sid,
                {
                    "series_id": int(sid),
                    "series": series,
                    "characters": {},
                    "updated_at": None,
                    "source_message_id": None,
                },
            )
            target["characters"][cid] = name

        for sid, entry in catalog.items():
            chars = entry.get("characters") or {}
            entry["characters"] = dict(sorted(chars.items(), key=lambda t: int(t[0])))
        return dict(sorted(catalog.items(), key=lambda t: int(t[0])))

    def _build_hash_map_from_series_catalog(self, catalog: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for sid, entry in catalog.items():
            try:
                series_id = int(entry.get("series_id", sid))
            except Exception:
                continue
            series_name = str(entry.get("series") or f"Series {series_id}").strip()
            chars = entry.get("characters") or {}
            if not isinstance(chars, dict):
                continue
            for cid_raw, name_raw in chars.items():
                try:
                    cid = int(cid_raw)
                except Exception:
                    continue
                name = str(name_raw or "").strip()
                if not name:
                    continue
                out[md5_hash(str(cid))] = {
                    "id": cid,
                    "name": name,
                    "series_id": series_id,
                    "series": series_name,
                }
        return dict(sorted(out.items(), key=lambda t: int(t[1]["id"])))

    def _save_catalog_files(self) -> None:
        self.series_catalog = dict(sorted(self.series_catalog.items(), key=lambda t: int(t[0])))
        self.new_map = self._build_hash_map_from_series_catalog(self.series_catalog)

        by_name: Dict[str, Dict[str, Any]] = {}
        old_lookup: Dict[str, str] = {}
        for waifu_hash, entry in self.new_map.items():
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            by_name[name] = {
                "id": entry.get("id"),
                "series_id": entry.get("series_id"),
                "series": entry.get("series"),
                "waifu_hash": waifu_hash,
            }
            old_lookup[waifu_hash] = name

        self._write_json_file(self.series_catalog_path, self.series_catalog)
        self._write_json_file(self.map_path, self.new_map)
        self._write_json_file(self.waifus_path, by_name)
        self._write_json_file(self.old_lookup_path, old_lookup)
        self.old_map = old_lookup
        self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        self._series_index = {}
        self._char_index = {}
        for entry in self.new_map.values():
            if not isinstance(entry, dict):
                continue
            try:
                sid = str(int(entry.get("series_id")))
                cid = str(int(entry.get("id")))
            except Exception:
                continue
            series_name = str(entry.get("series") or f"Series {sid}").strip()
            self._series_index.setdefault(sid, series_name)
            self._char_index.setdefault(cid, entry)

    def _replace_series_in_catalog(
        self,
        *,
        series_id: str,
        series_name: str,
        characters: Dict[str, str],
        source_message_id: int,
    ) -> Tuple[int, int, int]:
        old_entry = self.series_catalog.get(series_id, {})
        old_chars_raw = old_entry.get("characters") if isinstance(old_entry, dict) else {}
        old_chars = old_chars_raw if isinstance(old_chars_raw, dict) else {}

        old_ids = set(old_chars.keys())
        new_ids = set(characters.keys())
        added = len(new_ids - old_ids)
        removed = len(old_ids - new_ids)
        modified = sum(
            1
            for cid in new_ids & old_ids
            if str(old_chars.get(cid) or "").strip() != str(characters.get(cid) or "").strip()
        )

        self.series_catalog[series_id] = {
            "series_id": int(series_id),
            "series": series_name,
            "characters": dict(sorted(characters.items(), key=lambda t: int(t[0]))),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source_message_id": int(source_message_id),
        }
        self._save_catalog_files()
        return added, removed, modified

    # ------------------------------------------------------------
    # Event override files
    # ------------------------------------------------------------

    def _load_event_overrides(self) -> None:
        data = self._load_json_file(self.event_overrides_path, {"ids": {}, "ranges": []})
        if not isinstance(data, dict):
            data = {"ids": {}, "ranges": []}
        if not isinstance(data.get("ids"), dict):
            data["ids"] = {}
        if not isinstance(data.get("ranges"), list):
            data["ranges"] = []
        self.event_overrides = data

    def _save_event_overrides(self) -> None:
        self._write_json_file(self.event_overrides_path, self.event_overrides)

    # ------------------------------------------------------------
    # General helpers
    # ------------------------------------------------------------

    @staticmethod
    def _is_character_title(t: str) -> bool:
        return (t or "").strip("*_` ").lower() == "character"

    @staticmethod
    def _parse_series_title(title: str) -> Tuple[Optional[str], Optional[str]]:
        m = SERIES_TITLE_RE.match(title or "")
        if not m:
            return None, None
        return str(int(m.group(1))), m.group(2).strip()

    @staticmethod
    def _parse_page_hint(embed: discord.Embed) -> Tuple[Optional[int], Optional[int]]:
        desc = embed.description or ""
        m = PAGE_HINT_RE.search(desc)
        if not m:
            return None, None
        return int(m.group(1)), int(m.group(2))

    @staticmethod
    def _parse_id_name_lines(embed: discord.Embed) -> Dict[str, str]:
        desc = embed.description or ""
        chars: Dict[str, str] = {}
        for raw_line in desc.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if PAGE_HINT_RE.search(line):
                break
            m = LINE_ID_NAME_RE.match(line)
            if not m:
                continue
            chars[str(int(m.group(1)))] = m.group(2).strip()
        return chars

    @staticmethod
    def _is_waifu_completion_embed(embed: discord.Embed) -> bool:
        title = embed.title or ""
        if SERIES_ID_IN_TITLE_RE.search(title) is None:
            return False
        # "completion:" in the title is the only reliable signal that this
        # is a personal "still missing" list. A plain series roster from
        # `.sc <series>` has the exact same numbered-rows-plus-page-hint
        # shape (e.g. "(65) Fate Series" listing every character in the
        # series), so matching on shape alone previously caused `.wgscan`
        # to treat an entire series' full roster as the user's personal
        # missing list.
        return " completion:" in title.lower()

    @staticmethod
    def _is_series_characters_embed(embed: discord.Embed) -> bool:
        title = embed.title or ""
        desc = embed.description or ""
        if " completion:" in title.lower():
            return False
        sid, sname = Waifugami._parse_series_title(title)
        if not sid or not sname:
            return False
        return LINE_ID_NAME_RE.search(desc) is not None and PAGE_HINT_RE.search(desc) is not None

    @staticmethod
    def _parse_claimed_user_id(embed: discord.Embed) -> Optional[int]:
        desc = embed.description or ""
        m = CLAIMED_BY_RE.search(desc)
        if not m:
            return None
        try:
            return int(m.group(1))
        except Exception:
            return None

    @staticmethod
    def _parse_completion_series_id(embed: discord.Embed) -> Optional[str]:
        m = SERIES_ID_IN_TITLE_RE.search(embed.title or "")
        return str(int(m.group(1))) if m else None

    @staticmethod
    def _parse_completion_ids(embed: discord.Embed) -> List[str]:
        desc = embed.description or ""
        ids: List[str] = []
        for line in desc.splitlines():
            line = line.strip()
            if not line:
                continue
            if PAGE_HINT_RE.search(line):
                break
            m = LINE_ID_NAME_RE.match(line)
            if m:
                ids.append(str(int(m.group(1))))
        return ids

    @staticmethod
    def _parse_info_embed(embed: discord.Embed) -> Optional[Tuple[str, str, Optional[int]]]:
        """Parse a Waifugami `.i <id>` character-info embed.

        Returns ``(waifu_id, name, wishlist_value)`` if the embed's title
        matches the `.i` shape (``"(2734) Eizen"``), or ``None`` if it
        doesn't look like one at all. ``wishlist_value`` is ``None`` when
        the embed doesn't have a usable ``Wishlist`` field, in which case
        the title still matched (so it IS an `.i` embed), there's just
        nothing new to learn from it.
        """
        m = INFO_EMBED_TITLE_RE.match((embed.title or "").strip())
        if not m:
            return None
        waifu_id = str(int(m.group(1)))
        name = m.group(2).strip()

        wishlist_value: Optional[int] = None
        for field in embed.fields:
            if (field.name or "").strip().casefold() == "wishlist":
                raw = (field.value or "").strip()
                if raw.isdigit():
                    wishlist_value = int(raw)
                break  # only the first field named "Wishlist" counts

        return waifu_id, name, wishlist_value

    async def _learn_from_info_embed(self, message: discord.Message) -> bool:
        """Learn a character's Wishlist value from a `.i <id>` embed.

        Returns True if the message was recognised as an `.i` info embed
        at all (whether or not anything new was actually learned), so
        callers can tell "not an .i embed" apart from "nothing changed".
        """
        if not message.embeds:
            return False
        parsed = self._parse_info_embed(message.embeds[0])
        if not parsed:
            return False

        waifu_id, name, wishlist_value = parsed
        if wishlist_value is None:
            await self._log_debug(
                message.guild,
                f"wishlist-learn: recognised .i embed for ({waifu_id}) {name} but found no usable Wishlist field",
            )
            return True

        store = await self.listener_config.wishlist_by_waifu_id()
        if not isinstance(store, dict):
            store = {}
        old_value = store.get(waifu_id)
        if old_value == wishlist_value:
            return True  # already known and unchanged

        store[waifu_id] = wishlist_value
        await self.listener_config.wishlist_by_waifu_id.set(store)
        if old_value is None:
            await self._log_debug(
                message.guild, f"wishlist-learn: learned ({waifu_id}) {name} = {wishlist_value}"
            )
        else:
            await self._log_debug(
                message.guild,
                f"wishlist-learn: updated ({waifu_id}) {name}: {old_value} -> {wishlist_value}",
            )
        return True

    async def _get_known_wishlist(self, waifu_id: Optional[str]) -> Optional[int]:
        if not waifu_id:
            return None
        store = await self.listener_config.wishlist_by_waifu_id()
        if not isinstance(store, dict):
            return None
        value = store.get(str(waifu_id))
        return int(value) if isinstance(value, int) else None

    def _extract_hash_from_url(self, url: str) -> Optional[str]:
        if not url:
            return None
        m = WAFU_HASH_PRIMARY_RE.search(url)
        if m:
            return m.group(1).lower()
        m = WAFU_HASH_FALLBACK_RE.search(url)
        if m:
            return m.group(1).lower()

        parts = url.strip("/").split("/")
        candidates: List[str] = []
        for part in parts:
            if re.fullmatch(r"[a-f0-9]{32}", part, re.IGNORECASE):
                candidates.append(part.lower())
        for candidate in candidates:
            if candidate in self.new_map or candidate in self.old_map:
                return candidate
        return None

    @staticmethod
    def _extract_spawn_image_url(message: discord.Message) -> Optional[str]:
        if message.embeds and message.embeds[0].image:
            return message.embeds[0].image.url
        return None

    @staticmethod
    def _extract_spawn_thumbnail_url(message: discord.Message) -> Optional[str]:
        if message.embeds and message.embeds[0].thumbnail:
            return message.embeds[0].thumbnail.url
        return None

    def _lookup_name_from_message(
        self, message: discord.Message
    ) -> Optional[Tuple[str, Optional[str], Optional[str], Optional[str]]]:
        candidates: List[str] = []
        for embed in message.embeds:
            if embed.image and embed.image.url:
                candidates.append(str(embed.image.url))
            if embed.thumbnail and embed.thumbnail.url:
                candidates.append(str(embed.thumbnail.url))
            if embed.url:
                candidates.append(str(embed.url))
            if embed.description:
                candidates.append(embed.description)
            for field in embed.fields:
                candidates.extend((field.name or "", field.value or ""))

        candidates.extend(
            str(attachment.url)
            for attachment in message.attachments
            if attachment.url
        )
        if message.content:
            candidates.append(message.content)

        for candidate in candidates:
            result = self._lookup_name_from_link(candidate)
            if result:
                return result
        return None

    @staticmethod
    def _claim_lookup_content(
        name: str,
        series_id: Optional[str] = None,
        series_name: Optional[str] = None,
        wishlist: Optional[int] = None,
        missing: bool = False,
    ) -> str:
        content = f"### `.claim {name}`"
        if missing:
            content += " 📕"
        subtext_parts: List[str] = []
        if wishlist is not None:
            subtext_parts.append(f"𑣲: {wishlist}")
        if series_id and series_name:
            subtext_parts.append(f"({series_id}) {series_name}")
        if subtext_parts:
            content += "\n-# " + "・".join(subtext_parts)
        return content

    async def _send_channel_v2_lookup(
        self,
        channel: Any,
        *,
        name: str,
        series_id: Optional[str] = None,
        series_name: Optional[str] = None,
        wishlist: Optional[int] = None,
    ) -> Any:
        payload = {
            "flags": V2_FLAG,
            "allowed_mentions": {"parse": []},
            "components": [{
                "type": 17,
                "components": [{
                    "type": 10,
                    "content": self._claim_lookup_content(name, series_id, series_name, wishlist),
                }],
            }],
        }
        route = Route("POST", "/channels/{channel_id}/messages", channel_id=channel.id)
        return await self.bot.http.request(route, json=payload)

    async def _respond_v2_lookup(
        self,
        interaction: discord.Interaction,
        *,
        name: str,
        series_id: Optional[str] = None,
        series_name: Optional[str] = None,
        wishlist: Optional[int] = None,
        missing: bool = False,
    ) -> None:
        payload = {
            "flags": V2_FLAG | 64,
            "allowed_mentions": {"parse": []},
            "components": [{
                "type": 17,
                "components": [{
                    "type": 10,
                    "content": self._claim_lookup_content(name, series_id, series_name, wishlist, missing),
                }],
            }],
        }
        route = Route(
            "POST",
            "/interactions/{interaction_id}/{interaction_token}/callback",
            interaction_id=interaction.id,
            interaction_token=interaction.token,
        )
        await self.bot.http.request(route, json={"type": 4, "data": payload})

    async def _log_debug(self, guild: Optional[discord.Guild], text: str) -> None:
        try:
            debug_channel_id = await self.listener_config.debug_channel_id()
            if debug_channel_id and guild:
                ch = guild.get_channel(int(debug_channel_id))
                if ch and isinstance(ch, discord.TextChannel):
                    await ch.send(text[:1900])
                    return
        except Exception:
            pass
        log.info(text)

    def _mark_seen(self, message_id: int) -> None:
        self._msg_order.append(message_id)
        if len(self._msg_order) > self.CACHE_CAP:
            old_id = self._msg_order.popleft()
            self._event_seen.pop(old_id, None)
            self._announced.pop(old_id, None)
            self._tracking_alerted.pop(old_id, None)
            self._tier_alerted.pop(old_id, None)

    async def _get_spawn_channel_ids(self) -> Set[int]:
        ids: Set[int] = set()
        legacy = await self.listener_config.spawn_channel_id()
        if legacy:
            try:
                ids.add(int(legacy))
            except Exception:
                pass

        current = await self.listener_config.spawn_channel_ids()
        if isinstance(current, list):
            for item in current:
                try:
                    ids.add(int(item))
                except Exception:
                    continue

        if not ids:
            ids.update(int(x) for x in self.DEFAULT_SPAWN_CHANNEL_IDS)
        return ids

    async def _get_event_channel_ids(self) -> Set[int]:
        ids: Set[int] = set()
        configured = await self.listener_config.event_channel_ids()
        if isinstance(configured, list):
            for item in configured:
                try:
                    ids.add(int(item))
                except Exception:
                    continue
        if not ids:
            ids.update(await self._get_spawn_channel_ids())
        return ids

    async def _get_waifugami_bot_id(self) -> int:
        raw = await self.listener_config.waifugami_bot_id()
        try:
            return int(raw)
        except Exception:
            return self.DEFAULT_WAIFUGAMI_BOT_ID

    async def _get_event_role_id(self) -> int:
        raw = await self.listener_config.event_role_id()
        try:
            return int(raw)
        except Exception:
            return self.DEFAULT_EVENT_ROLE_ID

    async def _reply_or_send(self, message: discord.Message, content: str) -> Optional[discord.Message]:
        try:
            return await message.reply(content, mention_author=False)
        except Exception:
            try:
                return await message.channel.send(content)
            except Exception:
                return None

    async def _edit_status_or_send(
        self,
        state: Dict[str, Any],
        channel: Any,
        content: str,
    ) -> Optional[discord.Message]:
        status_message = state.get("status_message")
        if isinstance(status_message, discord.Message):
            try:
                await status_message.edit(content=content)
                return status_message
            except Exception:
                pass

        try:
            return await channel.send(content)
        except Exception:
            return None

    async def _get_referenced_message(self, message: discord.Message) -> Optional[discord.Message]:
        ref = message.reference
        if not ref or not ref.message_id:
            return None

        resolved = getattr(ref, "resolved", None)
        if isinstance(resolved, discord.Message):
            return resolved

        try:
            partial = message.channel.get_partial_message(ref.message_id)
            return await partial.fetch()
        except Exception:
            try:
                return await message.channel.fetch_message(ref.message_id)
            except Exception:
                return None

    # ------------------------------------------------------------
    # Event image logic
    # ------------------------------------------------------------

    def is_event_image(self, image_url: str) -> bool:
        m = self.image_id_re.search(image_url or "")
        if not m:
            return False

        id_str = m.group(1)
        if len(id_str) < self.EVENT_MIN_DIGITS:
            return False

        try:
            img_id = int(id_str)
        except ValueError:
            return False

        forced = self.event_overrides["ids"].get(id_str)
        if isinstance(forced, bool):
            return forced

        for rule in self.event_overrides.get("ranges", []):
            try:
                start = int(rule.get("start", 0))
                end = int(rule.get("end", -1))
                if start <= img_id <= end:
                    return bool(rule.get("event", False))
            except Exception:
                continue

        return self.EVENT_MIN_ID <= img_id <= self.EVENT_MAX_ID

    # ------------------------------------------------------------
    # Autocomplete
    # ------------------------------------------------------------

    async def _ac_series(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        cur = (current or "").strip().lower()
        items: List[Tuple[str, str]] = []
        for sid, name in self._series_index.items():
            label = f"({sid}) {name}"
            if not cur or cur in sid.lower() or cur in name.lower() or cur in label.lower():
                items.append((sid, label))

        def sort_key(item: Tuple[str, str]) -> Tuple[int, str]:
            sid, label = item
            if cur and sid.startswith(cur):
                return 0, label.lower()
            return 1, label.lower()

        items.sort(key=sort_key)
        return [app_commands.Choice(name=label, value=sid) for sid, label in items[:25]]

    # ------------------------------------------------------------
    # Completion tracking storage
    # ------------------------------------------------------------

    async def _tracking_add(
        self, field: str, reverse_field: str, user_id: int, series_id: str, ids: Iterable[str]
    ) -> Tuple[int, int]:
        """Additively merge ``ids`` into ``field[series_id]`` for one user."""
        ids_set: Set[str] = {str(int(x)) for x in ids if str(x).strip().isdigit()}

        user_conf = self.listener_config.user_from_id(user_id)
        store = await getattr(user_conf, field)()
        if not isinstance(store, dict):
            store = {}

        before_set = set(str(x) for x in (store.get(series_id) or []))
        after_set = before_set | ids_set
        store[series_id] = sorted(after_set, key=lambda s: int(s))
        await getattr(user_conf, field).set(store)

        reverse = await getattr(self.listener_config, reverse_field)()
        if not isinstance(reverse, dict):
            reverse = {}
        uids = set(int(x) for x in (reverse.get(series_id) or []) if isinstance(x, int) or str(x).isdigit())
        uids.add(int(user_id))
        reverse[series_id] = sorted(uids)
        await getattr(self.listener_config, reverse_field).set(reverse)

        return len(before_set), len(after_set)

    async def _tracking_sync(
        self, field: str, reverse_field: str, user_id: int, series_id: str, ids: Iterable[str]
    ) -> Tuple[int, int, int]:
        """Replace ``field[series_id]`` for one user with the authoritative ``ids``.

        Unlike ``_tracking_add`` (which only ever grows the stored set), this
        treats ``ids`` as the complete, up-to-date list (e.g. from a full,
        start-to-finish `.wgscan` pass) so entries no longer present are
        correctly dropped instead of lingering forever. Returns
        ``(added, removed, total)``.
        """
        after_set: Set[str] = {str(int(x)) for x in ids if str(x).strip().isdigit()}

        user_conf = self.listener_config.user_from_id(user_id)
        store = await getattr(user_conf, field)()
        if not isinstance(store, dict):
            store = {}
        before_set = set(str(x) for x in (store.get(series_id) or []))

        if after_set:
            store[series_id] = sorted(after_set, key=lambda s: int(s))
        else:
            store.pop(series_id, None)
        await getattr(user_conf, field).set(store)

        reverse = await getattr(self.listener_config, reverse_field)()
        if not isinstance(reverse, dict):
            reverse = {}
        uids = set(int(x) for x in (reverse.get(series_id) or []) if isinstance(x, int) or str(x).isdigit())
        if after_set:
            uids.add(int(user_id))
        else:
            uids.discard(int(user_id))
        if uids:
            reverse[series_id] = sorted(uids)
        else:
            reverse.pop(series_id, None)
        await getattr(self.listener_config, reverse_field).set(reverse)

        added = len(after_set - before_set)
        removed = len(before_set - after_set)
        return added, removed, len(after_set)

    async def _tracking_remove_one(
        self, field: str, reverse_field: str, user_id: int, series_id: str, char_id: str
    ) -> None:
        user_conf = self.listener_config.user_from_id(user_id)
        store = await getattr(user_conf, field)()
        if not isinstance(store, dict):
            return

        cur = [str(x) for x in (store.get(series_id) or [])]
        new = [x for x in cur if x != char_id]
        if new:
            store[series_id] = new
        else:
            store.pop(series_id, None)
        await getattr(user_conf, field).set(store)

        if new:
            return

        reverse = await getattr(self.listener_config, reverse_field)()
        if not isinstance(reverse, dict):
            return

        uids = [int(x) for x in (reverse.get(series_id) or []) if isinstance(x, int) or str(x).isdigit()]
        uids = [x for x in uids if x != int(user_id)]
        if uids:
            reverse[series_id] = sorted(set(uids))
        else:
            reverse.pop(series_id, None)
        await getattr(self.listener_config, reverse_field).set(reverse)

    # ---- completion (derived from `.wgscan`, independent of explicit tracking) ----

    async def _add_completion_tracking(self, user_id: int, series_id: str, ids: List[str]) -> Tuple[int, int]:
        return await self._tracking_add(
            "completion_missing_by_series", "completion_users_by_series", user_id, series_id, ids
        )

    async def _sync_completion_tracking(
        self, user_id: int, series_id: str, ids: Iterable[str]
    ) -> Tuple[int, int, int]:
        return await self._tracking_sync(
            "completion_missing_by_series", "completion_users_by_series", user_id, series_id, ids
        )

    async def _remove_completion_tracking(self, user_id: int, series_id: str, char_id: str) -> None:
        await self._tracking_remove_one(
            "completion_missing_by_series", "completion_users_by_series", user_id, series_id, char_id
        )

    # ---- explicit character tracking (`wgtrackid`/`wguntrackid`, persists regardless of ownership) ----

    async def _add_explicit_tracking(self, user_id: int, series_id: str, ids: List[str]) -> Tuple[int, int]:
        return await self._tracking_add(
            "tracked_characters_by_series", "character_tracked_users_by_series", user_id, series_id, ids
        )

    async def _remove_explicit_tracking(self, user_id: int, series_id: str, char_id: str) -> None:
        await self._tracking_remove_one(
            "tracked_characters_by_series", "character_tracked_users_by_series", user_id, series_id, char_id
        )

    async def _scan_completion_message(self, scanner_user_id: int, msg: discord.Message) -> None:
        if not msg.embeds:
            return
        emb = msg.embeds[0]
        if not self._is_waifu_completion_embed(emb):
            return

        series_id = self._parse_completion_series_id(emb)
        if not series_id:
            return
        ids = self._parse_completion_ids(emb)
        # Note: an empty `ids` list is not necessarily a parse failure — a
        # fully-completed series legitimately renders a completion page
        # with zero rows, and that's exactly the signal a sync needs to
        # clear the last stale entries for it.

        page_i, page_max = self._parse_page_hint(emb)
        has_next = page_i is not None and page_max is not None and page_i < page_max

        # Accumulate ids across every page of this scan session (keyed by the
        # Waifugami message id, since paging edits that same message) so a
        # sync at the end reflects the *entire* completion list, not just
        # whichever single page happened to trigger this call.
        session = self._active_completion_scans.get(msg.id)
        if not session or session.get("series_id") != series_id:
            session = {
                "user_id": int(scanner_user_id),
                "series_id": series_id,
                "seen_ids": set(),
                "seen_pages": set(),
                "status_message": None,
            }
        session["seen_ids"].update(ids)
        if page_i is not None:
            session["seen_pages"].add(page_i)

        if has_next:
            self._active_completion_scans[msg.id] = session
            status_message = await self._edit_status_or_send(
                session,
                msg.channel,
                f"-# Scanning: {len(session['seen_ids'])} found so far, please flip to the next page.",
            )
            session["status_message"] = status_message
            return

        self._active_completion_scans.pop(msg.id, None)

        # Only treat this as the complete, authoritative missing-list for the
        # series (and thus safe to remove stale entries) if every page from
        # 0 up to the last one was actually seen. Otherwise we don't have
        # full visibility (e.g. the scan started mid-way through the pages),
        # so fall back to the old, non-destructive add-only behaviour.
        full_pass = page_max is None or session["seen_pages"] == set(range(page_max + 1))

        if full_pass:
            added, removed, total = await self._sync_completion_tracking(
                scanner_user_id, series_id, session["seen_ids"]
            )
            lines = [f"-# Synced: {total} still missing"]
            details = []
            if added:
                details.append(f"{added} newly added")
            if removed:
                details.append(f"{removed} obtained elsewhere, removed")
            if details:
                lines.append("-# " + ", ".join(details))
            await self._edit_status_or_send(session, msg.channel, "\n".join(lines))
        else:
            before, after = await self._add_completion_tracking(scanner_user_id, series_id, session["seen_ids"])
            added = after - before
            lines = [f"-# Stored: {after} IDs"]
            if added:
                lines.append(f"-# Added: {added} new")
            lines.append("-# (scan started mid-page, so nothing was removed — reply starting from page 0 for a full sync)")
            await self._edit_status_or_send(session, msg.channel, "\n".join(lines))

    # ------------------------------------------------------------
    # Series update from .sc embed
    # ------------------------------------------------------------

    async def _start_series_update_from_message(
        self,
        *,
        actor: discord.abc.User,
        trigger_message: discord.Message,
        target_message: discord.Message,
    ) -> None:
        if not await self.bot.is_owner(actor):
            return

        if target_message.author.id != await self._get_waifugami_bot_id():
            await self._reply_or_send(trigger_message, "-# Not a Waifugami series list.")
            return

        if not target_message.embeds:
            await self._reply_or_send(trigger_message, "-# Not a Waifugami series list.")
            return

        embed = target_message.embeds[0]
        if not self._is_series_characters_embed(embed):
            await self._reply_or_send(trigger_message, "-# Not a Waifugami series list.")
            return

        series_id, series_name = self._parse_series_title(embed.title or "")
        page_i, page_max = self._parse_page_hint(embed)
        if series_id is None or series_name is None or page_i is None or page_max is None:
            await self._reply_or_send(trigger_message, "-# Not a Waifugami series list.")
            return

        if page_i != 0:
            await self._reply_or_send(trigger_message, f"-# Start from Page `0 of {page_max}`.")
            return

        chars = self._parse_id_name_lines(embed)
        if not chars:
            await self._reply_or_send(trigger_message, "-# No characters found.")
            return

        self._active_series_updates[target_message.id] = {
            "owner_id": int(actor.id),
            "series_id": series_id,
            "series_name": series_name,
            "page_max": int(page_max),
            "pages": {0: chars},
            "notified_missing": False,
            "status_message": None,
        }

        if page_max == 0:
            await self._finish_series_update(target_message)
            return

        status_message = await self._reply_or_send(
            trigger_message,
            f"-# Scanning: `({series_id}) {series_name}`\n-# Flip pages until complete.",
        )
        state = self._active_series_updates.get(target_message.id)
        if state is not None:
            state["status_message"] = status_message

    async def _handle_series_update_edit(self, message: discord.Message) -> bool:
        state = self._active_series_updates.get(message.id)
        if not state:
            return False

        if not message.embeds:
            return True

        embed = message.embeds[0]
        if not self._is_series_characters_embed(embed):
            return True

        series_id, series_name = self._parse_series_title(embed.title or "")
        page_i, page_max = self._parse_page_hint(embed)
        if series_id != state.get("series_id"):
            return True
        if page_i is None or page_max is None:
            return True

        chars = self._parse_id_name_lines(embed)
        if chars:
            state["pages"][int(page_i)] = chars

        state["series_name"] = series_name or state.get("series_name")
        state["page_max"] = int(page_max)

        if page_i == page_max:
            missing = [i for i in range(0, int(page_max) + 1) if i not in state["pages"]]
            if missing:
                if not state.get("notified_missing"):
                    state["notified_missing"] = True
                    status_message = await self._edit_status_or_send(
                        state,
                        message.channel,
                        "-# Missing pages: `" + ", ".join(str(i) for i in missing) + "`.",
                    )
                    state["status_message"] = status_message
                return True
            await self._finish_series_update(message)
        return True

    async def _finish_series_update(self, message: discord.Message) -> None:
        state = self._active_series_updates.pop(message.id, None)
        if not state:
            return

        series_id = str(state.get("series_id"))
        series_name = str(state.get("series_name") or self._series_index.get(series_id, f"Series {series_id}"))
        pages = state.get("pages") or {}
        if not isinstance(pages, dict):
            return

        characters: Dict[str, str] = {}
        for page_number in sorted(pages.keys()):
            page_chars = pages.get(page_number)
            if not isinstance(page_chars, dict):
                continue
            characters.update(page_chars)

        if not characters:
            await message.channel.send("-# No characters found.")
            return

        added, removed, modified = self._replace_series_in_catalog(
            series_id=series_id,
            series_name=series_name,
            characters=characters,
            source_message_id=message.id,
        )

        content = (
            f"-# Updated: `({series_id}) {series_name}`\n"
            f"-# Added: `{added}`\n"
            f"-# Removed: `{removed}`\n"
            f"-# Modified: `{modified}`"
        )

        await self._edit_status_or_send(state, message.channel, content)

    # ------------------------------------------------------------
    # DM helpers
    # ------------------------------------------------------------

    async def _dm_user_hit(
        self,
        *,
        user_id: int,
        char_name: str,
        series_name: str,
        thumb_url: str,
        jump_url: str,
    ) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        except Exception:
            return
        if not user:
            return

        emb = discord.Embed(
            description=f"**{char_name}**\n{series_name}",
            color=14548992,
        )
        if thumb_url:
            emb.set_thumbnail(url=thumb_url)

        view = discord.ui.View()
        if jump_url:
            view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Jump", url=jump_url))

        try:
            await user.send(embed=emb, view=view)
        except Exception:
            log.info("DM failed for uid=%s", user_id)

    async def _dm_user_tier_alert(
        self,
        *,
        user_id: int,
        tier: str,
        char_name: str,
        series_name: str,
        image_url: str,
        thumb_url: str,
        jump_url: str,
    ) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        except Exception:
            return
        if not user:
            return

        data = WG_TIER_ALERTS.get(tier, {})
        label = data.get("label", f"{tier.title()} tier spawn detected")
        color = int(data.get("color", 14548992))

        emb = discord.Embed(
            title=label,
            description=f"**{char_name}**\n{series_name}",
            color=color,
        )
        if thumb_url:
            emb.set_thumbnail(url=thumb_url)
        if image_url:
            emb.set_image(url=image_url)

        view = discord.ui.View()
        if jump_url:
            view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Jump", url=jump_url))

        try:
            await user.send(embed=emb, view=view)
        except Exception:
            log.info("Tier DM failed for uid=%s tier=%s", user_id, tier)

    # ------------------------------------------------------------
    # Claim handling
    # ------------------------------------------------------------

    async def _handle_claimed_character_edit(self, message: discord.Message) -> bool:
        if message.author.id != await self._get_waifugami_bot_id():
            return False
        if not message.embeds:
            return False

        embed = message.embeds[0]
        if not self._is_character_title(embed.title or ""):
            return False

        claimed_user_id = self._parse_claimed_user_id(embed)
        if claimed_user_id is None:
            return False

        image_url = embed.image.url if embed.image else None
        if not image_url:
            return True

        waifu_hash = self._extract_hash_from_url(str(image_url))
        if not waifu_hash:
            return True

        entry = self.new_map.get(waifu_hash)
        if not isinstance(entry, dict):
            return True

        try:
            char_id = str(int(entry.get("id")))
            series_id = str(int(entry.get("series_id")))
        except Exception:
            return True

        # Acquiring a character means it's no longer "still needed" for
        # completion — but this must NOT touch explicit `wgtrackid`
        # tracking, since that's an "I want pings regardless of ownership"
        # subscription that should survive claiming the character.
        await self._remove_completion_tracking(claimed_user_id, series_id, char_id)
        self._tracking_alerted.pop(message.id, None)
        self._mark_seen(message.id)
        return True

    # ------------------------------------------------------------
    # Spawn handling
    # ------------------------------------------------------------

    def _detect_spawn_tier(self, message: discord.Message) -> Optional[str]:
        if not message.embeds:
            return None

        embed = message.embeds[0]
        title = embed.title or ""
        desc = (embed.description or "").lower()
        image_url = embed.image.url if embed.image else ""
        thumb_url = embed.thumbnail.url if embed.thumbnail else ""

        if not self._is_character_title(title):
            return None
        if "a waifu/husbando appeared!" not in desc:
            return None
        if "[prefix]claim <name>" not in desc:
            return None
        if "waifugami.com/catalog/" not in image_url:
            return None

        thumb_url_lc = thumb_url.lower()
        for tier, data in WG_TIER_ALERTS.items():
            markers = data.get("markers", set())
            if any(str(marker).lower() in thumb_url_lc for marker in markers):
                return tier
        return None

    async def _handle_waifugami_spawn_message(self, message: discord.Message) -> None:
        if not message.embeds:
            return

        embed = message.embeds[0]
        title = embed.title or ""
        desc_lc = (embed.description or "").lower()
        image_url = embed.image.url if embed.image else None
        if not image_url:
            return

        if "claimed by" in desc_lc or "opened by" in desc_lc:
            self._event_seen[message.id] = True
            if message.id not in self._announced:
                self._announced[message.id] = "claimed"
            self._mark_seen(message.id)
            return

        if not self._is_character_title(title):
            return

        await self._maybe_event_ping(message, image_url)

        waifu_hash = self._extract_hash_from_url(str(image_url))
        if not waifu_hash:
            return

        entry = self.new_map.get(waifu_hash)
        if isinstance(entry, dict):
            try:
                char_id = str(int(entry.get("id")))
                series_id = str(int(entry.get("series_id")))
            except Exception:
                return

            char_name = str(entry.get("name") or "").strip() or f"ID {char_id}"
            series_name = str(entry.get("series") or "").strip() or self._series_index.get(series_id, f"Series {series_id}")

            await self._maybe_public_name_assist(message, waifu_hash, char_id, char_name, series_id, series_name)
            await self._maybe_tier_alerts(message, char_name, series_id, series_name, str(image_url))
            await self._maybe_tracking_alerts(message, waifu_hash, char_id, char_name, series_id, series_name, str(image_url))
            return

        old_name = self.old_map.get(waifu_hash)
        if old_name:
            if self._announced.get(message.id) != waifu_hash:
                await self._send_channel_v2_lookup(message.channel, name=old_name)
                self._announced[message.id] = waifu_hash
                self._mark_seen(message.id)

    async def _maybe_event_ping(self, message: discord.Message, image_url: str) -> None:
        event_channel_ids = await self._get_event_channel_ids()
        if message.channel.id not in event_channel_ids:
            return
        if self._event_seen.get(message.id):
            return
        if not self.is_event_image(image_url):
            return

        role_id = await self._get_event_role_id()
        try:
            await message.reply(
                content=f"<@&{role_id}> detected.",
                allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
                mention_author=False,
            )
            await self._log_debug(message.guild, f"[WG EVENT] role ping on msg {message.id} url {image_url}")
            self._event_seen[message.id] = True
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass
            self._mark_seen(message.id)
        except discord.HTTPException:
            self._event_seen[message.id] = True
            self._mark_seen(message.id)

    async def _maybe_public_name_assist(
        self,
        message: discord.Message,
        waifu_hash: str,
        char_id: str,
        char_name: str,
        series_id: str,
        series_name: str,
    ) -> None:
        spawn_channel_ids = await self._get_spawn_channel_ids()
        if message.channel.id not in spawn_channel_ids:
            return
        if self._announced.get(message.id) == waifu_hash:
            return
        # Reserved immediately, before any awaits, so a near-simultaneous
        # duplicate invocation (e.g. a phantom edit event racing the
        # original message-create event) can't also pass this check.
        self._announced[message.id] = waifu_hash

        wishlist = await self._get_known_wishlist(char_id)

        show_series = await self.listener_config.show_series()
        if show_series and series_id and series_name:
            await self._send_channel_v2_lookup(
                message.channel,
                name=char_name,
                series_id=series_id,
                series_name=series_name,
                wishlist=wishlist,
            )
        else:
            await self._send_channel_v2_lookup(message.channel, name=char_name, wishlist=wishlist)

        self._mark_seen(message.id)

    def _normalise_tier_key(self, tier: str) -> Optional[str]:
        raw = (tier or "").strip().lower()
        if raw in WG_TIER_ALERTS:
            return raw

        aliases = {
            "ζ": "zeta",
            "z": "zeta",
            "ε": "epsilon",
            "e": "epsilon",
            "σ": "sigma",
            "s": "sigma",
            "?": "fake",
            "faker": "fake",
        }
        return aliases.get(raw)

    def _tier_display_name(self, tier: str) -> str:
        key = self._normalise_tier_key(tier) or tier
        data = WG_TIER_ALERTS.get(key, {})
        label = str(data.get("label") or key.title()).strip()
        return label.replace(" tier spawn detected", "").replace(" Tier Spawn Detected", "")

    def _tier_display_list(self) -> str:
        return ", ".join(self._tier_display_name(tier) for tier in WG_TIER_ALERTS)

    async def _get_tier_alert_user_ids(self, tier: str) -> Set[int]:
        clean_uids: Set[int] = set()

        legacy_ids = await self.listener_config.tier_alert_user_ids()
        if isinstance(legacy_ids, list):
            clean_uids |= {
                int(x)
                for x in legacy_ids
                if isinstance(x, int) or str(x).isdigit()
            }

        by_tier = await self.listener_config.tier_alert_user_ids_by_tier()
        if isinstance(by_tier, dict):
            tier_ids = by_tier.get(tier) or []
            if isinstance(tier_ids, list):
                clean_uids |= {
                    int(x)
                    for x in tier_ids
                    if isinstance(x, int) or str(x).isdigit()
                }

        return clean_uids

    async def _maybe_tier_alerts(
        self,
        message: discord.Message,
        char_name: str,
        series_id: str,
        series_name: str,
        image_url: str,
    ) -> None:
        tier = self._detect_spawn_tier(message)
        if not tier:
            return
        if self._tier_alerted.get(message.id) == tier:
            return
        # Reserve this (message, tier) pair immediately, before any await,
        # so a near-simultaneous duplicate call (e.g. a message-edit event
        # racing the original message-create event) sees it's already
        # claimed instead of also passing the check above.
        self._tier_alerted[message.id] = tier

        thumb_url = self._extract_spawn_thumbnail_url(message) or image_url
        clean_uids = sorted(await self._get_tier_alert_user_ids(tier))
        if not clean_uids:
            return

        for uid in clean_uids:
            await self._dm_user_tier_alert(
                user_id=uid,
                tier=tier,
                char_name=char_name,
                series_name=f"({series_id}) {series_name}",
                image_url=image_url,
                thumb_url=str(thumb_url),
                jump_url=message.jump_url,
            )
        self._mark_seen(message.id)

    async def _maybe_tracking_alerts(
        self,
        message: discord.Message,
        waifu_hash: str,
        char_id: str,
        char_name: str,
        series_id: str,
        series_name: str,
        image_url: str,
    ) -> None:
        if self._tracking_alerted.get(message.id) == waifu_hash:
            return
        # Reserved immediately, before any awaits, so a near-simultaneous
        # duplicate invocation (e.g. a phantom edit event racing the
        # original message-create event) can't also pass this check.
        self._tracking_alerted[message.id] = waifu_hash

        completion_idx = await self.listener_config.completion_users_by_series()
        if not isinstance(completion_idx, dict):
            completion_idx = {}

        tracked_idx = await self.listener_config.character_tracked_users_by_series()
        if not isinstance(tracked_idx, dict):
            tracked_idx = {}

        watchers = await self.listener_config.series_watchers_by_series()
        if not isinstance(watchers, dict):
            watchers = {}

        candidate_uids = set(
            int(x)
            for x in (completion_idx.get(series_id) or [])
            if isinstance(x, int) or str(x).isdigit()
        )
        candidate_uids |= set(
            int(x)
            for x in (tracked_idx.get(series_id) or [])
            if isinstance(x, int) or str(x).isdigit()
        )
        candidate_uids |= set(
            int(x)
            for x in (watchers.get(series_id) or [])
            if isinstance(x, int) or str(x).isdigit()
        )
        if not candidate_uids:
            return

        hit_anyone = False

        for uid in sorted(candidate_uids):
            missing = await self.listener_config.user_from_id(uid).completion_missing_by_series()
            if not isinstance(missing, dict):
                missing = {}

            tracked_chars = await self.listener_config.user_from_id(uid).tracked_characters_by_series()
            if not isinstance(tracked_chars, dict):
                tracked_chars = {}

            watched = await self.listener_config.user_from_id(uid).watched_series()
            if not isinstance(watched, list):
                watched = []

            # A spawn can match more than one reason at once (e.g. you both
            # still need it AND explicitly track it); any one of them is
            # enough to notify, and we only ever send one DM either way.
            is_series_watch = series_id in {
                str(int(s))
                for s in watched
                if str(s).isdigit()
            }
            is_completion_hit = char_id in {
                str(int(x))
                for x in (missing.get(series_id) or [])
                if str(x).isdigit()
            }
            is_explicit_hit = char_id in {
                str(int(x))
                for x in (tracked_chars.get(series_id) or [])
                if str(x).isdigit()
            }
            if not is_series_watch and not is_completion_hit and not is_explicit_hit:
                continue

            await self._dm_user_hit(
                user_id=uid,
                char_name=char_name,
                series_name=f"({series_id}) {series_name}",
                thumb_url=image_url,
                jump_url=message.jump_url,
            )
            hit_anyone = True

        if hit_anyone:
            self._mark_seen(message.id)


    # ------------------------------------------------------------
    # Name lookup slash command helpers
    # ------------------------------------------------------------

    def _lookup_name_from_link(self, link: str) -> Optional[Tuple[str, Optional[str], Optional[str], Optional[str]]]:
        waifu_hash = self._extract_hash_from_url(link or "")
        if not waifu_hash:
            return None

        entry = self.new_map.get(waifu_hash)
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            if not name:
                return None

            waifu_id: Optional[str] = None
            try:
                raw_id = entry.get("id")
                if raw_id is not None:
                    waifu_id = str(int(raw_id))
            except Exception:
                waifu_id = None

            series_id: Optional[str] = None
            series_name: Optional[str] = None
            try:
                raw_sid = entry.get("series_id")
                if raw_sid is not None:
                    series_id = str(int(raw_sid))
            except Exception:
                series_id = None

            raw_series = entry.get("series")
            if raw_series:
                series_name = str(raw_series).strip() or None
            elif series_id:
                series_name = self._series_index.get(series_id)

            return name, series_id, series_name, waifu_id

        old_name = self.old_map.get(waifu_hash)
        if isinstance(old_name, str) and old_name.strip():
            return old_name.strip(), None, None, None

        return None

    # ------------------------------------------------------------
    # User installable message context command
    # ------------------------------------------------------------

    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def context_name(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        result = self._lookup_name_from_message(message)
        if not result:
            await interaction.response.send_message(
                "Character not found in catalog.",
                ephemeral=True,
            )
            return

        name, series_id, series_name, waifu_id = result
        wishlist = await self._get_known_wishlist(waifu_id)

        # Show 📕 if this character is in the invoking user's tracked/missing list.
        is_missing = False
        if series_id and waifu_id:
            uid = interaction.user.id
            user_conf = self.listener_config.user_from_id(uid)
            comp_missing = await user_conf.completion_missing_by_series()
            tracked_chars = await user_conf.tracked_characters_by_series()
            if not isinstance(comp_missing, dict):
                comp_missing = {}
            if not isinstance(tracked_chars, dict):
                tracked_chars = {}
            cid_norm = str(int(waifu_id)) if str(waifu_id).isdigit() else waifu_id
            in_missing = cid_norm in {
                str(int(x)) for x in (comp_missing.get(series_id) or []) if str(x).isdigit()
            }
            in_tracked = cid_norm in {
                str(int(x)) for x in (tracked_chars.get(series_id) or []) if str(x).isdigit()
            }
            is_missing = in_missing or in_tracked

        await self._respond_v2_lookup(
            interaction,
            name=name,
            series_id=series_id,
            series_name=series_name,
            wishlist=wishlist,
            missing=is_missing,
        )

    # ------------------------------------------------------------
    # Commands, catalog update
    # ------------------------------------------------------------

    @commands.command(name="wgupdate")
    @commands.is_owner()
    async def wgupdate(self, ctx: commands.Context) -> None:
        """Reply to a Waifugami .sc series list on Page 0 to update that series."""
        if not ctx.message.reference or not ctx.message.reference.message_id:
            await ctx.reply("-# Reply to a Waifugami series list.")
            return
        ref = await self._get_referenced_message(ctx.message)
        if ref is None:
            await ctx.reply("-# Could not fetch message.")
            return
        await self._start_series_update_from_message(
            actor=ctx.author,
            trigger_message=ctx.message,
            target_message=ref,
        )

    @commands.command(name="wgscan")
    async def wgscan(self, ctx: commands.Context) -> None:
        """Reply to a completion embed and store IDs from the current page."""
        if not ctx.message.reference or not ctx.message.reference.message_id:
            await ctx.reply("Reply to a completion embed message.")
            return
        ref = await self._get_referenced_message(ctx.message)
        if ref is None:
            await ctx.reply("Could not fetch the replied message.")
            return
        if not ref.embeds:
            await ctx.reply("Could not read the replied message embed.")
            return
        if not self._is_waifu_completion_embed(ref.embeds[0]):
            await ctx.reply("That does not look like a Waifugami completion embed.")
            return
        await self._scan_completion_message(ctx.author.id, ref)

    # ------------------------------------------------------------
    # Commands, display and event controls
    # ------------------------------------------------------------

    @commands.hybrid_group(name="wgseries", invoke_without_command=True)
    async def wgseries(self, ctx: commands.Context) -> None:
        """Toggle or check whether series info is appended to names."""
        show_series = await self.listener_config.show_series()
        await ctx.send(f"Series display is **{'ON' if show_series else 'OFF'}**.")

    @wgseries.command(name="on")
    async def wgseries_on(self, ctx: commands.Context) -> None:
        await self.listener_config.show_series.set(True)
        await ctx.send("Series display **ON**. New spawns will show `Name | (SeriesID) Series`.")

    @wgseries.command(name="off")
    async def wgseries_off(self, ctx: commands.Context) -> None:
        await self.listener_config.show_series.set(False)
        await ctx.send("Series display **OFF**. New spawns will show only `Name`.")

    @wgseries.command(name="status")
    async def wgseries_status(self, ctx: commands.Context) -> None:
        show_series = await self.listener_config.show_series()
        await ctx.send(f"Series display is **{'ON' if show_series else 'OFF'}**.")

    @commands.hybrid_group(name="wgevent", invoke_without_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def wgevent(self, ctx: commands.Context) -> None:
        await ctx.send("Subcommands: link, range, status.")

    @wgevent.command(name="link")
    @commands.has_guild_permissions(manage_guild=True)
    async def wgevent_link(self, ctx: commands.Context, link: str, event: bool) -> None:
        m = self.image_id_re.search(link or "")
        if not m:
            await ctx.send("Could not find an /images/<id> in that link.")
            return
        id_str = m.group(1)
        self.event_overrides["ids"][id_str] = bool(event)
        self._save_event_overrides()
        await ctx.send(f"Set id {id_str} to event={event}.")

    @wgevent.command(name="range")
    @commands.has_guild_permissions(manage_guild=True)
    async def wgevent_range(self, ctx: commands.Context, start: int, end: int, event: bool) -> None:
        if end < start:
            start, end = end, start
        self.event_overrides["ranges"].append({"start": int(start), "end": int(end), "event": bool(event)})
        self._save_event_overrides()
        await ctx.send(f"Added range {start}..{end} with event={event}.")

    @wgevent.command(name="status")
    async def wgevent_status(self, ctx: commands.Context, link: str) -> None:
        m = self.image_id_re.search(link or "")
        if not m:
            await ctx.send("No numeric image id found in the link.")
            return
        id_str = m.group(1)
        try:
            img_id = int(id_str)
        except ValueError:
            await ctx.send("Image id was not a number.")
            return

        forced = self.event_overrides["ids"].get(id_str)
        if isinstance(forced, bool):
            await ctx.send(f"id {id_str} is explicitly set to event={forced}.")
            return

        for rule in self.event_overrides.get("ranges", []):
            try:
                if int(rule.get("start", 0)) <= img_id <= int(rule.get("end", -1)):
                    await ctx.send(
                        f"id {id_str} falls in override range {rule['start']}..{rule['end']}, "
                        f"event={bool(rule.get('event', False))}."
                    )
                    return
            except Exception:
                continue

        default = (self.EVENT_MIN_ID <= img_id <= self.EVENT_MAX_ID) and len(id_str) >= self.EVENT_MIN_DIGITS
        await ctx.send(f"id {id_str} has no override, default rule says event={default}.")

    @commands.hybrid_group(name="wgdebug", invoke_without_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def wgdebug(self, ctx: commands.Context) -> None:
        ch_id = await self.listener_config.debug_channel_id()
        if ch_id:
            await ctx.send(f"Debug channel is <#{ch_id}>.")
        else:
            await ctx.send("Debug channel is not set.")

    @wgdebug.command(name="set")
    @commands.has_guild_permissions(manage_guild=True)
    async def wgdebug_set(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        await self.listener_config.debug_channel_id.set(channel.id)
        await ctx.send(f"Debug channel set to {channel.mention}.")

    @wgdebug.command(name="off")
    @commands.has_guild_permissions(manage_guild=True)
    async def wgdebug_off(self, ctx: commands.Context) -> None:
        await self.listener_config.debug_channel_id.set(None)
        await ctx.send("Debug channel unset. Logs will go to console.")

    @wgdebug.command(name="ping")
    async def wgdebug_ping(self, ctx: commands.Context) -> None:
        await self._log_debug(ctx.guild, "[WG DEBUG] hello from wgdebug ping")
        await ctx.send("Sent a debug line.")

    # ------------------------------------------------------------
    # Commands, channel and status controls
    # ------------------------------------------------------------

    @commands.group(invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def wgchannels(self, ctx: commands.Context) -> None:
        """Manage Waifugami spawn channels."""
        await ctx.send_help(ctx.command)

    @wgchannels.command(name="add")
    @commands.admin_or_permissions(manage_guild=True)
    async def wgchannels_add(self, ctx: commands.Context, channel_id: int) -> None:
        channel_ids = await self.listener_config.spawn_channel_ids()
        if not isinstance(channel_ids, list):
            channel_ids = []
        ids = {int(x) for x in channel_ids if str(x).isdigit()}
        ids.add(int(channel_id))
        await self.listener_config.spawn_channel_ids.set(sorted(ids))
        ch = self.bot.get_channel(int(channel_id))
        if ch:
            await ctx.reply(f"Added spawn channel: {ch.mention} `{channel_id}`")
        else:
            await ctx.reply(f"Added spawn channel ID `{channel_id}`.")

    @wgchannels.command(name="remove")
    @commands.admin_or_permissions(manage_guild=True)
    async def wgchannels_remove(self, ctx: commands.Context, channel_id: int) -> None:
        channel_ids = await self.listener_config.spawn_channel_ids()
        if not isinstance(channel_ids, list):
            channel_ids = []
        ids = {int(x) for x in channel_ids if str(x).isdigit()}
        before = len(ids)
        ids.discard(int(channel_id))
        await self.listener_config.spawn_channel_ids.set(sorted(ids))
        if len(ids) < before:
            await ctx.reply(f"Removed spawn channel ID `{channel_id}`.")
        else:
            await ctx.reply(f"Channel ID `{channel_id}` was not in the tracked list.")

    @wgchannels.command(name="list")
    async def wgchannels_list(self, ctx: commands.Context) -> None:
        ids = await self._get_spawn_channel_ids()
        if not ids:
            await ctx.reply("No spawn channels are configured.")
            return
        lines = []
        for channel_id in sorted(ids):
            ch = self.bot.get_channel(channel_id)
            if ch:
                guild_name = getattr(ch.guild, "name", "Unknown server")
                lines.append(f"`{channel_id}` | {ch.mention} | {guild_name}")
            else:
                lines.append(f"`{channel_id}` | unresolved")
        await ctx.reply("\n".join(lines[:50]))

    @commands.command()
    async def wgstatus(self, ctx: commands.Context) -> None:
        spawn_channel_ids = await self._get_spawn_channel_ids()
        waifugami_bot_id = await self._get_waifugami_bot_id()
        auto_remove = await self.listener_config.auto_remove_on_hit()
        wishlist_store = await self.listener_config.wishlist_by_waifu_id()
        wishlist_count = len(wishlist_store) if isinstance(wishlist_store, dict) else 0
        lines = []
        for channel_id in sorted(spawn_channel_ids):
            ch = self.bot.get_channel(channel_id)
            if ch:
                lines.append(f"{channel_id} | #{ch.name} | {ch.guild.name}")
            else:
                lines.append(f"{channel_id} | unresolved")
        channels_text = "\n".join(lines) if lines else "None"
        await ctx.reply(
            f"spawn_channel_ids:\n{channels_text}\n\n"
            f"waifugami_bot_id={waifugami_bot_id}\n"
            f"auto_remove_on_hit={auto_remove}\n"
            f"hash_map_entries={len(self.new_map)}\n"
            f"series_catalog_entries={len(self.series_catalog)}\n"
            f"series_index_entries={len(self._series_index)}\n"
            f"char_index_entries={len(self._char_index)}\n"
            f"wishlist_learned_entries={wishlist_count} (of {len(self._char_index)} known characters)\n"
            f"active_completion_scans={len(self._active_completion_scans)}\n"
            f"active_series_updates={len(self._active_series_updates)}"
        )

    # ------------------------------------------------------------
    # Wishlist V2 helpers
    # ------------------------------------------------------------

    # Invisible left-to-right mark used to prevent Discord collapsing the
    # leading space before 𖹭 on subsequent lines in some clients.
    _WL_ZWNJ = "\u200e"

    def _wl_char_line(self, cid: str, val: int) -> str:
        """Format one character row: ID・Name・𖹭 Count・(SeriesID)"""
        entry = self._char_index.get(cid)
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip() or f"ID {cid}"
            sid = entry.get("series_id")
            sid_str = f"({int(sid)})" if sid is not None else "(?"
        else:
            name = f"ID {cid}"
            sid_str = "(?)"
        # The invisible character (‎) keeps 𖹭 spacing consistent on all lines.
        return f"{cid}・{name}・{self._WL_ZWNJ}𖹭 {val}・{sid_str}"

    def _wl_wishlist_rows(self, store: Dict[str, Any]) -> List[Tuple[str, int]]:
        """Return (cid, value) pairs sorted by descending wishlist value."""
        return sorted(
            ((str(cid), val) for cid, val in store.items() if isinstance(val, int)),
            key=lambda t: (-t[1], int(t[0]) if t[0].isdigit() else 0),
        )

    def _wl_series_rows(
        self, store: Dict[str, Any], *, known_only: bool
    ) -> List[Tuple[str, str, int, int]]:
        """Return (series_id, series_name, total_wl, char_count) for Known or Unknown views.

        "Known" = every character in the series catalog has an entry in store.
        "Unknown" = at least one character is missing from store.
        char_count is the total number of characters in the catalog entry for that series.
        """
        rows: List[Tuple[str, str, int, int]] = []
        for sid, entry in self.series_catalog.items():
            if not isinstance(entry, dict):
                continue
            chars = entry.get("characters") or {}
            if not isinstance(chars, dict) or not chars:
                continue
            sname = str(entry.get("series") or self._series_index.get(sid, f"Series {sid}")).strip()
            total_wl = sum(
                int(store[cid]) for cid in chars if cid in store and isinstance(store.get(cid), int)
            )
            all_known = all(cid in store and isinstance(store.get(cid), int) for cid in chars)
            if known_only == all_known:
                rows.append((sid, sname, total_wl, len(chars)))
        return rows

    def _wl_series_sorted(
        self, rows: List[Tuple[str, str, int, int]], sort_key: str
    ) -> List[Tuple[str, str, int, int]]:
        if sort_key == "series":
            return sorted(rows, key=lambda t: int(t[0]) if t[0].isdigit() else 0)
        if sort_key == "chars":
            # Highest character count first; tie-break by series ID
            return sorted(rows, key=lambda t: (-t[3], int(t[0]) if t[0].isdigit() else 0))
        # default "wishlist": highest total wishlist first
        return sorted(rows, key=lambda t: (-t[2], int(t[0]) if t[0].isdigit() else 0))

    def _wl_v2_payload(
        self,
        user_id: int,
        view: str,
        page: int,
        store: Dict[str, Any],
        sort_key: str = "wishlist",
    ) -> Dict[str, Any]:
        """Build the full Components V2 payload for the wishlist message."""
        uid = str(user_id)

        # --- build the rows for the active view ---
        if view == "wishlist":
            all_rows = self._wl_wishlist_rows(store)
            total = len(all_rows)
            total_pages = max(1, (total + WL_PAGE_SIZE - 1) // WL_PAGE_SIZE)
            page = max(0, min(page, total_pages - 1))
            page_rows = all_rows[page * WL_PAGE_SIZE:(page + 1) * WL_PAGE_SIZE]
            body_lines = [self._wl_char_line(cid, val) for cid, val in page_rows]
        else:
            known_only = (view == "series_known")
            all_series = self._wl_series_rows(store, known_only=known_only)
            all_series = self._wl_series_sorted(all_series, sort_key)
            total = len(all_series)
            total_pages = max(1, (total + WL_PAGE_SIZE - 1) // WL_PAGE_SIZE)
            page = max(0, min(page, total_pages - 1))
            page_rows_s = all_series[page * WL_PAGE_SIZE:(page + 1) * WL_PAGE_SIZE]
            body_lines = [
                f"{sid}: {sname} (𖹭 {wl})" for sid, sname, wl, _cc in page_rows_s
            ]

        body_text = "\n".join(body_lines) if body_lines else "-# (nothing here)"

        at_first = page <= 0
        at_last = page >= total_pages - 1

        # --- legend line ---
        if view == "wishlist":
            legend = "-# ID ・ Character ・ Wishlist ・ Series ID"
        else:
            legend = "-# Series ID: Name (Total Wishlist)"

        # --- sort label for series views ---
        _sort_labels = {"wishlist": "Sort: Wishlist", "series": "Sort: Series", "chars": "Sort: # Chars"}
        sort_label = _sort_labels.get(sort_key, "Sort: Wishlist")

        # --- select menu: mark the current view ---
        def _opt(label: str, value: str) -> Dict[str, Any]:
            opt: Dict[str, Any] = {"label": label, "value": value}
            if value == view:
                opt["default"] = True
            return opt

        select_row = {
            "type": 1,
            "components": [{
                "type": 3,
                "custom_id": f"nebwl:{uid}:view",
                "options": [
                    _opt("Wishlist", "wishlist"),
                    _opt("Series Known", "series_known"),
                    _opt("Series Unknown", "series_unknown"),
                ],
                "min_values": 1,
                "max_values": 1,
            }],
        }

        # --- pagination buttons ---
        def _btn(label: str, action: str, disabled: bool) -> Dict[str, Any]:
            return {
                "type": 2,
                "style": 2,
                "label": label,
                "custom_id": f"nebwl:{uid}:{view}:{action}:{page}:{sort_key}",
                "disabled": disabled,
            }

        pagination_row = {
            "type": 1,
            "components": [
                _btn("<<", "first", at_first),
                _btn("<", "prev", at_first),
                _btn(">", "next", at_last),
                _btn(">>", "last", at_last),
            ],
        }

        inner: List[Dict[str, Any]] = [
            {"type": 10, "content": "## Wishlist"},
            {"type": 14},
            {"type": 10, "content": body_text},
            {"type": 14},
            {"type": 10, "content": legend},
            {"type": 14},
            {"type": 10, "content": f"-# Page {page + 1} of {total_pages}"},
            {"type": 14},
            select_row,
            {"type": 14},
        ]

        # Sort button for series views
        if view in ("series_known", "series_unknown"):
            if view == "series_unknown":
                # 3-way cycle: wishlist → series → chars → wishlist
                _cycle = {"wishlist": "series", "series": "chars", "chars": "wishlist"}
            else:
                # 2-way cycle: wishlist ↔ series
                _cycle = {"wishlist": "series", "series": "wishlist", "chars": "wishlist"}
            new_sort = _cycle.get(sort_key, "series")
            sort_btn_row = {
                "type": 1,
                "components": [{
                    "type": 2,
                    "style": 2,
                    "label": sort_label,
                    "custom_id": f"nebwl:{uid}:{view}:sort_toggle:{page}:{new_sort}",
                }],
            }
            inner.append(sort_btn_row)
            inner.append({"type": 14})

        inner.append(pagination_row)

        return {
            "flags": V2_FLAG,
            "allowed_mentions": {"parse": []},
            "components": [{"type": 17, "components": inner}],
        }

    @commands.command(name="wgwishlist")
    async def wgwishlist(self, ctx: commands.Context, *, query: Optional[str] = None) -> None:
        """Show learned `.i` Wishlist values, or look up one character.

        With no argument, opens a Components V2 browser with Wishlist,
        Series Known, and Series Unknown views. With a waifu ID or exact
        character name, shows just that one entry.
        """
        store = await self.listener_config.wishlist_by_waifu_id()
        if not isinstance(store, dict):
            store = {}

        if query:
            query = query.strip()
            waifu_id: Optional[str] = None
            if query.isdigit():
                waifu_id = str(int(query))
            else:
                q_cf = query.casefold()
                for cid, entry in self._char_index.items():
                    if isinstance(entry, dict) and str(entry.get("name") or "").casefold() == q_cf:
                        waifu_id = str(cid)
                        break
            if not waifu_id:
                await ctx.reply(f"Don't recognise a character matching `{query}`.")
                return

            value = store.get(waifu_id)
            entry = self._char_index.get(waifu_id)
            name = str(entry.get("name") or "") if isinstance(entry, dict) else ""
            name = name or f"ID {waifu_id}"
            if value is None:
                await ctx.reply(f"({waifu_id}) {name}: Wishlist not learned yet — run `.i {waifu_id}` to teach me.")
            else:
                await ctx.reply(f"({waifu_id}) {name}: Wishlist {value}")
            return

        if not store:
            await ctx.reply("No Wishlist values learned yet. Run `.i <id>` on a character to teach me one.")
            return

        payload = self._wl_v2_payload(ctx.author.id, "wishlist", 0, store)
        route = Route("POST", "/channels/{channel_id}/messages", channel_id=ctx.channel.id)
        await self.bot.http.request(route, json=payload)

    async def _do_wl_dump(self, ctx: commands.Context, dump: str) -> None:
        """Shared implementation for ``wgwishlistdump`` / ``wg wishlist dump``."""
        text = (dump or "").strip()
        if not text:
            await ctx.reply(
                "Paste the Kazuha `orderwl` dump text after the command. "
                "Example: `..wg wishlist dump Total: (9317) Wishlist\\n5272 | Kafka ((630) Wishlist)\\n...`"
            )
            return

        matches = KAZUHA_ORDERWL_LINE_RE.findall(text)
        if not matches:
            await ctx.reply(
                "No wishlist lines recognised in the pasted text. "
                "Make sure it contains lines like `5272 | Kafka ((630) Wishlist)`."
            )
            return

        store = await self.listener_config.wishlist_by_waifu_id()
        if not isinstance(store, dict):
            store = {}

        updated = 0
        skipped_unknown: List[str] = []
        for raw_id, _name, raw_count in matches:
            cid = str(int(raw_id))
            count = int(raw_count)
            if cid not in self._char_index:
                skipped_unknown.append(cid)
                continue
            store[cid] = count
            updated += 1

        await self.listener_config.wishlist_by_waifu_id.set(store)

        parts = [f"✅ Imported **{updated}** wishlist value(s)."]
        if skipped_unknown:
            parts.append(
                f"{len(skipped_unknown)} unknown ID(s) skipped "
                f"({', '.join(skipped_unknown[:5])}"
                + (f" and {len(skipped_unknown) - 5} more" if len(skipped_unknown) > 5 else "")
                + ") — run `..wgupdate` if the catalog is stale."
            )
        await ctx.reply(" ".join(parts))

    @commands.command(name="wgwishlistdump")
    async def wgwishlistdump(self, ctx: commands.Context, *, dump: str = "") -> None:
        """Import wishlist counts from a Kazuha `orderwl` DM dump.

        Paste the full text you received from Kazuha directly after the command,
        including the ``Total:`` header line. Lines like
        ``5272 | Kafka ((630) Wishlist)`` are parsed; everything else is ignored.

        Safe to run repeatedly — existing values are overwritten,
        no duplicates are created. Unknown character IDs are skipped.

        Example::

            ..wg wishlist dump Total: (9317) Wishlist
            5272 | Kafka ((630) Wishlist)
            5417 | Acheron ((383) Wishlist)
            ...
        """
        await self._do_wl_dump(ctx, dump)

    # ------------------------------------------------------------
    # Commands, tracking
    # ------------------------------------------------------------

    @commands.command()
    async def wgtrackid(self, ctx: commands.Context, char_id: str) -> None:
        cid = (char_id or "").strip()
        if not cid.isdigit():
            await ctx.reply("Give me a numeric character id, example: 40123")
            return
        cid = str(int(cid))

        entry = self._char_index.get(cid)
        if not isinstance(entry, dict):
            await ctx.reply(f"I do not know character id {cid}. Is your waifu_hash_map.json up to date?")
            return

        try:
            sid = str(int(entry.get("series_id")))
        except Exception:
            await ctx.reply(f"I found {cid}, but its series_id looks missing or invalid in the map.")
            return

        series_name = str(entry.get("series") or "").strip() or self._series_index.get(sid, "Unknown series")
        char_name = str(entry.get("name") or "").strip() or f"ID {cid}"
        before, after = await self._add_explicit_tracking(int(ctx.author.id), sid, [cid])
        if after > before:
            await ctx.reply(f"Now tracking {char_name} in ({sid}) {series_name}. Total tracked in this series: {after}")
        else:
            await ctx.reply(f"Already tracking {char_name} in ({sid}) {series_name}. Total tracked in this series: {after}")

    @commands.command()
    async def wguntrackid(self, ctx: commands.Context, char_id: str) -> None:
        cid = (char_id or "").strip()
        if not cid.isdigit():
            await ctx.reply("Give me a numeric character id, example: 40123")
            return
        cid = str(int(cid))

        entry = self._char_index.get(cid)
        if not isinstance(entry, dict):
            await ctx.reply(f"I do not know character id {cid}.")
            return

        try:
            sid = str(int(entry.get("series_id")))
        except Exception:
            await ctx.reply("I found the id, but the map entry has no usable series_id.")
            return

        series_name = str(entry.get("series") or "").strip() or self._series_index.get(sid, "Unknown series")
        char_name = str(entry.get("name") or "").strip() or f"ID {cid}"
        await self._remove_explicit_tracking(int(ctx.author.id), sid, cid)
        await ctx.reply(f"Stopped tracking {char_name} in ({sid}) {series_name}.")

    @commands.hybrid_command(name="wgwatch", with_app_command=True)
    async def wgwatch(self, ctx: commands.Context, series: str) -> None:
        sid = str(int(series))
        uid = int(ctx.author.id)
        watched = await self.listener_config.user_from_id(uid).watched_series()
        if not isinstance(watched, list):
            watched = []
        if sid not in watched:
            watched.append(sid)
            watched = sorted(set(str(int(x)) for x in watched if str(x).isdigit()), key=lambda s: int(s))
            await self.listener_config.user_from_id(uid).watched_series.set(watched)

        watchers = await self.listener_config.series_watchers_by_series()
        if not isinstance(watchers, dict):
            watchers = {}
        uids = set(int(x) for x in (watchers.get(sid) or []) if isinstance(x, int) or str(x).isdigit())
        uids.add(uid)
        watchers[sid] = sorted(uids)
        await self.listener_config.series_watchers_by_series.set(watchers)
        await ctx.reply(f"Watching ({sid}) {self._series_index.get(sid, 'Unknown series')}.")

    @wgwatch.app_command.autocomplete("series")
    async def _wgwatch_ac(self, interaction: discord.Interaction, current: str):
        return await self._ac_series(interaction, current)

    @commands.hybrid_command(name="wgunwatch", with_app_command=True)
    async def wgunwatch(self, ctx: commands.Context, series: str) -> None:
        sid = str(int(series))
        uid = int(ctx.author.id)
        watched = await self.listener_config.user_from_id(uid).watched_series()
        if not isinstance(watched, list):
            watched = []
        watched = [str(int(x)) for x in watched if str(x).isdigit() and str(int(x)) != sid]
        await self.listener_config.user_from_id(uid).watched_series.set(watched)

        watchers = await self.listener_config.series_watchers_by_series()
        if not isinstance(watchers, dict):
            watchers = {}
        cur = [int(x) for x in (watchers.get(sid) or []) if isinstance(x, int) or str(x).isdigit()]
        cur = [x for x in cur if x != uid]
        if cur:
            watchers[sid] = sorted(set(cur))
        else:
            watchers.pop(sid, None)
        await self.listener_config.series_watchers_by_series.set(watchers)
        await ctx.reply(f"Stopped watching ({sid}) {self._series_index.get(sid, 'Unknown series')}.")

    @wgunwatch.app_command.autocomplete("series")
    async def _wgunwatch_ac(self, interaction: discord.Interaction, current: str):
        return await self._ac_series(interaction, current)

    @commands.hybrid_command(name="wgtracked", with_app_command=True)
    async def wgtracked(self, ctx: commands.Context) -> None:
        uid = int(ctx.author.id)
        watched = await self.listener_config.user_from_id(uid).watched_series()
        if not isinstance(watched, list):
            watched = []
        watched_clean = sorted({str(int(s)) for s in watched if str(s).isdigit()}, key=lambda s: int(s))

        missing = await self.listener_config.user_from_id(uid).completion_missing_by_series()
        if not isinstance(missing, dict):
            missing = {}

        tracked_chars = await self.listener_config.user_from_id(uid).tracked_characters_by_series()
        if not isinstance(tracked_chars, dict):
            tracked_chars = {}

        page1 = discord.Embed(title="Waifugami Tracking", color=6235533)
        if watched_clean:
            lines = [f"({sid}) {self._series_index.get(sid, 'Unknown')}" for sid in watched_clean]
            page1.add_field(name="Watching series", value="\n".join(lines), inline=False)
        else:
            page1.add_field(name="Watching series", value="None", inline=False)

        def counts_for(store: Dict[str, Any]) -> List[Tuple[str, int]]:
            out: List[Tuple[str, int]] = []
            for sid, ids in store.items():
                if not str(sid).isdigit() or not isinstance(ids, list):
                    continue
                clean_ids = {str(int(x)) for x in ids if str(x).isdigit()}
                if clean_ids:
                    out.append((str(int(sid)), len(clean_ids)))
            out.sort(key=lambda t: int(t[0]))
            return out

        page2 = discord.Embed(title="Waifugami Completion & Character Tracking", color=6235533)
        completion_counts = counts_for(missing)
        if completion_counts:
            lines2 = [f"({sid}) {self._series_index.get(sid, 'Unknown')}: {count}" for sid, count in completion_counts]
            page2.add_field(name="Still needed (completion)", value="\n".join(lines2), inline=False)
        else:
            page2.add_field(name="Still needed (completion)", value="None", inline=False)

        tracked_counts = counts_for(tracked_chars)
        if tracked_counts:
            lines3 = [f"({sid}) {self._series_index.get(sid, 'Unknown')}: {count}" for sid, count in tracked_counts]
            page2.add_field(name="Explicitly tracked characters", value="\n".join(lines3), inline=False)
        else:
            page2.add_field(name="Explicitly tracked characters", value="None", inline=False)

        pages = [page1, page2]
        for i, embed in enumerate(pages, start=1):
            embed.set_footer(text=f"Page {i} of {len(pages)}")
        view = WGTrackedView(invoker_user_id=uid, pages=pages)
        await ctx.reply(embed=pages[0], view=view)

    @commands.hybrid_command(name="wgtrackedids", with_app_command=True)
    async def wgtrackedids(self, ctx: commands.Context, series: str) -> None:
        sid = str(int(series))
        uid = int(ctx.author.id)

        missing = await self.listener_config.user_from_id(uid).completion_missing_by_series()
        if not isinstance(missing, dict):
            missing = {}
        tracked_chars = await self.listener_config.user_from_id(uid).tracked_characters_by_series()
        if not isinstance(tracked_chars, dict):
            tracked_chars = {}

        missing_ids = {str(int(x)) for x in (missing.get(sid) or []) if str(x).isdigit()}
        tracked_ids = {str(int(x)) for x in (tracked_chars.get(sid) or []) if str(x).isdigit()}
        all_ids = sorted(missing_ids | tracked_ids, key=lambda s: int(s))

        if not all_ids:
            await ctx.reply(f"No tracked IDs for ({sid}) {self._series_index.get(sid, 'Unknown')}.")
            return

        def label(cid: str) -> str:
            if cid in missing_ids and cid in tracked_ids:
                return f"{cid} — needed + tracked"
            if cid in missing_ids:
                return f"{cid} — needed"
            return f"{cid} — tracked"

        lines = [label(cid) for cid in all_ids]
        pages: List[discord.Embed] = []
        title = f"({sid}) {self._series_index.get(sid, 'Unknown')}"
        for idx, page_chunk in enumerate(chunk(lines, 20), start=1):
            embed = discord.Embed(
                title=title,
                description="\n".join(page_chunk),
                color=6235533,
            )
            embed.set_footer(text=f"Page {idx} of {(len(lines) + 19) // 20}")
            pages.append(embed)
        view = WGTrackedView(invoker_user_id=uid, pages=pages)
        await ctx.reply(embed=pages[0], view=view)

    @wgtrackedids.app_command.autocomplete("series")
    async def _wgtrackedids_ac(self, interaction: discord.Interaction, current: str):
        return await self._ac_series(interaction, current)

    @commands.hybrid_group(name="wgtieralert", invoke_without_command=True)
    async def wgtieralert(self, ctx: commands.Context) -> None:
        tier_names = " | ".join(WG_TIER_ALERTS.keys())
        await ctx.reply(f"Use `/wgtieralert add tier:<{tier_names}>`.")

    @wgtieralert.command(name="add")
    @app_commands.describe(tier="Tier to subscribe to.")
    @app_commands.choices(
        tier=[
            app_commands.Choice(name="zeta", value="zeta"),
            app_commands.Choice(name="fake", value="fake"),
            app_commands.Choice(name="sigma", value="sigma"),
            app_commands.Choice(name="epsilon", value="epsilon"),
        ]
    )
    async def wgtieralert_add(self, ctx: commands.Context, tier: str) -> None:
        tier_key = self._normalise_tier_key(tier)
        if tier_key is None:
            await ctx.reply(f"Unknown tier. Choices: `{', '.join(WG_TIER_ALERTS.keys())}`.")
            return

        user_id = int(ctx.author.id)

        legacy_ids = await self.listener_config.tier_alert_user_ids()
        if not isinstance(legacy_ids, list):
            legacy_ids = []
        legacy_clean = {int(x) for x in legacy_ids if isinstance(x, int) or str(x).isdigit()}
        legacy_clean.discard(user_id)
        await self.listener_config.tier_alert_user_ids.set(sorted(legacy_clean))

        by_tier = await self.listener_config.tier_alert_user_ids_by_tier()
        if not isinstance(by_tier, dict):
            by_tier = {}

        ids = by_tier.get(tier_key) or []
        clean = {int(x) for x in ids if isinstance(x, int) or str(x).isdigit()}
        clean.add(user_id)
        by_tier[tier_key] = sorted(clean)
        await self.listener_config.tier_alert_user_ids_by_tier.set(by_tier)

        await ctx.reply(f"Added you to private {self._tier_display_name(tier_key)} tier DMs.")

    @wgtieralert.command(name="remove")
    @app_commands.describe(tier="Tier to unsubscribe from.")
    @app_commands.choices(
        tier=[
            app_commands.Choice(name="zeta", value="zeta"),
            app_commands.Choice(name="fake", value="fake"),
            app_commands.Choice(name="sigma", value="sigma"),
            app_commands.Choice(name="epsilon", value="epsilon"),
        ]
    )
    async def wgtieralert_remove(self, ctx: commands.Context, tier: str) -> None:
        tier_key = self._normalise_tier_key(tier)
        if tier_key is None:
            await ctx.reply(f"Unknown tier. Choices: `{', '.join(WG_TIER_ALERTS.keys())}`.")
            return

        user_id = int(ctx.author.id)
        by_tier = await self.listener_config.tier_alert_user_ids_by_tier()
        if not isinstance(by_tier, dict):
            by_tier = {}

        ids = by_tier.get(tier_key) or []
        clean = {int(x) for x in ids if isinstance(x, int) or str(x).isdigit()}
        clean.discard(user_id)
        if clean:
            by_tier[tier_key] = sorted(clean)
        else:
            by_tier.pop(tier_key, None)
        await self.listener_config.tier_alert_user_ids_by_tier.set(by_tier)

        await ctx.reply(f"Removed you from private {self._tier_display_name(tier_key)} tier DMs.")

    @wgtieralert.command(name="clear")
    async def wgtieralert_clear(self, ctx: commands.Context) -> None:
        user_id = int(ctx.author.id)

        legacy_ids = await self.listener_config.tier_alert_user_ids()
        if not isinstance(legacy_ids, list):
            legacy_ids = []
        legacy_clean = {int(x) for x in legacy_ids if isinstance(x, int) or str(x).isdigit()}
        legacy_clean.discard(user_id)
        await self.listener_config.tier_alert_user_ids.set(sorted(legacy_clean))

        by_tier = await self.listener_config.tier_alert_user_ids_by_tier()
        if not isinstance(by_tier, dict):
            by_tier = {}

        for key in list(by_tier.keys()):
            ids = by_tier.get(key) or []
            clean = {int(x) for x in ids if isinstance(x, int) or str(x).isdigit()}
            clean.discard(user_id)
            if clean:
                by_tier[key] = sorted(clean)
            else:
                by_tier.pop(key, None)

        await self.listener_config.tier_alert_user_ids_by_tier.set(by_tier)
        await ctx.reply("Removed you from all private tier DMs.")

    @wgtieralert.command(name="list")
    async def wgtieralert_list(self, ctx: commands.Context) -> None:
        user_id = int(ctx.author.id)

        legacy_ids = await self.listener_config.tier_alert_user_ids()
        if not isinstance(legacy_ids, list):
            legacy_ids = []
        legacy_clean = {int(x) for x in legacy_ids if isinstance(x, int) or str(x).isdigit()}
        if user_id in legacy_clean:
            await ctx.reply(f"You are subscribed to all private tier DMs: {self._tier_display_list()}.")
            return

        by_tier = await self.listener_config.tier_alert_user_ids_by_tier()
        if not isinstance(by_tier, dict):
            by_tier = {}

        subscribed: List[str] = []
        for key in WG_TIER_ALERTS:
            ids = by_tier.get(key) or []
            clean = {int(x) for x in ids if isinstance(x, int) or str(x).isdigit()}
            if user_id in clean:
                subscribed.append(self._tier_display_name(key))

        if not subscribed:
            await ctx.reply("You are not subscribed to any private tier DMs.")
            return

        await ctx.reply("Your private tier DMs: " + ", ".join(subscribed) + ".")

    # ------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------
    @commands.Cog.listener(name="on_message")
    async def on_message_spawn(self, message: discord.Message) -> None:
        if message.author.bot:
            waifugami_bot_id = await self._get_waifugami_bot_id()
            if message.author.id != waifugami_bot_id:
                return

            # `.i <id>` character-info embeds can be run in any channel, not
            # just configured spawn channels, and learning from them is a
            # standalone catalog side-effect, so this runs before (and
            # regardless of) the spawn-channel restriction below.
            was_info = await self._learn_from_info_embed(message)
            if was_info:
                # Advance any WL session whose next queued id matches this embed.
                parsed_info = self._parse_info_embed(message.embeds[0]) if message.embeds else None
                if parsed_info:
                    learned_id = parsed_info[0]  # waifu_id str
                    for uid, sess in list(self._active_wl_sessions.items()):
                        if (sess.get("channel_id") == message.channel.id
                                and sess.get("queue")
                                and sess["queue"][0][0] == learned_id):
                            await self._wl_advance(uid, learned=True, waifu_id=learned_id)
                            break
            elif message.content and INFO_NOT_FOUND_RE.search(message.content):
                # Waifugami returned 'Something went wrong' (catalog gap).
                # Advance any session waiting in this channel — the gap id is
                # skipped rather than stalling the whole session.
                for uid, sess in list(self._active_wl_sessions.items()):
                    if sess.get("channel_id") == message.channel.id and sess.get("queue"):
                        await self._wl_advance(uid, learned=False, waifu_id=sess["queue"][0][0])
                        break

            spawn_channel_ids = await self._get_spawn_channel_ids()
            event_channel_ids = await self._get_event_channel_ids()
            if message.channel.id not in spawn_channel_ids and message.channel.id not in event_channel_ids:
                return
            await self._handle_waifugami_spawn_message(message)
            return

        content = (message.content or "").strip().lower()
        if content == "cancel":
            session = self._active_wl_sessions.get(message.author.id)
            if session and session.get("channel_id") == message.channel.id:
                done = session["done"]
                total = session["total"]
                self._finish_wl_session(message.author.id)
                try:
                    await message.channel.send(
                        f"WL learn session cancelled. Learned {done} of {total} this session."
                    )
                except discord.HTTPException:
                    pass
                return

        if content == "wgscan" and message.reference and message.reference.message_id:
            ref = await self._get_referenced_message(message)
            if ref and ref.embeds and self._is_waifu_completion_embed(ref.embeds[0]):
                await self._scan_completion_message(message.author.id, ref)
            return

        if content == "wgupdate" and message.reference and message.reference.message_id:
            ref = await self._get_referenced_message(message)
            if ref is None:
                return
            await self._start_series_update_from_message(
                actor=message.author,
                trigger_message=message,
                target_message=ref,
            )
            return


    @commands.Cog.listener(name="on_message_edit")
    async def on_message_edit_spawn(self, before: discord.Message, after: discord.Message) -> None:
        if await self._handle_series_update_edit(after):
            return

        completion_state = self._active_completion_scans.get(after.id)
        if completion_state:
            user_id = int(completion_state.get("user_id") or 0)
            if not user_id:
                self._active_completion_scans.pop(after.id, None)
                return
            if after.embeds and self._is_waifu_completion_embed(after.embeds[0]):
                await self._scan_completion_message(user_id, after)
                return

        if after.author.id != await self._get_waifugami_bot_id():
            return
        spawn_channel_ids = await self._get_spawn_channel_ids()
        event_channel_ids = await self._get_event_channel_ids()
        if after.channel.id not in spawn_channel_ids and after.channel.id not in event_channel_ids:
            return

        if await self._handle_claimed_character_edit(after):
            return

        await self._handle_waifugami_spawn_message(after)



    # ------------------------------------------------------------
    # Unified "wg" command tree
    # ------------------------------------------------------------
    # Every command below forwards to the original, still-intact
    # implementation via `.callback(...)`, so behaviour is unchanged.
    # This section exists purely so `[p]help wg` shows everything at once.
    # Each legacy top-level name/alias keeps working exactly as before.

    @commands.group(name="wg", invoke_without_command=True)
    async def wg(self, ctx: commands.Context) -> None:
        """Waifugami — card tracking, team building, and spawn tools.

        Run `[p]help wg` to see every Waifugami command in one place.
        Slash-only commands (`/wgcards`, `/wgcd`) and the right-click
        "Name" context menu live outside this prefix tree, unchanged.
        Replying to a message with `[p]wgupdate` or `[p]wgscan` also
        still works exactly as before.
        """
        await ctx.send_help(ctx.command)

    # ---- card tracking & browsing ----

    @wg.group(name="track", invoke_without_command=True)
    async def wg_track(self, ctx: commands.Context) -> None:
        """Enable, disable, or clear your Waifugami card tracking."""
        await self.card_tracking_group.callback(self, ctx)

    @wg_track.command(name="enable")
    async def wg_track_enable(self, ctx: commands.Context) -> None:
        """Start tracking your Waifugami cards."""
        await self.wgtrack_enable.callback(self, ctx)

    @wg_track.command(name="disable")
    async def wg_track_disable(self, ctx: commands.Context) -> None:
        """Pause tracking, keeping your stored data."""
        await self.wgtrack_disable.callback(self, ctx)

    @wg_track.command(name="clear")
    async def wg_track_clear(self, ctx: commands.Context) -> None:
        """Delete all of your stored tracking data."""
        await self.wgtrack_clear.callback(self, ctx)

    @wg.command(name="cd")
    async def wg_cd(self, ctx: commands.Context) -> None:
        """Show your Waifugami cooldowns."""
        await self.wgcd_prefix.callback(self, ctx)

    # ---- completion tracking & series watching ----

    @wg.command(name="status")
    async def wg_status(self, ctx: commands.Context) -> None:
        """Show spawn-tracking configuration and catalog stats. Same as `[p]wgstatus`."""
        await self.wgstatus.callback(self, ctx)

    @wg.group(name="wishlist", invoke_without_command=True)
    async def wg_wishlist(self, ctx: commands.Context, *, query: Optional[str] = None) -> None:
        """Show wishlist browser."""
        await self.wgwishlist.callback(self, ctx, query=query)

    @wg_wishlist.command(name="dump")
    async def wg_wishlist_dump(self, ctx: commands.Context, *, dump: str = "") -> None:
        """Import wishlist counts from a Kazuha `orderwl` DM dump.
        """
        await self._do_wl_dump(ctx, dump)

    @wg.command(name="learnwl")
    async def wg_learnwl(
        self, ctx: commands.Context, *, series: Optional[str] = None
    ) -> None:
        """Start a session to learn Wishlist values for every catalog character.

        Optional argument: a series ID or series name to limit the session to
        one series (e.g. `..wg learnwl 25` or `..wg learnwl Date A Live`).

        Type `cancel` in this channel to stop the session at any time.
        """
        filter_sid: Optional[str] = None
        if series:
            s = series.strip()
            if s.isdigit():
                sid_candidate = str(int(s))
                if sid_candidate in self._series_index:
                    filter_sid = sid_candidate
                else:
                    await ctx.reply(f"Series ID {s!r} not found in the catalog. Try `..wg learnwl` without a filter, or run `..wgupdate` first.")
                    return
            else:
                s_cf = s.casefold()
                for sid_c, sname in self._series_index.items():
                    if sname.casefold() == s_cf:
                        filter_sid = sid_c
                        break
                if filter_sid is None:
                    await ctx.reply(f"Series {s!r} not found in the catalog. Try the numeric series ID instead.")
                    return

        # Cancel any existing session for this user first.
        if ctx.author.id in self._active_wl_sessions:
            self._finish_wl_session(ctx.author.id)

        await self._start_wl_session(ctx, filter_series_id=filter_sid)

    @wg.command(name="trackid")
    async def wg_trackid(self, ctx: commands.Context, char_id: str) -> None:
        """Track completion of one character id."""
        await self.wgtrackid.callback(self, ctx, char_id)

    @wg.command(name="untrackid")
    async def wg_untrackid(self, ctx: commands.Context, char_id: str) -> None:
        """Stop tracking one character id."""
        await self.wguntrackid.callback(self, ctx, char_id)

    @wg.command(name="watch")
    async def wg_watch(self, ctx: commands.Context, series: str) -> None:
        """Watch a series for new spawns."""
        await self.wgwatch.callback(self, ctx, series)

    @wg.command(name="unwatch")
    async def wg_unwatch(self, ctx: commands.Context, series: str) -> None:
        """Stop watching a series."""
        await self.wgunwatch.callback(self, ctx, series)

    @wg.command(name="tracked")
    async def wg_tracked(self, ctx: commands.Context) -> None:
        """Show the series you watch and the characters you track."""
        await self.wgtracked.callback(self, ctx)

    @wg.command(name="trackedids")
    async def wg_trackedids(self, ctx: commands.Context, series: str) -> None:
        """List your tracked character ids for one series."""
        await self.wgtrackedids.callback(self, ctx, series)

    # ---- series display toggle ----

    @wg.group(name="series", invoke_without_command=True)
    async def wg_series(self, ctx: commands.Context) -> None:
        """Toggle whether spawn names also show their series."""
        await self.wgseries.callback(self, ctx)

    @wg_series.command(name="on")
    async def wg_series_on(self, ctx: commands.Context) -> None:
        """Turn series display on."""
        await self.wgseries_on.callback(self, ctx)

    @wg_series.command(name="off")
    async def wg_series_off(self, ctx: commands.Context) -> None:
        """Turn series display off."""
        await self.wgseries_off.callback(self, ctx)

    @wg_series.command(name="status")
    async def wg_series_status(self, ctx: commands.Context) -> None:
        """Show whether series display is currently on or off."""
        await self.wgseries_status.callback(self, ctx)

    # ---- manual event-tier overrides (admin) ----

    @wg.group(name="event", invoke_without_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def wg_event(self, ctx: commands.Context) -> None:
        """Manage manual event-tier overrides for spawn image ids."""
        await self.wgevent.callback(self, ctx)

    @wg_event.command(name="link")
    @commands.has_guild_permissions(manage_guild=True)
    async def wg_event_link(self, ctx: commands.Context, link: str, event: bool) -> None:
        """Force a single image id to always/never count as an event spawn."""
        await self.wgevent_link.callback(self, ctx, link, event)

    @wg_event.command(name="range")
    @commands.has_guild_permissions(manage_guild=True)
    async def wg_event_range(self, ctx: commands.Context, start: int, end: int, event: bool) -> None:
        """Force a range of image ids to always/never count as event spawns."""
        await self.wgevent_range.callback(self, ctx, start, end, event)

    @wg_event.command(name="status")
    async def wg_event_status(self, ctx: commands.Context, link: str) -> None:
        """Show whether an image id currently counts as an event spawn."""
        await self.wgevent_status.callback(self, ctx, link)

    # ---- debug log channel (admin) ----

    @wg.group(name="debug", invoke_without_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def wg_debug(self, ctx: commands.Context) -> None:
        """Manage the debug log channel."""
        await self.wgdebug.callback(self, ctx)

    @wg_debug.command(name="set")
    @commands.has_guild_permissions(manage_guild=True)
    async def wg_debug_set(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        """Set the debug log channel."""
        await self.wgdebug_set.callback(self, ctx, channel)

    @wg_debug.command(name="off")
    @commands.has_guild_permissions(manage_guild=True)
    async def wg_debug_off(self, ctx: commands.Context) -> None:
        """Unset the debug log channel (logs go to console instead)."""
        await self.wgdebug_off.callback(self, ctx)

    @wg_debug.command(name="ping")
    async def wg_debug_ping(self, ctx: commands.Context) -> None:
        """Send a test line to the debug log channel."""
        await self.wgdebug_ping.callback(self, ctx)

    # ---- spawn channels (admin) ----

    @wg.group(name="channels", invoke_without_command=True)
    @commands.admin_or_permissions(manage_guild=True)
    async def wg_channels(self, ctx: commands.Context) -> None:
        """Manage which channels are watched for Waifugami spawns."""
        await self.wgchannels.callback(self, ctx)

    @wg_channels.command(name="add")
    @commands.admin_or_permissions(manage_guild=True)
    async def wg_channels_add(self, ctx: commands.Context, channel_id: int) -> None:
        """Add a spawn channel by id."""
        await self.wgchannels_add.callback(self, ctx, channel_id)

    @wg_channels.command(name="remove")
    @commands.admin_or_permissions(manage_guild=True)
    async def wg_channels_remove(self, ctx: commands.Context, channel_id: int) -> None:
        """Remove a spawn channel by id."""
        await self.wgchannels_remove.callback(self, ctx, channel_id)

    @wg_channels.command(name="list")
    async def wg_channels_list(self, ctx: commands.Context) -> None:
        """List currently configured spawn channels."""
        await self.wgchannels_list.callback(self, ctx)

    # ---- tier alert DMs ----

    @wg.group(name="tieralert", invoke_without_command=True)
    async def wg_tieralert(self, ctx: commands.Context) -> None:
        """Subscribe to private DMs for rare spawn tiers."""
        await self.wgtieralert.callback(self, ctx)

    @wg_tieralert.command(name="add")
    async def wg_tieralert_add(self, ctx: commands.Context, tier: str) -> None:
        """Subscribe to a tier's private spawn DMs (zeta, fake, sigma, epsilon)."""
        await self.wgtieralert_add.callback(self, ctx, tier)

    @wg_tieralert.command(name="remove")
    async def wg_tieralert_remove(self, ctx: commands.Context, tier: str) -> None:
        """Unsubscribe from a tier's private spawn DMs."""
        await self.wgtieralert_remove.callback(self, ctx, tier)

    @wg_tieralert.command(name="clear")
    async def wg_tieralert_clear(self, ctx: commands.Context) -> None:
        """Unsubscribe from every tier's private DMs."""
        await self.wgtieralert_clear.callback(self, ctx)

    @wg_tieralert.command(name="list")
    async def wg_tieralert_list(self, ctx: commands.Context) -> None:
        """Show which tiers you're currently subscribed to."""
        await self.wgtieralert_list.callback(self, ctx)

    # ---- audit engine ----

    @wg.group(name="audit", invoke_without_command=True)
    async def wg_audit(self, ctx: commands.Context) -> None:
        """List Audit & Cleanup Engine — identify and safely remove low-value cards.

        Workflow:
          1. ``[p]wg audit start``       — begin session; run `.l -event all` when prompted
          2. ``[p]wg audit sell``        — browse SELL candidates
          3. ``[p]wg audit confirm``     — execute `.rm` batches

        Same as ``[p]wgaudit`` / ``[p]wga``.
        """
        await self._wg_audit_dispatch(ctx)

    @wg_audit.command(name="start")
    async def wg_audit_start(self, ctx: commands.Context) -> None:
        """Begin an audit session and wait for `.l -event all`."""
        await self.wgaudit_start.callback(self, ctx)

    @wg_audit.command(name="classify")
    async def wg_audit_classify(self, ctx: commands.Context) -> None:
        """Classify without harvesting event cards."""
        await self.wgaudit_classify.callback(self, ctx)

    @wg_audit.command(name="sell")
    async def wg_audit_sell(self, ctx: commands.Context, *, args: str = "") -> None:
        """Browse SELL candidates."""
        await self.wgaudit_sell.callback(self, ctx, args=args)

    @wg_audit.command(name="review")
    async def wg_audit_review(self, ctx: commands.Context) -> None:
        """Inspect REVIEW / UNKNOWN cards."""
        await self.wgaudit_review.callback(self, ctx)

    @wg_audit.command(name="confirm")
    async def wg_audit_confirm(self, ctx: commands.Context, *, ids_str: str = "") -> None:
        """Execute removal of SELL cards."""
        await self.wgaudit_confirm.callback(self, ctx, ids_str=ids_str)

    @wg_audit.command(name="cancel")
    async def wg_audit_cancel(self, ctx: commands.Context) -> None:
        """Cancel the active audit without removing anything."""
        await self.wgaudit_cancel.callback(self, ctx)

    @wg_audit.command(name="status")
    async def wg_audit_status(self, ctx: commands.Context) -> None:
        """Show current audit session state."""
        await self.wgaudit_status.callback(self, ctx)

