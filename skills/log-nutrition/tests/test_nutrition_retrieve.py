"""Acceptance tests for the read-only retrieval-assisted nutrition memory."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "skills" / "log-nutrition" / "scripts"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "retrieval_corpus.json"
sys.path.insert(0, str(SCRIPTS_DIR))

import nutrition_ingest  # noqa: E402
import nutrition_retrieve  # noqa: E402
from nutrition_ingest import migrate_database  # noqa: E402
from nutrition_retrieve import RetrievalError, retrieve_nutrition  # noqa: E402


def _item(name: str, portion_g: float, calories: float, protein: float, carbs: float, fat: float, aliases=None):
    item = {
        "item": name,
        "portion_g": portion_g,
        "calories": calories,
        "protein_g": protein,
        "carbs_g": carbs,
        "fat_total_g": fat,
    }
    if aliases:
        item["aliases"] = aliases
    return item


@pytest.fixture()
def retrieval_fixture(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "retrieval.duckdb"
    migrate_database(db)
    breakfast_items = [
        _item("baguette", 30, 82, 2.7, 16.8, 0.5, ["法棍"]),
        _item("avocado", 75, 120, 1.5, 6.4, 11.0, ["牛油果"]),
        _item("hard-boiled egg", 50, 78, 6.3, 0.6, 5.3, ["白煮蛋", "egg"]),
        _item("black coffee", 240, 2, 0.3, 0, 0, ["黑咖啡", "coffee"]),
    ]
    morning_items = [
        _item("egg", 50, 78, 6.3, 0.6, 5.3),
        _item("rice", 100, 130, 2.4, 28.0, 0.3),
    ]
    conn = duckdb.connect(str(db))
    conn.execute(
        """INSERT INTO nutrition_log
        (entry_id, meal_time, meal_type, meal_name, meal_description, food_items,
         calories, protein_g, carbs_g, fat_total_g, source)
        VALUES (101, '2026-08-16 08:05:00', 'breakfast',
                'Baguette avocado egg coffee', 'habitual multilingual breakfast',
                ?, 282, 10.8, 23.8, 16.8, 'fixture-history')""",
        [json.dumps(breakfast_items, ensure_ascii=False)],
    )
    conn.execute(
        """INSERT INTO nutrition_log
        (entry_id, meal_time, meal_type, meal_name, meal_description, food_items,
         calories, protein_g, carbs_g, fat_total_g, source)
        VALUES (102, '2026-08-17 08:10:00', 'breakfast',
                'Morning plate', 'historical meal with the same name as a recipe',
                ?, 208, 8.7, 28.6, 5.6, 'fixture-history')""",
        [json.dumps(morning_items)],
    )
    recipe_id = conn.execute("SELECT id FROM recipes WHERE name='Example breakfast'").fetchone()[0]
    conn.execute(
        """INSERT INTO recipes
        (name, description, food_items, total_calories, total_protein_g,
         total_carbs_g, total_fat_g)
        VALUES ('Morning plate', 'Canonical fixture recipe', ?, 300, 20, 30, 10)""",
        [json.dumps(morning_items)],
    )
    morning_recipe_id = conn.execute("SELECT id FROM recipes WHERE name='Morning plate'").fetchone()[0]
    assert morning_recipe_id != recipe_id
    conn.execute(
        """CREATE TABLE recipe_aliases (
          alias_id INTEGER PRIMARY KEY,
          recipe_id INTEGER NOT NULL,
          alias VARCHAR NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO recipe_aliases VALUES (1, ?, 'Alex breakfast')",
        [morning_recipe_id],
    )
    conn.close()

    profile = tmp_path / "user-profile.yaml"
    profile.write_text(
        "nutrition_defaults:\n  egg: hard-boiled\n  coffee:\n    kind: black filtered\n    additions: none\n",
        encoding="utf-8",
    )
    return db, profile


