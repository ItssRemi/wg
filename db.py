"""db.py — SQLite card store for the Waifugami cog.

Replaces the ``cards`` and ``removed_cards`` dicts in Red Config.
Everything else (pending acquisitions, teams, reminders, …) stays in Config.

Schema
──────
One database file per user, stored in the cog's data path:
    <cog_data_path>/cards/<user_id>.db

Table: cards
    global_id       INTEGER PRIMARY KEY
    owner_id        INTEGER NOT NULL
    local_id        INTEGER             -- NULL when removed
    local_id_source TEXT
    status          TEXT NOT NULL       -- 'active' | 'removed'
    name            TEXT
    waifu_id        INTEGER
    image_url       TEXT
    last_seen       TEXT
    removed_at      TEXT
    removal_source  TEXT
    acquisition     TEXT               -- JSON blob
    local_id_history TEXT              -- JSON blob  (list of {local_id, ended_at})
    data            TEXT NOT NULL      -- JSON blob for all remaining fields

Index: cards_local_id  ON cards(owner_id, local_id)  WHERE status = 'active'

Usage
──────
    store = CardStore(cog_data_path(cog))
    await store.upsert_cards(owner_id, [card_dict, ...])
    cards: dict = await store.get_cards(owner_id)
    await store.mark_removed(owner_id, local_id, source, removed_at)
    await store.shift_local_ids_above(owner_id, above_local, delta=-1)
    await store.get_removed_cards(owner_id) -> dict
    await store.migrate_from_config(owner_id, cards_dict, removed_dict)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("red.nebula.Waifugami.db")

# Top-level extracted columns (all others go into the JSON ``data`` blob).
_COLUMNS = frozenset({
    "global_id", "owner_id", "local_id", "local_id_source", "status",
    "name", "waifu_id", "image_url", "last_seen",
    "removed_at", "removal_source", "acquisition", "local_id_history",
})

_DDL = """
CREATE TABLE IF NOT EXISTS cards (
    global_id        INTEGER PRIMARY KEY,
    owner_id         INTEGER NOT NULL,
    local_id         INTEGER,
    local_id_source  TEXT,
    status           TEXT    NOT NULL DEFAULT 'active',
    name             TEXT,
    waifu_id         INTEGER,
    image_url        TEXT,
    last_seen        TEXT,
    removed_at       TEXT,
    removal_source   TEXT,
    acquisition      TEXT,
    local_id_history TEXT,
    data             TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS cards_local_id
    ON cards (owner_id, local_id)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS cards_owner_status
    ON cards (owner_id, status);
CREATE TABLE IF NOT EXISTS meta (
    owner_id       INTEGER PRIMARY KEY,
    config_cleared INTEGER NOT NULL DEFAULT 0
);
"""

_UPSERT_SQL = """
INSERT INTO cards (
    global_id, owner_id, local_id, local_id_source, status,
    name, waifu_id, image_url, last_seen,
    removed_at, removal_source, acquisition, local_id_history, data
) VALUES (
    :global_id, :owner_id, :local_id, :local_id_source, :status,
    :name, :waifu_id, :image_url, :last_seen,
    :removed_at, :removal_source, :acquisition, :local_id_history, :data
)
ON CONFLICT(global_id) DO UPDATE SET
    owner_id         = excluded.owner_id,
    local_id         = excluded.local_id,
    local_id_source  = excluded.local_id_source,
    status           = excluded.status,
    name             = excluded.name,
    waifu_id         = excluded.waifu_id,
    image_url        = excluded.image_url,
    last_seen        = excluded.last_seen,
    removed_at       = COALESCE(excluded.removed_at,  cards.removed_at),
    removal_source   = COALESCE(excluded.removal_source, cards.removal_source),
    acquisition      = COALESCE(excluded.acquisition, cards.acquisition),
    local_id_history = excluded.local_id_history,
    data             = excluded.data
;
"""


def _row_to_card(row: sqlite3.Row) -> Dict[str, Any]:
    """Reconstruct the full card dict from a DB row."""
    card: Dict[str, Any] = json.loads(row["data"] or "{}")
    for col in _COLUMNS - {"data"}:
        val = row[col]
        if col in ("acquisition", "local_id_history") and isinstance(val, str):
            try:
                val = json.loads(val)
            except (ValueError, TypeError):
                val = None
        if val is not None:
            card[col] = val
    return card


def _card_to_row(card: Dict[str, Any]) -> Dict[str, Any]:
    """Split a card dict into extracted columns + remaining JSON blob."""
    row: Dict[str, Any] = {}
    data: Dict[str, Any] = {}
    for key, value in card.items():
        if key in _COLUMNS - {"data"}:
            if key in ("acquisition", "local_id_history") and value is not None:
                row[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            else:
                row[key] = value
        else:
            data[key] = value
    row["data"] = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # Fill missing columns with None so the SQL bind dict is complete.
    for col in _COLUMNS - {"data"}:
        row.setdefault(col, None)
    return row


class CardStore:
    """Async wrapper around a per-user SQLite file.

    All blocking SQLite calls are offloaded to a thread via
    ``asyncio.to_thread`` so the event loop is never stalled.
    """

    def __init__(self, data_root: Path) -> None:
        self._root = data_root / "cards"
        self._root.mkdir(parents=True, exist_ok=True)
        # In-memory connection cache (one connection per user_id).
        # Connections are opened lazily and kept open for the lifetime of the
        # cog.  sqlite3 is thread-safe in serialized mode (the default), and
        # asyncio.to_thread ensures only one thread touches each connection at
        # a time because we never call two to_thread ops for the same user_id
        # concurrently (the caller holds a per-user asyncio.Lock).
        self._conns: Dict[int, sqlite3.Connection] = {}

    # ── Internal helpers ──────────────────────────────────────────────────

    def _conn(self, user_id: int) -> sqlite3.Connection:
        if user_id not in self._conns:
            path = self._root / f"{user_id}.db"
            conn = sqlite3.connect(str(path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_DDL)
            conn.commit()
            self._conns[user_id] = conn
        return self._conns[user_id]

    def _close_all(self) -> None:
        for conn in self._conns.values():
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()

    # ── Public API ────────────────────────────────────────────────────────

    async def upsert_cards(
        self, owner_id: int, cards: List[Dict[str, Any]]
    ) -> None:
        """Insert or update a batch of cards (one round-trip)."""
        if not cards:
            return
        rows = [_card_to_row(c) for c in cards]

        def _run() -> None:
            conn = self._conn(owner_id)
            conn.executemany(_UPSERT_SQL, rows)
            conn.commit()

        await asyncio.to_thread(_run)
        log.debug("db.upsert_cards owner=%s n=%d", owner_id, len(cards))

    async def get_cards(self, owner_id: int) -> Dict[str, Any]:
        """Return {str(global_id): card_dict} for all active cards."""
        def _run() -> List[sqlite3.Row]:
            conn = self._conn(owner_id)
            return conn.execute(
                "SELECT * FROM cards WHERE owner_id = ? AND status = 'active'",
                (owner_id,),
            ).fetchall()

        rows = await asyncio.to_thread(_run)
        return {str(row["global_id"]): _row_to_card(row) for row in rows}

    async def get_all_cards(self, owner_id: int) -> Dict[str, Any]:
        """Return {str(global_id): card_dict} for ALL cards (active + removed)."""
        def _run() -> List[sqlite3.Row]:
            conn = self._conn(owner_id)
            return conn.execute(
                "SELECT * FROM cards WHERE owner_id = ?", (owner_id,)
            ).fetchall()

        rows = await asyncio.to_thread(_run)
        return {str(row["global_id"]): _row_to_card(row) for row in rows}

    async def get_removed_cards(self, owner_id: int) -> Dict[str, Any]:
        """Return {str(global_id): card_dict} for removed cards only."""
        def _run() -> List[sqlite3.Row]:
            conn = self._conn(owner_id)
            return conn.execute(
                "SELECT * FROM cards WHERE owner_id = ? AND status = 'removed'",
                (owner_id,),
            ).fetchall()

        rows = await asyncio.to_thread(_run)
        return {str(row["global_id"]): _row_to_card(row) for row in rows}

    async def get_card_by_local_id(
        self, owner_id: int, local_id: int
    ) -> Optional[Dict[str, Any]]:
        """Return the active card with this local_id, or None."""
        def _run() -> Optional[sqlite3.Row]:
            conn = self._conn(owner_id)
            return conn.execute(
                "SELECT * FROM cards WHERE owner_id = ? AND local_id = ? AND status = 'active'",
                (owner_id, local_id),
            ).fetchone()

        row = await asyncio.to_thread(_run)
        return _row_to_card(row) if row else None

    async def mark_removed(
        self,
        owner_id: int,
        local_id: int,
        source: str,
        removed_at: str,
    ) -> Optional[Dict[str, Any]]:
        """Mark the active card at local_id as removed.

        Returns the full card dict (with removal fields set) so the caller
        can write it to removed_cards if needed, or None if no matching card
        was found.
        """
        def _run() -> Optional[sqlite3.Row]:
            conn = self._conn(owner_id)
            row = conn.execute(
                "SELECT * FROM cards "
                "WHERE owner_id = ? AND local_id = ? AND status = 'active'",
                (owner_id, local_id),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE cards SET status = 'removed', local_id = NULL, "
                "removed_at = ?, removal_source = ? "
                "WHERE global_id = ?",
                (removed_at, source, row["global_id"]),
            )
            conn.commit()
            return row

        row = await asyncio.to_thread(_run)
        if row is None:
            return None
        card = _row_to_card(row)
        card.update({"status": "removed", "local_id": None,
                     "removed_at": removed_at, "removal_source": source})
        return card

    async def shift_local_ids_above(
        self,
        owner_id: int,
        above_local: int,
        delta: int = -1,
    ) -> int:
        """Shift local_ids of all active cards with local_id > above_local
        by ``delta`` (default: -1, i.e. shift down by 1).

        Returns the count of rows updated.
        """
        def _run() -> int:
            conn = self._conn(owner_id)
            cur = conn.execute(
                "UPDATE cards SET local_id = local_id + ?, local_id_source = 'calculated_removal' "
                "WHERE owner_id = ? AND status = 'active' AND local_id > ?",
                (delta, owner_id, above_local),
            )
            conn.commit()
            return cur.rowcount

        count = await asyncio.to_thread(_run)
        log.debug(
            "db.shift_local_ids owner=%s above=%s delta=%s rows=%s",
            owner_id, above_local, delta, count,
        )
        return count

    async def shift_local_ids_above_batch(
        self,
        owner_id: int,
        removed_locals: List[int],
    ) -> None:
        """Apply all shifts from a bulk removal in one transaction.

        ``removed_locals`` must be the original (pre-removal) local IDs,
        passed in any order.  For each removed local_id R, every active card
        with local_id > R is shifted down by 1.  Processing in descending
        order means we can do this in a single pass without double-counting.
        """
        if not removed_locals:
            return
        targets = sorted(removed_locals, reverse=True)

        def _run() -> None:
            conn = self._conn(owner_id)
            with conn:  # single transaction for the whole batch
                for target in targets:
                    conn.execute(
                        "UPDATE cards "
                        "SET local_id = local_id - 1, "
                        "    local_id_source = 'calculated_removal' "
                        "WHERE owner_id = ? AND status = 'active' AND local_id > ?",
                        (owner_id, target),
                    )

        await asyncio.to_thread(_run)
        log.debug(
            "db.shift_batch owner=%s targets=%s", owner_id, targets
        )

    async def max_active_local_id(self, owner_id: int) -> Optional[int]:
        """Return the highest active local_id for this user, or None."""
        def _run() -> Optional[int]:
            conn = self._conn(owner_id)
            row = conn.execute(
                "SELECT MAX(local_id) FROM cards "
                "WHERE owner_id = ? AND status = 'active'",
                (owner_id,),
            ).fetchone()
            return row[0] if row else None

        return await asyncio.to_thread(_run)

    # ── Migration ─────────────────────────────────────────────────────────

    async def migrate_from_config(
        self,
        owner_id: int,
        cards_dict: Dict[str, Any],
        removed_dict: Dict[str, Any],
    ) -> int:
        """One-time import of the old Config dicts.

        Existing rows are not overwritten (ON CONFLICT IGNORE) so this is
        safe to call more than once.  Returns the number of rows inserted.
        """
        all_cards: List[Dict[str, Any]] = []
        for card in cards_dict.values():
            if "global_id" not in card or "owner_id" not in card:
                continue
            card.setdefault("status", "active")
            all_cards.append(card)
        for card in removed_dict.values():
            if "global_id" not in card or "owner_id" not in card:
                continue
            card.setdefault("status", "removed")
            all_cards.append(card)

        if not all_cards:
            return 0

        rows = [_card_to_row(c) for c in all_cards]

        _migrate_sql = """
        INSERT OR IGNORE INTO cards (
            global_id, owner_id, local_id, local_id_source, status,
            name, waifu_id, image_url, last_seen,
            removed_at, removal_source, acquisition, local_id_history, data
        ) VALUES (
            :global_id, :owner_id, :local_id, :local_id_source, :status,
            :name, :waifu_id, :image_url, :last_seen,
            :removed_at, :removal_source, :acquisition, :local_id_history, :data
        );
        """

        def _run() -> int:
            conn = self._conn(owner_id)
            cur = conn.executemany(_migrate_sql, rows)
            conn.commit()
            return cur.rowcount

        inserted = await asyncio.to_thread(_run)
        log.info(
            "db.migrate owner=%s total=%d inserted=%d",
            owner_id, len(all_cards), inserted,
        )
        return inserted

    async def is_migrated(self, owner_id: int) -> bool:
        """Return True if this user's DB already has any rows."""
        def _run() -> bool:
            conn = self._conn(owner_id)
            row = conn.execute(
                "SELECT 1 FROM cards WHERE owner_id = ? LIMIT 1", (owner_id,)
            ).fetchone()
            return row is not None

        return await asyncio.to_thread(_run)

    async def is_config_cleared(self, owner_id: int) -> bool:
        """Return True if the legacy Red Config ``cards``/``removed_cards``
        dicts for this user have already been cleared.

        Backed by SQLite (not the in-process ``_db_migrated`` set the cog
        also keeps) so the answer survives a restart: without this, every
        bot restart would make every active user's first card-touch pay
        for two extra Config reads just to re-confirm dicts that were
        already emptied out, forever.
        """
        def _run() -> bool:
            conn = self._conn(owner_id)
            row = conn.execute(
                "SELECT config_cleared FROM meta WHERE owner_id = ?", (owner_id,)
            ).fetchone()
            return bool(row and row[0])

        return await asyncio.to_thread(_run)

    async def mark_config_cleared(self, owner_id: int) -> None:
        def _run() -> None:
            conn = self._conn(owner_id)
            conn.execute(
                "INSERT INTO meta (owner_id, config_cleared) VALUES (?, 1) "
                "ON CONFLICT(owner_id) DO UPDATE SET config_cleared = 1",
                (owner_id,),
            )
            conn.commit()

        await asyncio.to_thread(_run)
