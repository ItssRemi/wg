from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .catalog import ItemCatalog, OutcomeCatalog, extract_emoji_ids, normalize_name

DANK_MEMER_ID = 270904126974590976
COMPASS_EMOJI_IDS = {1092733218387656754, 1182734600800321577, 1251923034865139914, 1454131035305414666}

ADVENTURES: Dict[str, Dict[str, Any]] = {
    "space": {"name": "Pepe goes to Space!", "tickets": 1, "max": 13},
    "west": {"name": "Pepe goes out West", "tickets": 1, "max": 13},
    "halloween": {"name": "Pepe goes Trick or Treating", "tickets": 4, "max": 6},
    "holiday": {"name": "Pepe's Winter Wonderland!", "tickets": 2, "max": 12},
    "museum": {"name": "Pepe goes to the Museum!", "tickets": 3, "max": 9},
    "brazil": {"name": "Pepe goes to Brazil!", "tickets": 3, "max": 7},
    "vacation": {"name": "Pepe goes on Vacation!", "tickets": 1, "max": 8},
    "fishing": {"name": "Pepe goes fishing with friends", "tickets": 1, "max": 9},
    "down-under": {"name": "Pepe goes down under", "tickets": 1, "max": 14},
}
NAME_TO_KEY = {normalize_name(v["name"]): k for k, v in ADVENTURES.items()}

FOOTER_KEY_HINTS = {
    "🚀": "space",
    "🇧🇷": "brazil",
    "🐟": "fishing",
    "🤠": "west",
    "🎃": "halloween",
    "🎄": "holiday",
    "🏛": "museum",
    "🏖": "vacation",
    "🇦🇺": "down-under",
    "🐨": "down-under",
}

FISHING_TOOL_NAMES = {
    normalize_name(x)
    for x in [
        "Dynamite", "Golden Bait", "Harpoon", "Lucky Bait", "Magnet Bait", "Money Bait",
        "Net", "Timely Bait", "Vintage Bait", "Weighted Bait", "XP Bait",
    ]
}

