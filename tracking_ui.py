"""Interactive Components V2 tracking + completion management UI.

Implements `/wgtracked` and `/wgcompletion` (+ their prefix-command
equivalents via `hybrid_command`), replacing the old embed-based
`wgtracked`/`wgtrackedids` commands and the standalone `wgwatch` /
`wgunwatch` / `wgtrackid` / `wguntrackid` commands.

Architecture notes
───────────────────
This mixin owns ONE concept the rest of the cog didn't have before:
series-level add/remove helpers (`_add_series_tracking` /
`_remove_series_tracking`). Unlike completion/character tracking,
`tracked_series` is a flat per-user list (not a dict grouped by series),
so it can't reuse the generic `_tracking_add`/`_tracking_remove_one`
helpers in waifugami.py — those assume a `{series_id: [ids...]}` shape.
The read-modify-write logic here is a straight port of what the retired
`wgwatch`/`wgunwatch` commands did, now against the renamed
`tracked_series` field.

All rendering goes through the same raw-HTTP Components V2 pattern
already established by `_browser_payload`/`_send_browser`/`_update_browser`
in waifugami.py (type 4 = initial interaction response, type 7 = edit the
message a component interaction came from). Interaction ownership is
enforced the same way as the rest of the cog: the acting user's ID is
embedded directly in every custom_id and checked against
`interaction.user.id` before anything is read or written.

Requirements from the host cog
───────────────────────────────
self.bot, self.listener_config, self._series_index, self.series_catalog,
self._char_index, self._resolve_series_id(ctx, text),
self._ac_series(interaction, current), V2_FLAG, Route (from discord.http)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.http import Route
from redbot.core import commands

log = logging.getLogger("red.waifugami.tracking_ui")

V2_FLAG = 32768

TRACKED_PAGE_SIZE = 10          # characters shown per page in the default view
TRACKED_REMOVE_PAGE_SIZE = 25   # Discord's hard cap on select menu options
COMPLETION_COMPACT_PAGE_SIZE = 14
COMPLETION_EXPAND_LINE_BUDGET = 40  # soft cap on rendered lines per expanded page


class TrackingUIMixin:
    """Mixin providing /wgtracked and /wgcompletion."""

    def _tracking_ui_init(self) -> None:
        """Call from __init__ of the host cog."""
        # series_ids a user picked in the completion "Remove" flow, pending
        # their confirmation. Not persisted — if they never confirm, it's
        # just dropped on the next unrelated interaction or TTL check.
        self._pending_completion_removal: Dict[int, Tuple[List[str], float]] = {}

    # ------------------------------------------------------------
    # Series-level tracking (flat list — see module docstring for why
    # this doesn't go through the generic _tracking_add/_tracking_remove_one)
    # ------------------------------------------------------------

    async def _get_tracked_series(self, user_id: int) -> List[str]:
        watched = await self.listener_config.user_from_id(user_id).tracked_series()
        if not isinstance(watched, list):
            return []
        return sorted({str(int(s)) for s in watched if str(s).isdigit()}, key=lambda s: int(s))

    async def _add_series_tracking(self, user_id: int, sid: str) -> bool:
        """Returns True if this newly subscribed the user (False if already watching)."""
        current = await self._get_tracked_series(user_id)
        if sid in current:
            return False
        new_list = sorted(set(current) | {sid}, key=lambda s: int(s))
        await self.listener_config.user_from_id(user_id).tracked_series.set(new_list)

        watchers = await self.listener_config.series_watchers_by_series()
        if not isinstance(watchers, dict):
            watchers = {}
        uids = set(int(x) for x in (watchers.get(sid) or []) if isinstance(x, int) or str(x).isdigit())
        uids.add(int(user_id))
        watchers[sid] = sorted(uids)
        await self.listener_config.series_watchers_by_series.set(watchers)
        return True

    async def _remove_series_tracking(self, user_id: int, sid: str) -> bool:
        """Returns True if the user was actually watching it (False = no-op)."""
        current = await self._get_tracked_series(user_id)
        if sid not in current:
            return False
        new_list = [s for s in current if s != sid]
        await self.listener_config.user_from_id(user_id).tracked_series.set(new_list)

        watchers = await self.listener_config.series_watchers_by_series()
        if not isinstance(watchers, dict):
            watchers = {}
        cur = [x for x in (watchers.get(sid) or []) if (isinstance(x, int) or str(x).isdigit()) and int(x) != int(user_id)]
        if cur:
            watchers[sid] = sorted(set(int(x) for x in cur))
        else:
            watchers.pop(sid, None)
        await self.listener_config.series_watchers_by_series.set(watchers)
        return True

    # ------------------------------------------------------------
    # Shared resolution helpers
    # ------------------------------------------------------------

    def _resolve_character_in_series(self, sid: str, text: str) -> Optional[Tuple[str, str]]:
        """Resolve `text` (an ID or a name) to (char_id, char_name) within series `sid`.

        Returns None if `sid` isn't in the catalog, or nothing matches.
        Ambiguous name matches (more than one hit) also return None —
        callers should ask for the ID instead rather than guess.
        """
        entry = self.series_catalog.get(sid)
        chars = entry.get("characters") if isinstance(entry, dict) else None
        if not isinstance(chars, dict) or not chars:
            return None

        stripped = text.strip()
        if stripped.isdigit():
            cid = str(int(stripped))
            name = chars.get(cid)
            return (cid, name) if name else None

        needle = stripped.casefold()
        exact = [(cid, name) for cid, name in chars.items() if name.casefold() == needle]
        if len(exact) == 1:
            return exact[0]
        if not exact:
            partial = [(cid, name) for cid, name in chars.items() if needle in name.casefold()]
            if len(partial) == 1:
                return partial[0]
        return None

    async def _resolve_series_id_text(self, text: str) -> Optional[str]:
        """Like _resolve_series_id, but without a ctx to reply through
        (used from modal submit handlers, which reply via interaction)."""
        stripped = text.strip()
        if stripped.isdigit():
            return str(int(stripped))
        needle = stripped.casefold()
        matches = [sid for sid, name in self._series_index.items() if name.casefold() == needle]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            matches = [sid for sid, name in self._series_index.items() if needle in name.casefold()]
        return matches[0] if len(matches) == 1 else None

    # ------------------------------------------------------------
    # /wgtracked — default view + category switch + add + remove
    # ------------------------------------------------------------

    @commands.hybrid_command(name="wgtracked", with_app_command=True)
    async def wgtracked_cmd(self, ctx: commands.Context) -> None:
        """Show and manage your tracked characters and series."""
        uid = int(ctx.author.id)
        await self._send_or_followup_v2(
            ctx, await self._wgtracked_payload(uid, category="characters", page=0, mode="view")
        )

    async def _send_or_followup_v2(self, ctx: commands.Context, payload: Dict[str, Any]) -> None:
        """Send a V2 payload as this command's response, whichever invocation
        style (slash vs. prefix) triggered it.

        discord.py's interaction.response.send_message doesn't accept raw
        Components V2 dicts, so for the slash path we ack with a minimal
        placeholder through discord.py's own machinery (required within 3s),
        then immediately replace it with the real payload via the
        original-response webhook endpoint — the same two-step shape as
        every other raw-HTTP send in this cog, just routed through the
        interaction's webhook instead of the channel.
        """
        if ctx.interaction:
            route = Route(
                "POST",
                "/interactions/{interaction_id}/{interaction_token}/callback",
                interaction_id=ctx.interaction.id,
                interaction_token=ctx.interaction.token,
            )
            await self.bot.http.request(
                route,
                json={
                    "type": 4,
                    "data": payload,
                },
            )
        else:
            route = Route(
                "POST",
                "/channels/{channel_id}/messages",
                channel_id=ctx.channel.id,
            )
            await self.bot.http.request(route, json=payload)

    async def _wgtracked_payload(
        self, user_id: int, *, category: str, page: int, mode: str
    ) -> Dict[str, Any]:
        """mode: 'view' (default browse) or 'remove' (select-to-remove)."""
        if category not in ("characters", "series"):
            category = "characters"

        if category == "characters":
            tracked_chars = await self.listener_config.user_from_id(user_id).tracked_characters_by_series()
            if not isinstance(tracked_chars, dict):
                tracked_chars = {}
            rows: List[Tuple[str, str, str, str]] = []  # (cid, cname, sid, sname)
            for sid, ids in tracked_chars.items():
                if not str(sid).isdigit() or not isinstance(ids, list):
                    continue
                sname = self._series_index.get(str(int(sid)), f"Series {sid}")
                for raw_cid in ids:
                    if not str(raw_cid).isdigit():
                        continue
                    cid = str(int(raw_cid))
                    char_entry = self._char_index.get(cid) or {}
                    cname = str(char_entry.get("name") or f"ID {cid}")
                    rows.append((cid, cname, str(int(sid)), sname))
            rows.sort(key=lambda r: (int(r[2]), int(r[0])))
            header = "### Tracked Characters"
        else:
            tracked_series = await self._get_tracked_series(user_id)
            rows = [(sid, self._series_index.get(sid, f"Series {sid}")) for sid in tracked_series]
            header = "### Tracked Series"

        if mode == "remove":
            return self._wgtracked_remove_payload(user_id, category, rows, page)
        return self._wgtracked_view_payload(user_id, category, rows, page, header)

    def _wgtracked_view_payload(
        self, user_id: int, category: str, rows: List[Tuple], page: int, header: str
    ) -> Dict[str, Any]:
        if category == "characters":
            page_count = max(1, (len(rows) + TRACKED_PAGE_SIZE - 1) // TRACKED_PAGE_SIZE)
            page = max(0, min(page, page_count - 1))
            visible = rows[page * TRACKED_PAGE_SIZE:(page + 1) * TRACKED_PAGE_SIZE]
            if visible:
                body = "\n".join(f"{cid} | {cname}\n-# ({sid}) {sname}" for cid, cname, sid, sname in visible)
            else:
                body = "You aren't tracking any individual characters yet."
        else:
            page_count = max(1, (len(rows) + TRACKED_PAGE_SIZE - 1) // TRACKED_PAGE_SIZE)
            page = max(0, min(page, page_count - 1))
            visible = rows[page * TRACKED_PAGE_SIZE:(page + 1) * TRACKED_PAGE_SIZE]
            if visible:
                body = "\n".join(f"({sid}) {sname}" for sid, sname in visible)
            else:
                body = "You aren't tracking any series yet."

        components: List[Dict[str, Any]] = [
            {"type": 10, "content": header},
            {"type": 14},
            {"type": 10, "content": body},
            {"type": 14},
            {
                "type": 1,
                "components": [{
                    "type": 3,
                    "custom_id": f"nebwg:trk:{user_id}:cat",
                    "min_values": 1,
                    "max_values": 1,
                    "options": [
                        {"label": "Characters", "value": "characters", "description": "Tracked characters",
                         "default": category == "characters"},
                        {"label": "Series", "value": "series", "description": "Tracked series",
                         "default": category == "series"},
                    ],
                }],
            },
            {"type": 14},
            {
                "type": 1,
                "components": [
                    {"type": 2, "style": 2, "label": "Add",
                     "custom_id": f"nebwg:trk:{user_id}:add:{category}"},
                    {"type": 2, "style": 4, "label": "Remove",
                     "custom_id": f"nebwg:trk:{user_id}:rm:{category}:0"},
                ],
            },
        ]
        if page_count > 1:
            components.append({"type": 14})
            components.append({
                "type": 1,
                "components": [
                    {"type": 2, "style": 2, "label": "◀", "disabled": page <= 0,
                     "custom_id": f"nebwg:trk:{user_id}:cat:{category}:{page - 1}"},
                    {"type": 2, "style": 2, "label": f"{page + 1}/{page_count}", "disabled": True,
                     "custom_id": f"nebwg:trk:{user_id}:noop"},
                    {"type": 2, "style": 2, "label": "▶", "disabled": page >= page_count - 1,
                     "custom_id": f"nebwg:trk:{user_id}:cat:{category}:{page + 1}"},
                ],
            })

        return {
            "flags": V2_FLAG,
            "allowed_mentions": {"parse": []},
            "components": [{"type": 17, "components": components}],
        }

    def _wgtracked_remove_payload(
        self, user_id: int, category: str, rows: List[Tuple], page: int
    ) -> Dict[str, Any]:
        page_count = max(1, (len(rows) + TRACKED_REMOVE_PAGE_SIZE - 1) // TRACKED_REMOVE_PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        visible = rows[page * TRACKED_REMOVE_PAGE_SIZE:(page + 1) * TRACKED_REMOVE_PAGE_SIZE]

        if not visible:
            empty_label = "individual characters" if category == "characters" else "series"
            components = [
                {"type": 10, "content": f"### Remove {'Tracked Characters' if category == 'characters' else 'Tracked Series'}"},
                {"type": 14},
                {"type": 10, "content": f"Nothing to remove — you aren't tracking any {empty_label}."},
                {"type": 14},
                {"type": 1, "components": [
                    {"type": 2, "style": 2, "label": "Back", "custom_id": f"nebwg:trk:{user_id}:cat:{category}:0"},
                ]},
            ]
            return {"flags": V2_FLAG, "allowed_mentions": {"parse": []}, "components": [{"type": 17, "components": components}]}

        if category == "characters":
            options = [
                {
                    "label": f"{cname}"[:100],
                    "value": f"{sid}:{cid}",
                    "description": f"{cid} · ({sid}) {sname}"[:100],
                }
                for cid, cname, sid, sname in visible
            ]
        else:
            options = [
                {"label": sname[:100], "value": sid, "description": f"({sid})"[:100]}
                for sid, sname in visible
            ]

        components: List[Dict[str, Any]] = [
            {"type": 10, "content": "### Select what to remove"},
            {"type": 14},
            {
                "type": 1,
                "components": [{
                    "type": 3,
                    "custom_id": f"nebwg:trk:{user_id}:rmsel:{category}:{page}",
                    "min_values": 1,
                    "max_values": len(options),
                    "options": options,
                }],
            },
            {"type": 14},
            {"type": 1, "components": [
                {"type": 2, "style": 2, "label": "Back", "custom_id": f"nebwg:trk:{user_id}:cat:{category}:0"},
            ]},
        ]
        if page_count > 1:
            components.append({"type": 14})
            components.append({
                "type": 1,
                "components": [
                    {"type": 2, "style": 2, "label": "◀", "disabled": page <= 0,
                     "custom_id": f"nebwg:trk:{user_id}:rm:{category}:{page - 1}"},
                    {"type": 2, "style": 2, "label": f"{page + 1}/{page_count}", "disabled": True,
                     "custom_id": f"nebwg:trk:{user_id}:noop"},
                    {"type": 2, "style": 2, "label": "▶", "disabled": page >= page_count - 1,
                     "custom_id": f"nebwg:trk:{user_id}:rm:{category}:{page + 1}"},
                ],
            })
        return {"flags": V2_FLAG, "allowed_mentions": {"parse": []}, "components": [{"type": 17, "components": components}]}

    # ------------------------------------------------------------
    # /wgcompletion — compact + expanded + remove
    # ------------------------------------------------------------

    @commands.hybrid_command(name="wgcompletion", with_app_command=True)
    async def wgcompletion_cmd(self, ctx: commands.Context) -> None:
        """Show and manage your harvested completion (`.wgscan`) data."""
        uid = int(ctx.author.id)
        await self._send_or_followup_v2(ctx, await self._wgcompletion_payload(uid, mode="compact", page=0))

    async def _completion_rows(self, user_id: int) -> List[Tuple[str, str, List[Tuple[str, str]]]]:
        """Returns [(sid, sname, [(cid, cname), ...]), ...] sorted by series id,
        only for series with at least one missing character."""
        missing = await self.listener_config.user_from_id(user_id).completion_missing_by_series()
        if not isinstance(missing, dict):
            missing = {}
        out: List[Tuple[str, str, List[Tuple[str, str]]]] = []
        for sid, ids in missing.items():
            if not str(sid).isdigit() or not isinstance(ids, list):
                continue
            clean_ids = sorted({str(int(x)) for x in ids if str(x).isdigit()}, key=int)
            if not clean_ids:
                continue
            sid_clean = str(int(sid))
            sname = self._series_index.get(sid_clean, f"Series {sid_clean}")
            chars = [(cid, str((self._char_index.get(cid) or {}).get("name") or f"ID {cid}")) for cid in clean_ids]
            out.append((sid_clean, sname, chars))
        out.sort(key=lambda t: int(t[0]))
        return out

    async def _wgcompletion_payload(self, user_id: int, *, mode: str, page: int) -> Dict[str, Any]:
        rows = await self._completion_rows(user_id)

        if not rows:
            components = [
                {"type": 10, "content": "### Waifugami Completion"},
                {"type": 14},
                {"type": 10, "content": "Nothing saved yet. Reply `wgscan` to a `.completion <series id>` embed to harvest missing characters."},
            ]
            return {"flags": V2_FLAG, "allowed_mentions": {"parse": []}, "components": [{"type": 17, "components": components}]}

        if mode == "expand":
            return self._wgcompletion_expand_payload(user_id, rows, page)
        return self._wgcompletion_compact_payload(user_id, rows, page)

    def _wgcompletion_compact_payload(
        self, user_id: int, rows: List[Tuple[str, str, List[Tuple[str, str]]]], page: int
    ) -> Dict[str, Any]:
        page_count = max(1, (len(rows) + COMPLETION_COMPACT_PAGE_SIZE - 1) // COMPLETION_COMPACT_PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        visible = rows[page * COMPLETION_COMPACT_PAGE_SIZE:(page + 1) * COMPLETION_COMPACT_PAGE_SIZE]
        body = "\n".join(f"({sid}) {sname}: {len(chars)}" for sid, sname, chars in visible)

        components = [
            {"type": 10, "content": "### Waifugami Completion"},
            {"type": 14},
            {"type": 10, "content": body},
            {"type": 14},
            {"type": 1, "components": [
                {"type": 2, "style": 2, "label": "Expand",
                 "custom_id": f"nebwg:cmp:{user_id}:view:expand:0"},
                {"type": 2, "style": 2, "label": "Remove",
                 "custom_id": f"nebwg:cmp:{user_id}:remove:0"},
            ]},
            {"type": 14},
            {"type": 1, "components": [
                {"type": 2, "style": 2, "label": "◀", "disabled": page <= 0,
                 "custom_id": f"nebwg:cmp:{user_id}:view:compact:{page - 1}"},
                {"type": 2, "style": 2, "label": f"{page + 1}/{page_count}", "disabled": True,
                 "custom_id": f"nebwg:cmp:{user_id}:noop"},
                {"type": 2, "style": 2, "label": "▶", "disabled": page >= page_count - 1,
                 "custom_id": f"nebwg:cmp:{user_id}:view:compact:{page + 1}"},
            ]},
        ]
        return {"flags": V2_FLAG, "allowed_mentions": {"parse": []}, "components": [{"type": 17, "components": components}]}

    def _wgcompletion_expand_payload(
        self, user_id: int, rows: List[Tuple[str, str, List[Tuple[str, str]]]], page: int
    ) -> Dict[str, Any]:
        # Paginate by rendered line budget, not series count: one series with
        # 13 missing characters can outweigh a dozen series with one each.
        pages: List[List[Tuple[str, str, List[Tuple[str, str]]]]] = []
        current: List[Tuple[str, str, List[Tuple[str, str]]]] = []
        current_lines = 0
        for sid, sname, chars in rows:
            cost = 1 + len(chars)  # one heading line + one line per character
            if current and current_lines + cost > COMPLETION_EXPAND_LINE_BUDGET:
                pages.append(current)
                current, current_lines = [], 0
            current.append((sid, sname, chars))
            current_lines += cost
        if current:
            pages.append(current)
        if not pages:
            pages = [[]]

        page_count = len(pages)
        page = max(0, min(page, page_count - 1))
        visible = pages[page]

        lines: List[str] = []
        for sid, sname, chars in visible:
            lines.append(f"({sid}) {sname}")
            lines.extend(f"-# {cid} | {cname}" for cid, cname in chars)
        body = "\n".join(lines) if lines else "Nothing on this page."

        components = [
            {"type": 10, "content": "### Waifugami Completion"},
            {"type": 14},
            {"type": 10, "content": body},
            {"type": 14},
            {"type": 1, "components": [
                {"type": 2, "style": 2, "label": "Compact",
                 "custom_id": f"nebwg:cmp:{user_id}:view:compact:0"},
                {"type": 2, "style": 2, "label": "Remove",
                 "custom_id": f"nebwg:cmp:{user_id}:remove:0"},
            ]},
            {"type": 14},
            {"type": 1, "components": [
                {"type": 2, "style": 2, "label": "◀", "disabled": page <= 0,
                 "custom_id": f"nebwg:cmp:{user_id}:view:expand:{page - 1}"},
                {"type": 2, "style": 2, "label": f"{page + 1}/{page_count}", "disabled": True,
                 "custom_id": f"nebwg:cmp:{user_id}:noop"},
                {"type": 2, "style": 2, "label": "▶", "disabled": page >= page_count - 1,
                 "custom_id": f"nebwg:cmp:{user_id}:view:expand:{page + 1}"},
            ]},
        ]
        return {"flags": V2_FLAG, "allowed_mentions": {"parse": []}, "components": [{"type": 17, "components": components}]}

    def _wgcompletion_remove_select_payload(
        self, user_id: int, rows: List[Tuple[str, str, List[Tuple[str, str]]]], page: int
    ) -> Dict[str, Any]:
        page_count = max(1, (len(rows) + TRACKED_REMOVE_PAGE_SIZE - 1) // TRACKED_REMOVE_PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        visible = rows[page * TRACKED_REMOVE_PAGE_SIZE:(page + 1) * TRACKED_REMOVE_PAGE_SIZE]
        options = [
            {"label": sname[:100], "value": sid, "description": f"({sid}) · {len(chars)} missing"[:100]}
            for sid, sname, chars in visible
        ]
        components: List[Dict[str, Any]] = [
            {"type": 10, "content": "### Remove Completion Data"},
            {"type": 14},
            {"type": 10, "content": "Select the series whose saved completion data you want to delete."},
            {"type": 14},
            {"type": 1, "components": [{
                "type": 3,
                "custom_id": f"nebwg:cmp:{user_id}:rmsel:{page}",
                "min_values": 1,
                "max_values": len(options),
                "options": options,
            }]},
            {"type": 14},
            {"type": 1, "components": [
                {"type": 2, "style": 2, "label": "Back", "custom_id": f"nebwg:cmp:{user_id}:view:compact:0"},
            ]},
        ]
        if page_count > 1:
            components.append({"type": 14})
            components.append({
                "type": 1,
                "components": [
                    {"type": 2, "style": 2, "label": "◀", "disabled": page <= 0,
                     "custom_id": f"nebwg:cmp:{user_id}:remove:{page - 1}"},
                    {"type": 2, "style": 2, "label": f"{page + 1}/{page_count}", "disabled": True,
                     "custom_id": f"nebwg:cmp:{user_id}:noop"},
                    {"type": 2, "style": 2, "label": "▶", "disabled": page >= page_count - 1,
                     "custom_id": f"nebwg:cmp:{user_id}:remove:{page + 1}"},
                ],
            })
        return {"flags": V2_FLAG, "allowed_mentions": {"parse": []}, "components": [{"type": 17, "components": components}]}

    def _wgcompletion_confirm_payload(self, user_id: int, selected_sids: List[str]) -> Dict[str, Any]:
        preview = ", ".join(f"({sid}) {self._series_index.get(sid, 'Unknown')}" for sid in selected_sids[:10])
        if len(selected_sids) > 10:
            preview += f", and {len(selected_sids) - 10} more"
        components = [
            {"type": 10, "content": "### Confirm removal"},
            {"type": 14},
            {"type": 10, "content": f"This permanently deletes your saved completion data for: {preview}.\nTracked series/character subscriptions are not affected."},
            {"type": 14},
            {"type": 1, "components": [
                {"type": 2, "style": 4, "label": "Delete", "custom_id": f"nebwg:cmp:{user_id}:rmconfirm:yes"},
                {"type": 2, "style": 2, "label": "Cancel", "custom_id": f"nebwg:cmp:{user_id}:rmconfirm:no"},
            ]},
        ]
        return {"flags": V2_FLAG, "allowed_mentions": {"parse": []}, "components": [{"type": 17, "components": components}]}

    # ------------------------------------------------------------
    # Interaction routing (separate listener method — see module docstring)
    # ------------------------------------------------------------

    @commands.Cog.listener(name="on_interaction")
    async def on_interaction_tracking_ui(self, interaction: discord.Interaction) -> None:
        data = interaction.data or {}
        custom_id = data.get("custom_id", "")
        if not custom_id.startswith("nebwg:trk:") and not custom_id.startswith("nebwg:cmp:") and not custom_id.startswith("nebwg:trkmodal:"):
            return

        # Modal submits carry interaction.type == 5 and their own custom_id
        # scheme (nebwg:trkmodal:...); everything else is a component click.
        if interaction.type == discord.InteractionType.modal_submit:
            await self._handle_tracking_modal_submit(interaction, custom_id)
            return

        parts = custom_id.split(":")
        if len(parts) < 4:
            return
        _, kind, uid_str, action = parts[0], parts[1], parts[2], parts[3]
        if not uid_str.isdigit():
            return
        owner_id = int(uid_str)
        if interaction.user.id != owner_id:
            await interaction.response.send_message("This menu belongs to someone else.", ephemeral=True)
            return

        if action == "noop":
            # Disabled page-indicator button; Discord still requires an ack.
            await interaction.response.defer()
            return

        if kind == "trk":
            await self._handle_wgtracked_interaction(interaction, owner_id, parts)
        elif kind == "cmp":
            await self._handle_wgcompletion_interaction(interaction, owner_id, parts)

    async def _edit_in_place(self, interaction: discord.Interaction, payload: Dict[str, Any]) -> None:
        payload = dict(payload)
        payload.pop("flags", None)  # flags can't be changed on an edit
        route = Route(
            "POST", "/interactions/{interaction_id}/{interaction_token}/callback",
            interaction_id=interaction.id, interaction_token=interaction.token,
        )
        await self.bot.http.request(route, json={"type": 7, "data": payload})

    async def _handle_wgtracked_interaction(
        self, interaction: discord.Interaction, owner_id: int, parts: List[str]
    ) -> None:
        action = parts[3]
        data = interaction.data or {}

        if action == "cat":
            # nebwg:trk:<uid>:cat  (select menu submit, category in values)
            # nebwg:trk:<uid>:cat:<category>:<page>  (pagination button)
            if len(parts) == 4:
                values = data.get("values") or []
                category = values[0] if values else "characters"
                page = 0
            else:
                category = parts[4]
                page = int(parts[5]) if len(parts) > 5 and parts[5].lstrip("-").isdigit() else 0
            payload = await self._wgtracked_payload(owner_id, category=category, page=page, mode="view")
            await self._edit_in_place(interaction, payload)
            return

        if action == "rm":
            category = parts[4] if len(parts) > 4 else "characters"
            page = int(parts[5]) if len(parts) > 5 and parts[5].lstrip("-").isdigit() else 0
            payload = await self._wgtracked_payload(owner_id, category=category, page=page, mode="remove")
            await self._edit_in_place(interaction, payload)
            return

        if action == "rmsel":
            category = parts[4] if len(parts) > 4 else "characters"
            values = data.get("values") or []
            if category == "characters":
                for value in values:
                    if ":" not in value:
                        continue
                    sid, cid = value.split(":", 1)
                    await self._remove_explicit_tracking(owner_id, sid, cid)
            else:
                for sid in values:
                    await self._remove_series_tracking(owner_id, sid)
            payload = await self._wgtracked_payload(owner_id, category=category, page=0, mode="view")
            await self._edit_in_place(interaction, payload)
            return

        if action == "add":
            category = parts[4] if len(parts) > 4 else "characters"
            await self._show_add_modal(interaction, owner_id, category)
            return

    async def _show_add_modal(self, interaction: discord.Interaction, owner_id: int, category: str) -> None:
        if category == "series":
            modal_data = {
                "custom_id": f"nebwg:trkmodal:{owner_id}:addseries",
                "title": "Track a series",
                "components": [{
                    "type": 1,
                    "components": [{
                        "type": 4, "custom_id": "series", "style": 1,
                        "label": "Series (ID or name)", "required": True, "max_length": 100,
                    }],
                }],
            }
        else:
            modal_data = {
                "custom_id": f"nebwg:trkmodal:{owner_id}:addchar",
                "title": "Track a character",
                "components": [
                    {"type": 1, "components": [{
                        "type": 4, "custom_id": "series", "style": 1,
                        "label": "Series (ID or name)", "required": True, "max_length": 100,
                    }]},
                    {"type": 1, "components": [{
                        "type": 4, "custom_id": "character", "style": 1,
                        "label": "Character (ID or name)", "required": True, "max_length": 100,
                    }]},
                ],
            }
        route = Route(
            "POST", "/interactions/{interaction_id}/{interaction_token}/callback",
            interaction_id=interaction.id, interaction_token=interaction.token,
        )
        await self.bot.http.request(route, json={"type": 9, "data": modal_data})

    async def _handle_tracking_modal_submit(self, interaction: discord.Interaction, custom_id: str) -> None:
        parts = custom_id.split(":")
        if len(parts) < 4:
            return
        uid_str, kind = parts[2], parts[3]
        if not uid_str.isdigit() or interaction.user.id != int(uid_str):
            await interaction.response.send_message("This form belongs to someone else.", ephemeral=True)
            return
        owner_id = int(uid_str)

        # Modal-submit field values arrive as a nested action-row structure.
        fields: Dict[str, str] = {}
        for row in (interaction.data or {}).get("components", []):
            for comp in row.get("components", []):
                fields[comp.get("custom_id")] = comp.get("value", "")

        if kind == "addseries":
            sid = await self._resolve_series_id_text(fields.get("series", ""))
            if sid is None:
                await interaction.response.send_message(
                    f"I don't recognize a series matching {fields.get('series', '')!r}.", ephemeral=True
                )
                return
            await self._add_series_tracking(owner_id, sid)
            payload = await self._wgtracked_payload(owner_id, category="series", page=0, mode="view")
            route = Route(
                "POST", "/interactions/{interaction_id}/{interaction_token}/callback",
                interaction_id=interaction.id, interaction_token=interaction.token,
            )
            await self.bot.http.request(route, json={"type": 4, "data": payload})
            return

        if kind == "addchar":
            sid = await self._resolve_series_id_text(fields.get("series", ""))
            if sid is None:
                await interaction.response.send_message(
                    f"I don't recognize a series matching {fields.get('series', '')!r}.", ephemeral=True
                )
                return
            resolved = self._resolve_character_in_series(sid, fields.get("character", ""))
            if resolved is None:
                await interaction.response.send_message(
                    f"I don't recognize a character matching {fields.get('character', '')!r} "
                    f"in ({sid}) {self._series_index.get(sid, 'Unknown series')}.",
                    ephemeral=True,
                )
                return
            cid, _cname = resolved
            await self._add_explicit_tracking(owner_id, sid, [cid])
            payload = await self._wgtracked_payload(owner_id, category="characters", page=0, mode="view")
            route = Route(
                "POST", "/interactions/{interaction_id}/{interaction_token}/callback",
                interaction_id=interaction.id, interaction_token=interaction.token,
            )
            await self.bot.http.request(route, json={"type": 4, "data": payload})
            return

    async def _handle_wgcompletion_interaction(
        self, interaction: discord.Interaction, owner_id: int, parts: List[str]
    ) -> None:
        action = parts[3]
        data = interaction.data or {}

        if action == "view":
            mode = parts[4] if len(parts) > 4 else "compact"
            page = int(parts[5]) if len(parts) > 5 and parts[5].lstrip("-").isdigit() else 0
            payload = await self._wgcompletion_payload(owner_id, mode=mode, page=page)
            await self._edit_in_place(interaction, payload)
            return

        if action == "remove":
            page = int(parts[4]) if len(parts) > 4 and parts[4].lstrip("-").isdigit() else 0
            rows = await self._completion_rows(owner_id)
            if not rows:
                payload = await self._wgcompletion_payload(owner_id, mode="compact", page=0)
                await self._edit_in_place(interaction, payload)
                return
            payload = self._wgcompletion_remove_select_payload(owner_id, rows, page)
            await self._edit_in_place(interaction, payload)
            return

        if action == "rmsel":
            values = data.get("values") or []
            selected = sorted({str(int(v)) for v in values if str(v).isdigit()}, key=int)
            if not selected:
                payload = await self._wgcompletion_payload(owner_id, mode="compact", page=0)
                await self._edit_in_place(interaction, payload)
                return
            self._pending_completion_removal[owner_id] = (selected, time.monotonic())
            payload = self._wgcompletion_confirm_payload(owner_id, selected)
            await self._edit_in_place(interaction, payload)
            return

        if action == "rmconfirm":
            decision = parts[4] if len(parts) > 4 else "no"
            pending = self._pending_completion_removal.pop(owner_id, None)
            if decision == "yes" and pending:
                selected, _ts = pending
                for sid in selected:
                    await self._sync_completion_tracking(owner_id, sid, [])
            payload = await self._wgcompletion_payload(owner_id, mode="compact", page=0)
            await self._edit_in_place(interaction, payload)
            return
