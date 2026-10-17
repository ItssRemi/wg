from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 3


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class AdventureDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._run(self._initialize_sync)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    async def _run(self, func, *args):
        async with self._lock:
            return await asyncio.to_thread(func, *args)

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS message_states (
                    message_id INTEGER NOT NULL,
                    state_hash TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY (message_id, state_hash)
                );

                CREATE TABLE IF NOT EXISTS adventure_runs (
                    message_id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    channel_id INTEGER,
                    user_id INTEGER NOT NULL,
                    adventure_key TEXT,
                    adventure_name TEXT,
                    state TEXT NOT NULL DEFAULT 'pending',
                    started_at TEXT,
                    completed_at TEXT,
                    started_utc_day TEXT,
                    completed_utc_day TEXT,
                    last_seen_at TEXT NOT NULL,
                    current_prompt_text TEXT,
                    current_prompt_json TEXT,
                    selected_item_ids_json TEXT NOT NULL DEFAULT '[]',
                    selected_items_json TEXT NOT NULL DEFAULT '[]',
                    selected_items_observed INTEGER NOT NULL DEFAULT 0,
                    returned_items_json TEXT NOT NULL DEFAULT '[]',
                    backpack_losses_json TEXT NOT NULL DEFAULT '[]',
                    node_item_losses_json TEXT NOT NULL DEFAULT '[]',
                    rewards_json TEXT NOT NULL DEFAULT '[]',
                    ignored_rewards_json TEXT NOT NULL DEFAULT '[]',
                    unknown_rewards_json TEXT NOT NULL DEFAULT '[]',
                    multipliers_json TEXT NOT NULL DEFAULT '[]',
                    forfeited_rewards_json TEXT NOT NULL DEFAULT '[]',
                    ticket_quantity INTEGER NOT NULL DEFAULT 0,
                    max_interactions INTEGER,
                    interactions INTEGER,
                    status TEXT NOT NULL DEFAULT 'incomplete',
                    completion_reason TEXT,
                    ending_type TEXT,
                    ending_text TEXT,
                    premature_end_seen INTEGER NOT NULL DEFAULT 0,
                    lose_all_seen INTEGER NOT NULL DEFAULT 0,
                    completion_percent REAL,
                    coins_gained INTEGER NOT NULL DEFAULT 0,
                    fish_tokens_gained INTEGER NOT NULL DEFAULT 0,
                    skin_fragments_gained INTEGER NOT NULL DEFAULT 0,
                    gross_reward_net INTEGER NOT NULL DEFAULT 0,
                    gross_reward_market INTEGER NOT NULL DEFAULT 0,
                    ticket_cost_net INTEGER NOT NULL DEFAULT 0,
                    ticket_cost_market INTEGER NOT NULL DEFAULT 0,
                    backpack_loss_net INTEGER NOT NULL DEFAULT 0,
                    backpack_loss_market INTEGER NOT NULL DEFAULT 0,
                    profit_net INTEGER NOT NULL DEFAULT 0,
                    profit_market INTEGER NOT NULL DEFAULT 0,
                    compass_proc_interactions INTEGER NOT NULL DEFAULT 0,
                    compass_doubled_components INTEGER NOT NULL DEFAULT 0,
                    compass_bonus_net INTEGER NOT NULL DEFAULT 0,
                    compass_bonus_market INTEGER NOT NULL DEFAULT 0,
                    compass_bonus_coins INTEGER NOT NULL DEFAULT 0,
                    raw_summary_json TEXT,
                    finalized INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_runs_user_day ON adventure_runs(user_id, completed_utc_day);
                CREATE INDEX IF NOT EXISTS idx_runs_guild_day ON adventure_runs(guild_id, completed_utc_day);
                CREATE INDEX IF NOT EXISTS idx_runs_state ON adventure_runs(state, last_seen_at);

                CREATE TABLE IF NOT EXISTS adventure_message_aliases (
                    alias_message_id INTEGER PRIMARY KEY,
                    canonical_message_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    adventure_key TEXT,
                    linked_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    FOREIGN KEY(canonical_message_id) REFERENCES adventure_runs(message_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_adventure_alias_canonical
                    ON adventure_message_aliases(canonical_message_id);
                CREATE INDEX IF NOT EXISTS idx_adventure_alias_user
                    ON adventure_message_aliases(user_id, adventure_key);

                CREATE TABLE IF NOT EXISTS node_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    event_hash TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    selected_custom_id TEXT,
                    selected_label TEXT,
                    prompt_text TEXT,
                    result_text TEXT NOT NULL,
                    ending_type TEXT,
                    fatal_end INTEGER NOT NULL DEFAULT 0,
                    lose_all INTEGER NOT NULL DEFAULT 0,
                    item_losses_json TEXT NOT NULL DEFAULT '[]',
                    parsed_rewards_json TEXT NOT NULL DEFAULT '[]',
                    compass_marker INTEGER NOT NULL DEFAULT 0,
                    compass_proc INTEGER NOT NULL DEFAULT 0,
                    doubled_components_json TEXT NOT NULL DEFAULT '[]',
                    compass_bonus_net INTEGER NOT NULL DEFAULT 0,
                    compass_bonus_market INTEGER NOT NULL DEFAULT 0,
                    compass_bonus_coins INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT,
                    UNIQUE(message_id, event_hash),
                    FOREIGN KEY(message_id) REFERENCES adventure_runs(message_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_nodes_message ON node_events(message_id, occurred_at);

                CREATE TABLE IF NOT EXISTS ledger_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_message_id INTEGER,
                    guild_id INTEGER,
                    user_id INTEGER NOT NULL,
                    event_at TEXT NOT NULL,
                    utc_day TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    direction INTEGER NOT NULL,
                    item_key TEXT,
                    name TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    amount INTEGER NOT NULL DEFAULT 0,
                    net_unit INTEGER NOT NULL DEFAULT 0,
                    market_unit INTEGER NOT NULL DEFAULT 0,
                    net_total INTEGER NOT NULL DEFAULT 0,
                    market_total INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_user_day ON ledger_entries(user_id, utc_day);
                CREATE INDEX IF NOT EXISTS idx_ledger_run ON ledger_entries(run_message_id);

                CREATE TABLE IF NOT EXISTS compass_sessions (
                    session_message_id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    channel_id INTEGER,
                    user_id INTEGER NOT NULL,
                    used_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    explicit_observed INTEGER NOT NULL DEFAULT 1,
                    cost_quantity INTEGER NOT NULL DEFAULT 1,
                    cost_net INTEGER NOT NULL DEFAULT 0,
                    cost_market INTEGER NOT NULL DEFAULT 0,
                    proc_interactions INTEGER NOT NULL DEFAULT 0,
                    doubled_components INTEGER NOT NULL DEFAULT 0,
                    bonus_net INTEGER NOT NULL DEFAULT 0,
                    bonus_market INTEGER NOT NULL DEFAULT 0,
                    bonus_coins INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_compass_user_time ON compass_sessions(user_id, used_at, expires_at);

                CREATE TABLE IF NOT EXISTS compass_bonus_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_message_id INTEGER,
                    run_message_id INTEGER NOT NULL,
                    node_event_id INTEGER,
                    occurred_at TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    item_key TEXT,
                    name TEXT NOT NULL,
                    base_amount INTEGER NOT NULL,
                    actual_amount INTEGER NOT NULL,
                    bonus_amount INTEGER NOT NULL,
                    bonus_net INTEGER NOT NULL DEFAULT 0,
                    bonus_market INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(run_message_id, node_event_id, kind, name),
                    FOREIGN KEY(session_message_id) REFERENCES compass_sessions(session_message_id) ON DELETE SET NULL,
                    FOREIGN KEY(run_message_id) REFERENCES adventure_runs(message_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS effect_sessions (
                    session_message_id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    channel_id INTEGER,
                    user_id INTEGER NOT NULL,
                    effect_key TEXT NOT NULL,
                    used_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    raw_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_effect_user_time ON effect_sessions(user_id, effect_key, used_at, expires_at);

                CREATE TABLE IF NOT EXISTS adventure_luck_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_event_id INTEGER NOT NULL UNIQUE,
                    run_message_id INTEGER NOT NULL,
                    guild_id INTEGER,
                    user_id INTEGER NOT NULL,
                    adventure_key TEXT NOT NULL,
                    interaction_key TEXT NOT NULL,
                    option_label TEXT,
                    occurred_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'live',
                    active_items_known INTEGER NOT NULL DEFAULT 0,
                    active_items_before_json TEXT NOT NULL DEFAULT '[]',
                    active_items_after_json TEXT NOT NULL DEFAULT '[]',
                    contributors_json TEXT NOT NULL DEFAULT '[]',
                    total_luck_percent REAL NOT NULL DEFAULT 0,
                    item_losses_json TEXT NOT NULL DEFAULT '[]',
                    state_conflicts_json TEXT NOT NULL DEFAULT '[]',
                    result_text TEXT NOT NULL,
                    FOREIGN KEY(node_event_id) REFERENCES node_events(id) ON DELETE CASCADE,
                    FOREIGN KEY(run_message_id) REFERENCES adventure_runs(message_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_luck_observation_interaction
                    ON adventure_luck_observations(adventure_key, interaction_key, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_luck_observation_user
                    ON adventure_luck_observations(user_id, occurred_at);

                CREATE TABLE IF NOT EXISTS adventure_luck_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observation_id INTEGER NOT NULL,
                    item_key TEXT,
                    item_name TEXT NOT NULL,
                    luck_percent REAL NOT NULL,
                    active_at_interaction INTEGER,
                    known_item INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(observation_id, item_name),
                    FOREIGN KEY(observation_id) REFERENCES adventure_luck_observations(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_luck_source_item
                    ON adventure_luck_sources(item_key, item_name);

                CREATE TABLE IF NOT EXISTS unknown_rewards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_message_id INTEGER,
                    adventure_key TEXT,
                    user_id INTEGER,
                    raw_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    raw_json TEXT
                );
                """
            )
            run_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(adventure_runs)").fetchall()}
            if "selected_items_observed" not in run_columns:
                conn.execute(
                    "ALTER TABLE adventure_runs ADD COLUMN selected_items_observed INTEGER NOT NULL DEFAULT 0"
                )
            # Existing non-empty selections were necessarily observed from an
            # adventure-start payload. Historical empty selections remain
            # unknown because old rows cannot distinguish no items from a
            # missed backpack screen.
            conn.execute(
                """
                UPDATE adventure_runs SET selected_items_observed=1
                WHERE selected_items_observed=0
                  AND (selected_item_ids_json!='[]' OR selected_items_json!='[]')
                """
            )
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
            conn.commit()

    async def claim_state(self, message_id: int, state_hash: str, processed_at: str) -> bool:
        def op() -> bool:
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO message_states(message_id, state_hash, processed_at) VALUES(?,?,?)",
                    (int(message_id), state_hash, processed_at),
                )
                conn.commit()
                return cur.rowcount > 0
        return await self._run(op)

    @staticmethod
    def _resolve_message_id_sync(conn: sqlite3.Connection, message_id: int) -> int:
        row = conn.execute(
            "SELECT canonical_message_id FROM adventure_message_aliases WHERE alias_message_id=?",
            (int(message_id),),
        ).fetchone()
        return int(row[0]) if row else int(message_id)

    async def resolve_message_id(self, message_id: int) -> int:
        def op() -> int:
            with self._connect() as conn:
                return self._resolve_message_id_sync(conn, int(message_id))
        return await self._run(op)

    async def get_run(self, message_id: int) -> Optional[Dict[str, Any]]:
        def op():
            with self._connect() as conn:
                canonical_id = self._resolve_message_id_sync(conn, int(message_id))
                row = conn.execute(
                    "SELECT * FROM adventure_runs WHERE message_id=?",
                    (canonical_id,),
                ).fetchone()
                return dict(row) if row else None
        return await self._run(op)

    async def link_message_alias(
        self,
        alias_message_id: int,
        canonical_message_id: int,
        user_id: int,
        adventure_key: str,
        linked_at: str,
        reason: str,
    ) -> None:
        if int(alias_message_id) == int(canonical_message_id):
            return

        def op() -> None:
            with self._connect() as conn:
                canonical = self._resolve_message_id_sync(conn, int(canonical_message_id))
                exists = conn.execute(
                    "SELECT 1 FROM adventure_runs WHERE message_id=? AND finalized=0",
                    (canonical,),
                ).fetchone()
                if not exists:
                    return
                conn.execute(
                    """
                    INSERT INTO adventure_message_aliases(
                        alias_message_id,canonical_message_id,user_id,adventure_key,linked_at,reason
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(alias_message_id) DO UPDATE SET
                        canonical_message_id=excluded.canonical_message_id,
                        user_id=excluded.user_id,
                        adventure_key=excluded.adventure_key,
                        linked_at=excluded.linked_at,
                        reason=excluded.reason
                    """,
                    (
                        int(alias_message_id), canonical, int(user_id), adventure_key or None,
                        linked_at, reason,
                    ),
                )
                conn.commit()
        await self._run(op)

    async def find_resumable_run(
        self,
        user_id: int,
        adventure_key: str,
        prompt_text: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Find the canonical unfinished run represented by a newly rendered message.

        Dank Memer can render the same active adventure in a new message when a
        user runs /adventure again. Exact prompt matching is authoritative. A
        sole active run of the same adventure is a safe fallback when the prompt
        was not captured before the continuation.
        """

        def normalize_prompt(value: Any) -> str:
            text = str(value or "")
            text = re.sub(
                r"(?:^|\n)\s*(?:[-•]\s*)?You have already completed this event,\s*"
                r"click\s*<a?:[^:>]+:\d+>\s*to see your rewards\s*$",
                "",
                text,
                flags=re.IGNORECASE,
            )
            return re.sub(r"\s+", " ", text).strip().casefold()

        def op():
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM adventure_runs
                    WHERE user_id=? AND adventure_key=? AND finalized=0
                      AND state IN ('active','stale')
                    ORDER BY CASE state WHEN 'active' THEN 0 ELSE 1 END, last_seen_at DESC
                    """,
                    (int(user_id), adventure_key),
                ).fetchall()
                candidates = [dict(row) for row in rows]
                wanted = normalize_prompt(prompt_text)
                if wanted:
                    for candidate in candidates:
                        if normalize_prompt(candidate.get("current_prompt_text")) == wanted:
                            return candidate
                active = [candidate for candidate in candidates if candidate.get("state") == "active"]
                if len(active) == 1:
                    return active[0]
                return None
        return await self._run(op)

    async def upsert_pending_run(self, data: Dict[str, Any]) -> None:
        def op():
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO adventure_runs(
                        message_id,guild_id,channel_id,user_id,adventure_key,adventure_name,state,
                        last_seen_at,selected_item_ids_json,selected_items_json,selected_items_observed,
                        ticket_quantity,max_interactions
                    ) VALUES(?,?,?,?,?,?, 'pending',?,?,?,1,?,?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        guild_id=excluded.guild_id, channel_id=excluded.channel_id, user_id=excluded.user_id,
                        adventure_key=excluded.adventure_key, adventure_name=excluded.adventure_name,
                        last_seen_at=excluded.last_seen_at,
                        selected_item_ids_json=excluded.selected_item_ids_json,
                        selected_items_json=excluded.selected_items_json,
                        selected_items_observed=1,
                        ticket_quantity=excluded.ticket_quantity,
                        max_interactions=excluded.max_interactions
                    """,
                    (
                        data["message_id"], data.get("guild_id"), data.get("channel_id"), data["user_id"],
                        data.get("adventure_key"), data.get("adventure_name"), data["last_seen_at"],
                        dumps(data.get("selected_item_ids", [])), dumps(data.get("selected_items", [])),
                        data.get("ticket_quantity", 0), data.get("max_interactions"),
                    ),
                )
                conn.commit()
        await self._run(op)

    async def ensure_active_run(self, data: Dict[str, Any]) -> None:
        def op():
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO adventure_runs(
                        message_id,guild_id,channel_id,user_id,adventure_key,adventure_name,state,
                        started_at,started_utc_day,last_seen_at,ticket_quantity,max_interactions
                    ) VALUES(?,?,?,?,?,?, 'active',?,?,?,?,?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        guild_id=COALESCE(adventure_runs.guild_id, excluded.guild_id),
                        channel_id=COALESCE(adventure_runs.channel_id, excluded.channel_id),
                        user_id=excluded.user_id,
                        adventure_key=COALESCE(adventure_runs.adventure_key, excluded.adventure_key),
                        adventure_name=COALESCE(adventure_runs.adventure_name, excluded.adventure_name),
                        state=CASE WHEN adventure_runs.finalized=1 THEN adventure_runs.state ELSE 'active' END,
                        started_at=COALESCE(adventure_runs.started_at, excluded.started_at),
                        started_utc_day=COALESCE(adventure_runs.started_utc_day, excluded.started_utc_day),
                        last_seen_at=excluded.last_seen_at,
                        ticket_quantity=CASE WHEN adventure_runs.ticket_quantity=0 THEN excluded.ticket_quantity ELSE adventure_runs.ticket_quantity END,
                        max_interactions=COALESCE(adventure_runs.max_interactions, excluded.max_interactions)
                    """,
                    (
                        data["message_id"], data.get("guild_id"), data.get("channel_id"), data["user_id"],
                        data.get("adventure_key"), data.get("adventure_name"), data["started_at"],
                        data["started_utc_day"], data["last_seen_at"], data.get("ticket_quantity", 0),
                        data.get("max_interactions"),
                    ),
                )
                conn.commit()
        await self._run(op)



    async def ensure_start_ticket_ledger(self, run: Dict[str, Any], ticket_item: Dict[str, Any]) -> None:
        def op():
            with self._connect() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM ledger_entries WHERE run_message_id=? AND source='run_start' AND kind='ticket_cost' LIMIT 1",
                    (run["message_id"],),
                ).fetchone()
                if exists:
                    return
                quantity = int(run.get("ticket_quantity") or 0)
                if quantity <= 0:
                    return
                net_unit = int(ticket_item.get("net_unit") or 0)
                market_unit = int(ticket_item.get("market_unit") or 0)
                event_at = str(run.get("started_at") or run.get("last_seen_at"))
                conn.execute(
                    """
                    INSERT INTO ledger_entries(
                        run_message_id,guild_id,user_id,event_at,utc_day,kind,source,direction,item_key,name,
                        quantity,net_unit,market_unit,net_total,market_total,reason,metadata_json
                    ) VALUES(?,?,?,?,?,'ticket_cost','run_start',-1,'adventureticket','Adventure Ticket',?,?,?,?,?,'adventure_entry','{}')
                    """,
                    (
                        run["message_id"], run.get("guild_id"), run["user_id"], event_at, event_at[:10],
                        quantity, net_unit, market_unit, -net_unit * quantity, -market_unit * quantity,
                    ),
                )
                conn.commit()
        await self._run(op)

    async def update_prompt(self, message_id: int, prompt_text: str, prompt_json: Dict[str, Any], seen_at: str) -> None:
        def op():
            with self._connect() as conn:
                conn.execute(
                    "UPDATE adventure_runs SET current_prompt_text=?, current_prompt_json=?, last_seen_at=? WHERE message_id=?",
                    (prompt_text, dumps(prompt_json), seen_at, message_id),
                )
                conn.commit()
        await self._run(op)

    async def add_node_event(self, run_message_id: int, event: Dict[str, Any]) -> Optional[int]:
        def op():
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO node_events(
                        message_id,event_hash,occurred_at,selected_custom_id,selected_label,prompt_text,result_text,
                        ending_type,fatal_end,lose_all,item_losses_json,parsed_rewards_json,compass_marker,
                        compass_proc,doubled_components_json,compass_bonus_net,compass_bonus_market,
                        compass_bonus_coins,raw_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_message_id, event["event_hash"], event["occurred_at"], event.get("selected_custom_id"),
                        event.get("selected_label"), event.get("prompt_text"), event.get("result_text", ""),
                        event.get("ending_type"), int(bool(event.get("fatal_end"))), int(bool(event.get("lose_all"))),
                        dumps(event.get("item_losses", [])), dumps(event.get("parsed_rewards", [])),
                        int(bool(event.get("compass_marker"))), int(bool(event.get("compass_proc"))),
                        dumps(event.get("doubled_components", [])), int(event.get("compass_bonus_net", 0)),
                        int(event.get("compass_bonus_market", 0)), int(event.get("compass_bonus_coins", 0)),
                        dumps(event.get("raw", {})),
                    ),
                )
                if cur.rowcount == 0:
                    row = conn.execute(
                        "SELECT id FROM node_events WHERE message_id=? AND event_hash=?",
                        (run_message_id, event["event_hash"]),
                    ).fetchone()
                    return int(row[0]) if row else None
                node_id = int(cur.lastrowid)
                if event.get("fatal_end") or event.get("item_losses"):
                    run = conn.execute("SELECT node_item_losses_json, ending_type, ending_text FROM adventure_runs WHERE message_id=?", (run_message_id,)).fetchone()
                    prior = loads(run[0] if run else None, [])
                    prior.extend(event.get("item_losses", []))
                    conn.execute(
                        """
                        UPDATE adventure_runs SET
                            node_item_losses_json=?,
                            ending_type=CASE WHEN ?!='' THEN ? ELSE ending_type END,
                            ending_text=CASE WHEN ?!='' THEN ? ELSE ending_text END,
                            premature_end_seen=MAX(premature_end_seen, ?),
                            lose_all_seen=MAX(lose_all_seen, ?),
                            last_seen_at=?
                        WHERE message_id=?
                        """,
                        (
                            dumps(prior), event.get("ending_type", ""), event.get("ending_type", ""),
                            event.get("result_text", "") if event.get("fatal_end") else "",
                            event.get("result_text", "") if event.get("fatal_end") else "",
                            int(bool(event.get("fatal_end"))), int(bool(event.get("lose_all"))),
                            event["occurred_at"], run_message_id,
                        ),
                    )
                else:
                    conn.execute("UPDATE adventure_runs SET last_seen_at=? WHERE message_id=?", (event["occurred_at"], run_message_id))
                if event.get("compass_proc"):
                    components = event.get("doubled_components", [])
                    conn.execute(
                        """
                        UPDATE adventure_runs SET
                            compass_proc_interactions=compass_proc_interactions+1,
                            compass_doubled_components=compass_doubled_components+?,
                            compass_bonus_net=compass_bonus_net+?,
                            compass_bonus_market=compass_bonus_market+?,
                            compass_bonus_coins=compass_bonus_coins+?
                        WHERE message_id=?
                        """,
                        (len(components), int(event.get("compass_bonus_net", 0)),
                         int(event.get("compass_bonus_market", 0)), int(event.get("compass_bonus_coins", 0)),
                         run_message_id),
                    )
                conn.commit()
                return node_id
        return await self._run(op)

    async def add_luck_observation(self, observation: Dict[str, Any]) -> int:
        """Upsert one raw interaction observation and its individual sources."""

        def op():
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO adventure_luck_observations(
                        node_event_id,run_message_id,guild_id,user_id,adventure_key,interaction_key,
                        option_label,occurred_at,source,active_items_known,active_items_before_json,
                        active_items_after_json,contributors_json,total_luck_percent,item_losses_json,
                        state_conflicts_json,result_text
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(node_event_id) DO UPDATE SET
                        guild_id=excluded.guild_id,
                        user_id=excluded.user_id,
                        adventure_key=excluded.adventure_key,
                        interaction_key=excluded.interaction_key,
                        option_label=excluded.option_label,
                        occurred_at=excluded.occurred_at,
                        source=excluded.source,
                        active_items_known=MAX(adventure_luck_observations.active_items_known, excluded.active_items_known),
                        active_items_before_json=CASE
                            WHEN excluded.active_items_known=1 THEN excluded.active_items_before_json
                            ELSE adventure_luck_observations.active_items_before_json END,
                        active_items_after_json=CASE
                            WHEN excluded.active_items_known=1 THEN excluded.active_items_after_json
                            ELSE adventure_luck_observations.active_items_after_json END,
                        contributors_json=excluded.contributors_json,
                        total_luck_percent=excluded.total_luck_percent,
                        item_losses_json=excluded.item_losses_json,
                        state_conflicts_json=CASE
                            WHEN excluded.active_items_known=1 THEN excluded.state_conflicts_json
                            ELSE adventure_luck_observations.state_conflicts_json END,
                        result_text=excluded.result_text
                    """,
                    (
                        int(observation["node_event_id"]), int(observation["run_message_id"]),
                        observation.get("guild_id"), int(observation["user_id"]),
                        str(observation.get("adventure_key") or ""),
                        str(observation.get("interaction_key") or ""),
                        observation.get("option_label"), str(observation["occurred_at"]),
                        str(observation.get("source") or "live"),
                        int(bool(observation.get("active_items_known"))),
                        dumps(observation.get("active_items_before", [])),
                        dumps(observation.get("active_items_after", [])),
                        dumps(observation.get("contributors", [])),
                        float(observation.get("total_luck_percent") or 0),
                        dumps(observation.get("item_losses", [])),
                        dumps(observation.get("state_conflicts", [])),
                        str(observation.get("result_text") or ""),
                    ),
                )
                row = conn.execute(
                    "SELECT id FROM adventure_luck_observations WHERE node_event_id=?",
                    (int(observation["node_event_id"]),),
                ).fetchone()
                observation_id = int(row[0])
                conn.execute(
                    "DELETE FROM adventure_luck_sources WHERE observation_id=?",
                    (observation_id,),
                )
                active_names = {
                    str(item.get("item_key") or item.get("name") or "").casefold()
                    for item in observation.get("active_items_before", [])
                }
                for source in observation.get("contributors", []):
                    identity = str(source.get("item_key") or source.get("name") or "").casefold()
                    active_flag = None
                    if observation.get("active_items_known"):
                        active_flag = int(identity in active_names)
                    conn.execute(
                        """
                        INSERT INTO adventure_luck_sources(
                            observation_id,item_key,item_name,luck_percent,active_at_interaction,known_item
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (
                            observation_id, source.get("item_key"), str(source.get("name") or "Unknown"),
                            float(source.get("luck_percent") or 0), active_flag,
                            int(bool(source.get("known"))),
                        ),
                    )
                conn.commit()
                return observation_id

        return await self._run(op)

    async def luck_backfill_rows(self) -> List[Dict[str, Any]]:
        """Return complete ordered runs containing at least one unlogged node."""

        def op():
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT
                        r.message_id AS run_message_id, r.guild_id, r.user_id, r.adventure_key,
                        r.selected_items_json, r.selected_items_observed,
                        n.id AS node_event_id, n.occurred_at, n.selected_custom_id, n.selected_label,
                        n.result_text, n.item_losses_json, n.lose_all
                    FROM adventure_runs r
                    JOIN node_events n ON n.message_id=r.message_id
                    WHERE r.message_id IN (
                        SELECT DISTINCT n2.message_id
                        FROM node_events n2
                        LEFT JOIN adventure_luck_observations o ON o.node_event_id=n2.id
                        WHERE o.id IS NULL
                    )
                    ORDER BY r.message_id,n.occurred_at,n.id
                    """
                ).fetchall()
                return [dict(row) for row in rows]

        return await self._run(op)

    async def find_active_compass_session(self, user_id: int, occurred_at: str) -> Optional[Dict[str, Any]]:
        def op():
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT * FROM compass_sessions
                    WHERE user_id=? AND used_at<=? AND expires_at>=?
                    ORDER BY used_at DESC LIMIT 1
                    """,
                    (user_id, occurred_at, occurred_at),
                ).fetchone()
                return dict(row) if row else None
        return await self._run(op)

    async def add_compass_bonus(self, session_message_id: Optional[int], run_message_id: int, node_event_id: int, user_id: int, occurred_at: str, components: Sequence[Dict[str, Any]]) -> None:
        def op():
            with self._connect() as conn:
                inserted = 0
                total_net = total_market = total_coins = 0
                for component in components:
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO compass_bonus_entries(
                            session_message_id,run_message_id,node_event_id,occurred_at,user_id,kind,item_key,name,
                            base_amount,actual_amount,bonus_amount,bonus_net,bonus_market
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            session_message_id, run_message_id, node_event_id, occurred_at, user_id,
                            component.get("kind"), component.get("item_key"), component.get("name") or "Unknown",
                            component.get("base_amount", 0), component.get("actual_amount", 0),
                            component.get("bonus_amount", 0), component.get("bonus_net", 0),
                            component.get("bonus_market", 0),
                        ),
                    )
                    if cur.rowcount:
                        inserted += 1
                        total_net += int(component.get("bonus_net", 0))
                        total_market += int(component.get("bonus_market", 0))
                        if component.get("kind") == "coins":
                            total_coins += int(component.get("bonus_amount", 0))
                if inserted:
                    if session_message_id is not None:
                        conn.execute(
                            """
                            UPDATE compass_sessions SET
                                proc_interactions=proc_interactions+1,
                                doubled_components=doubled_components+?,
                                bonus_net=bonus_net+?, bonus_market=bonus_market+?, bonus_coins=bonus_coins+?
                            WHERE session_message_id=?
                            """,
                            (inserted, total_net, total_market, total_coins, session_message_id),
                        )
                conn.commit()
        await self._run(op)

    async def add_compass_session(self, session: Dict[str, Any]) -> None:
        def op():
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO compass_sessions(
                        session_message_id,guild_id,channel_id,user_id,used_at,expires_at,explicit_observed,
                        cost_quantity,cost_net,cost_market,raw_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        session["session_message_id"], session.get("guild_id"), session.get("channel_id"),
                        session["user_id"], session["used_at"], session["expires_at"],
                        int(bool(session.get("explicit_observed", True))), session.get("cost_quantity", 1),
                        session.get("cost_net", 0), session.get("cost_market", 0), dumps(session.get("raw", {})),
                    ),
                )
                if cur.rowcount:
                    conn.execute(
                        """
                        INSERT INTO ledger_entries(
                            run_message_id,guild_id,user_id,event_at,utc_day,kind,source,direction,item_key,name,
                            quantity,net_unit,market_unit,net_total,market_total,reason,metadata_json
                        ) VALUES(NULL,?,?,?,?, 'compass_use','effect_use',-1,'compass','Adventure Compass',?,?,?,?,?,'compass_consumed','{}')
                        """,
                        (
                            session.get("guild_id"), session["user_id"], session["used_at"],
                            session["used_at"][:10], session.get("cost_quantity", 1), session.get("cost_net", 0),
                            session.get("cost_market", 0), -int(session.get("cost_net", 0)),
                            -int(session.get("cost_market", 0)),
                        ),
                    )
                conn.commit()
        await self._run(op)

    async def add_effect_session(self, session: Dict[str, Any]) -> None:
        def op():
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO effect_sessions(
                        session_message_id,guild_id,channel_id,user_id,effect_key,used_at,expires_at,raw_json
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        session["session_message_id"], session.get("guild_id"), session.get("channel_id"),
                        session["user_id"], session["effect_key"], session["used_at"], session["expires_at"],
                        dumps(session.get("raw", {})),
                    ),
                )
                conn.commit()
        await self._run(op)

    async def finalize_run(self, message_id: int, final: Dict[str, Any], ledger_entries: Sequence[Dict[str, Any]]) -> None:
        def op():
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE adventure_runs SET
                        guild_id=?,channel_id=?,user_id=?,adventure_key=?,adventure_name=?,state='finalized',
                        completed_at=?,completed_utc_day=?,last_seen_at=?,returned_items_json=?,
                        backpack_losses_json=?,rewards_json=?,ignored_rewards_json=?,unknown_rewards_json=?,
                        multipliers_json=?,forfeited_rewards_json=?,ticket_quantity=?,max_interactions=?,interactions=?,
                        status=?,completion_reason=?,ending_type=?,ending_text=?,premature_end_seen=?,lose_all_seen=?,
                        completion_percent=?,coins_gained=?,fish_tokens_gained=?,skin_fragments_gained=?,
                        gross_reward_net=?,gross_reward_market=?,ticket_cost_net=?,ticket_cost_market=?,
                        backpack_loss_net=?,backpack_loss_market=?,profit_net=?,profit_market=?,raw_summary_json=?,finalized=1
                    WHERE message_id=?
                    """,
                    (
                        final.get("guild_id"), final.get("channel_id"), final["user_id"], final.get("adventure_key"),
                        final.get("adventure_name"), final["completed_at"], final["completed_utc_day"],
                        final["completed_at"], dumps(final.get("returned_items", [])),
                        dumps(final.get("backpack_losses", [])), dumps(final.get("rewards", [])),
                        dumps(final.get("ignored_rewards", [])), dumps(final.get("unknown_rewards", [])),
                        dumps(final.get("multipliers", [])), dumps(final.get("forfeited_rewards", [])),
                        final.get("ticket_quantity", 0), final.get("max_interactions"), final.get("interactions"),
                        final.get("status"), final.get("completion_reason"), final.get("ending_type"),
                        final.get("ending_text"), int(bool(final.get("premature_end_seen"))),
                        int(bool(final.get("lose_all_seen"))), final.get("completion_percent"),
                        final.get("coins_gained", 0), final.get("fish_tokens_gained", 0),
                        final.get("skin_fragments_gained", 0), final.get("gross_reward_net", 0),
                        final.get("gross_reward_market", 0), final.get("ticket_cost_net", 0),
                        final.get("ticket_cost_market", 0), final.get("backpack_loss_net", 0),
                        final.get("backpack_loss_market", 0), final.get("profit_net", 0),
                        final.get("profit_market", 0), dumps(final.get("raw_summary", {})), message_id,
                    ),
                )
                conn.execute("DELETE FROM ledger_entries WHERE run_message_id=? AND source='run_final'", (message_id,))
                for entry in ledger_entries:
                    conn.execute(
                        """
                        INSERT INTO ledger_entries(
                            run_message_id,guild_id,user_id,event_at,utc_day,kind,source,direction,item_key,name,
                            quantity,amount,net_unit,market_unit,net_total,market_total,reason,metadata_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            message_id, final.get("guild_id"), final["user_id"], final["completed_at"],
                            final["completed_utc_day"], entry["kind"], "run_final", entry.get("direction", 1),
                            entry.get("item_key"), entry.get("name", "Unknown"), entry.get("quantity", 0),
                            entry.get("amount", 0), entry.get("net_unit", 0), entry.get("market_unit", 0),
                            entry.get("net_total", 0), entry.get("market_total", 0), entry.get("reason"),
                            dumps(entry.get("metadata", {})),
                        ),
                    )
                for reward in final.get("unknown_rewards", []):
                    conn.execute(
                        """
                        INSERT INTO unknown_rewards(run_message_id,adventure_key,user_id,raw_name,quantity,first_seen_at,raw_json)
                        VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            message_id, final.get("adventure_key"), final["user_id"], reward.get("name", "Unknown"),
                            reward.get("quantity", 1), final["completed_at"], dumps(reward),
                        ),
                    )
                conn.commit()
        await self._run(op)

    async def node_events_for_run(self, message_id: int) -> List[Dict[str, Any]]:
        def op():
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM node_events WHERE message_id=? ORDER BY occurred_at,id", (message_id,)).fetchall()
                return [dict(row) for row in rows]
        return await self._run(op)

    async def user_stats(self, user_id: int, utc_day: str) -> Dict[str, Any]:
        def op():
            with self._connect() as conn:
                summary = conn.execute(
                    """
                    SELECT
                        COUNT(*) completed,
                        SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) won,
                        SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) lost,
                        SUM(CASE WHEN lose_all_seen=1 THEN 1 ELSE 0 END) died,
                        SUM(CASE WHEN status!='won' AND lose_all_seen=0 THEN 1 ELSE 0 END) neutral,
                        COALESCE(SUM(profit_net),0) profit_net,
                        COALESCE(SUM(profit_market),0) profit_market,
                        COALESCE(SUM(gross_reward_net),0) gross_net,
                        COALESCE(SUM(gross_reward_market),0) gross_market,
                        COALESCE(SUM(ticket_cost_net),0) ticket_net,
                        COALESCE(SUM(ticket_cost_market),0) ticket_market,
                        COALESCE(SUM(backpack_loss_net),0) backpack_net,
                        COALESCE(SUM(backpack_loss_market),0) backpack_market,
                        COALESCE(SUM(ticket_quantity),0) tickets,
                        COALESCE(SUM(coins_gained),0) coins,
                        COALESCE(SUM(fish_tokens_gained),0) fish_tokens,
                        COALESCE(SUM(skin_fragments_gained),0) skin_fragments,
                        COALESCE(SUM(compass_proc_interactions),0) compass_procs,
                        COALESCE(SUM(compass_doubled_components),0) doubled_components,
                        COALESCE(SUM(compass_bonus_net),0) compass_bonus_net,
                        COALESCE(SUM(compass_bonus_market),0) compass_bonus_market,
                        AVG(completion_percent) avg_completion
                    FROM adventure_runs
                    WHERE user_id=? AND completed_utc_day=? AND finalized=1
                    """,
                    (user_id, utc_day),
                ).fetchone()
                active = conn.execute(
                    "SELECT COUNT(*) FROM adventure_runs WHERE user_id=? AND state='active' AND started_utc_day=?",
                    (user_id, utc_day),
                ).fetchone()[0]
                incomplete = conn.execute(
                    "SELECT COUNT(*) FROM adventure_runs WHERE user_id=? AND state='stale' AND started_utc_day=?",
                    (user_id, utc_day),
                ).fetchone()[0]
                by_adv = conn.execute(
                    """
                    SELECT adventure_key,adventure_name,COUNT(*) played,
                           SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) won,
                           SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) lost,
                           SUM(CASE WHEN lose_all_seen=1 THEN 1 ELSE 0 END) died,
                           SUM(CASE WHEN status!='won' AND lose_all_seen=0 THEN 1 ELSE 0 END) neutral,
                           SUM(profit_net) profit_net,SUM(profit_market) profit_market,
                           COALESCE(SUM(gross_reward_net),0) gain_net,
                           COALESCE(SUM(gross_reward_market),0) gain_market,
                           COALESCE(SUM(ticket_cost_net + backpack_loss_net),0) loss_net,
                           COALESCE(SUM(ticket_cost_market + backpack_loss_market),0) loss_market,
                           COALESCE(SUM(coins_gained),0) coins_gained,
                           COALESCE(SUM(fish_tokens_gained),0) fish_tokens,
                           COALESCE(SUM(skin_fragments_gained),0) skin_fragments,
                           COALESCE(SUM(ticket_quantity),0) tickets,
                           COALESCE(SUM(ticket_cost_net),0) ticket_net,
                           COALESCE(SUM(ticket_cost_market),0) ticket_market,
                           COALESCE(SUM(backpack_loss_net),0) backpack_net,
                           COALESCE(SUM(backpack_loss_market),0) backpack_market,
                           AVG(interactions) avg_interactions,
                           MAX(max_interactions) max_interactions,
                           AVG(completion_percent) avg_completion
                    FROM adventure_runs
                    WHERE user_id=? AND completed_utc_day=? AND finalized=1
                    GROUP BY adventure_key,adventure_name ORDER BY profit_net DESC, played DESC, adventure_name
                    """,
                    (user_id, utc_day),
                ).fetchall()
                compass = conn.execute(
                    """
                    SELECT COUNT(*) used,COALESCE(SUM(cost_net),0) cost_net,COALESCE(SUM(cost_market),0) cost_market,
                           COALESCE(SUM(proc_interactions),0) procs,COALESCE(SUM(doubled_components),0) components,
                           COALESCE(SUM(bonus_net),0) bonus_net,COALESCE(SUM(bonus_market),0) bonus_market
                    FROM compass_sessions WHERE user_id=? AND substr(used_at,1,10)=?
                    """,
                    (user_id, utc_day),
                ).fetchone()
                gained = conn.execute(
                    """
                    SELECT item_key,name,SUM(quantity) quantity,SUM(net_total) net_total,SUM(market_total) market_total
                    FROM ledger_entries WHERE user_id=? AND utc_day=? AND source='run_final'
                      AND kind='item_reward' GROUP BY item_key,name ORDER BY net_total DESC
                    """,
                    (user_id, utc_day),
                ).fetchall()
                lost = conn.execute(
                    """
                    SELECT item_key,name,SUM(quantity) quantity,SUM(-net_total) net_total,SUM(-market_total) market_total
                    FROM ledger_entries WHERE user_id=? AND utc_day=? AND source='run_final'
                      AND kind='backpack_loss' GROUP BY item_key,name ORDER BY net_total DESC
                    """,
                    (user_id, utc_day),
                ).fetchall()

                # Keep the complete ledger in SQLite and expose ranked net-value item
                # contributors for each adventure. The renderer decides whether to show
                # three, four, or five entries based on how many adventures need space.
                item_contributors = conn.execute(
                    """
                    SELECT r.adventure_key,l.item_key,l.name,
                           SUM(l.quantity) quantity,SUM(l.net_total) net_total,SUM(l.market_total) market_total
                    FROM ledger_entries l
                    JOIN adventure_runs r ON r.message_id=l.run_message_id
                    WHERE r.user_id=? AND r.completed_utc_day=? AND l.source='run_final'
                      AND l.kind='item_reward' AND l.net_total>0 AND r.finalized=1
                    GROUP BY r.adventure_key,l.item_key,l.name
                    ORDER BY r.adventure_key,net_total DESC,quantity DESC,name ASC
                    """,
                    (user_id, utc_day),
                ).fetchall()
                top_items_by_adventure: Dict[str, List[Dict[str, Any]]] = {}
                for raw_item_row in item_contributors:
                    item_row = dict(raw_item_row)
                    key = str(item_row.get("adventure_key") or "")
                    if key:
                        top_items_by_adventure.setdefault(key, []).append(item_row)

                # Keep each completed adventure's actual costs available for the detailed
                # page. Ticket rows are written at run start while backpack losses are
                # written at finalization, so filter by the joined run's completion day.
                loss_contributors = conn.execute(
                    """
                    SELECT r.adventure_key,l.kind,l.item_key,l.name,
                           SUM(l.quantity) quantity,SUM(-l.net_total) net_total,SUM(-l.market_total) market_total
                    FROM ledger_entries l
                    JOIN adventure_runs r ON r.message_id=l.run_message_id
                    WHERE r.user_id=? AND r.completed_utc_day=? AND r.finalized=1
                      AND l.kind IN ('ticket_cost','backpack_loss') AND l.net_total<0
                    GROUP BY r.adventure_key,l.kind,l.item_key,l.name
                    ORDER BY r.adventure_key,
                             CASE WHEN l.kind='backpack_loss' THEN 0 ELSE 1 END,
                             net_total DESC,quantity DESC,name ASC
                    """,
                    (user_id, utc_day),
                ).fetchall()
                losses_by_adventure: Dict[str, List[Dict[str, Any]]] = {}
                for raw_loss_row in loss_contributors:
                    loss_row = dict(raw_loss_row)
                    key = str(loss_row.get("adventure_key") or "")
                    if key:
                        losses_by_adventure.setdefault(key, []).append(loss_row)

                # Multipliers are non-monetary rewards stored on each finalized run.
                # Aggregate exact type/strength/duration combinations. Repeated identical
                # awards increase the count instead of being displayed as one longer buff.
                multiplier_rows = conn.execute(
                    """
                    SELECT multipliers_json
                    FROM adventure_runs
                    WHERE user_id=? AND completed_utc_day=? AND finalized=1
                    """,
                    (user_id, utc_day),
                ).fetchall()
                multiplier_map: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
                for row in multiplier_rows:
                    for reward in loads(row["multipliers_json"], []):
                        if not isinstance(reward, dict):
                            continue
                        multiplier_type = str(reward.get("multiplier_type") or "unknown").casefold()
                        strength = str(reward.get("strength") or "?").strip()
                        quantity = max(1, int(reward.get("quantity") or 1))
                        duration_seconds = max(0, int(reward.get("duration_seconds") or 0))
                        map_key = (multiplier_type, strength, duration_seconds)
                        entry = multiplier_map.setdefault(
                            map_key,
                            {
                                "multiplier_type": multiplier_type,
                                "strength": strength,
                                "duration_seconds": duration_seconds,
                                "count": 0,
                            },
                        )
                        entry["count"] += quantity

                multiplier_order = {"luck": 0, "xp": 1, "coin": 2}

                def strength_sort_value(value: Any) -> Tuple[float, str]:
                    text = str(value or "")
                    match = re.search(r"-?\d+(?:\.\d+)?", text)
                    return (float(match.group(0)) if match else float("inf"), text.casefold())

                multipliers = sorted(
                    multiplier_map.values(),
                    key=lambda row: (
                        multiplier_order.get(str(row.get("multiplier_type")), 99),
                        str(row.get("multiplier_type")),
                        strength_sort_value(row.get("strength")),
                        int(row.get("duration_seconds") or 0),
                    ),
                )

                enriched_by_adv = []
                for raw_row in by_adv:
                    row = dict(raw_row)
                    top_items = top_items_by_adventure.get(str(row.get("adventure_key") or ""), [])
                    row["top_items"] = top_items
                    row["loss_items"] = losses_by_adventure.get(str(row.get("adventure_key") or ""), [])
                    # Retain the old singular field for compatibility with any external
                    # consumer that already reads the first contributor.
                    row["top_item"] = top_items[0] if top_items else None
                    enriched_by_adv.append(row)

                return {
                    "summary": dict(summary), "active": int(active), "incomplete": int(incomplete),
                    "by_adventure": enriched_by_adv, "compass": dict(compass),
                    "gained": [dict(r) for r in gained], "lost": [dict(r) for r in lost],
                    "multipliers": multipliers,
                }
        return await self._run(op)

    async def leaderboard(self, utc_day: str, metric: str = "net", limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
        metric_sql = {
            "net": "SUM(profit_net)",
            "market": "SUM(profit_market)",
            "wins": "SUM(CASE WHEN status='won' THEN 1 ELSE 0 END)",
            "completed": "COUNT(*)",
            "winrate": "1.0*SUM(CASE WHEN status='won' THEN 1 ELSE 0 END)/COUNT(*)",
        }.get(metric, "SUM(profit_net)")
        def op():
            with self._connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT user_id,COUNT(*) completed,
                           SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) won,
                           SUM(CASE WHEN lose_all_seen=1 THEN 1 ELSE 0 END) lost,
                           SUM(CASE WHEN lose_all_seen=1 THEN 1 ELSE 0 END) died,
                           SUM(profit_net) profit_net,SUM(profit_market) profit_market,
                           1.0*SUM(CASE WHEN status='won' THEN 1 ELSE 0 END)/COUNT(*) win_rate,
                           {metric_sql} metric_value
                    FROM adventure_runs
                    WHERE completed_utc_day=? AND finalized=1
                    GROUP BY user_id
                    ORDER BY metric_value DESC, profit_net DESC, user_id ASC
                    LIMIT ? OFFSET ?
                    """,
                    (utc_day, limit, offset),
                ).fetchall()
                return [dict(r) for r in rows]
        return await self._run(op)


    async def mark_stale_runs(self, cutoff_iso: str) -> int:
        def op():
            with self._connect() as conn:
                # Selection/catalogue messages are staging records, not adventure runs.
                # Remove abandoned staging rows and any accidental ticket ledger rows.
                abandoned = [
                    int(row[0])
                    for row in conn.execute(
                        """
                        SELECT r.message_id
                        FROM adventure_runs r
                        WHERE r.finalized=0 AND r.state IN ('pending','active') AND r.last_seen_at<?
                          AND NOT EXISTS (SELECT 1 FROM node_events n WHERE n.message_id=r.message_id)
                        """,
                        (cutoff_iso,),
                    ).fetchall()
                ]
                if abandoned:
                    marks = ",".join("?" for _ in abandoned)
                    conn.execute(f"DELETE FROM ledger_entries WHERE run_message_id IN ({marks})", abandoned)
                    conn.execute(f"DELETE FROM adventure_runs WHERE message_id IN ({marks})", abandoned)

                cur = conn.execute(
                    """
                    UPDATE adventure_runs SET state='stale', status='incomplete', completion_reason='no_summary'
                    WHERE finalized=0 AND state='active' AND last_seen_at<?
                    """,
                    (cutoff_iso,),
                )
                conn.commit()
                return int(cur.rowcount)
        return await self._run(op)

    async def db_counts(self) -> Dict[str, int]:
        def op():
            with self._connect() as conn:
                return {
                    "runs": conn.execute("SELECT COUNT(*) FROM adventure_runs").fetchone()[0],
                    "finalized": conn.execute("SELECT COUNT(*) FROM adventure_runs WHERE finalized=1").fetchone()[0],
                    "nodes": conn.execute("SELECT COUNT(*) FROM node_events").fetchone()[0],
                    "ledger": conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0],
                    "compass_sessions": conn.execute("SELECT COUNT(*) FROM compass_sessions").fetchone()[0],
                    "unknown_rewards": conn.execute("SELECT COUNT(*) FROM unknown_rewards").fetchone()[0],
                    "aliases": conn.execute("SELECT COUNT(*) FROM adventure_message_aliases").fetchone()[0],
                    "luck_observations": conn.execute("SELECT COUNT(*) FROM adventure_luck_observations").fetchone()[0],
                    "luck_sources": conn.execute("SELECT COUNT(*) FROM adventure_luck_sources").fetchone()[0],
                    "luck_known": conn.execute(
                        "SELECT COUNT(*) FROM adventure_luck_observations WHERE active_items_known=1"
                    ).fetchone()[0],
                    "luck_conflicts": conn.execute(
                        "SELECT COUNT(*) FROM adventure_luck_observations WHERE state_conflicts_json!='[]'"
                    ).fetchone()[0],
                }
        return await self._run(op)
