"""Read-only Halloween Adventure path analyzer for Nebula.

Reads AdventureTracker's SQLite database without modifying it.
Produces a Markdown report based only on stored evidence.

Usage:
    python halloween_analyzer.py "C:\\path\\to\\adventuretracker.db"
    python halloween_analyzer.py "C:\\path\\to\\adventuretracker.db" --out halloween_report.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

HALLOWEEN_KEY = "halloween"
HALLOWEEN_NAME = "pepe goes trick or treating"

LEADING_PATHS = {
    "adventure-leading:0:0": "Trick or Treat Path",
    "adventure-leading:0:1": "Monsters Path",
    "adventure-leading:2:0": "Monsters Path",
    "adventure-leading:2:1": "Trick or Treat Path",
    "adventure-leading:3:0": "Monsters Path",
    "adventure-leading:3:1": "Trick or Treat Path",
    "adventure-leading:1:0": "Trick or Treat Path",
    "adventure-leading:1:1": "Monsters Path",
    "adventure-leading:41:0": "Monsters Path",
    "adventure-leading:41:1": "Trick or Treat Path",
    "adventure-leading:40:0": "Trick or Treat Path",
    "adventure-leading:40:1": "Monsters Path",
}

COLOR_NAMES = {
    15753235: "red",
    13568200: "purple",
    2171169: "dark grey",
    2829617: "summary grey",
}


def parse_json(value: Any, fallback: Any = None) -> Any:
    if not value:
        return {} if fallback is None else fallback

    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {} if fallback is None else fallback


def get_embed_data(raw: Any) -> tuple[str, int | None, str]:
    payload = parse_json(raw)

    if not isinstance(payload, dict):
        return "", None, ""

    embeds = payload.get("embeds") or []
    if not embeds or not isinstance(embeds[0], dict):
        return "", None, ""

    embed = embeds[0]
    description = str(embed.get("description") or "").strip()

    try:
        color = int(embed["color"]) if embed.get("color") is not None else None
    except (TypeError, ValueError):
        color = None

    footer = str((embed.get("footer") or {}).get("text") or "")
    return description, color, footer


def is_intermission(event: dict[str, Any]) -> bool:
    """Identify narrative-only adventure messages with no choice buttons."""
    payload = parse_json(event.get("raw_json"))

    if not isinstance(payload, dict):
        return False

    # A null node ID is represented by the absence of a captured choice ID.
    if event.get("selected_custom_id"):
        return False

    components = payload.get("components") or []
    button_ids: list[str] = []

    def collect_buttons(items: Any) -> None:
        if not isinstance(items, list):
            return

        for item in items:
            if not isinstance(item, dict):
                continue

            if item.get("type") == 2:
                custom_id = str(item.get("custom_id") or "")
                if custom_id:
                    button_ids.append(custom_id)

            collect_buttons(item.get("components"))

    collect_buttons(components)

    # An intermission may have navigation buttons, but no decision buttons.
    if any(
        custom_id.startswith(("adventure-leading:", "adventure-option:"))
        for custom_id in button_ids
    ):
        return False

    description, _, _ = get_embed_data(event.get("raw_json"))
    description = description.casefold()

    return (
        "nothing interesting happened" in description
        and any(
            custom_id.startswith("adventure-next:")
            for custom_id in button_ids
        )
    )


def choice_node(custom_id: str) -> str:
    """Normalize a choice ID by removing the player ID."""
    parts = (custom_id or "").split(":")

    if (
        len(parts) >= 4
        and parts[0] in {"adventure-leading", "adventure-option"}
    ):
        return f"{parts[0]}:{parts[2]}:{parts[3]}"

    return custom_id or "(choice not captured)"


def choice_label(event: dict[str, Any]) -> str:
    label = str(event.get("selected_label") or "").strip()
    if label:
        return label

    custom_id = str(event.get("selected_custom_id") or "")
    parts = custom_id.split(":")

    if (
        len(parts) >= 4
        and parts[0] in {"adventure-leading", "adventure-option"}
    ):
        return f"Node {parts[2]}, option {parts[3]}"

    return "(unknown choice)"


def is_halloween(run: dict[str, Any]) -> bool:
    key = str(run.get("adventure_key") or "").casefold()
    name = str(run.get("adventure_name") or "").casefold()

    return key == HALLOWEEN_KEY or HALLOWEEN_NAME in name


def load_data(
    database: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not database.is_file():
        raise FileNotFoundError(f"SQLite database not found: {database}")

    # Read-only connection. This analyzer cannot write to the database.
    uri = database.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row

    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        required = {"adventure_runs", "node_events"}
        missing = required - tables

        if missing:
            raise RuntimeError(
                "This does not look like an AdventureTracker database. "
                f"Missing tables: {', '.join(sorted(missing))}"
            )

        runs = [
            dict(row)
            for row in connection.execute("SELECT * FROM adventure_runs")
        ]

        events = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM node_events ORDER BY occurred_at ASC, id ASC"
            )
        ]

        return runs, events

    finally:
        connection.close()


def reduce_snapshots(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep the latest stored snapshot per selected node choice.

    This is a practical first-pass deduplication rule, not proof that two
    events represent the same underlying interaction. If a route can revisit
    the same node and option, this rule could merge separate interactions.
    """
    latest: dict[tuple[Any, ...], dict[str, Any]] = {}

    for event in events:
        custom_id = str(event.get("selected_custom_id") or "")

        if custom_id:
            key = (
                event.get("message_id"),
                choice_node(custom_id),
            )
        else:
            key = (
                event.get("message_id"),
                "fallback",
                str(event.get("prompt_text") or ""),
                str(event.get("result_text") or ""),
            )

        previous = latest.get(key)

        current_order = (
            str(event.get("occurred_at") or ""),
            int(event.get("id") or 0),
        )

        previous_order = (
            str(previous.get("occurred_at") or ""),
            int(previous.get("id") or 0),
        ) if previous else None

        if previous is None or current_order >= previous_order:
            latest[key] = event

    return sorted(
        latest.values(),
        key=lambda event: (
            str(event.get("occurred_at") or ""),
            int(event.get("id") or 0),
        ),
    )