@pytest.fixture()
def recent_reuse_fixture(tmp_path: Path) -> Path:
    db = tmp_path / "recent_reuse.duckdb"
    migrate_database(db)
    conn = duckdb.connect(str(db))
    rows = [
        (201, "2026-08-15 08:00:00", "breakfast", "Alpha bowl", "first baseline", [_item("oats", 100, 389, 16.9, 66.3, 6.9)]),
        (202, "2026-08-16 08:10:00", "breakfast", "Bravo bowl", "second baseline", [_item("rice", 150, 195, 3.6, 42.0, 0.4)]),
        (203, "2026-08-17 08:20:00", "breakfast", "Charlie bowl", "third baseline", [_item("banana", 120, 107, 1.3, 27.0, 0.4)]),
        (204, "2026-08-17 12:30:00", "lunch", "Lunch bowl", "midday baseline", [_item("tofu", 100, 144, 15.7, 3.8, 8.7)]),
    ]
    for entry_id, meal_time, meal_type, meal_name, meal_description, items in rows:
        conn.execute(
            """INSERT INTO nutrition_log
            (entry_id, meal_time, meal_type, meal_name, meal_description, food_items,
             calories, protein_g, carbs_g, fat_total_g, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fixture-history')""",
            [
                entry_id,
                meal_time,
                meal_type,
                meal_name,
                meal_description,
                json.dumps(items, ensure_ascii=False),
                sum(item["calories"] for item in items),
                sum(item["protein_g"] for item in items),
                sum(item["carbs_g"] for item in items),
                sum(item["fat_total_g"] for item in items),
            ],
        )
    conn.close()
    return db


def _find(result: dict, *, candidate_type: str | None = None, label: str | None = None, match_type: str | None = None):
    for candidate in result["candidates"]:
        if candidate_type is not None and candidate["candidate_type"] != candidate_type:
            continue
        if label is not None and candidate["label"].casefold() != label.casefold():
            continue
        if match_type is not None and candidate["match_type"] != match_type:
            continue
        return candidate
    return None


@pytest.mark.parametrize("case", json.loads(FIXTURE_PATH.read_text())["cases"], ids=lambda case: case["id"])
def test_retrieval_fixture_corpus(retrieval_fixture, case):
    db, profile = retrieval_fixture
    result = retrieve_nutrition(
        db,
        case["query"],
        profile_path=profile if case.get("use_profile") else None,
        top_k=20,
    )
    expected = case["expect"]
    assert result["status"] == "ok"
    assert result["read_only"] is True
    if "top_action" in expected:
        assert result["suggested_action"] == expected["top_action"]
    if "reason" in expected:
        assert expected["reason"] in result["reason_codes"]
    if "defaults_status" in expected:
        assert result["sources"]["defaults_status"] == expected["defaults_status"]
    if "first_candidate_type" in expected:
        assert result["candidates"][0]["candidate_type"] == expected["first_candidate_type"]
    if "also_candidate_type" in expected:
        assert any(c["candidate_type"] == expected["also_candidate_type"] for c in result["candidates"])
    if "candidate_type" in expected:
        candidate = _find(
            result,
            candidate_type=expected.get("candidate_type"),
            label=expected.get("label"),
            match_type=expected.get("match_type"),
        )
        assert candidate is not None, result["candidates"]
        if "suggested_action" in expected:
            assert candidate["suggested_action"] == expected["suggested_action"]
    if "no_candidate_type" in expected:
        assert all(c["candidate_type"] != expected["no_candidate_type"] for c in result["candidates"])
    if expected.get("no_write_recommendation"):
        assert result["suggested_action"] in {"no_match", "needs_agent_decision", "pass_through"}
        assert all(c["suggested_action"] != "recipe_exact" for c in result["candidates"])


