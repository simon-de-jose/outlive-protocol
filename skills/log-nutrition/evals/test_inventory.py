#!/usr/bin/env python3
"""
Deterministic tests for inventory-aware meal logging.

Exercises the inventory helper module (no LLM, no API, no DB).

Usage:
    cd ~/Projects/outlive-protocol
    python skills/log-nutrition/evals/test_inventory.py
"""

import json
import sys
from copy import deepcopy
from pathlib import Path

# Add scripts dir to path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from inventory import (
    ConsumedItem,
    InventoryItem,
    MatchResult,
    match_and_confirm,
    match_ingredient,
    parse_confirmation,
    subtract_inventory,
)

# ── Color helpers ─────────────────────────────────────────────────────────────
GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"
BOLD = "\033[1m"


def pass_fail(passed: bool) -> str:
    return f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"


# ── Sample inventory for tests ────────────────────────────────────────────────

SAMPLE_INVENTORY: dict = {
    "ground_chicken": {
        "name": "Free-Range Ground Chicken",
        "quantity": 3,
        "unit": "packs",
    },
    "tofu_momen": {
        "name": "Momen Medium Tofu",
        "quantity": 3,
        "unit": "blocks",
    },
    "salmon_fillet": {
        "name": "Fresh Salmon Fillet (Kirimi)",
        "quantity": 12,
        "unit": "pieces",
    },
    "ginger": {
        "name": "Organic Ginger",
        "quantity": 0.18,
        "unit": "lbs",
    },
    "eggs": {
        "name": "Vital Farms Organic Eggs",
        "quantity": 9,
        "unit": "count",
    },
    "avocados": {
        "name": "365 Organic Hass Avocados",
        "quantity": 3.5,
        "unit": "count",
    },
    "nappa_cabbage": {
        "name": "Nappa Cabbage",
        "quantity": 7.26,
        "unit": "lbs",
    },
    "salad": {
        "name": "Earthbound Farm Spring Mix",
        "quantity": 0,
        "unit": "boxes (16oz)",
    },
    "pork_loin": {
        "name": "Dubreton Pork Loin",
        "quantity": 3,
        "unit": "packs",
    },
    "pork_shoulder": {
        "name": "Dubreton Pork Shoulder",
        "quantity": 1,
        "unit": "pack",
    },
    "shiitake_mushroom": {
        "name": "Organic Shiitake Mushroom",
        "quantity": 3,
        "unit": "packs",
    },
}


# ── Test suites ───────────────────────────────────────────────────────────────

def test_fuzzy_matching():
    """Test that ingredient mentions map to the right inventory keys."""
    results = []

    cases = [
        # (mention, expected_key, description)
        ("chicken", "ground_chicken", "bare 'chicken' → ground_chicken (highest qty)"),
        ("ground chicken", "ground_chicken", "'ground chicken' → ground_chicken"),
        ("tofu", "tofu_momen", "'tofu' → tofu_momen"),
        ("momen tofu", "tofu_momen", "'momen tofu' → tofu_momen"),
        ("salmon", "salmon_fillet", "'salmon' → salmon_fillet"),
        ("ginger", "ginger", "'ginger' → ginger"),
        ("pork", "pork_loin", "'pork' → pork_loin (highest qty)"),
        ("pork loin", "pork_loin", "'pork loin' → pork_loin"),
        ("pork shoulder", "pork_shoulder", "'pork shoulder' → pork_shoulder"),
        ("mushroom", "shiitake_mushroom", "'mushroom' → shiitake_mushroom (highest qty)"),
        ("shiitake", "shiitake_mushroom", "'shiitake' → shiitake_mushroom"),
        ("cabbage", "nappa_cabbage", "'cabbage' → nappa_cabbage"),
        ("egg", "eggs", "'egg' → eggs"),
        ("avocado", "avocados", "'avocado' → avocados"),
    ]

    for mention, expected_key, desc in cases:
        inv = deepcopy(SAMPLE_INVENTORY)
        mr = match_ingredient(mention, inv)
        ok = mr.matched and mr.item is not None and mr.item.key == expected_key
        actual = mr.item.key if mr.item else "NONE"
        results.append({
            "name": f"match_{mention.replace(' ', '_')}",
            "passed": ok,
            "evidence": f"{desc} → got {actual}",
        })

    # Test no match
    mr = match_ingredient("unicorn meat", SAMPLE_INVENTORY)
    results.append({
        "name": "match_no_match",
        "passed": not mr.matched,
        "evidence": f"'unicorn meat' matched={mr.matched} (expected False)",
    })

    # Test empty string
    mr = match_ingredient("", SAMPLE_INVENTORY)
    results.append({
        "name": "match_empty_string",
        "passed": not mr.matched,
        "evidence": f"empty string matched={mr.matched} (expected False)",
    })

    return results


