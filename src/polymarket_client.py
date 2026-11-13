"""Pure market parsing only. HTTP client and wallet interfaces are excluded."""
from __future__ import annotations
import json,re
from typing import Any
from src.models import Outcome,OutcomeMetadata


def outcome_name_from_market(market: dict[str, Any]) -> str | None:
    label = str(market.get("groupItemTitle") or market.get("question") or "")
    return outcome_name_from_label(label)


def outcome_name_from_label(label: str) -> str | None:
    metadata = parse_outcome_metadata(label)
    value = metadata.temperature_value if metadata.bucket_type == "exact" else metadata.threshold
    if value is None:
        return None

    # Do not hard-code a minimum temperature. Some daily markets include lower
    # buckets such as 24C_OR_LOWER, 25C, 26C, etc. The current market's real
    # outcome list must be preserved exactly and filtered only by parseability.
    return metadata.normalized_name


def parse_outcome_metadata(raw_name: str) -> OutcomeMetadata:
    raw = str(raw_name).strip()
    label = normalize_outcome_label(raw)

    # Support labels like:
    # 24C_OR_LOWER, 24C or below, 24° or below, 24 or below,
    # 38C_PLUS, 38C+, 38 or above.
    match = re.search(r"(\d+(?:\.\d+)?)(?:C)?", label)
    if not match:
        return OutcomeMetadata(raw, label, "unknown")

    value = float(match.group(1))
    display_value = int(value) if value.is_integer() else value

    lower_markers = (
        "ORLOWER",
        "LOWER",
        "ORBELOW",
        "BELOW",
        "ORLESS",
        "LESS",
        "ORUNDER",
        "UNDER",
    )
    upper_markers = (
        "ORHIGHER",
        "HIGHER",
        "ORABOVE",
        "ABOVE",
        "ORMORE",
        "MORE",
        "PLUS",
        "ATLEAST",
    )

    if any(marker in label for marker in lower_markers):
        return OutcomeMetadata(raw, f"{display_value}C_OR_LOWER", "lower_tail", threshold=value)
    if any(marker in label for marker in upper_markers):
        return OutcomeMetadata(raw, f"{display_value}C_PLUS", "upper_tail", threshold=value)
    return OutcomeMetadata(raw, f"{display_value}C", "exact", temperature_value=value)


def normalize_outcome_label(raw_name: str) -> str:
    return (
        str(raw_name)
        .upper()
        .replace("\u00b0", "")
        .replace("\u00ba", "")
        .replace("°", "")
        .replace("℃", "C")
        .replace("DEGREES", "")
        .replace("DEGREE", "")
        .replace("掳", "")
        .replace("脗", "")
        .replace("_", "")
        .replace("-", "")
        .replace("+", "PLUS")
        .replace(" ", "")
    )


def is_multi_outcome_temperature_market(market: dict[str, Any]) -> bool:
    outcomes = parse_json_list(market.get("outcomes"))
    temperature_outcomes = [outcome for outcome in outcomes if outcome_name_from_label(str(outcome)) is not None]
    yes_no = {str(outcome).strip().upper() for outcome in outcomes} <= {"YES", "NO"}
    return len(temperature_outcomes) >= 2 and not yes_no


def outcome_refs_from_gamma_market(market_slug: str, outcome_label: str, market: dict[str, Any]) -> dict[str, Any]:
    outcomes = parse_json_list(market.get("outcomes"))
    prices = parse_json_list(market.get("outcomePrices"))
    token_ids = parse_json_list(market.get("clobTokenIds"))
    requested_key = outcome_match_key(outcome_label)

    for index, outcome in enumerate(outcomes):
        if outcome_match_key(str(outcome)) != requested_key:
            continue
        gamma_price = _as_float(prices[index]) if index < len(prices) else None
        token_id = token_ids[index] if index < len(token_ids) else None
        return {
            "market_slug": market_slug,
            "outcome": str(outcome),
            "gamma_price": gamma_price,
            "clob_token_id": str(token_id) if token_id else None,
        }

    raise ValueError(
        "Outcome not found in Gamma market: "
        f"market_slug={market_slug}; requested outcome label={outcome_label}; "
        f"available outcomes={', '.join(str(outcome) for outcome in outcomes)}"
    )


def outcome_match_key(label: str) -> str:
    metadata = parse_outcome_metadata(label)
    if metadata.bucket_type != "unknown":
        return metadata.normalized_name
    return normalize_outcome_label(label)


def buy_yes_price(market: dict[str, Any]) -> float:
    prices = parse_json_list(market.get("outcomePrices"))
    yes_index = yes_outcome_index(market)
    if len(prices) > yes_index:
        price = _as_float(prices[yes_index])
        if price is not None:
            return price
    best_ask = _as_float(market.get("bestAsk"))
    if best_ask is not None and best_ask > 0:
        return best_ask
    raise ValueError(f"No Yes price found for market {market.get('slug')}")


def yes_clob_token_id(market: dict[str, Any]) -> str | None:
    token_ids = parse_json_list(market.get("clobTokenIds"))
    yes_index = yes_outcome_index(market)
    if len(token_ids) <= yes_index:
        return None
    token_id = token_ids[yes_index]
    return str(token_id) if token_id else None


def yes_outcome_index(market: dict[str, Any]) -> int:
    outcomes = parse_json_list(market.get("outcomes"))
    for index, outcome in enumerate(outcomes):
        if str(outcome).strip().upper() == "YES":
            return index
    return 0


def orderbook_levels(book: dict[str, Any], side: str) -> list[tuple[float, float]]:
    raw_levels = book.get("bids" if side.lower() == "bid" else "asks") or []
    levels: list[tuple[float, float]] = []
    for level in raw_levels:
        if not isinstance(level, dict):
            continue
        price = _as_float(level.get("price"))
        size = _as_float(level.get("size") or level.get("quantity") or level.get("shares"))
        if price is None or size is None or price <= 0 or size <= 0:
            continue
        levels.append((price, size))
    reverse = side.lower() == "bid"
    return sorted(levels, key=lambda item: item[0], reverse=reverse)


def outcome_template(name: str) -> tuple[float, str, float]:
    metadata = parse_outcome_metadata(name)
    temp = metadata.temperature_value if metadata.bucket_type == "exact" else metadata.threshold
    return 0.0, "main", float(temp or 0.0)


def outcome_sort_key(name: str) -> float:
    metadata = parse_outcome_metadata(name)
    if metadata.bucket_type == "lower_tail":
        return float(metadata.threshold or 0.0) - 0.5
    if metadata.bucket_type == "upper_tail":
        return float(metadata.threshold or 0.0) + 0.5
    if metadata.bucket_type == "exact":
        return float(metadata.temperature_value or 0.0)
    return 999.0


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return []


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