@pytest.mark.parametrize("query", ["same as before", "和以前一样"])
def test_clear_idless_reuse_intent_returns_a_single_recent_baseline_candidate_and_requires_confirmation(
    recent_reuse_fixture, query
):
    result = retrieve_nutrition(
        recent_reuse_fixture,
        {"text": query, "context": {"meal_type": "lunch"}},
        top_k=10,
    )
    assert result["suggested_action"] == "needs_agent_decision"
    assert "historical_baseline_requires_confirmation" in result["reason_codes"]
    candidates = [candidate for candidate in result["candidates"] if candidate["candidate_type"] == "historical_meal"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["label"] == "Lunch bowl"
    assert candidate["provenance"] == {"table": "nutrition_log", "entry_id": 204}
    assert candidate["match_type"] == "historical_recent_baseline"
    assert candidate["suggested_action"] == "needs_agent_decision"
    assert candidate["reason_codes"][0] == "historical_baseline_requires_confirmation"
    assert candidate["reason_codes"][-1] == "meal_type_filter_applied"
    assert candidate["per_item_basis"][0]["item"] == "tofu"


def test_recent_reuse_candidates_are_ordered_newest_first_when_unfiltered(recent_reuse_fixture):
    result = retrieve_nutrition(recent_reuse_fixture, "same as before", top_k=10)
    ids = [candidate["provenance"]["entry_id"] for candidate in result["candidates"] if candidate["candidate_type"] == "historical_meal"]
    assert ids == [204, 203, 202, 201]
    assert all(candidate["suggested_action"] == "needs_agent_decision" for candidate in result["candidates"] if candidate["candidate_type"] == "historical_meal")


def test_recent_meal_reuse_respects_explicit_meal_type_filter(recent_reuse_fixture):
    result = retrieve_nutrition(
        recent_reuse_fixture,
        {"text": "same breakfast again", "context": {"meal_type": "breakfast"}},
        top_k=10,
    )
    ids = [candidate["provenance"]["entry_id"] for candidate in result["candidates"] if candidate["candidate_type"] == "historical_meal"]
    assert ids == [203, 202, 201]
    assert 204 not in ids
    assert all("meal_type_filter_applied" in candidate["reason_codes"] for candidate in result["candidates"] if candidate["candidate_type"] == "historical_meal")


def test_recent_meal_reuse_route_is_read_only_and_leaves_the_temp_db_unchanged(monkeypatch, recent_reuse_fixture):
    before_hash = hashlib.sha256(recent_reuse_fixture.read_bytes()).hexdigest()
    conn = duckdb.connect(str(recent_reuse_fixture), read_only=True)
    before = conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0]
    conn.close()

    original_connect = nutrition_retrieve.duckdb.connect
    calls = []

    def checked_connect(*args, **kwargs):
        calls.append(kwargs.copy())
        assert kwargs.get("read_only") is True
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(nutrition_retrieve.duckdb, "connect", checked_connect)
    result = retrieve_nutrition(recent_reuse_fixture, {"text": "same as before", "context": {"meal_type": "lunch"}})
    assert result["suggested_action"] == "needs_agent_decision"
    assert calls and all(call == {"read_only": True} for call in calls)

    conn = original_connect(str(recent_reuse_fixture), read_only=True)
    after = conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0]
    conn.close()
    assert after == before
    assert hashlib.sha256(recent_reuse_fixture.read_bytes()).hexdigest() == before_hash


def test_ordinary_non_reuse_unknown_query_still_returns_no_match(recent_reuse_fixture):
    result = retrieve_nutrition(recent_reuse_fixture, "zorblatt fruit 250g", top_k=10)
    assert result["suggested_action"] == "no_match"
    assert result["candidates"] == []


def test_output_contract_has_rank_score_provenance_nutrients_and_no_private_paths(retrieval_fixture):
    db, profile = retrieval_fixture
    result = retrieve_nutrition(
        db,
        {"terms": ["avacado"], "meal_time": "2026-08-18T08:00:00-07:00", "context": {"meal_type": "breakfast", "db_path": str(db)}},
        profile_path=profile,
    )
    avocado = _find(result, candidate_type="ingredient_basis", label="avocado")
    assert avocado is not None
    assert 0 <= avocado["score"] <= 1
    assert avocado["rank"] >= 1
    assert avocado["suggested_action"] == "ingredient_basis_candidate"
    assert avocado["provenance"] == {"table": "nutrition_log", "entry_id": 101}
    assert avocado["meal_time"].startswith("2026-08-16 08:05:00")
    assert avocado["per_item_basis"][0]["basis"] == {"kind": "logged_portion", "amount": 75.0, "unit": "g"}
    assert avocado["per_item_basis"][0]["nutrients"]["calories"] == 120.0
    assert avocado["per_item_basis"][0]["per_100g_nutrients"]["calories"] == 160.0
    assert all(
        candidate["suggested_action"] != "exact_reuse_candidate"
        for candidate in result["candidates"]
        if candidate["candidate_type"] == "historical_meal"
    )
    serialized = json.dumps(result, ensure_ascii=False)
    assert str(db) not in serialized
    assert str(profile) not in serialized
    assert "db_path" not in result["query"]["context"]