def test_unit_based_items():
    """Test matching and suggestions for unit-based items (blocks, packs, etc.)."""
    results = []

    inv = deepcopy(SAMPLE_INVENTORY)

    # Tofu: 3 blocks
    mr = match_ingredient("tofu", inv)
    ok = mr.matched and mr.item is not None and mr.item.quantity == 3 and mr.item.unit == "blocks"
    results.append({
        "name": "unit_tofu_3_blocks",
        "passed": ok,
        "evidence": f"tofu → qty={mr.item.quantity}, unit={mr.item.unit}" if mr.item else "no match",
    })

    # Check suggestion text mentions blocks
    if mr.item:
        has_blocks = "block" in mr.suggestion.lower()
        results.append({
            "name": "unit_tofu_suggestion_mentions_blocks",
            "passed": has_blocks,
            "evidence": f"suggestion: '{mr.suggestion}'",
        })

    # Salmon: 12 pieces
    mr = match_ingredient("salmon", inv)
    ok = mr.matched and mr.item is not None and mr.item.quantity == 12
    results.append({
        "name": "unit_salmon_12_pieces",
        "passed": ok,
        "evidence": f"salmon → qty={mr.item.quantity}" if mr.item else "no match",
    })

    # Ground chicken: 3 packs
    mr = match_ingredient("ground chicken", inv)
    ok = mr.matched and mr.item is not None and mr.item.quantity == 3 and mr.item.unit == "packs"
    results.append({
        "name": "unit_ground_chicken_3_packs",
        "passed": ok,
        "evidence": f"ground chicken → qty={mr.item.quantity}, unit={mr.item.unit}" if mr.item else "no match",
    })

    return results


def test_small_remainder():
    """Test small remainder behavior — auto-suggest 'used all'."""
    results = []

    # Ginger: 0.18 lbs → small remainder
    inv = deepcopy(SAMPLE_INVENTORY)
    mr = match_ingredient("ginger", inv)
    suggests_all = "used it all" in mr.suggestion.lower() or "used all" in mr.suggestion.lower()
    results.append({
        "name": "remainder_ginger_small_suggests_all",
        "passed": suggests_all,
        "evidence": f"ginger (0.18 lbs) suggestion: '{mr.suggestion}'",
    })

    # Salad: 0 boxes → should suggest used all
    mr = match_ingredient("spring mix", inv)
    # salad key is "salad", name has "Spring Mix" — let's test direct match
    mr = match_ingredient("salad", inv)
    if mr.matched and mr.item:
        suggests_all = "used it all" in mr.suggestion.lower() or "left" in mr.suggestion.lower()
        results.append({
            "name": "remainder_salad_zero",
            "passed": mr.item.quantity == 0,
            "evidence": f"salad → qty={mr.item.quantity}",
        })

    return results


