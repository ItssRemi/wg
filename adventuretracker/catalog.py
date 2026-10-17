from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

EMOJI_RE = re.compile(r"<a?:[^:>]+:(\d+)>")
ID_FROM_URL_RE = re.compile(r"/emojis/(\d+)")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_name(value: str) -> str:
    value = value.casefold().replace("’", "'")
    return NON_ALNUM_RE.sub("", value)


def extract_emoji_ids(value: str) -> List[int]:
    return [int(x) for x in EMOJI_RE.findall(value or "")]


@dataclass(frozen=True)
class ItemInfo:
    key: str
    item_id: int
    name: str
    net_value: int
    market_value: int
    emoji: str
    emoji_ids: Tuple[int, ...] = field(default_factory=tuple)


class ItemCatalog:
    """Resolves Dank item ids, names and both Dank/Nebula emoji ids."""

    def __init__(self, items_path: Path, emoji_csv_path: Optional[Path] = None):
        self.items_path = Path(items_path)
        self.emoji_csv_path = Path(emoji_csv_path) if emoji_csv_path else None
        self.by_key: Dict[str, ItemInfo] = {}
        self.by_item_id: Dict[int, ItemInfo] = {}
        self.by_name: Dict[str, ItemInfo] = {}
        self.by_emoji_id: Dict[int, ItemInfo] = {}
        self.global_emoji_by_item_key: Dict[str, str] = {}
        self.global_emoji_by_name: Dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        raw: Dict[str, Dict[str, Any]] = json.loads(self.items_path.read_text(encoding="utf-8"))
        for key, data in raw.items():
            emoji_ids = set(extract_emoji_ids(str(data.get("emoji") or "")))
            image_url = str(data.get("imageURL") or "")
            match = ID_FROM_URL_RE.search(image_url)
            if match:
                emoji_ids.add(int(match.group(1)))
            for skin in (data.get("skins") or {}).values():
                match = ID_FROM_URL_RE.search(str((skin or {}).get("imageURL") or ""))
                if match:
                    emoji_ids.add(int(match.group(1)))

            info = ItemInfo(
                key=str(data.get("itemKey") or key),
                item_id=int(data.get("id") or 0),
                name=str(data.get("name") or key),
                net_value=int(data.get("netValue") or data.get("value") or 0),
                market_value=int(data.get("marketValue") or 0),
                emoji=str(data.get("emoji") or ""),
                emoji_ids=tuple(sorted(emoji_ids)),
            )
            self.by_key[info.key] = info
            if info.item_id:
                self.by_item_id[info.item_id] = info
            self.by_name[normalize_name(info.name)] = info
            self.by_name[normalize_name(info.key)] = info
            for emoji_id in info.emoji_ids:
                self.by_emoji_id[emoji_id] = info

        if self.emoji_csv_path and self.emoji_csv_path.exists():
            self._load_global_emoji_map(self.emoji_csv_path)

    def _load_global_emoji_map(self, path: Path) -> None:
        with path.open("r", encoding="utf-8-sig", newline="") as fp:
            for row in csv.reader(fp):
                if len(row) < 2:
                    continue
                emoji, display_name = row[0].strip(), row[1].strip()
                if not emoji or not display_name:
                    continue
                norm = normalize_name(display_name)
                self.global_emoji_by_name[norm] = emoji
                info = self.by_name.get(norm)
                if info:
                    self.global_emoji_by_item_key[info.key] = emoji
                    for emoji_id in extract_emoji_ids(emoji):
                        self.by_emoji_id[emoji_id] = info

    def resolve_item_id(self, item_id: int) -> Optional[ItemInfo]:
        return self.by_item_id.get(int(item_id))

    def resolve_key(self, key: str) -> Optional[ItemInfo]:
        return self.by_key.get(key)

    def resolve_name(self, name: str) -> Optional[ItemInfo]:
        return self.by_name.get(normalize_name(name))

    def resolve_emoji_id(self, emoji_id: int) -> Optional[ItemInfo]:
        return self.by_emoji_id.get(int(emoji_id))

    def resolve(self, *, item_id: Optional[int] = None, name: str = "", emoji_id: Optional[int] = None) -> Optional[ItemInfo]:
        if item_id is not None:
            hit = self.resolve_item_id(item_id)
            if hit:
                return hit
        if emoji_id is not None:
            hit = self.resolve_emoji_id(emoji_id)
            if hit:
                return hit
        if name:
            return self.resolve_name(name)
        return None

    def global_emoji(self, item: ItemInfo | str) -> str:
        if isinstance(item, ItemInfo):
            return self.global_emoji_by_item_key.get(item.key, item.emoji)
        hit = self.resolve_name(item)
        if hit:
            return self.global_emoji_by_item_key.get(hit.key, hit.emoji)
        return self.global_emoji_by_name.get(normalize_name(item), "")

    def selected_items_from_ids(self, ids: Iterable[int]) -> List[Dict[str, Any]]:
        output: List[Dict[str, Any]] = []
        for raw_id in ids:
            info = self.resolve_item_id(int(raw_id))
            if info:
                output.append(self.snapshot(info, quantity=1))
            else:
                output.append({
                    "item_id": int(raw_id),
                    "item_key": None,
                    "name": f"Unknown item {raw_id}",
                    "quantity": 1,
                    "net_unit": 0,
                    "market_unit": 0,
                    "net_total": 0,
                    "market_total": 0,
                    "known": False,
                })
        return output

    @staticmethod
    def snapshot(info: ItemInfo, quantity: int = 1) -> Dict[str, Any]:
        quantity = int(quantity)
        return {
            "item_id": info.item_id,
            "item_key": info.key,
            "name": info.name,
            "quantity": quantity,
            "net_unit": info.net_value,
            "market_unit": info.market_value,
            "net_total": info.net_value * quantity,
            "market_total": info.market_value * quantity,
            "known": True,
        }