def test_recipe_exact_alias_provenance_and_fuzzy_recipe_never_auto_selected(retrieval_fixture):
    db, _ = retrieval_fixture
    exact = retrieve_nutrition(db, terms=["Alex breakfast"], top_k=20)
    recipe = _find(exact, candidate_type="recipe", match_type="recipe_exact_alias")
    assert recipe["provenance"]["table"] == "recipes"
    assert recipe["provenance"]["matched_alias_table"] == "recipe_aliases"
    assert recipe["provenance"]["alias_id"] == 1
    assert recipe["suggested_action"] == "recipe_exact_candidate"
    assert exact["suggested_action"] == "needs_agent_decision"

    fuzzy = retrieve_nutrition(db, terms=["Alex breakfst"], top_k=20)
    recipe = _find(fuzzy, candidate_type="recipe", label="Morning plate")
    assert recipe["match_type"] == "recipe_fuzzy"
    assert recipe["suggested_action"] == "needs_agent_decision"
    assert fuzzy["suggested_action"] != "recipe_exact"


def test_exact_recipe_with_extra_terms_is_noncommitting_and_not_exact(retrieval_fixture):
    db, _ = retrieval_fixture
    result = retrieve_nutrition(db, terms=["Morning plate", "extra avocado"], top_k=20)
    recipe = _find(result, candidate_type="recipe", label="Morning plate")
    assert recipe is not None
    assert recipe["match_type"] == "recipe_fuzzy"
    assert recipe["suggested_action"] == "needs_agent_decision"
    assert result["suggested_action"] == "needs_agent_decision"


@pytest.mark.parametrize(
    ("query", "context"),
    [
        ({"text": "restaurant salad"}, None),
        ({"text": "Sweetgreen harvest bowl"}, None),
        ({"text": "salad"}, {"restaurant": True}),
        ({"text": "salad"}, {"brand_or_restaurant": True}),
    ],
    ids=["raw-restaurant", "sweetgreen", "restaurant-flag", "canonical-brand-flag"],
)
def test_general_brand_and_restaurant_safety_always_requires_agent(retrieval_fixture, query, context):
    db, _ = retrieval_fixture
    result = retrieve_nutrition(db, query, context=context, top_k=20)
    assert result["suggested_action"] == "needs_agent_decision"
    assert "brand_or_restaurant_requires_external_source" in result["reason_codes"]
    assert all(candidate["suggested_action"] != "recipe_exact" for candidate in result["candidates"])


def test_every_candidate_uses_the_same_fixed_schema_and_list_basis(retrieval_fixture):
    db, profile = retrieval_fixture
    expected_keys = {
        "rank",
        "candidate_type",
        "label",
        "score",
        "match_type",
        "suggested_action",
        "provenance",
        "meal_time",
        "nutrients",
        "per_item_basis",
        "reason_codes",
    }
    result = retrieve_nutrition(
        db,
        {"text": "Morning plate egg coffee", "terms": ["Morning plate", "egg", "coffee"]},
        profile_path=profile,
        top_k=50,
    )
    assert {candidate["candidate_type"] for candidate in result["candidates"]} == {
        "recipe",
        "historical_meal",
        "ingredient_basis",
        "user_default",
    }
    for candidate in result["candidates"]:
        assert set(candidate) == expected_keys
        assert candidate["meal_time"] is None or isinstance(candidate["meal_time"], str)
        assert isinstance(candidate["nutrients"], dict)
        assert isinstance(candidate["per_item_basis"], list)
        assert json.loads(json.dumps(candidate, allow_nan=False))