def test_subtraction_floor_zero():
    """Test that subtraction never goes below zero."""
    results = []

    inv = deepcopy(SAMPLE_INVENTORY)

    # Tofu: 3 blocks, consume 5 → should floor at 0
    consumed = subtract_inventory(inv, [("tofu_momen", 5)])
    ok = consumed[0].remaining == 0 and consumed[0].depleted
    results.append({
        "name": "sub_floor_zero_overconsume",
        "passed": ok,
        "evidence": f"3 blocks - 5 = {consumed[0].remaining} (expected 0), depleted={consumed[0].depleted}",
    })

    # Verify inventory is at 0
    results.append({
        "name": "sub_inventory_updated_to_zero",
        "passed": inv["tofu_momen"]["quantity"] == 0,
        "evidence": f"inventory['tofu_momen']['quantity'] = {inv['tofu_momen']['quantity']}",
    })

    # Normal subtraction
    inv2 = deepcopy(SAMPLE_INVENTORY)
    consumed = subtract_inventory(inv2, [("ground_chicken", 1)])
    ok = consumed[0].remaining == 2 and not consumed[0].depleted
    results.append({
        "name": "sub_normal_3_minus_1",
        "passed": ok,
        "evidence": f"3 packs - 1 = {consumed[0].remaining}, depleted={consumed[0].depleted}",
    })

    # Weight-based: 0.18 lbs ginger, consume 0.18 → floor at 0
    inv3 = deepcopy(SAMPLE_INVENTORY)
    consumed = subtract_inventory(inv3, [("ginger", 0.18)])
    ok = consumed[0].remaining == 0 and consumed[0].depleted
    results.append({
        "name": "sub_weight_floor_zero",
        "passed": ok,
        "evidence": f"0.18 lbs - 0.18 = {consumed[0].remaining}, depleted={consumed[0].depleted}",
    })

    # Weight-based: 7.26 lbs cabbage, consume 10 → floor at 0
    consumed = subtract_inventory(inv3, [("nappa_cabbage", 10)])
    ok = consumed[0].remaining == 0 and consumed[0].depleted
    results.append({
        "name": "sub_weight_overconsume_floor",
        "passed": ok,
        "evidence": f"7.26 lbs - 10 = {consumed[0].remaining}, depleted={consumed[0].depleted}",
    })

    return results


def test_batch_accumulation():
    """Test that multiple consumptions accumulate correctly."""
    results = []

    inv = deepcopy(SAMPLE_INVENTORY)

    # Two meals both use tofu: meal 1 uses 1 block, meal 2 uses 1 block
    # Simulate batch: both consumptions at once
    consumed = subtract_inventory(inv, [
        ("tofu_momen", 1),
        ("tofu_momen", 1),
    ])

    # Should be 1 result (accumulated), remaining = 1
    tofu_results = [c for c in consumed if c.key == "tofu_momen"]
    ok = len(tofu_results) == 1 and tofu_results[0].remaining == 1 and tofu_results[0].amount == 2
    results.append({
        "name": "batch_tofu_two_meals_accumulate",
        "passed": ok,
        "evidence": f"tofu: amount={tofu_results[0].amount}, remaining={tofu_results[0].remaining}",
    })

    # Mixed: ground chicken (1 pack) + tofu (1 block) + salmon (2 pieces)
    inv2 = deepcopy(SAMPLE_INVENTORY)
    consumed = subtract_inventory(inv2, [
        ("ground_chicken", 1),
        ("tofu_momen", 1),
        ("salmon_fillet", 2),
    ])

    checks = {
        "ground_chicken": (2, False),
        "tofu_momen": (2, False),
        "salmon_fillet": (10, False),
    }

    for c in consumed:
        if c.key in checks:
            expected_remaining, expected_depleted = checks[c.key]
            ok = c.remaining == expected_remaining and c.depleted == expected_depleted
            results.append({
                "name": f"batch_mixed_{c.key}",
                "passed": ok,
                "evidence": f"{c.key}: remaining={c.remaining}, depleted={c.depleted} "
                            f"(expected {expected_remaining}, {expected_depleted})",
            })

    # Verify inventory state
    results.append({
        "name": "batch_inventory_ground_chicken",
        "passed": inv2["ground_chicken"]["quantity"] == 2,
        "evidence": f"ground_chicken qty = {inv2['ground_chicken']['quantity']}",
    })
    results.append({
        "name": "batch_inventory_salmon",
        "passed": inv2["salmon_fillet"]["quantity"] == 10,
        "evidence": f"salmon_fillet qty = {inv2['salmon_fillet']['quantity']}",
    })

    return results


