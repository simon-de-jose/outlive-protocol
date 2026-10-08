"""
Deterministic inventory helper for inventory-aware meal logging.

Provides:
  - Fuzzy matching of ingredient names to inventory keys
  - Portion suggestions based on available stock
  - Confirmation normalization (parse "1 bag of chicken", "half a block", etc.)
  - Safe subtraction with zero-floor

This module is pure Python — no LLM, no API calls, no DuckDB.
It operates on the inventory dict loaded from inventory.json.
"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Optional


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class InventoryItem:
    key: str
    name: str
    quantity: float
    unit: str


@dataclass
class MatchResult:
    """Result of matching an ingredient mention to inventory."""
    matched: bool
    item: Optional[InventoryItem] = None
    score: float = 0.0
    suggestion: str = ""


@dataclass
class ConsumedItem:
    """A confirmed consumption against an inventory item."""
    key: str
    name: str
    amount: float
    unit: str
    remaining: float
    depleted: bool = False


# ── Fuzzy matching ────────────────────────────────────────────────────────────

# Normalized keyword → list of inventory keys that should match.
# This is the canonical matching table — extend here as new items are added.
_ALIAS_MAP: dict[str, list[str]] = {
    "chicken": ["ground_chicken", "chicken_drumsticks", "chicken_leg_thigh", "chicken_bone"],
    "ground chicken": ["ground_chicken"],
    "chicken drumsticks": ["chicken_drumsticks"],
    "chicken drumstick": ["chicken_drumsticks"],
    "chicken leg": ["chicken_leg_thigh"],
    "chicken thigh": ["chicken_leg_thigh"],
    "chicken bone": ["chicken_bone"],
    "tofu": ["tofu_momen"],
    "momen tofu": ["tofu_momen"],
    "tofu momen": ["tofu_momen"],
    "pork": ["pork_loin", "pork_shoulder"],
    "pork loin": ["pork_loin"],
    "pork shoulder": ["pork_shoulder"],
    "salmon": ["salmon_fillet"],
    "scallop": ["scallop"],
    "squid": ["squid_tentacles"],
    "egg": ["eggs"],
    "eggs": ["eggs"],
    "avocado": ["avocados"],
    "avocados": ["avocados"],
    "milk": ["milk"],
    "banana": ["bananas"],
    "bananas": ["bananas"],
    "apple": ["apples"],
    "apples": ["apples"],
    "ginger": ["ginger", "young_ginger"],
    "young ginger": ["young_ginger"],
    "onion": ["brown_onion", "asatsuki_onion", "green_onion", "tokyo_negi"],
    "brown onion": ["brown_onion"],
    "green onion": ["green_onion"],
    "mushroom": ["shiitake_mushroom", "cremini_mushroom", "buna_shimeji"],
    "shiitake": ["shiitake_mushroom"],
    "shiitake mushroom": ["shiitake_mushroom"],
    "shimeji": ["buna_shimeji"],
    "buna shimeji": ["buna_shimeji"],
    "cremini": ["cremini_mushroom"],
    "spinach": ["spinach"],
    "nappa": ["nappa_cabbage"],
    "nappa cabbage": ["nappa_cabbage"],
    "cabbage": ["nappa_cabbage"],
    "celery": ["celery"],
    "tomato": ["tomato"],
    "garlic": ["garlic"],
    "pea sprouts": ["pea_sprouts"],
    "bean sprouts": ["bean_sprouts"],
    "daikon": ["daikon_radish"],
    "daikon radish": ["daikon_radish"],
    "bok choy": ["bok_choy"],
    "choy sum": ["choy_sum"],
    "nira": ["nira"],
    "bell pepper": ["bell_pepper"],
    "noodle": ["chanpon_noodle", "frozen_ramen"],
    "chanpon": ["chanpon_noodle"],
    "ramen": ["frozen_ramen"],
    "abura age": ["abura_age"],
    "miso": ["white_miso"],
    "wasabi": ["wasabi"],
    "takana": ["kizami_takana", "jyukusei_kuro_takana"],
    "kizami takana": ["kizami_takana"],
    "shirataki": ["komusubi_shirataki"],
    "cake": ["napoleon_cake"],
    "napoleon cake": ["napoleon_cake"],
}


def _normalize(text: str) -> str:
    """Lowercase, strip, collapse whitespace."""
    return re.sub(r"\s+", " ", text.strip().lower())


def match_ingredient(
    mention: str,
    inventory: dict[str, dict[str, Any]],
    *,
    prefer_exact: bool = True,
) -> MatchResult:
    """
    Match a user's ingredient mention to an inventory item.

    Strategy:
      1. Check alias map for the normalized mention (exact alias match).
      2. If multiple keys match, pick the one with the highest quantity
         (prefer items that are actually in stock).
      3. Fall back to substring matching against inventory keys and names.

    Returns a MatchResult with matched=True/False.
    """
    norm = _normalize(mention)
    if not norm:
        return MatchResult(matched=False)

    # 1. Exact alias lookup
    if norm in _ALIAS_MAP:
        candidates = [k for k in _ALIAS_MAP[norm] if k in inventory]
        if candidates:
            best = max(candidates, key=lambda k: inventory[k].get("quantity", 0))
            item = inventory[best]
            inv_item = InventoryItem(
                key=best,
                name=item.get("name", best),
                quantity=float(item.get("quantity", 0)),
                unit=item.get("unit", ""),
            )
            return MatchResult(
                matched=True,
                item=inv_item,
                score=1.0,
                suggestion=_build_suggestion(inv_item),
            )

    # 2. Substring matching against alias keys
    partial_matches: list[tuple[str, float]] = []
    for alias, keys in _ALIAS_MAP.items():
        if norm in alias or alias in norm:
            for k in keys:
                if k in inventory:
                    # Score by how close the lengths are
                    score = min(len(norm), len(alias)) / max(len(norm), len(alias))
                    partial_matches.append((k, score))

    # 3. Direct key/name substring match
    for key, item in inventory.items():
        name_lower = item.get("name", "").lower()
        if norm in key or key in norm or norm in name_lower:
            partial_matches.append((key, 0.6))

    if partial_matches:
        # Deduplicate, pick best score, break ties by quantity
        seen: dict[str, float] = {}
        for k, s in partial_matches:
            seen[k] = max(seen.get(k, 0), s)
        best_key = max(seen, key=lambda k: (seen[k], inventory[k].get("quantity", 0)))
        item = inventory[best_key]
        inv_item = InventoryItem(
            key=best_key,
            name=item.get("name", best_key),
            quantity=float(item.get("quantity", 0)),
            unit=item.get("unit", ""),
        )
        return MatchResult(
            matched=True,
            item=inv_item,
            score=seen[best_key],
            suggestion=_build_suggestion(inv_item),
        )

    return MatchResult(matched=False)


def _build_suggestion(item: InventoryItem) -> str:
    """Build a human-readable suggestion for how much was used."""
    qty = item.quantity
    unit = item.unit

    # Small remainder → suggest "used all"
    if _is_small_remainder(qty, unit):
        return f"{item.name} — {qty} {unit} left. Used it all?"

    # Unit-based
    if _is_unit_based(unit):
        if qty >= 2:
            return f"{item.name} — you have {qty:g} {unit}. Used one? Half?"
        elif qty == 1:
            return f"{item.name} — you have 1 {unit.rstrip('s') if unit.endswith('s') else unit}. Used it all? Half?"
        else:
            return f"{item.name} — {qty:g} {unit} left. Used it all?"

    # Weight-based
    if qty >= 1:
        return f"{item.name} — you have {qty:.2f} {unit}. How much did you use?"
    else:
        return f"{item.name} — {qty:.2f} {unit} left. Used it all?"


def _is_unit_based(unit: str) -> bool:
    """Check if the unit is count-based (blocks, packs, pieces, etc.)."""
    unit_based = {"blocks", "packs", "pack", "pieces", "piece", "count", "cartons",
                  "bunches", "bunch", "tubes", "tube", "containers", "container",
                  "bags", "bag", "boxes", "box", "pint"}
    return unit.rstrip("(s)") in unit_based or unit in unit_based or unit.rstrip("s") in unit_based


def _is_small_remainder(qty: float, unit: str) -> bool:
    """Check if the remaining quantity is small enough to suggest 'used all'."""
    if _is_unit_based(unit):
        return qty < 1
    else:
        # Weight-based: less than 0.1 lbs
        return qty < 0.1


# ── Confirmation normalization ────────────────────────────────────────────────

# Patterns for parsing user confirmations
_NUMBER_WORDS: dict[str, float] = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "half": 0.5, "quarter": 0.25, "third": 1/3,
    "all": -1,  # sentinel: consume everything
}

_FRACTION_MAP: dict[str, float] = {
    "1/2": 0.5, "1/3": 1/3, "2/3": 2/3, "1/4": 0.25, "3/4": 0.75,
}


def parse_confirmation(
    text: str,
    item: InventoryItem,
) -> Optional[float]:
    """
    Parse a user confirmation into a quantity to subtract.

    Accepts:
      - "1 bag of chicken", "one block of tofu", "2 packs"
      - "half", "1/2", "half a block"
      - "all of it", "used all", "the rest"
      - "0.5 lbs", "250g"
      - Bare number: "1" → 1 unit
      - "half" → 0.5 * current quantity for weight-based, or 0.5 units for unit-based

    Returns the absolute quantity to subtract, or None if unparseable.
    """
    norm = _normalize(text)

    # "all" / "the rest" / "used all" / "all of it"
    if any(phrase in norm for phrase in ["all", "rest", "everything", "the whole"]):
        return item.quantity

    # "N unit of X" or "N unit" pattern (digit-based)
    match = re.match(
        r"^(\d+\.?\d*)\s*(?:packs?|blocks?|bags?|pieces?|bunches?|bunch|cartons?|"
        r"tubes?|containers?|pints?)\b",
        norm,
    )
    if match:
        return float(match.group(1))

    # Word-number + unit: "one block", "two packs", etc.
    _CARDINAL = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "a": 1, "an": 1}
    for word, val in _CARDINAL.items():
        unit_pattern = rf"^{word}\s+(?:packs?|blocks?|bags?|pieces?|bunches?|bunch|cartons?|tubes?|containers?|pints?)\b"
        if re.match(unit_pattern, norm):
            return float(val)

    # Try to parse fraction
    for frac_str, frac_val in _FRACTION_MAP.items():
        if frac_str in norm:
            return round(frac_val * item.quantity, 4)

    # "half" / "quarter" / "third" (as word, without unit) → fraction of total
    _FRACTION_WORDS = {"half": 0.5, "quarter": 0.25, "third": 1/3}
    for word, val in _FRACTION_WORDS.items():
        if word in norm:
            return round(val * item.quantity, 4)

    # "N lbs" / "N g" / "N kg" / "N oz"
    match = re.match(r"^(\d+\.?\d*)\s*(lbs?|pounds?|oz|ounces?|g|kg|grams?)\b", norm)
    if match:
        amount = float(match.group(1))
        unit_match = match.group(2)
        # Convert to the item's unit
        if item.unit in ("lbs",) and unit_match in ("g", "gram", "grams"):
            return round(amount / 453.6, 4)
        elif item.unit in ("lbs",) and unit_match in ("oz", "ounce", "ounces"):
            return round(amount / 16, 4)
        elif item.unit in ("lbs",) and unit_match in ("kg",):
            return round(amount * 2.2046, 4)
        return amount

    # Bare number: "1" → 1 unit
    match = re.match(r"^(\d+\.?\d*)$", norm)
    if match:
        return float(match.group(1))

    return None


# ── Subtraction ───────────────────────────────────────────────────────────────

def subtract_inventory(
    inventory: dict[str, dict[str, Any]],
    consumptions: list[tuple[str, float]],
) -> list[ConsumedItem]:
    """
    Subtract consumed quantities from inventory. Modifies inventory IN-PLACE.

    Args:
        inventory: The items dict from inventory.json (will be modified).
        consumptions: List of (inventory_key, amount_to_subtract) tuples.

    Returns:
        List of ConsumedItem describing what happened.

    Rules:
        - Floor at zero — never go negative.
        - If quantity hits zero or near-zero (< 0.1 for weight, < 1 for units),
          mark as depleted.
        - Multiple consumptions of the same key accumulate correctly.
    """
    results: list[ConsumedItem] = []

    # Accumulate by key first
    acc: dict[str, float] = {}
    for key, amount in consumptions:
        acc[key] = acc.get(key, 0) + amount

    for key, total_amount in acc.items():
        if key not in inventory:
            continue

        item = inventory[key]
        old_qty = float(item.get("quantity", 0))
        new_qty = max(0.0, round(old_qty - total_amount, 4))
        unit = item.get("unit", "")

        item["quantity"] = new_qty

        depleted = _is_small_remainder(new_qty, unit) or new_qty == 0

        results.append(ConsumedItem(
            key=key,
            name=item.get("name", key),
            amount=total_amount,
            unit=unit,
            remaining=new_qty,
            depleted=depleted,
        ))

    return results


# ── Convenience: full pipeline for a single ingredient ────────────────────────

def match_and_confirm(
    mention: str,
    inventory: dict[str, dict[str, Any]],
    confirmation: str,
) -> tuple[Optional[ConsumedItem], Optional[MatchResult]]:
    """
    One-shot: match ingredient mention, parse confirmation, subtract.

    Returns (ConsumedItem or None, MatchResult or None).
    Does NOT modify inventory — returns the ConsumedItem for the caller to apply.
    """
    mr = match_ingredient(mention, inventory)
    if not mr.matched or mr.item is None:
        return None, mr

    amount = parse_confirmation(confirmation, mr.item)
    if amount is None:
        return None, mr

    unit = mr.item.unit
    new_qty = max(0.0, round(mr.item.quantity - amount, 4))
    depleted = _is_small_remainder(new_qty, unit) or new_qty == 0

    consumed = ConsumedItem(
        key=mr.item.key,
        name=mr.item.name,
        amount=amount,
        unit=unit,
        remaining=new_qty,
        depleted=depleted,
    )
    return consumed, mr