@pytest.mark.parametrize(
    "query",
    [
        {"terms": ["Morning plate"]},
        {"terms": ["Alex breakfast"]},
        {"text": "Morning plate"},
    ],
    ids=["canonical", "alias", "raw-text"],
)
def test_exact_recipe_results_remain_candidates_only(retrieval_fixture, query):
    db, _ = retrieval_fixture
    result = retrieve_nutrition(db, query, top_k=20)
    recipe = _find(result, candidate_type="recipe")
    assert recipe["suggested_action"] == "recipe_exact_candidate"
    assert result["suggested_action"] == "needs_agent_decision"
    assert "write" not in json.dumps(result).casefold()


def test_recipe_alias_table_is_optional_and_canonical_lookup_remains_compatible(retrieval_fixture):
    db, _ = retrieval_fixture
    conn = duckdb.connect(str(db))
    conn.execute("DROP TABLE recipe_aliases")
    conn.close()
    result = retrieve_nutrition(db, terms=["Morning plate"])
    recipe = _find(result, candidate_type="recipe", match_type="recipe_exact_name")
    assert recipe is not None
    assert result["sources"]["counts"]["recipe_aliases"] == 0


def test_retrieval_opens_database_read_only_and_never_calls_ingest_helpers(monkeypatch, retrieval_fixture):
    db, profile = retrieval_fixture
    before_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    conn = duckdb.connect(str(db), read_only=True)
    before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("nutrition_log", "nutrition_ingest_receipts", "nutrition_ingest_identities", "recipes")
    }
    conn.close()

    def forbidden(*args, **kwargs):
        raise AssertionError("retrieval called a shared write/ingest helper")

    monkeypatch.setattr(nutrition_ingest, "ingest_nutrition", forbidden)
    monkeypatch.setattr(nutrition_ingest, "resolve_and_ingest_quick_text", forbidden)
    monkeypatch.setattr(nutrition_ingest, "writable_database", forbidden)
    original_connect = nutrition_retrieve.duckdb.connect
    calls = []

    def checked_connect(*args, **kwargs):
        calls.append(kwargs.copy())
        assert kwargs.get("read_only") is True
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(nutrition_retrieve.duckdb, "connect", checked_connect)
    result = retrieve_nutrition(db, {"text": "bagute avacado cofee"}, profile_path=profile)
    assert result["status"] == "ok"
    assert calls and all(call == {"read_only": True} for call in calls)

    conn = original_connect(str(db), read_only=True)
    after = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    conn.close()
    assert after == before
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before_hash


def test_missing_database_fails_without_creating_it_or_leaking_path(tmp_path):
    missing = tmp_path / "private" / "live.duckdb"
    with pytest.raises(RetrievalError) as error:
        retrieve_nutrition(missing, text="coffee")
    assert error.value.code == "database_unavailable"
    assert str(missing) not in error.value.message
    assert not missing.exists()


def test_cli_accepts_raw_and_structured_query_and_emits_deterministic_json(retrieval_fixture):
    db, profile = retrieval_fixture
    command = [
        sys.executable,
        str(SCRIPTS_DIR / "nutrition_retrieve.py"),
        "--db", str(db),
        "--json", json.dumps({"text": "早餐", "terms": ["bagute", "avacado"], "meal_time": "2026-08-18T08:15:00-07:00"}),
        "--profile", str(profile),
        "--top-k", "10",
    ]
    completed = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "ok"
    assert result["query"]["terms"] == ["bagute", "avacado"]
    assert result["query"]["meal_time"] == "2026-08-18T08:15:00-07:00"
    assert _find(result, candidate_type="ingredient_basis", label="baguette") is not None
    assert str(db) not in completed.stdout
    assert str(profile) not in completed.stdout