def test_confirmation_parsing():
    """Test that user confirmations are parsed into correct quantities."""
    results = []

    inv = deepcopy(SAMPLE_INVENTORY)
    tofu_item = InventoryItem(key="tofu_momen", name="Momen Medium Tofu", quantity=3, unit="blocks")
    chicken_item = InventoryItem(key="ground_chicken", name="Free-Range Ground Chicken", quantity=3, unit="packs")
    ginger_item = InventoryItem(key="ginger", name="Organic Ginger", quantity=0.18, unit="lbs")
    cabbage_item = InventoryItem(key="nappa_cabbage", name="Nappa Cabbage", quantity=7.26, unit="lbs")
    salmon_item = InventoryItem(key="salmon_fillet", name="Fresh Salmon Fillet", quantity=12, unit="pieces")

    cases = [
        # (text, item, expected_amount, description)
        ("1 block of tofu", tofu_item, 1, "'1 block of tofu'"),
        ("one block", tofu_item, 1, "'one block'"),
        ("2 blocks", tofu_item, 2, "'2 blocks'"),
        ("half", tofu_item, 1.5, "'half' → 0.5 * 3"),
        ("1/2", tofu_item, 1.5, "'1/2' → 0.5 * 3"),
        ("all of it", tofu_item, 3, "'all of it'"),
        ("used all", tofu_item, 3, "'used all'"),
        ("the rest", tofu_item, 3, "'the rest'"),
        ("1 bag of chicken", chicken_item, 1, "'1 bag of chicken'"),
        ("1 pack", chicken_item, 1, "'1 pack'"),
        ("2 packs", chicken_item, 2, "'2 packs'"),
        ("all", ginger_item, 0.18, "'all' for ginger (0.18 lbs)"),
        ("0.5 lbs", cabbage_item, 0.5, "'0.5 lbs'"),
        ("8 oz", cabbage_item, 0.5, "'8 oz'"),
        ("453.6 g", cabbage_item, 1.0, "'453.6 g'"),
        ("1 ounce", cabbage_item, 0.0625, "'1 ounce'"),
        ("1", salmon_item, 1, "bare '1'"),
        ("2", salmon_item, 2, "bare '2'"),
    ]

    for text, item, expected, desc in cases:
        actual = parse_confirmation(text, item)
        ok = actual is not None and abs(actual - expected) < 0.01
        safe_desc = desc.replace(" ", "_").replace("'", "")
        results.append({
            "name": f"confirm_{safe_desc}",
            "passed": ok,
            "evidence": f"{desc}: expected {expected}, got {actual}",
        })

    # Unparseable
    actual = parse_confirmation("i dunno maybe some", tofu_item)
    results.append({
        "name": "confirm_unparseable_returns_none",
        "passed": actual is None,
        "evidence": f"'i dunno maybe some' → {actual}",
    })

    return results


def test_match_and_confirm_pipeline():
    """Test the full one-shot pipeline: match → parse → compute remainder."""
    results = []

    inv = deepcopy(SAMPLE_INVENTORY)

    # "1 block of tofu" → should work
    consumed, mr = match_and_confirm("tofu", inv, "1 block of tofu")
    ok = consumed is not None and consumed.remaining == 2 and not consumed.depleted
    results.append({
        "name": "pipeline_tofu_1_block",
        "passed": ok,
        "evidence": f"consumed={consumed}" if consumed else f"match={mr}",
    })

    # "all" for ginger → should consume 0.18, remaining 0
    consumed, mr = match_and_confirm("ginger", inv, "all")
    ok = consumed is not None and abs(consumed.remaining) < 0.01 and consumed.depleted
    results.append({
        "name": "pipeline_ginger_all",
        "passed": ok,
        "evidence": f"remaining={consumed.remaining}, depleted={consumed.depleted}" if consumed else "no match",
    })

    # Unknown ingredient
    consumed, mr = match_and_confirm("unicorn", inv, "1 horn")
    results.append({
        "name": "pipeline_unknown_ingredient",
        "passed": consumed is None and not mr.matched,
        "evidence": f"consumed={consumed}, matched={mr.matched}",
    })

    # Bad confirmation
    consumed, mr = match_and_confirm("tofu", inv, "maybe some")
    results.append({
        "name": "pipeline_bad_confirmation",
        "passed": consumed is None and mr.matched,
        "evidence": f"consumed={consumed}, matched={mr.matched}",
    })

    return results


def test_real_inventory_file():
    """Test against the actual inventory.json if it exists."""
    results = []

    inv_path = Path.home() / "clawd" / "skills" / "grocery" / "inventory.json"
    if not inv_path.exists():
        results.append({
            "name": "real_inventory_skipped",
            "passed": True,
            "evidence": "inventory.json not found, skipping",
        })
        return results

    with open(inv_path) as f:
        inv = json.load(f)

    items = inv.get("items", {})
    results.append({
        "name": "real_inventory_loaded",
        "passed": len(items) > 0,
        "evidence": f"Loaded {len(items)} items from inventory.json",
    })

    # Test a few key matches
    key_cases = [
        ("chicken", "ground_chicken"),
        ("tofu", "tofu_momen"),
        ("pork", "pork_loin"),
        ("salmon", "salmon_fillet"),
        ("egg", "eggs"),
    ]
    for mention, expected_key in key_cases:
        mr = match_ingredient(mention, items)
        if mr.matched and mr.item:
            ok = mr.item.key == expected_key
            results.append({
                "name": f"real_match_{mention}",
                "passed": ok,
                "evidence": f"'{mention}' → {mr.item.key} (expected {expected_key}), qty={mr.item.quantity}",
            })
        else:
            results.append({
                "name": f"real_match_{mention}",
                "passed": False,
                "evidence": f"'{mention}' → no match",
            })

    return results