class OutcomeCatalog:
    """Compiles Assistant JSON node outcomes for Compass base attribution."""

    NODE_FILES = {
        "space": "space.json",
        "west": "west.json",
        "halloween": "halloween.json",
        "holiday": "holiday.json",
        "museum": "museum.json",
        "brazil": "brazil.json",
        "vacation": "vacation.json",
        "fishing": "fishing.json",
        "down-under": "down-under.json",
    }

    def __init__(self, nodes_dir: Path):
        self.nodes_dir = Path(nodes_dir)
        self.by_custom_id: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.by_fallback: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self._load()

    @staticmethod
    def normalize_custom_id(custom_id: str) -> str:
        parts = (custom_id or "").split(":")
        if len(parts) >= 4 and parts[0] in {"adventure-option", "adventure-leading"}:
            return f"{parts[0]}:{parts[-2]}:{parts[-1]}"
        return custom_id or ""

    def _load(self) -> None:
        for adventure_key, filename in self.NODE_FILES.items():
            path = self.nodes_dir / filename
            if not path.exists():
                continue
            records = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                compiled = dict(record)
                cid = self.normalize_custom_id(str(record.get("custom_id") or ""))
                if cid:
                    self.by_custom_id[(adventure_key, cid)] = compiled
                desc = normalize_name(str(record.get("description") or ""))
                label = normalize_name(str(record.get("button_label") or ""))
                if desc or label:
                    self.by_fallback[(adventure_key, desc, label)] = compiled

        # Live-verified correction supplied by the user on 2026-07-13.
        self._override_outcome("space", "adventure-option:16:0", "1 Meteorite")
        self._override_outcome("space", "adventure-option:16:1", "1 Black Hole\n1 Meteorite")

    def _override_outcome(self, adventure_key: str, custom_id: str, outcome: str) -> None:
        key = (adventure_key, custom_id)
        record = dict(self.by_custom_id.get(key, {}))
        record.setdefault("custom_id", custom_id)
        record["outcome"] = outcome
        self.by_custom_id[key] = record

    def find(self, adventure_key: str, custom_id: str = "", description: str = "", label: str = "") -> Optional[Dict[str, Any]]:
        cid = self.normalize_custom_id(custom_id)
        if cid:
            hit = self.by_custom_id.get((adventure_key, cid))
            if hit:
                return hit
        return self.by_fallback.get((adventure_key, normalize_name(description), normalize_name(label)))