def test_identical_cli_calls_emit_byte_identical_json_without_runtime_fields(retrieval_fixture):
    db, profile = retrieval_fixture
    command = [
        sys.executable,
        str(SCRIPTS_DIR / "nutrition_retrieve.py"),
        "--db", str(db),
        "--json", json.dumps({"terms": ["avocado"], "context": {"meal_type": "breakfast"}}),
        "--profile", str(profile),
    ]
    first = subprocess.run(command, cwd=REPO_ROOT, text=False, capture_output=True, timeout=10)
    second = subprocess.run(command, cwd=REPO_ROOT, text=False, capture_output=True, timeout=10)
    assert first.returncode == second.returncode == 0
    assert first.stderr == second.stderr == b""
    assert first.stdout == second.stdout
    assert b"latency" not in first.stdout


@pytest.mark.parametrize(
    ("arguments", "expected_code"),
    [
        (["--text", "coffee"], "invalid_arguments"),
        (["--db", "unused.duckdb", "--text", "coffee", "--top-k", "nope"], "invalid_arguments"),
    ],
    ids=["missing-db", "bad-top-k"],
)
def test_argparse_failures_emit_one_json_error_without_usage(arguments, expected_code):
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "nutrition_retrieve.py"), *arguments],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.count("\n") == 1
    assert "usage:" not in completed.stderr.casefold()
    assert json.loads(completed.stderr)["error"]["code"] == expected_code


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_cli_rejects_nonfinite_json_constants_with_strict_json_error(retrieval_fixture, constant):
    db, _ = retrieval_fixture
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "nutrition_retrieve.py"),
            "--db", str(db),
            "--json", '{"terms":["coffee"],"context":{"confidence":' + constant + "}}",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert json.loads(completed.stderr)["error"]["code"] == "invalid_json"
    assert constant not in completed.stderr


def test_cli_errors_are_json_and_do_not_disclose_explicit_path(tmp_path):
    missing = tmp_path / "sensitive" / "health.duckdb"
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "nutrition_retrieve.py"), "--db", str(missing), "--text", "coffee"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 2
    error = json.loads(completed.stderr)
    assert error["error"]["code"] == "database_unavailable"
    assert str(missing) not in completed.stderr
    assert not missing.exists()


@pytest.mark.parametrize(
    "terms",
    [
        {"coffee": True},
        {"coffee"},
        (value for value in ["coffee"]),
        7,
        True,
        ["coffee", 7],
    ],
    ids=["dict", "set", "generator", "number", "bool", "mixed-list"],
)
def test_terms_require_an_actual_list_or_tuple_of_strings(retrieval_fixture, terms):
    db, _ = retrieval_fixture
    with pytest.raises(RetrievalError) as error:
        retrieve_nutrition(db, terms=terms)
    assert error.value.code == "invalid_query_terms"
    assert str(db) not in error.value.message


def test_tuple_terms_are_accepted(retrieval_fixture):
    db, _ = retrieval_fixture
    result = retrieve_nutrition(db, terms=("coffee",))
    assert result["status"] == "ok"


@pytest.mark.parametrize(
    ("context", "expected_code"),
    [
        (["not", "an", "object"], "invalid_context"),
        ({"confidence": float("nan")}, "invalid_context_value"),
        ({"nested": {"values": {"unsupported"}}}, "invalid_context_value"),
        ({1: "non-string-key"}, "invalid_context_value"),
        ({"has_photo": "yes"}, "invalid_context_value"),
    ],
    ids=["list", "nan", "set", "non-string-key", "bad-flag-type"],
)
def test_context_requires_a_json_like_object_with_typed_classifiers(retrieval_fixture, context, expected_code):
    db, _ = retrieval_fixture
    with pytest.raises(RetrievalError) as error:
        retrieve_nutrition(db, terms=["coffee"], context=context)
    assert error.value.code == expected_code
    assert str(db) not in error.value.message