EMOJI_TOKEN_RE = re.compile(r"<a?:[^:>]+:(\d+)>")
CUSTOM_ID_RE = re.compile(r"^(adventure-(?:start|backpack|refresh|option|leading|next|progress)):(.*)$")
COIN_RE = re.compile(r"⏣\s*([\d,]+)")
QUANTITY_EMOJI_NAME_RE = re.compile(r"^(\d+)\s+<a?:[^:>]+:(\d+)>\s+(.+?)\s*$")
EMOJI_NAME_RE = re.compile(r"^<a?:[^:>]+:(\d+)>\s+(.+?)\s*$")
QUANTITY_NAME_RE = re.compile(r"^(\d+)\s+(.+?)\s*$")
ITEM_LOSS_RE = re.compile(r"You\s+lost\s+your\s+(?:<a?:[^:>]+:(\d+)>\s*)?([^\n]+)", re.IGNORECASE)
LUCK_CONTRIBUTION_RE = re.compile(
    r"(?im)^\s*[-•]\s*(?:\*\*)?\s*(?:<a?:[^:>]+:\d+>\s*)?"
    r"(?P<name>.+?)\s*(?:\*\*)?\s+gave\s+you\s+"
    r"(?P<percent>\d+(?:\.\d+)?)%\s+extra\s+luck\s+during\s+this\s+interaction\.\s*$"
)
MULTIPLIER_A_RE = re.compile(
    r"(?P<value>[+-]?\d+(?:\.\d+)?%|\d+(?:\.\d+)?x)\s+(?P<kind>Luck|XP|Coin)(?:\s+Multiplier)?\s+for\s+(?P<num>\d+)\s*(?P<unit>[mhd])",
    re.IGNORECASE,
)
MULTIPLIER_B_RE = re.compile(
    r"(?P<kind>Luck|XP|Coin)\s+(?P<value>[+-]?\d+(?:\.\d+)?%|\d+(?:\.\d+)?x)(?:\s+Multiplier)?\s+for\s+(?P<num>\d+)\s*(?P<unit>[mhd])",
    re.IGNORECASE,
)
PROBABILITY_SUFFIX_RE = re.compile(r"\s+`\d+(?:\.\d+)?`\s*$")
BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-•]|-#)\s*")
MARKDOWN_RE = re.compile(r"[*_`~]+")
COMPLETED_EVENT_REPLAY_RE = re.compile(
    r"(?:^|\n)\s*(?:[-•]\s*)?You have already completed this event,\s*"
    r"click\s*<a?:[^:>]+:\d+>\s*to see your rewards\s*$",
    re.IGNORECASE,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    value = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def json_hash(data: Any) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def strip_markdown(text: str) -> str:
    return MARKDOWN_RE.sub("", text or "")


def split_completed_event_replay(text: str) -> Tuple[bool, str]:
    """Return whether Dank replayed an already-resolved node and its base prompt.

    When /adventure is run after a node choice was resolved but before Next was
    clicked, Dank renders the old prompt in a new message and replaces the
    already-awarded result with an informational notice. The notice is not a
    new node result and must not overwrite the canonical run's prompt.
    """

    raw = str(text or "").strip()
    match = COMPLETED_EVENT_REPLAY_RE.search(raw)
    if match is None:
        return False, raw
    return True, raw[: match.start()].strip()


def walk_components(components: Sequence[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for component in components or []:
        if not isinstance(component, dict):
            continue
        yield component
        children = component.get("components") or []
        yield from walk_components(children)


def collect_custom_ids(components: Sequence[Dict[str, Any]]) -> List[str]:
    return [str(c.get("custom_id")) for c in walk_components(components) if c.get("custom_id")]


def selected_component(components: Sequence[Dict[str, Any]]) -> Tuple[str, str]:
    for component in walk_components(components):
        if component.get("type") != 2:
            continue
        if int(component.get("style") or 0) == 3 and bool(component.get("disabled")):
            return str(component.get("custom_id") or ""), str(component.get("label") or "")
    return "", ""


def has_next_component(components: Sequence[Dict[str, Any]]) -> bool:
    return any(str(cid).startswith("adventure-next:") for cid in collect_custom_ids(components))


def has_enabled_choice(components: Sequence[Dict[str, Any]]) -> bool:
    for component in walk_components(components):
        cid = str(component.get("custom_id") or "")
        if cid.startswith(("adventure-option:", "adventure-leading:")) and not component.get("disabled"):
            return True
    return False


def embed_author_name(embed: Dict[str, Any]) -> str:
    return str((embed.get("author") or {}).get("name") or "")


def embed_footer_text(embed: Dict[str, Any]) -> str:
    return str((embed.get("footer") or {}).get("text") or "")


def fields_to_map(embed: Dict[str, Any]) -> Dict[str, str]:
    return {str(f.get("name") or ""): str(f.get("value") or "") for f in (embed.get("fields") or []) if isinstance(f, dict)}


def extract_user_id(payload: Dict[str, Any], custom_ids: Optional[Sequence[str]] = None) -> Optional[int]:
    for cid in custom_ids or collect_custom_ids(payload.get("components") or []):
        parts = cid.split(":")
        if len(parts) >= 2 and parts[0].startswith("adventure-"):
            try:
                return int(parts[1])
            except (TypeError, ValueError):
                pass
        if len(parts) >= 3 and parts[0] == "use-again":
            try:
                return int(parts[1])
            except (TypeError, ValueError):
                pass
    for key in ("interaction_metadata", "interaction"):
        meta = payload.get(key) or {}
        user = meta.get("user") or {}
        if user.get("id"):
            try:
                return int(user["id"])
            except (TypeError, ValueError):
                pass
    return None


def infer_adventure_key(payload: Dict[str, Any], existing_key: str = "") -> str:
    if existing_key:
        return existing_key
    for cid in collect_custom_ids(payload.get("components") or []):
        parts = cid.split(":")
        if len(parts) >= 3 and parts[0] in {"adventure-start", "adventure-backpack", "adventure-refresh"}:
            return parts[2]
    for embed in payload.get("embeds") or []:
        if embed_author_name(embed) == "Adventure Summary":
            name = fields_to_map(embed).get("Name", "")
            return NAME_TO_KEY.get(normalize_name(name), "")
        footer = embed_footer_text(embed)
        for marker, key in FOOTER_KEY_HINTS.items():
            if marker in footer:
                return key
    return ""


def parse_start_custom_id(custom_id: str) -> Optional[Dict[str, Any]]:
    parts = (custom_id or "").split(":", 3)
    if len(parts) < 4 or parts[0] != "adventure-start":
        return None
    try:
        user_id = int(parts[1])
    except ValueError:
        return None
    item_ids = []
    if parts[3].strip():
        for token in parts[3].split(","):
            try:
                item_ids.append(int(token))
            except ValueError:
                continue
    return {"user_id": user_id, "adventure_key": parts[2], "item_ids": item_ids}


def classify_ending(text: str) -> Tuple[str, bool, bool]:
    normalized = " ".join(strip_markdown(text).split()).casefold()
    lose_all = (
        "lost all items in your backpack" in normalized
        and "all your rewards" in normalized
        and "adventure has ended" in normalized
    )
    ended = "your adventure has ended" in normalized
    named_loss = "you lost your" in normalized
    if lose_all:
        return "lose_all_and_end", True, True
    if named_loss and ended:
        return "item_loss_and_end", True, False
    if ended:
        return "premature_end", True, False
    if named_loss:
        return "item_loss_continue", False, False
    return "", False, False


def parse_duration_seconds(number: int, unit: str) -> int:
    return int(number) * {"m": 60, "h": 3600, "d": 86400}[unit.casefold()]


@dataclass
class RewardParser:
    items: ItemCatalog

    def parse(self, text: str, adventure_key: str = "", source: str = "live") -> List[Dict[str, Any]]:
        rewards: List[Dict[str, Any]] = []
        compass_marked = any(emoji_id in COMPASS_EMOJI_IDS for emoji_id in extract_emoji_ids(text or ""))
        cleaned_text = text or ""
        for emoji_id in COMPASS_EMOJI_IDS:
            cleaned_text = re.sub(rf"<a?:[^:>]+:{emoji_id}>", "", cleaned_text)

        for raw_line in cleaned_text.splitlines():
            line = BULLET_PREFIX_RE.sub("", raw_line).strip()
            if not line:
                continue
            if "gave you" in line.casefold() and "during this interaction" in line.casefold():
                continue
            if line.casefold() in {"nothing interesting happened", "nothing happened", "-"}:
                continue
            if "you lost your" in line.casefold() or "adventure has ended" in line.casefold():
                continue
            line = PROBABILITY_SUFFIX_RE.sub("", line).strip()

            coin_match = COIN_RE.search(line)
            if coin_match:
                amount = int(coin_match.group(1).replace(",", ""))
                rewards.append({
                    "kind": "coins",
                    "name": "Coins",
                    "amount": amount,
                    "quantity": amount,
                    "tracked": True,
                    "compass_marked": compass_marked,
                    "compass_eligible": True,
                    "net_total": amount,
                    "market_total": amount,
                })
                continue

            mult_match = MULTIPLIER_A_RE.search(line) or MULTIPLIER_B_RE.search(line)
            if mult_match:
                gd = mult_match.groupdict()
                kind = str(gd["kind"]).title()
                value = str(gd["value"])
                duration = parse_duration_seconds(int(gd["num"]), str(gd["unit"]))
                rewards.append({
                    "kind": "multiplier",
                    "name": f"{kind} {value}",
                    "multiplier_type": kind.casefold(),
                    "strength": value,
                    "duration_seconds": duration,
                    "quantity": 1,
                    "tracked": True,
                    "compass_marked": False,
                    "compass_eligible": False,
                    "net_total": 0,
                    "market_total": 0,
                })
                continue

            item_match = QUANTITY_EMOJI_NAME_RE.match(line)
            if item_match:
                quantity = int(item_match.group(1))
                emoji_id = int(item_match.group(2))
                name = strip_markdown(item_match.group(3)).strip()
                rewards.append(self._item_or_currency(adventure_key, name, quantity, emoji_id, compass_marked))
                continue

            emoji_name_match = EMOJI_NAME_RE.match(line)
            if emoji_name_match:
                emoji_id = int(emoji_name_match.group(1))
                name = strip_markdown(emoji_name_match.group(2)).strip()
                rewards.append(self._item_or_currency(adventure_key, name, 1, emoji_id, compass_marked))
                continue

            # Static Assistant data occasionally omits emoji markup.
            plain_match = QUANTITY_NAME_RE.match(strip_markdown(line))
            if plain_match and source == "expected":
                quantity = int(plain_match.group(1))
                name = plain_match.group(2).strip()
                rewards.append(self._item_or_currency(adventure_key, name, quantity, None, compass_marked))
                continue

            lowered = strip_markdown(line).casefold()
            if lowered.endswith(" title") or " skin for " in lowered or lowered.endswith(" pet skin") or lowered.endswith(" style"):
                rewards.append({
                    "kind": "ignored",
                    "name": strip_markdown(line).strip(),
                    "quantity": 1,
                    "tracked": False,
                    "ignore_reason": "non_economic_adventure_reward",
                    "compass_marked": False,
                    "compass_eligible": False,
                    "net_total": 0,
                    "market_total": 0,
                })
        return rewards

    def _item_or_currency(self, adventure_key: str, name: str, quantity: int, emoji_id: Optional[int], compass_marked: bool) -> Dict[str, Any]:
        norm = normalize_name(name)
        if "fishtoken" in norm:
            return {
                "kind": "fish_tokens", "name": "Fish Tokens", "quantity": quantity,
                "tracked": True, "compass_marked": False, "compass_eligible": False,
                "net_total": 0, "market_total": 0,
            }
        if "skinfragment" in norm:
            return {
                "kind": "skin_fragments", "name": "Skin Fragments", "quantity": quantity,
                "tracked": True, "compass_marked": False, "compass_eligible": False,
                "net_total": 0, "market_total": 0,
            }

        info = self.items.resolve(name=name, emoji_id=emoji_id)
        canonical_name = info.name if info else name
        canonical_norm = normalize_name(canonical_name)
        if adventure_key == "fishing" and canonical_norm in FISHING_TOOL_NAMES:
            return {
                "kind": "ignored", "name": canonical_name, "quantity": quantity,
                "item_key": info.key if info else None, "item_id": info.item_id if info else None,
                "tracked": False, "ignore_reason": "fishing_tool_reward_in_fishing_adventure",
                "compass_marked": compass_marked, "compass_eligible": True,
                "net_total": 0, "market_total": 0,
            }

        lowered = canonical_name.casefold()
        if (" skin for " in lowered or lowered.endswith(" pet skin") or lowered.endswith(" style")
                or lowered.endswith(" title") or lowered.startswith("pet ") or lowered.endswith(" pet")):
            return {
                "kind": "ignored", "name": canonical_name, "quantity": quantity,
                "tracked": False, "ignore_reason": "non_economic_adventure_reward",
                "compass_marked": False, "compass_eligible": False,
                "net_total": 0, "market_total": 0,
            }

        if info:
            snapshot = self.items.snapshot(info, quantity)
            snapshot.update({
                "kind": "item", "tracked": True,
                "compass_marked": compass_marked, "compass_eligible": True,
            })
            return snapshot

        return {
            "kind": "unknown", "name": canonical_name, "quantity": quantity,
            "item_key": None, "item_id": None, "tracked": False,
            "ignore_reason": None, "compass_marked": compass_marked,
            "compass_eligible": True, "net_total": 0, "market_total": 0,
        }

    def parse_luck_contributions(self, text: str) -> List[Dict[str, Any]]:
        """Parse exact item-name luck contributions from one resolved interaction.

        Dank Memer may render any equipped item skin, so the observed emoji is
        deliberately ignored. The visible item name is resolved through the
        same canonical item catalogue used by all other AdventureTracker data.
        """

        combined: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for match in LUCK_CONTRIBUTION_RE.finditer(text or ""):
            raw_name = strip_markdown(match.group("name")).strip().rstrip(".! ")
            if not raw_name:
                continue
            percent_raw = float(match.group("percent"))
            percent: int | float = int(percent_raw) if percent_raw.is_integer() else percent_raw
            info = self.items.resolve_name(raw_name)
            canonical_name = info.name if info else raw_name
            item_key = info.key if info else None
            identity = str(item_key or normalize_name(canonical_name))
            if not identity:
                continue
            if identity not in combined:
                combined[identity] = {
                    "item_id": info.item_id if info else None,
                    "item_key": item_key,
                    "name": canonical_name,
                    "luck_percent": 0,
                    "known": bool(info),
                }
                order.append(identity)
            total = float(combined[identity]["luck_percent"]) + float(percent)
            combined[identity]["luck_percent"] = int(total) if total.is_integer() else total
        return [combined[key] for key in order]

    def parse_item_losses(self, text: str) -> List[Dict[str, Any]]:
        losses: List[Dict[str, Any]] = []
        for match in ITEM_LOSS_RE.finditer(text or ""):
            emoji_id = int(match.group(1)) if match.group(1) else None
            name = strip_markdown(match.group(2)).strip().rstrip(".! ")
            info = self.items.resolve(name=name, emoji_id=emoji_id)
            if info:
                losses.append(self.items.snapshot(info, 1))
            else:
                losses.append({
                    "item_id": None, "item_key": None, "name": name, "quantity": 1,
                    "net_unit": 0, "market_unit": 0, "net_total": 0, "market_total": 0,
                    "known": False,
                })
        return losses

    def parse_backpack(self, value: str) -> List[Dict[str, Any]]:
        value = (value or "").strip()
        if not value or value.casefold() in {"-", "none", "nothing"}:
            return []
        output: List[Dict[str, Any]] = []
        for emoji_id in extract_emoji_ids(value):
            info = self.items.resolve_emoji_id(emoji_id)
            if info:
                output.append(self.items.snapshot(info, 1))
            else:
                output.append({
                    "item_id": None, "item_key": None, "name": f"Unknown emoji {emoji_id}",
                    "emoji_id": emoji_id, "quantity": 1, "net_unit": 0, "market_unit": 0,
                    "net_total": 0, "market_total": 0, "known": False,
                })
        return output


def multiset_difference(selected: Sequence[Dict[str, Any]], returned: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    selected_counter: Counter[str] = Counter()
    returned_counter: Counter[str] = Counter()
    source: Dict[str, Dict[str, Any]] = {}
    for item in selected:
        key = str(item.get("item_key") or item.get("item_id") or normalize_name(str(item.get("name") or "")))
        selected_counter[key] += int(item.get("quantity") or 1)
        source[key] = item
    for item in returned:
        key = str(item.get("item_key") or item.get("item_id") or normalize_name(str(item.get("name") or "")))
        returned_counter[key] += int(item.get("quantity") or 1)
    output: List[Dict[str, Any]] = []
    for key, quantity in (selected_counter - returned_counter).items():
        base = dict(source[key])
        base["quantity"] = quantity
        base["net_total"] = int(base.get("net_unit") or 0) * quantity
        base["market_total"] = int(base.get("market_unit") or 0) * quantity
        output.append(base)
    return output


def aggregate_rewards(rewards: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    result = {
        "coins": 0,
        "fish_tokens": 0,
        "skin_fragments": 0,
        "gross_net": 0,
        "gross_market": 0,
        "tracked": [],
        "ignored": [],
        "unknown": [],
        "multipliers": [],
    }
    for reward in rewards:
        kind = reward.get("kind")
        if kind == "coins":
            result["coins"] += int(reward.get("amount") or reward.get("quantity") or 0)
        elif kind == "fish_tokens":
            result["fish_tokens"] += int(reward.get("quantity") or 0)
        elif kind == "skin_fragments":
            result["skin_fragments"] += int(reward.get("quantity") or 0)
        elif kind == "multiplier":
            result["multipliers"].append(dict(reward))
        elif kind == "ignored":
            result["ignored"].append(dict(reward))
        elif kind == "unknown":
            result["unknown"].append(dict(reward))
        if reward.get("tracked") and kind in {"item", "coins"}:
            result["tracked"].append(dict(reward))
            result["gross_net"] += int(reward.get("net_total") or 0)
            result["gross_market"] += int(reward.get("market_total") or 0)
    return result


def compass_attribution(actual: Sequence[Dict[str, Any]], expected: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    expected_map: Dict[Tuple[str, str], int] = Counter()
    for reward in expected:
        if not reward.get("compass_eligible"):
            continue
        kind = str(reward.get("kind"))
        name = normalize_name(str(reward.get("name") or "Coins"))
        amount = int(reward.get("amount") or reward.get("quantity") or 0)
        expected_map[(kind, name)] += amount

    components: List[Dict[str, Any]] = []
    for reward in actual:
        if not reward.get("compass_eligible"):
            continue
        kind = str(reward.get("kind"))
        name = normalize_name(str(reward.get("name") or "Coins"))
        actual_amount = int(reward.get("amount") or reward.get("quantity") or 0)
        base_amount = int(expected_map.get((kind, name), 0))
        if base_amount <= 0 or actual_amount <= base_amount:
            continue
        bonus = actual_amount - base_amount
        entry = {
            "kind": kind,
            "name": reward.get("name"),
            "item_key": reward.get("item_key"),
            "base_amount": base_amount,
            "actual_amount": actual_amount,
            "bonus_amount": bonus,
            "bonus_net": 0,
            "bonus_market": 0,
        }
        if kind == "coins":
            entry["bonus_net"] = bonus
            entry["bonus_market"] = bonus
        elif kind == "item":
            entry["bonus_net"] = int(reward.get("net_unit") or 0) * bonus
            entry["bonus_market"] = int(reward.get("market_unit") or 0) * bonus
        components.append(entry)
    return {
        "proc": bool(components),
        "doubled_components": components,
        "bonus_net": sum(int(x["bonus_net"]) for x in components),
        "bonus_market": sum(int(x["bonus_market"]) for x in components),
        "bonus_coins": sum(int(x["bonus_amount"]) for x in components if x["kind"] == "coins"),
    }


def payload_is_dank(payload: Dict[str, Any]) -> bool:
    ids = []
    for key in ("webhook_id", "application_id"):
        if payload.get(key):
            ids.append(payload.get(key))
    author = payload.get("author") or {}
    if author.get("id"):
        ids.append(author.get("id"))
    for value in ids:
        try:
            if int(value) == DANK_MEMER_ID:
                return True
        except (TypeError, ValueError):
            continue
    return False


def is_result_state(payload: Dict[str, Any]) -> bool:
    embeds = payload.get("embeds") or []
    description = "\n".join(str(e.get("description") or "") for e in embeds)
    completed_event_replay, _ = split_completed_event_replay(description)
    if completed_event_replay:
        return False
    cid, _ = selected_component(payload.get("components") or [])
    if cid:
        return True
    lowered = description.casefold()
    if any(token in lowered for token in ("your adventure has ended", "you lost your", "nothing interesting happened")):
        return True
    if has_next_component(payload.get("components") or []) and not has_enabled_choice(payload.get("components") or []):
        return True
    if COIN_RE.search(description) or QUANTITY_EMOJI_NAME_RE.search(description.strip().splitlines()[-1] if description.strip() else ""):
        return True
    return False


def message_timestamp(payload: Dict[str, Any]) -> datetime:
    return parse_iso(str(payload.get("edited_timestamp") or payload.get("timestamp") or ""))
