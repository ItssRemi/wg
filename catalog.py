"""Waifu/series catalog: on-disk JSON storage + in-memory lookup indexes.

Extracted from waifugami.py.

Why this is its own module
───────────────────────────
``_save_catalog_files`` rewrites four JSON files in full
(waifu_hash_map.json, series_catalog.json, waifus.json,
waifugami_catalog_lookup.json) every time a series gets (re)learned. All
four only ever grow. The old code did this with a plain synchronous
``Path.write_text`` call, sitting directly inside the ``on_message_edit``
listener — so the entire bot (every guild, every other listener, including
whatever reminder ``asyncio.sleep()`` happened to be due at that moment)
blocked on the event loop for however long that serialize-and-write took.
As the catalog grows, that stall grows with it.

Every write in this module goes through ``asyncio.to_thread`` so it runs on
a worker thread instead of the event loop, the same way db.py already
handles SQLite. The mixin's public write entry points
(``_save_catalog_files``, ``_save_event_overrides``,
``_replace_series_in_catalog``) are now coroutines — callers must
``await`` them. Everything that only *reads* this state
(``self.new_map``, ``self.old_map``, ``self.series_catalog``,
``self._series_index``, ``self._char_index``, ``self.event_overrides``)
is unaffected: these stay plain instance attributes set by the mixin, so
every existing read site elsewhere in the cog keeps working untouched.

Requirements from the host cog
───────────────────────────────
self.base_path  — Path, directory the catalog JSON files live in
md5_hash(value: str) -> str
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

log = logging.getLogger("red.waifugami.catalog")


def md5_hash(value: str) -> str:
    import hashlib
    return hashlib.md5(value.encode("utf-8")).hexdigest()


class CatalogMixin:
    """Mixin that adds waifu/series catalog storage to the Waifugami cog."""

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def _catalog_init(self) -> None:
        """Call from __init__ of the host cog."""
        base = self.base_path
        self.map_path = base / "waifu_hash_map.json"
        self.series_catalog_path = base / "series_catalog.json"
        self.waifus_path = base / "waifus.json"
        self.old_lookup_path = base / "waifugami_catalog_lookup.json"
        self.event_overrides_path = base / "wgevent_overrides.json"

        self.new_map: Dict[str, Dict[str, Any]] = {}
        self.old_map: Dict[str, str] = {}
        self.series_catalog: Dict[str, Dict[str, Any]] = {}
        self._series_index: Dict[str, str] = {}
        self._char_index: Dict[str, Dict[str, Any]] = {}

        self.event_overrides: Dict[str, Any] = {"ids": {}, "ranges": []}

    # ── Plain file I/O (only ever called off-thread — see below) ───────────

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

    # ── Loading (startup only — cog_load is async, so we hop to a thread
    #    too, but there's no hot-path urgency here the way there is for
    #    the save side) ───────────────────────────────────────────────────

    async def _load_catalogs(self) -> None:
        await asyncio.to_thread(self._load_catalogs_sync)

    def _load_catalogs_sync(self) -> None:
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
                self._save_catalog_files_sync()

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

    # ── Saving — THE HOT PATH. Everything below this point runs off the
    #    event loop. ─────────────────────────────────────────────────────

    async def _save_catalog_files(self) -> None:
        """Async entry point — awaits the blocking save on a worker thread.

        This used to be a plain synchronous method called directly from
        ``_replace_series_in_catalog`` (itself reachable from the
        ``on_message_edit`` listener). Every caller of the old
        ``_save_catalog_files`` must now ``await`` this instead.
        """
        await asyncio.to_thread(self._save_catalog_files_sync)

    def _save_catalog_files_sync(self) -> None:
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

    async def _replace_series_in_catalog(
        self,
        *,
        series_id: str,
        series_name: str,
        characters: Dict[str, str],
        source_message_id: int,
    ) -> Tuple[int, int, int]:
        """Now a coroutine — the old sync version ended with a blocking
        ``self._save_catalog_files()`` call; callers must ``await`` this."""
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
        await self._save_catalog_files()
        return added, removed, modified

    # ── Event override files ────────────────────────────────────────────

    async def _load_event_overrides(self) -> None:
        await asyncio.to_thread(self._load_event_overrides_sync)

    def _load_event_overrides_sync(self) -> None:
        data = self._load_json_file(self.event_overrides_path, {"ids": {}, "ranges": []})
        if not isinstance(data, dict):
            data = {"ids": {}, "ranges": []}
        if not isinstance(data.get("ids"), dict):
            data["ids"] = {}
        if not isinstance(data.get("ranges"), list):
            data["ranges"] = []
        self.event_overrides = data

    async def _save_event_overrides(self) -> None:
        await asyncio.to_thread(self._write_json_file, self.event_overrides_path, self.event_overrides)
