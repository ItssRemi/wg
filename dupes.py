"""Duplicate-review workflow (``list dupes``).

How it works
------------
1. Run a Waifugami list, e.g.  ``.l -event none -orderby quantity -series 65``
2. Reply to that list embed with ``list dupes``.
3. Flip through every page. Nebula reads each page as it appears and groups
   the cards by character name. Every character with two or more copies is a
   duplicate group, and Nebula hands you ``.v`` commands (5 IDs at a time)
   for those copies only.
4. Run the ``.v`` commands. Nebula reads Skill and Luck from each result.
5. Once every page has been seen and every duplicate copy has been viewed,
   Nebula posts ``.fav <up to 20 ids> 🚮`` commands for the copies to trash.
   You do not have to flip every page: say ``dupes done`` at any point to
   stop reading pages and review only what has been scanned so far (Nebula
   still asks you to ``.v`` the duplicates already found).
6. Reply ``keep 123`` or ``keep 123 456 789`` to pull cards out of the trash
   list. Nebula reposts the updated ``.fav`` commands.

Everything here is session-only. Local IDs shift whenever cards are removed,
so nothing is persisted between sessions. As a safety check, if a local ID
ever shows a different character name than the list showed, the session is
aborted instead of risking the wrong card.

Nothing is ever deleted by this workflow: ``.fav ... 🚮`` only tags cards.
Do not ``.rm`` cards between the list and the ``.fav``, or IDs will shift.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import discord

from .audit_engine import WAIFUGAMI_ID

log = logging.getLogger("red.wg.dupes")

DUPES_SESSION_TTL = 60 * 60       # seconds of inactivity
DUPES_VIEW_BATCH = 5              # `.v` accepts at most 5 ids
DUPES_FAV_BATCH = 20              # ids per `.fav` command
DUPES_MESSAGE_LIMIT = 1900        # stay under Discord's 2000-char limit
TRASH_EMOJI = "🚮"

# Skill decides first. A copy is "good" (always kept) only when its Skill
# reaches GOOD_SKILL AND its Luck reaches GOOD_LUCK. Luck can never rescue a
# low-Skill card. These are separate from the main audit thresholds on purpose.
GOOD_SKILL = 90.0
GOOD_LUCK = 6

_LIST_TITLE_RE = re.compile(r"^.+?'s Waifus \(Page (\d+)\)$", re.I)
_LIST_ENTRY_RE = re.compile(r"^(\d+)\s*\|\s*(.+?)\s*$", re.M)
_ENTRY_BODY_RE = re.compile(r"\[([^\]]+)\]\s*(.+)$")
_PAGE_FIELD_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)
_DONE_RE = re.compile(r"(?:list\s+)?dupes\s+done", re.I)
_KEEP_RE = re.compile(r"\.?keep\s+(\d+(?:[\s,]+\d+)*)", re.I)


def _name_key(name: str) -> str:
    return name.strip().strip("*_`~ ").casefold()


def _chunk_lines(lines: List[str], limit: int = DUPES_MESSAGE_LIMIT) -> List[str]:
    """Pack lines into messages no longer than ``limit`` characters."""
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for line in lines:
        add = len(line) + (1 if current else 0)
        if current and size + add > limit:
            chunks.append("\n".join(current))
            current, size = [], 0
            add = len(line)
        current.append(line)
        size += add
    if current:
        chunks.append("\n".join(current))
    return chunks


def _fmt_stats(stats: Dict[str, Any]) -> str:
    skill = stats.get("skill")
    luck = stats.get("luck")
    skill_s = f"{skill:.2f}" if skill is not None else "?"
    luck_s = f"{luck:+d}" if luck is not None else "?"
    return f"Skill {skill_s} · Luck {luck_s}"


class DupesMixin:
    """``list dupes`` workflow. Mixed into the Waifugami cog."""

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def _dupes_init(self) -> None:
        self._dupe_sessions: Dict[int, Dict[str, Any]] = {}
        # list message id -> user id
        self._dupe_list_owners: Dict[int, int] = {}

    def _dupes_drop(self, user_id: int) -> None:
        session = self._dupe_sessions.pop(user_id, None)
        if session:
            self._dupe_list_owners.pop(session["list_message_id"], None)

    def _dupes_live_session(self, user_id: int) -> Optional[Dict[str, Any]]:
        session = self._dupe_sessions.get(user_id)
        if not session:
            return None
        if time.monotonic() - session["last_active"] > DUPES_SESSION_TTL:
            self._dupes_drop(user_id)
            return None
        return session

    # ── Parsing ───────────────────────────────────────────────────────────

    @staticmethod
    def _dupes_parse_list(
        embed: discord.Embed,
    ) -> Optional[Tuple[int, Optional[int], List[Tuple[int, str, str]]]]:
        """Parse one list page.

        Returns (page_index, last_page_index, [(local_id, rarity, name)])
        or None when the embed is not a Waifugami list page.
        """
        title_match = _LIST_TITLE_RE.match(embed.title or "")
        if not title_match:
            return None
        page_index = int(title_match.group(1))
        last_page: Optional[int] = None
        for field in embed.fields:
            field_match = _PAGE_FIELD_RE.search(field.name or "")
            if field_match:
                page_index = int(field_match.group(1))
                last_page = int(field_match.group(2))
                break

        entries: List[Tuple[int, str, str]] = []
        for raw_id, raw_entry in _LIST_ENTRY_RE.findall(embed.description or ""):
            body = _ENTRY_BODY_RE.search(raw_entry.strip())
            if not body:
                continue
            name = body.group(2).strip().strip("*_`~ ")
            if not name:
                continue
            entries.append((int(raw_id), body.group(1).strip(), name))
        return page_index, last_page, entries

    # ── Session state helpers ─────────────────────────────────────────────

    @staticmethod
    def _dupes_pages_complete(session: Dict[str, Any]) -> bool:
        last = session["last_page"]
        if last is None:
            return False
        return set(range(last + 1)).issubset(session["pages"])

    @staticmethod
    def _dupes_dupe_ids(session: Dict[str, Any]) -> List[int]:
        """Local IDs of every copy of every character with 2+ copies,
        in list order."""
        ids: List[int] = []
        for members in session["name_ids"].values():
            if len(members) >= 2:
                ids.extend(members)
        ids.sort(key=lambda i: session["entries"][i]["pos"])
        return ids

    def _dupes_unviewed(self, session: Dict[str, Any]) -> List[int]:
        return [
            i for i in self._dupes_dupe_ids(session)
            if i not in session["stats"]
        ]

    def _dupes_ingest(
        self,
        session: Dict[str, Any],
        page_index: int,
        last_page: Optional[int],
        entries: List[Tuple[int, str, str]],
    ) -> Optional[str]:
        """Record one page. Returns an error string if the list is no
        longer consistent with what was already seen, else None."""
        signature = tuple((i, _name_key(n)) for i, _r, n in entries)

        if last_page is not None:
            previous = session["last_page"]
            session["last_page"] = (
                last_page if previous is None else max(previous, last_page)
            )

        known = session["pages"].get(page_index)
        if known is not None:
            if known != signature:
                return (
                    f"Page {page_index} changed since I first read it "
                    "(local IDs shifted)."
                )
            return None

        for row, (local_id, rarity, name) in enumerate(entries):
            existing = session["entries"].get(local_id)
            if existing is not None:
                if existing["key"] != _name_key(name):
                    return (
                        f"Local ID {local_id} was `{existing['name']}` and "
                        f"is now `{name}` (local IDs shifted)."
                    )
                continue
            session["entries"][local_id] = {
                "name": name,
                "key": _name_key(name),
                "rarity": rarity,
                "pos": (page_index, row),
            }
            session["name_ids"].setdefault(_name_key(name), []).append(local_id)

        session["pages"][page_index] = signature
        return None

    # ── Starting a session ────────────────────────────────────────────────

    async def _dupes_on_user_message(self, message: discord.Message) -> bool:
        """Handle ``list dupes`` and ``keep ...``. True if consumed."""
        content = (message.content or "").strip()
        if not content:
            return False

        if content.casefold() == "list dupes":
            return await self._dupes_try_start(message)

        if _DONE_RE.fullmatch(content):
            session = self._dupes_live_session(message.author.id)
            if not session or session["channel_id"] != message.channel.id:
                return False
            await self._dupes_close(message, session)
            return True

        keep_match = _KEEP_RE.fullmatch(content)
        if keep_match:
            session = self._dupes_live_session(message.author.id)
            if not session or session["channel_id"] != message.channel.id:
                return False
            ids = [int(v) for v in re.split(r"[\s,]+", keep_match.group(1))]
            await self._dupes_apply_keep(message, session, ids)
            return True

        return False

    async def _dupes_close(
        self, message: discord.Message, session: Dict[str, Any]
    ) -> None:
        """`dupes done`: stop reading pages and review what was scanned."""
        async with session["lock"]:
            if session["phase"] != "collecting":
                await message.reply(
                    "This scan is already finished.", mention_author=False
                )
                return
            session["closed"] = True
            session["last_active"] = time.monotonic()
            unviewed = self._dupes_unviewed(session)
            if unviewed:
                await message.reply(
                    f"Closing the scan with `{len(session['pages'])}` page(s) "
                    f"read. `{len(unviewed)}` duplicate card(s) still need "
                    "a `.v`.",
                    mention_author=False,
                )
            await self._dupes_refresh(session, post_new=True)

    async def _dupes_try_start(self, message: discord.Message) -> bool:
        reference = message.reference
        if not reference or not reference.message_id:
            return False
        list_message = reference.resolved
        if not isinstance(list_message, discord.Message):
            try:
                list_message = await message.channel.fetch_message(
                    reference.message_id
                )
            except discord.HTTPException:
                return False
        if list_message.author.id != WAIFUGAMI_ID or not list_message.embeds:
            return False
        parsed = self._dupes_parse_list(list_message.embeds[0])
        if parsed is None:
            return False

        self._dupes_drop(message.author.id)

        response = await message.reply(
            "Preparing the duplicate scan...", mention_author=False
        )
        session: Dict[str, Any] = {
            "user_id": message.author.id,
            "channel_id": message.channel.id,
            "list_message_id": list_message.id,
            "guide_message_id": response.id,
            "last_content": None,
            "created": time.monotonic(),
            "last_active": time.monotonic(),
            "pages": {},
            "last_page": None,
            "entries": {},
            "name_ids": {},
            "stats": {},
            "kept": set(),
            "trash_ids": [],
            "phase": "collecting",
            "closed": False,   # True once the user said `dupes done`
            "lock": asyncio.Lock(),
        }
        self._dupe_sessions[message.author.id] = session
        self._dupe_list_owners[list_message.id] = message.author.id

        page_index, last_page, entries = parsed
        async with session["lock"]:
            error = self._dupes_ingest(session, page_index, last_page, entries)
            if error:
                await self._dupes_abort(session, error)
                return True
            await self._dupes_refresh(session, post_new=False)
        return True

    # ── Event hooks ───────────────────────────────────────────────────────

    async def _dupes_on_list_edit(self, after: discord.Message) -> None:
        """A tracked list message was edited (user flipped a page)."""
        user_id = self._dupe_list_owners.get(after.id)
        if user_id is None or not after.embeds:
            return
        session = self._dupes_live_session(user_id)
        if (
            not session
            or session["phase"] != "collecting"
            or session["closed"]
        ):
            return
        parsed = self._dupes_parse_list(after.embeds[0])
        if parsed is None:
            return
        page_index, last_page, entries = parsed
        async with session["lock"]:
            if session["phase"] != "collecting" or session["closed"]:
                return
            session["last_active"] = time.monotonic()
            error = self._dupes_ingest(session, page_index, last_page, entries)
            if error:
                await self._dupes_abort(session, error)
                return
            await self._dupes_refresh(session, post_new=False)

    async def _dupes_on_cards(
        self, message: discord.Message, observed_cards: List[Dict[str, Any]]
    ) -> None:
        """Waifugami posted card embeds (a ``.v`` result)."""
        if not observed_cards:
            return
        owners = {int(c["owner_id"]) for c in observed_cards if c.get("owner_id")}
        for user_id in owners:
            session = self._dupes_live_session(user_id)
            if (
                not session
                or session["phase"] != "collecting"
                or session["channel_id"] != message.channel.id
            ):
                continue
            async with session["lock"]:
                if session["phase"] != "collecting":
                    continue
                recorded = 0
                for card in observed_cards:
                    if int(card.get("owner_id") or 0) != user_id:
                        continue
                    local_id = card.get("local_id")
                    entry = session["entries"].get(local_id)
                    if entry is None:
                        continue
                    if _name_key(str(card.get("name") or "")) != entry["key"]:
                        await self._dupes_abort(
                            session,
                            f"Local ID {local_id} is `{card.get('name')}` "
                            f"but the list showed `{entry['name']}` "
                            "(local IDs shifted).",
                        )
                        break
                    if local_id in session["stats"]:
                        continue
                    session["stats"][local_id] = {
                        "name": entry["name"],
                        "rarity": entry["rarity"],
                        "skill": card.get("skill"),
                        "luck": card.get("luck"),
                        "waifu_id": card.get("waifu_id"),
                    }
                    recorded += 1
                else:
                    if recorded:
                        session["last_active"] = time.monotonic()
                        await self._dupes_refresh(session, post_new=True)

    # ── Guide / progress ──────────────────────────────────────────────────

    def _dupes_progress_line(self, session: Dict[str, Any]) -> str:
        seen = len(session["pages"])
        last = session["last_page"]
        total = str(last + 1) if last is not None else "?"
        dupes = self._dupes_dupe_ids(session)
        viewed = len(dupes) - len(self._dupes_unviewed(session))
        line = (
            f"-# Duplicates · pages `{seen}/{total}` · "
            f"`{len(dupes)}` duplicate cards found · `{viewed}` viewed"
        )
        if not session["closed"] and not self._dupes_pages_complete(session):
            line += "\n-# Say `dupes done` to finish with the pages read so far."
        return line

    async def _dupes_refresh(
        self, session: Dict[str, Any], *, post_new: bool
    ) -> None:
        """Update the guide message, or finalise when everything is in.

        Caller must hold session["lock"].
        """
        unviewed = self._dupes_unviewed(session)

        finished_scanning = (
            session["closed"] or self._dupes_pages_complete(session)
        )
        if finished_scanning and not unviewed:
            await self._dupes_finalise(session)
            return

        progress = self._dupes_progress_line(session)
        if unviewed:
            batch = unviewed[:DUPES_VIEW_BATCH]
            content = f"{progress}\n`.v {' '.join(map(str, batch))}`"
        elif finished_scanning:
            content = progress
        else:
            content = f"{progress}\nAll duplicates so far are viewed. Flip to the next page."

        channel = self.bot.get_channel(session["channel_id"])
        if channel is None:
            return

        try:
            if post_new:
                guide = await channel.send(content)
                session["guide_message_id"] = guide.id
                session["last_content"] = content
            elif content != session["last_content"]:
                guide = channel.get_partial_message(session["guide_message_id"])
                await guide.edit(content=content)
                session["last_content"] = content
        except discord.HTTPException:
            log.exception("Could not update the dupes guide message")

    async def _dupes_abort(self, session: Dict[str, Any], reason: str) -> None:
        channel = self.bot.get_channel(session["channel_id"])
        self._dupes_drop(session["user_id"])
        session["phase"] = "aborted"
        if channel is None:
            return
        try:
            await channel.send(
                f"⛔ Duplicate scan stopped: {reason}\n"
                "Run the list again and reply with `list dupes` to restart. "
                "Nothing was tagged."
            )
        except discord.HTTPException:
            pass

    # ── Decision logic ────────────────────────────────────────────────────

    @staticmethod
    def _dupes_is_good(stats: Dict[str, Any]) -> bool:
        """Skill first: it must reach GOOD_SKILL, and only then does Luck
        get a say."""
        skill, luck = stats.get("skill"), stats.get("luck")
        if skill is None or luck is None:
            return False
        return skill >= GOOD_SKILL and luck >= GOOD_LUCK

    @staticmethod
    def _dupes_rank(session: Dict[str, Any], local_id: int) -> Tuple:
        """Sort key, best first: Skill, then Luck, then lowest local ID."""
        stats = session["stats"][local_id]
        return (-(stats["skill"]), -(stats["luck"]), local_id)

    def _dupes_decide(
        self, session: Dict[str, Any]
    ) -> Tuple[List[int], List[int], List[int], int, List[int]]:
        """Return (keep_ids, trash_ids, unreadable_ids, group_count,
        left_alone_ids).

        Cards are first grouped by list name, then split by the Waifu ID
        read from ``.v``. Only copies that share a Waifu ID are real
        duplicates, so two different characters (or versions) that happen to
        share a name are never compared and never trashed.

        Per group of true duplicates:
          * every good copy (Skill >= GOOD_SKILL and Luck >= GOOD_LUCK)
            is kept
          * if no copy is good, only the single best copy is kept, ranked
            by Skill first and Luck as the tie-break
          * every other copy is trashed
        A copy whose Skill, Luck or Waifu ID could not be read is kept,
        never trashed.
        """
        keep: List[int] = []
        trash: List[int] = []
        unreadable: List[int] = []
        left_alone: List[int] = []
        groups = 0

        for members in session["name_ids"].values():
            if len(members) < 2:
                continue

            by_waifu: Dict[Any, List[int]] = {}
            for local_id in members:
                stats = session["stats"][local_id]
                if (
                    stats.get("skill") is None
                    or stats.get("luck") is None
                    or stats.get("waifu_id") is None
                ):
                    unreadable.append(local_id)
                    keep.append(local_id)
                else:
                    by_waifu.setdefault(stats["waifu_id"], []).append(local_id)

            for copies in by_waifu.values():
                if len(copies) < 2:
                    left_alone.extend(copies)
                    continue
                groups += 1

                good = [
                    i for i in copies
                    if self._dupes_is_good(session["stats"][i])
                ]
                if good:
                    survivors = set(good)
                else:
                    survivors = {
                        min(copies, key=lambda i: self._dupes_rank(session, i))
                    }

                for local_id in copies:
                    (keep if local_id in survivors else trash).append(local_id)

        order = lambda i: session["entries"][i]["pos"]  # noqa: E731
        return (
            sorted(keep, key=order),
            sorted(trash, key=order),
            unreadable,
            groups,
            sorted(left_alone, key=order),
        )

    def _dupes_trash_after_keeps(self, session: Dict[str, Any]) -> List[int]:
        return [i for i in session["trash_ids"] if i not in session["kept"]]

    def _dupes_fav_lines(self, ids: List[int]) -> List[str]:
        return [
            f"`.fav {' '.join(map(str, ids[i:i + DUPES_FAV_BATCH]))} {TRASH_EMOJI}`"
            for i in range(0, len(ids), DUPES_FAV_BATCH)
        ]

    # ── Final report ──────────────────────────────────────────────────────

    async def _dupes_finalise(self, session: Dict[str, Any]) -> None:
        """Post the keep list and the `.fav` commands. Lock is held."""
        session["phase"] = "done"
        keep, trash, unreadable, groups, left_alone = self._dupes_decide(session)
        session["trash_ids"] = trash

        final_trash = self._dupes_trash_after_keeps(session)
        channel = self.bot.get_channel(session["channel_id"])
        if channel is None:
            return

        seen = len(session["pages"])
        last = session["last_page"]
        scope = (
            f"all `{seen}` pages"
            if self._dupes_pages_complete(session)
            else f"`{seen}` of `{last + 1 if last is not None else '?'}` pages"
        )
        header = (
            f"**Duplicate review complete** · scanned {scope}\n"
            f"`{groups}` characters with duplicates · "
            f"keeping `{len(keep)}` · trashing `{len(final_trash)}`\n"
            f"-# Kept: Skill ≥ {GOOD_SKILL:g} with Luck ≥ {GOOD_LUCK}. "
            "If no copy qualifies, only the highest-Skill copy is kept "
            "(Luck breaks ties)."
        )
        kept_lines = [
            f"`{i}` **{session['entries'][i]['name']}** · "
            f"{_fmt_stats(session['stats'][i])}"
            for i in keep
        ]

        try:
            await channel.send(header)
            if kept_lines:
                for chunk in _chunk_lines(["**Keeping**"] + kept_lines):
                    await channel.send(chunk)
            if left_alone:
                await channel.send(
                    "-# Same name but a different Waifu ID, so not "
                    "duplicates (left alone): "
                    + ", ".join(f"`{i}`" for i in left_alone)
                )
            if unreadable:
                await channel.send(
                    "-# Could not read stats for these (kept, never trashed): "
                    + ", ".join(f"`{i}`" for i in unreadable)
                )
            await self._dupes_send_trash(channel, final_trash, "Trash list")
        except discord.HTTPException:
            log.exception("Could not post the dupes final report")

    async def _dupes_send_trash(
        self,
        channel: discord.abc.Messageable,
        ids: List[int],
        title: str,
    ) -> None:
        if not ids:
            await channel.send(f"**{title}**: nothing to trash.")
            return
        lines = [f"**{title}** · `{len(ids)}` cards"] + self._dupes_fav_lines(ids)
        lines.append(
            "-# Reply `keep <id> [<id> ...]` to pull cards out of the trash list."
        )
        for chunk in _chunk_lines(lines):
            await channel.send(chunk)

    # ── keep <ids> ────────────────────────────────────────────────────────

    async def _dupes_apply_keep(
        self,
        message: discord.Message,
        session: Dict[str, Any],
        ids: List[int],
    ) -> None:
        async with session["lock"]:
            session["last_active"] = time.monotonic()
            known = [i for i in ids if i in session["entries"]]
            unknown = [i for i in ids if i not in session["entries"]]

            if session["phase"] == "done":
                before = set(self._dupes_trash_after_keeps(session))
                session["kept"].update(known)
                after = self._dupes_trash_after_keeps(session)
                removed = sorted(before - set(after))
                notes: List[str] = []
                if removed:
                    notes.append(
                        "Removed from the trash list: "
                        + ", ".join(f"`{i}`" for i in removed)
                    )
                not_trash = [i for i in known if i not in before and i not in removed]
                if not_trash:
                    notes.append(
                        "Already not in the trash list: "
                        + ", ".join(f"`{i}`" for i in not_trash)
                    )
                if unknown:
                    notes.append(
                        "Not part of this scan: "
                        + ", ".join(f"`{i}`" for i in unknown)
                    )
                await message.reply("\n".join(notes), mention_author=False)
                if removed:
                    await self._dupes_send_trash(
                        message.channel, after, "Updated trash list"
                    )
            else:
                session["kept"].update(known)
                notes = []
                if known:
                    notes.append(
                        "Will keep: " + ", ".join(f"`{i}`" for i in known)
                    )
                if unknown:
                    notes.append(
                        "Not seen in this scan yet: "
                        + ", ".join(f"`{i}`" for i in unknown)
                    )
                await message.reply("\n".join(notes), mention_author=False)