def test_public_context_whitelist_never_echoes_innocuous_nested_or_list_paths(retrieval_fixture):
    db, _ = retrieval_fixture
    secrets = [
        "/Users/reviewer/private/health.duckdb",
        "file:///Users/reviewer/private/photo.jpg",
        "~/private/profile.yaml",
    ]
    context = {
        "meal_type": "breakfast",
        "input_kind": "text",
        "has_photo": False,
        "brand": "Sweetgreen",
        "note": secrets[0],
        "innocuous_values": [secrets[1], {"nested": secrets[2]}],
    }
    result = retrieve_nutrition(db, terms=["coffee"], context=context)
    assert result["query"]["context"] == {
        "meal_type": "breakfast",
        "input_kind": "text",
        "brand_or_restaurant": True,
    }
    serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
    assert not any(secret in serialized for secret in secrets)
    assert "Sweetgreen" not in serialized
    assert "innocuous_values" not in serialized


def test_defaults_present_and_absent_are_explicit_without_path_disclosure(retrieval_fixture):
    db, profile = retrieval_fixture
    present = retrieve_nutrition(db, terms=["cofee"], profile_path=profile, top_k=20)
    default = _find(present, candidate_type="user_default", label="coffee")
    assert default["per_item_basis"][0]["value"] == {"kind": "black filtered", "additions": "none"}
    assert default["provenance"] == {"table": "user_defaults", "key": "coffee"}
    assert present["sources"]["defaults_status"] == "available"
    assert str(profile) not in json.dumps(present)

    absent = retrieve_nutrition(db, terms=["coffee"], top_k=20)
    assert absent["sources"]["defaults_status"] == "not_supplied"
    assert _find(absent, candidate_type="user_default") is None


def test_retrieval_fixture_p95_under_500ms_and_benchmark_artifact_unchanged(retrieval_fixture):
    db, profile = retrieval_fixture
    results_path = REPO_ROOT / "skills" / "log-nutrition" / "evals" / "results.json"
    before = results_path.read_bytes()
    durations = []
    for _ in range(30):
        started = time.perf_counter()
        result = retrieve_nutrition(db, {"text": "两片 bagute 牛油果 白煮蛋 cofee"}, profile_path=profile, top_k=12)
        durations.append((time.perf_counter() - started) * 1000)
        assert result["status"] == "ok"
    ordered = sorted(durations)
    p95 = ordered[max(0, math_ceil(0.95 * len(ordered)) - 1)]
    assert p95 <= 500, {"p95_ms": p95, "durations_ms": durations}
    assert results_path.read_bytes() == before


def math_ceil(value: float) -> int:
    integer = int(value)
    return integer if value == integer else integer + 1


def test_phrase_coverage_ranks_homemade_latte_above_fior_di_latte_and_stays_candidate_only(tmp_path):
    db = tmp_path / "latte-ranking.duckdb"
    migrate_database(db)
    conn = duckdb.connect(str(db))
    rows = [
        (301, "2026-08-20 13:00:00", "lunch", "Household homemade latte", [
            _item("homemade latte", 240, 130, 7, 11, 6, ["自制拿铁", "家里做的拿铁"]),
        ]),
        (302, "2026-08-21 19:00:00", "dinner", "Pizza with Fior di Latte", [
            _item("Fior di Latte mozzarella", 125, 300, 20, 2, 24, ["mozzarella", "马苏里拉"]),
        ]),
    ]
    for entry_id, meal_time, meal_type, meal_name, items in rows:
        conn.execute(
            """INSERT INTO nutrition_log
            (entry_id, meal_time, meal_type, meal_name, food_items, calories, protein_g, carbs_g, fat_total_g, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'fixture')""",
            [entry_id, meal_time, meal_type, meal_name, json.dumps(items, ensure_ascii=False),
             sum(item["calories"] for item in items), sum(item["protein_g"] for item in items),
             sum(item["carbs_g"] for item in items), sum(item["fat_total_g"] for item in items)],
        )
    conn.close()

    english = retrieve_nutrition(db, {"terms": ["homemade latte"], "context": {"meal_type": "lunch"}}, top_k=20)
    chinese = retrieve_nutrition(db, {"terms": ["自制拿铁", "homemade latte"], "context": {"meal_type": "lunch"}}, top_k=20)
    mozzarella = retrieve_nutrition(db, {"terms": ["Fior di Latte mozzarella"], "context": {"meal_type": "dinner"}}, top_k=20)

    for result in (english, chinese):
        labels = [candidate["label"] for candidate in result["candidates"]]
        assert labels.index("homemade latte") < labels.index("Fior di Latte mozzarella")
        assert result["suggested_action"] == "needs_agent_decision"
        assert all(candidate["suggested_action"] != "recipe_exact" for candidate in result["candidates"])
    assert _find(mozzarella, candidate_type="ingredient_basis", label="Fior di Latte mozzarella") is not None
    assert mozzarella["suggested_action"] == "needs_agent_decision"