def test_depleted_detection():
    """Test that items are correctly flagged as depleted."""
    results = []

    # Exact zero
    inv = deepcopy(SAMPLE_INVENTORY)
    consumed = subtract_inventory(inv, [("tofu_momen", 3)])
    ok = consumed[0].remaining == 0 and consumed[0].depleted
    results.append({
        "name": "depleted_exact_zero",
        "passed": ok,
        "evidence": f"3 - 3 = {consumed[0].remaining}, depleted={consumed[0].depleted}",
    })

    # Small weight remainder (< 0.1 lbs)
    inv = deepcopy(SAMPLE_INVENTORY)
    consumed = subtract_inventory(inv, [("ginger", 0.1)])
    # 0.18 - 0.1 = 0.08 → depleted (small remainder)
    ok = abs(consumed[0].remaining - 0.08) < 0.01 and consumed[0].depleted
    results.append({
        "name": "depleted_small_remainder_weight",
        "passed": ok,
        "evidence": f"0.18 - 0.1 = {consumed[0].remaining:.4f}, depleted={consumed[0].depleted}",
    })

    # Fractional unit (< 1)
    inv = deepcopy(SAMPLE_INVENTORY)
    consumed = subtract_inventory(inv, [("tofu_momen", 2.5)])
    ok = consumed[0].remaining == 0.5 and consumed[0].depleted
    results.append({
        "name": "depleted_fractional_unit",
        "passed": ok,
        "evidence": f"3 - 2.5 = {consumed[0].remaining}, depleted={consumed[0].depleted}",
    })

    # Not depleted: weight still significant
    inv = deepcopy(SAMPLE_INVENTORY)
    consumed = subtract_inventory(inv, [("nappa_cabbage", 2)])
    ok = abs(consumed[0].remaining - 5.26) < 0.01 and not consumed[0].depleted
    results.append({
        "name": "depleted_not_depleted_weight",
        "passed": ok,
        "evidence": f"7.26 - 2 = {consumed[0].remaining:.2f}, depleted={consumed[0].depleted}",
    })

    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{BOLD}🧪 Inventory-Aware Meal Logging Tests{RESET}\n")

    all_results = []

    suites = [
        ("Fuzzy Matching", test_fuzzy_matching),
        ("Unit-Based Items", test_unit_based_items),
        ("Small Remainder", test_small_remainder),
        ("Subtraction Floor at Zero", test_subtraction_floor_zero),
        ("Batch Accumulation", test_batch_accumulation),
        ("Confirmation Parsing", test_confirmation_parsing),
        ("Full Pipeline", test_match_and_confirm_pipeline),
        ("Depleted Detection", test_depleted_detection),
        ("Real Inventory File", test_real_inventory_file),
    ]

    for suite_name, suite_fn in suites:
        print(f"\n{BOLD}── {suite_name} ──{RESET}")
        try:
            suite_results = suite_fn()
            for r in suite_results:
                status = pass_fail(r["passed"])
                print(f"  {status}  {r['name']}: {r['evidence']}")
            all_results.extend(suite_results)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  {RED}ERROR{RESET}  {suite_name}: {e}")
            all_results.append({"name": suite_name, "passed": False, "evidence": str(e)})

    # Summary
    total = len(all_results)
    passed = sum(1 for r in all_results if r["passed"])
    failed = total - passed

    print(f"\n{BOLD}── Summary ──{RESET}")
    print(f"  Total: {total}  |  {GREEN}Passed: {passed}{RESET}  |  {RED}Failed: {failed}{RESET}")

    # Write results
    eval_dir = Path(__file__).resolve().parent
    results_path = eval_dir / "inventory_results.json"
    import time
    with open(results_path, "w") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total": total,
            "passed": passed,
            "failed": failed,
            "results": all_results,
        }, f, indent=2)
    print(f"\nResults saved to: {results_path}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