def shorten(text: str, limit: int = 220) -> str:
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def build_report(database: Path) -> str:
    runs, all_events = load_data(database)

    halloween_runs = {
        str(run.get("message_id")): run
        for run in runs
        if is_halloween(run)
    }

    raw_events = [
        event
        for event in all_events
        if str(event.get("message_id")) in halloween_runs
    ]

    events = reduce_snapshots(raw_events)

    events_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for event in events:
        events_by_run[str(event.get("message_id"))].append(event)

    leading_counts: Counter[str] = Counter()
    color_counts: Counter[str] = Counter()
    outcome_counts: Counter[tuple[str, str]] = Counter()
    route_counts: Counter[str] = Counter()

    evidence_sections: list[str] = []

    for run_id, run_events in events_by_run.items():
        leading_path = None
        route_steps: list[str] = []

        for event in run_events:
            custom_id = str(event.get("selected_custom_id") or "")
            node = choice_node(custom_id)

            intermission = is_intermission(event)

            if intermission:
                description, _, _ = get_embed_data(event.get("raw_json"))
                label = f"Intermission: {shorten(description, 100)}"
            else:
                label = choice_label(event)

            if custom_id.startswith("adventure-leading:"):
                mapped_path = LEADING_PATHS.get(node)

                if mapped_path:
                    leading_counts[mapped_path] += 1
                    leading_path = leading_path or mapped_path
                else:
                    leading_path = leading_path or "Unmapped leading choice"

            raw_description, color, footer = get_embed_data(
                event.get("raw_json")
            )

            result = str(event.get("result_text") or raw_description).strip()
            ending = str(event.get("ending_type") or "").strip()
            ending = ending or "unclassified"

            if color is None:
                color_label = "not captured"
            else:
                color_label = (
                    f"{color} ({COLOR_NAMES.get(color, 'unknown color')})"
                )

            color_counts[color_label] += 1
            outcome_counts[(label, ending)] += 1

            route_steps.append(f"{label} [{ending}]")

            prompt = str(event.get("prompt_text") or "").strip()
            evidence_sections.extend(
                [
                    f"### Run `{run_id}`, node `{node}`",
                    f"- Timestamp: {event.get('occurred_at') or 'unknown'}",
                    f"- Prompt recorded: {prompt or '(not stored)'}",
                    f"- {'Event type' if intermission else 'Selected choice'}: **{label}**",
                    f"- Result classification: `{ending}`",
                    f"- Result color: `{color_label}`",
                    f"- Footer progress markers: `{footer or 'not captured'}`",
                    f"- Result text: {shorten(result) or '(not captured)'}",
                    "",
                ]
            )

        path_name = leading_path or "Leading choice not captured"
        route_counts[path_name + " | " + " -> ".join(route_steps)] += 1

    lines = [
        "# Halloween Adventure Path Report",
        "",
        f"- Database: `{database}`",
        f"- Matching adventure records: **{len(halloween_runs)}**",
        f"- Raw matching node events: **{len(raw_events)}**",
        f"- Events after snapshot reduction: **{len(events)}**",
        f"- Message IDs containing events: **{len(events_by_run)}**",
        "",
        "## Important limitations",
        "",
        "- The database is opened read-only. No existing records are changed.",
        "- Historic pre-choice prompt payloads are not preserved per node by the current schema. The recorded result color must not be treated as the prompt color.",
        "- Repeated snapshots are reduced using message ID and selected node choice. This can merge repeated visits to the same choice if the adventure permits revisiting nodes.",
        "- An observed result does not prove that a choice always produces that result.",
        "- Message IDs are treated as separate run groups here. If AdventureTracker has reused or aliased messages in a way not represented in its run table, that may affect grouping.",
        "",
        "## Leading path counts",
        "",
    ]

    if leading_counts:
        lines.extend(
            f"- **{name}:** {count}"
            for name, count in leading_counts.most_common()
        )
    else:
        lines.append("- No mapped leading choices were found.")

    lines.extend(["", "## Result color counts", ""])

    if color_counts:
        lines.extend(
            f"- `{color}`: {count}"
            for color, count in color_counts.most_common()
        )
    else:
        lines.append("- No result colors were available.")

    lines.extend(["", "## Observed choice outcomes", ""])

    if outcome_counts:
        for (label, outcome), count in sorted(
            outcome_counts.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        ):
            lines.append(f"- **{label}**, `{outcome}`: {count}")
    else:
        lines.append("- No selected choices were captured.")

    lines.extend(["", "## Observed route sequences", ""])

    if route_counts:
        for route, count in route_counts.most_common():
            lines.append(f"- **{count} occurrence(s):** {route}")
    else:
        lines.append("- No route sequences could be reconstructed.")

    lines.extend(["", "## Event evidence", ""])
    lines.extend(evidence_sections or ["No matching events were found."])

    lines.extend(
        [
            "",
            "## What is needed to reconstruct branching paths reliably",
            "",
            "The companion capture step should preserve the full prompt payload before a choice is made, then associate it with the selected button, the result payload, and the next prompt. This provides the sequence prompt -> choice -> result -> next prompt, without inferring missing branches from colors or isolated outcomes.",
            "",
        ]
    )

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "database",
        type=Path,
        help="Path to AdventureTracker's SQLite database",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Optional path for the Markdown report",
    )
    args = parser.parse_args()

    try:
        report = build_report(args.database)
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as error:
        parser.error(str(error))

    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"Report saved to: {args.out}")
    else:
        print(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