def test_meal_type_and_recency_are_tie_breaking_signals_not_write_authority(tmp_path):
    db = tmp_path / "signals.duckdb"
    migrate_database(db)
    conn = duckdb.connect(str(db))
    for entry_id, meal_time, meal_type in [
        (1, "2026-08-20 08:00:00", "breakfast"),
        (2, "2026-08-21 13:00:00", "lunch"),
    ]:
        conn.execute(
            """INSERT INTO nutrition_log
            (entry_id, meal_time, meal_type, meal_name, food_items, calories, protein_g, carbs_g, fat_total_g, source)
            VALUES (?, ?, ?, 'House latte', ?, 100, 5, 10, 4, 'fixture')""",
            [entry_id, meal_time, meal_type, json.dumps([_item("latte", 200, 100, 5, 10, 4)])],
        )
    conn.close()
    result = retrieve_nutrition(db, {"terms": ["house latte"], "context": {"meal_type": "lunch"}}, top_k=20)
    historical = [candidate for candidate in result["candidates"] if candidate["candidate_type"] == "historical_meal"]
    assert [candidate["provenance"]["entry_id"] for candidate in historical[:2]] == [2, 1]
    assert all(candidate["suggested_action"] in {"exact_reuse_candidate", "needs_agent_decision"} for candidate in historical)
    assert result["suggested_action"] == "needs_agent_decision"


def test_equal_score_recency_precedes_label_fallback(tmp_path):
    db = tmp_path / "recency-before-label.duckdb"
    migrate_database(db)
    conn = duckdb.connect(str(db))
    for entry_id, meal_time, label in [
        (1, "2026-08-20 08:00:00", "A older"),
        (2, "2026-08-21 08:00:00", "Z newer"),
    ]:
        item = _item(label, 100, 100, 5, 10, 4, ["same signal"])
        conn.execute(
            """INSERT INTO nutrition_log
            (entry_id, meal_time, meal_type, meal_name, food_items, calories, protein_g, carbs_g, fat_total_g, source)
            VALUES (?, ?, 'breakfast', ?, ?, 100, 5, 10, 4, 'fixture')""",
            [entry_id, meal_time, label, json.dumps([item])],
        )
    conn.close()

    first = retrieve_nutrition(db, {"terms": ["same signal"], "context": {"meal_type": "breakfast"}}, top_k=20)
    second = retrieve_nutrition(db, {"terms": ["same signal"], "context": {"meal_type": "breakfast"}}, top_k=20)
    ingredients = [candidate for candidate in first["candidates"] if candidate["candidate_type"] == "ingredient_basis"]
    assert [(candidate["label"], candidate["meal_time"]) for candidate in ingredients[:2]] == [
        ("Z newer", "2026-08-21 08:00:00"),
        ("A older", "2026-08-20 08:00:00"),
    ]
    assert first == second


def test_malformed_optional_sources_fail_soft_without_fabricating_nutrition(tmp_path):
    db = tmp_path / "minimal.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE TABLE nutrition_log (entry_id INTEGER, meal_time TIMESTAMP, meal_name VARCHAR, food_items VARCHAR, calories DOUBLE)")
    conn.execute("INSERT INTO nutrition_log VALUES (1, '2026-08-18 12:00:00', 'Mystery plate', '{bad json', NULL)")
    conn.close()
    result = retrieve_nutrition(db, text="mystery plate")
    meal = _find(result, candidate_type="historical_meal", label="Mystery plate")
    assert meal is not None
    assert meal["nutrients"] == {}
    assert meal["per_item_basis"] == []
    assert result["suggested_action"] == "needs_agent_decision"
