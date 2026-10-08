from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "skills" / "log-nutrition" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from nutrition_ingest import migrate_database  # noqa: E402
import nutrition_evidence_bundle as neb  # noqa: E402
import nutrition_retrieve  # noqa: E402


def test_household_defaults_loaded_without_explicit_leaf_requests(monkeypatch, tmp_path):
    db = tmp_path / "defaults.duckdb"
    migrate_database(db)
    profile = tmp_path / "profile.yaml"
    profile.write_text("nutrition_defaults:\n  homemade_ingredient_quantity_basis: raw\n  coffee: black\n  egg: boiled\n  latte: household recipe\nprivate: excluded\n")
    monkeypatch.setattr(neb, "user_profile_path", lambda: profile)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    result = neb.nutrition_evidence_bundle({
        "db": str(db), "include_household_defaults": True,
        "profile_keys": ["nutrition_defaults.coffee", "nutrition_defaults.latte"],
    })
    # Returned evidence is bounded, deduplicated, and does not rewrite history.
    serialized = json.dumps(result)
    assert "household recipe" in serialized and '"raw"' in serialized
    assert "excluded" not in serialized
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert result["status"] == "ok"


@pytest.mark.parametrize("keys", ["nutrition_defaults.coffee", ["private"], [None], {}])
def test_household_defaults_do_not_bypass_profile_allowlist(tmp_path, keys):
    db = tmp_path / "invalid.duckdb"
    migrate_database(db)
    with pytest.raises(neb.EvidenceBundleError, match="allowlisted"):
        neb.nutrition_evidence_bundle({"db": str(db), "include_household_defaults": True, "profile_keys": keys})


def test_missing_household_profile_is_missing_evidence_not_invented_defaults(monkeypatch, tmp_path):
    db = tmp_path / "missing.duckdb"
    migrate_database(db)
    monkeypatch.setattr(neb, "user_profile_path", lambda: tmp_path / "absent.yaml")
    result = neb.nutrition_evidence_bundle({"db": str(db), "include_household_defaults": True})
    assert result["status"] != "ok"
    assert '"raw"' not in json.dumps(result)


def _seed_entry(db: Path) -> None:
    conn = duckdb.connect(str(db))
    try:
        conn.execute(
            """
            INSERT INTO nutrition_log (
                entry_id, meal_time, meal_type, meal_name, meal_description, food_items,
                calories, protein_g, carbs_g, fat_total_g, fat_saturated_g, fiber_g, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                11,
                "2026-08-19 08:05:00",
                "lunch",
                "Lunch bowl",
                "baseline meal for batch evidence",
                json.dumps([
                    {"item": "egg", "portion_g": 50, "calories": 78, "protein_g": 6.3, "carbs_g": 0.6, "fat_total_g": 5.3},
                ]),
                78.0,
                6.3,
                0.6,
                5.3,
                1.6,
                0.0,
                "fixture",
            ],
        )
    finally:
        conn.close()


def test_evidence_bundle_batches_queries_and_keeps_read_only(monkeypatch, tmp_path):
    db = tmp_path / "bundle.duckdb"
    profile = tmp_path / "profile.yaml"
    profile.write_text("nutrition_defaults:\n  banana: ripe\n", encoding="utf-8")
    migrate_database(db)
    _seed_entry(db)

    before_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    connect_flags: list[bool | None] = []

    orig_bundle_connect = neb.duckdb.connect
    orig_retrieve_connect = nutrition_retrieve.duckdb.connect

    def bundle_connect(*args, **kwargs):
        connect_flags.append(kwargs.get("read_only"))
        return orig_bundle_connect(*args, **kwargs)

    def retrieve_connect(*args, **kwargs):
        connect_flags.append(kwargs.get("read_only"))
        return orig_retrieve_connect(*args, **kwargs)

    monkeypatch.setattr(neb.duckdb, "connect", bundle_connect)
    monkeypatch.setattr(nutrition_retrieve.duckdb, "connect", retrieve_connect)

    result = neb.nutrition_evidence_bundle(
        {
            "db": str(db),
            "profile_path": str(profile),
            "queries": [
                {
                    "text": "same as before",
                    "context": {"meal_type": "lunch"},
                    "entry_ids": [11],
                },
                {
                    "text": "egg",
                    "top_k": 0,
                },
            ],
        }
    )

    assert result["status"] == "partial"
    assert result["query_count"] == 2
    assert result["failure_count"] == 1
    assert result["profile_path"] == str(profile)

    first = result["results"][0]
    assert first["status"] == "ok"
    assert first["retrieval"]["read_only"] is True
    assert first["entries"][0]["found"] is True
    assert first["entries"][0]["entry"]["food_items"][0]["item"] == "egg"

    second = result["results"][1]
    assert second["status"] == "error"
    assert second["retrieval_error"]["code"] == "invalid_top_k"

    conn = duckdb.connect(str(db), read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0] == 1
    finally:
        conn.close()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before_hash
    assert connect_flags and all(flag is True for flag in connect_flags)


def test_evidence_bundle_cli_returns_json(tmp_path):
    db = tmp_path / "bundle-cli.duckdb"
    migrate_database(db)
    _seed_entry(db)

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "nutrition_evidence_bundle.py"),
            "--json",
            json.dumps({
                "db": str(db),
                "queries": [{"text": "same as before", "context": {"meal_type": "lunch"}}],
            }),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    assert payload["results"][0]["retrieval"]["read_only"] is True


def test_source_complete_local_packet_is_item_scoped_bounded_and_hash_stable(monkeypatch, tmp_path):
    db = tmp_path / "source-complete.duckdb"
    profile = tmp_path / "profile.yaml"
    kb_root = tmp_path / "recipes"
    kb_root.mkdir()
    recipe = kb_root / "house-sourdough.md"
    profile.write_text(
        "nutrition_defaults:\n  latte:\n    milk_g: 180\n    espresso_shots: 2\n  egg: hard-boiled\n",
        encoding="utf-8",
    )
    recipe.write_text(
        "---\ntitle: House Sourdough\n---\n# House Sourdough\n\n## Formula\n- flour: 500 g\n- water: 350 g\n\n## Yield\n650 g baked loaf\n\n## Notes\nnot requested\n",
        encoding="utf-8",
    )
    migrate_database(db)
    conn = duckdb.connect(str(db))
    conn.execute(
        """INSERT INTO nutrition_log
        (entry_id, meal_time, meal_type, meal_name, food_items, calories, protein_g, carbs_g, fat_total_g, source)
        VALUES (77, '2026-08-20 19:00:00', 'dinner', 'Pizza', ?, 500, 25, 50, 20, 'fixture')""",
        [json.dumps([
            {"item": "Fior di Latte mozzarella", "portion_g": 125, "calories": 300, "protein_g": 20, "source": "package"},
            {"item": "tomato sauce", "portion_g": 80, "calories": 40, "source": "estimate"},
        ])],
    )
    conn.close()
    monkeypatch.setattr(neb, "KB_RECIPE_ROOT", kb_root)
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (db, profile, recipe)}

    request = {
        "db": str(db),
        "profile_path": str(profile),
        "queries": [{"terms": ["homemade latte"], "context": {"meal_type": "lunch"}}],
        "entry_requests": [{"entry_id": 77, "item_terms": ["mozzarella"], "fields": ["item", "portion_g", "calories", "protein_g", "source"]}],
        "profile_keys": ["nutrition_defaults.latte"],
        "kb_recipe_queries": [{"query": "house sourdough", "sections": ["Formula", "Yield"]}],
    }
    first = neb.nutrition_evidence_bundle(request)
    second = neb.nutrition_evidence_bundle(request)

    assert first == second
    assert first["status"] == "ok"
    assert first["network_free"] is True and first["read_only"] is True
    assert first["coverage"] == {
        "scope": "requested_local_evidence",
        "requested_classes": ["queries", "entries", "profile", "kb_recipes"],
        "returned_classes": ["queries", "entries", "profile", "kb_recipes"],
        "missing_classes": [],
        "request_statuses": {"queries": ["ok"], "entries": ["ok"], "profile": ["ok"], "kb_recipes": ["ok"]},
        "errors": [],
    }
    def keys(value):
        if isinstance(value, dict):
            return set(value) | {key for item in value.values() for key in keys(item)}
        if isinstance(value, list):
            return {key for item in value for key in keys(item)}
        return set()
    assert "source_complete" not in keys(first)
    selected = first["entries"][0]
    assert selected["evidence_ref"] == "nutrition_log:77"
    assert [item["item"] for item in selected["items"]] == ["Fior di Latte mozzarella"]
    assert selected["items"][0]["evidence_ref"] == "nutrition_log:77:item:0"
    assert first["profile"][0]["value"] == {"milk_g": 180, "espresso_shots": 2}
    kb = first["kb_recipes"][0]["recipes"][0]
    assert Path(kb["canonical_path"]) == recipe.resolve()
    assert [(section["name"], section["evidence_ref"]) for section in kb["sections"]] == [
        ("formula", "kb-recipe:house-sourdough#formula"),
        ("yield", "kb-recipe:house-sourdough#yield"),
    ]
    assert all(section["line_start"] <= section["line_end"] for section in kb["sections"])
    assert "not requested" not in json.dumps(kb)
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before} == before


def test_compact_retrieval_keeps_recent_exact_core_token_match(tmp_path):
    db = tmp_path / "compact-ranking.duckdb"
    migrate_database(db)
    conn = duckdb.connect(str(db))
    pork = {"item": "Lean pork", "portion_g": 100, "calories": 180, "protein_g": 19, "carbs_g": 0, "fat_total_g": 10}
    shredded_pork = {**pork, "item": "Lean pork, shredded"}
    celery = {"item": "Celery", "portion_g": 100, "calories": 20, "protein_g": 1, "carbs_g": 5, "fat_total_g": 0}
    pepper = {"item": "Bell pepper", "portion_g": 100, "calories": 20, "protein_g": 1, "carbs_g": 5, "fat_total_g": 0}
    rows = [
        (41, "2026-04-27 17:45:00", "青椒猪肉丝", "older baseline", [shredded_pork, pepper]),
        (42, "2026-04-17 12:30:00", "Shredded pork noodle soup", "older baseline", [shredded_pork]),
        (43, "2026-04-15 18:00:00", "Rigatoni with celery pork stir-fry", "older baseline", [pork, celery]),
        (44, "2026-08-19 18:00:00", "Pizza + celery pork stir-fry", "consumed shared portion", [pork, celery]),
    ]
    for entry_id, meal_time, meal_name, description, items in rows:
        conn.execute(
            """INSERT INTO nutrition_log
            (entry_id, meal_time, meal_type, meal_name, meal_description, food_items,
             calories, protein_g, carbs_g, fat_total_g, source)
            VALUES (?, ?, 'dinner', ?, ?, ?, 200, 20, 5, 10, 'fixture')""",
            [
                entry_id,
                meal_time,
                meal_name,
                description,
                json.dumps(items),
            ],
        )
    conn.close()

    result = neb.nutrition_evidence_bundle({
        "db": str(db),
        "queries": [{
            "text": "celery shredded pork portion two days ago",
            "terms": ["celery shredded pork", "芹菜肉丝", "肉丝"],
            "meal_time": "2026-08-20T12:30:00-07:00",
            "context": {"input_kind": "text"},
            "top_k": 8,
        }],
    })

    retrieval = result["results"][0]["retrieval"]
    recent = next(candidate for candidate in retrieval["candidates"] if candidate.get("evidence_ref") == "nutrition_log:44")
    assert recent["rank"] <= 3
    assert recent["suggested_action"] == "needs_agent_decision"
    assert retrieval["suggested_action"] == "needs_agent_decision"
    assert retrieval["candidates_truncated"] is True


def test_bundle_missing_coverage_is_honest_and_arbitrary_profile_keys_are_rejected(monkeypatch, tmp_path):
    db = tmp_path / "missing.duckdb"
    profile = tmp_path / "profile.yaml"
    kb_root = tmp_path / "recipes"
    kb_root.mkdir()
    profile.write_text("nutrition_defaults:\n  latte: house\nsecret: nope\n", encoding="utf-8")
    migrate_database(db)
    monkeypatch.setattr(neb, "KB_RECIPE_ROOT", kb_root)

    result = neb.nutrition_evidence_bundle({
        "db": str(db),
        "profile_path": str(profile),
        "entry_requests": [{"entry_id": 999, "item_terms": ["mozzarella"], "fields": ["item"]}],
        "profile_keys": ["nutrition_defaults.unknown"],
        "kb_recipe_queries": [{"query": "unknown recipe", "sections": ["Yield"]}],
    })
    assert result["status"] == "partial"
    assert result["coverage"]["returned_classes"] == []
    assert result["coverage"]["missing_classes"] == ["entries", "profile", "kb_recipes"]
    assert {error["code"] for error in result["coverage"]["errors"]} == {"missing_entry_id", "kb_recipe_not_found"}

    try:
        neb.nutrition_evidence_bundle({"db": str(db), "profile_path": str(profile), "profile_keys": ["secret"]})
    except neb.EvidenceBundleError as exc:
        assert "nutrition_defaults" in str(exc)
    else:
        raise AssertionError("non-allowlisted profile path should fail")


def test_profile_keys_default_to_canonical_profile_while_explicit_override_wins(monkeypatch, tmp_path):
    db = tmp_path / "canonical-profile.duckdb"
    canonical = tmp_path / "canonical.yaml"
    override = tmp_path / "override.yaml"
    migrate_database(db)
    canonical.write_text(
        "nutrition_defaults:\n  latte:\n    milk_g: 180\nsecret: canonical-private\n",
        encoding="utf-8",
    )
    override.write_text(
        "nutrition_defaults:\n  latte:\n    milk_g: 240\nsecret: override-private\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(neb, "user_profile_path", lambda: canonical)
    before_profiles = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (canonical, override)}

    default_result = neb.nutrition_evidence_bundle({
        "db": str(db),
        "profile_keys": ["nutrition_defaults.latte.milk_g"],
    })
    assert default_result["status"] == "ok"
    assert default_result["profile_path"] == str(canonical)
    assert default_result["profile"] == [{
        "key": "nutrition_defaults.latte.milk_g",
        "status": "ok",
        "value": 180,
        "evidence_ref": "profile:nutrition-defaults-latte-milk-g",
    }]
    assert default_result["read_only"] is True and default_result["network_free"] is True

    override_result = neb.nutrition_evidence_bundle({
        "db": str(db),
        "profile": str(override),
        "profile_keys": ["nutrition_defaults.latte.milk_g"],
    })
    assert override_result["profile_path"] == str(override)
    assert override_result["profile"][0]["value"] == 240

    try:
        neb.nutrition_evidence_bundle({"db": str(db), "profile_keys": ["secret"]})
    except neb.EvidenceBundleError as exc:
        assert "nutrition_defaults" in str(exc)
    else:
        raise AssertionError("canonical profile fallback must not widen the profile allowlist")
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before_profiles} == before_profiles


def test_bundle_without_profile_keys_does_not_resolve_canonical_profile(monkeypatch, tmp_path):
    db = tmp_path / "no-profile-request.duckdb"
    migrate_database(db)

    def unexpected_profile_resolution():
        raise AssertionError("canonical profile must not be resolved without requested profile keys")

    monkeypatch.setattr(neb, "user_profile_path", unexpected_profile_resolution)
    result = neb.nutrition_evidence_bundle({
        "db": str(db),
        "entry_requests": [{"entry_id": 999, "fields": ["item"]}],
    })
    assert result["profile_path"] is None
    assert result["profile"] == []


def test_missing_or_unreadable_canonical_profile_is_missing_coverage_not_an_error(monkeypatch, tmp_path):
    db = tmp_path / "profile-unavailable.duckdb"
    missing = tmp_path / "missing-profile.yaml"
    unreadable = tmp_path / "unreadable-profile.yaml"
    migrate_database(db)

    monkeypatch.setattr(neb, "user_profile_path", lambda: missing)
    missing_result = neb.nutrition_evidence_bundle({
        "db": str(db),
        "profile_keys": ["nutrition_defaults.latte"],
    })
    assert missing_result["status"] == "partial"
    assert missing_result["profile"][0]["status"] == "missing"
    assert missing_result["coverage"]["missing_classes"] == ["profile"]

    unreadable.write_text("nutrition_defaults:\n  latte: house\n", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_profile_read(path, *args, **kwargs):
        if path == unreadable:
            raise PermissionError("fixture denies profile read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_profile_read)
    monkeypatch.setattr(neb, "user_profile_path", lambda: unreadable)
    unreadable_result = neb.nutrition_evidence_bundle({
        "db": str(db),
        "profile_keys": ["nutrition_defaults.latte"],
    })
    assert unreadable_result["status"] == "partial"
    assert unreadable_result["profile"][0]["status"] == "missing"
    assert unreadable_result["coverage"]["request_statuses"] == {"profile": ["missing"]}


def test_kb_root_canonicalization_does_not_follow_symlinks_outside_root(monkeypatch, tmp_path):
    db = tmp_path / "safe.duckdb"
    root = tmp_path / "recipes"
    outside = tmp_path / "outside.md"
    root.mkdir()
    outside.write_text("# Secret\n\n## Yield\nprivate\n", encoding="utf-8")
    (root / "escape.md").symlink_to(outside)
    migrate_database(db)
    monkeypatch.setattr(neb, "KB_RECIPE_ROOT", root)
    result = neb.nutrition_evidence_bundle({
        "db": str(db),
        "kb_recipe_queries": [{"query": "secret", "sections": ["Yield"]}],
    })
    assert result["kb_recipes"][0]["recipes"] == []


def test_entry_request_is_partial_when_any_selected_item_lacks_any_requested_field(tmp_path):
    db = tmp_path / "partial-fields.duckdb"; migrate_database(db)
    conn = duckdb.connect(str(db))
    conn.execute(
        """INSERT INTO nutrition_log
        (entry_id, meal_time, meal_type, meal_name, food_items, calories, protein_g, carbs_g, fat_total_g, source)
        VALUES (88, '2026-08-20 08:00:00', 'breakfast', 'mixed completeness', ?, 100, 5, 10, 4, 'row source')""",
        [json.dumps([
            {"item": "complete", "calories": 50, "protein_g": 3},
            {"item": "incomplete", "calories": 50},
        ])],
    )
    conn.close()
    result = neb.nutrition_evidence_bundle({
        "db": str(db),
        "entry_requests": [{"entry_id": 88, "fields": ["item", "calories", "protein_g", "source"]}],
    })
    request = result["entries"][0]
    assert request["status"] == "partial" and result["status"] == "partial"
    assert request["missing_fields"] == {
        "items": [
            {"item_index": 0, "fields": ["source"]},
            {"item_index": 1, "fields": ["protein_g", "source"]},
        ],
        "provenance": [],
    }
    assert result["coverage"]["request_statuses"] == {"entries": ["partial"]}
    assert result["coverage"]["missing_classes"] == ["entries"]


def test_mixed_ok_partial_and_missing_requests_aggregate_honestly(tmp_path):
    db = tmp_path / "mixed-status.duckdb"; migrate_database(db)
    conn = duckdb.connect(str(db))
    conn.execute(
        """INSERT INTO nutrition_log
        (entry_id, meal_time, meal_type, meal_name, food_items, calories, protein_g, carbs_g, fat_total_g, source)
        VALUES (89, '2026-08-20', 'snack', 'mixed', ?, 10, 1, 1, 1, 'fixture')""",
        [json.dumps([{"item": "ok", "calories": 10}, {"item": "partial"}])],
    )
    conn.close()
    result = neb.nutrition_evidence_bundle({"db": str(db), "entry_requests": [
        {"entry_id": 89, "item_terms": ["ok"], "fields": ["item", "calories"]},
        {"entry_id": 89, "item_terms": ["partial"], "fields": ["item", "calories"]},
        {"entry_id": 999, "fields": ["item"]},
    ]})
    assert [entry["status"] for entry in result["entries"]] == ["ok", "partial", "missing"]
    assert result["status"] == "partial"
    assert result["coverage"]["returned_classes"] == ["entries"]
    assert result["coverage"]["missing_classes"] == ["entries"]


def test_legacy_entry_only_query_retains_complete_baseline_row_shape_and_content(tmp_path):
    db = tmp_path / "legacy.duckdb"; migrate_database(db); _seed_entry(db)
    result = neb.nutrition_evidence_bundle({"db": str(db), "queries": [{"entry_ids": [11]}]})
    assert result["status"] == "ok" and result["results"][0]["status"] == "ok"
    legacy = result["results"][0]["entries"][0]
    conn = duckdb.connect(str(db), read_only=True)
    try:
        cursor = conn.execute("SELECT * FROM nutrition_log WHERE entry_id=11")
        columns = [column[0] for column in cursor.description]
        expected = dict(zip(columns, cursor.fetchone()))
    finally:
        conn.close()
    expected = neb._json_value(expected)
    expected["food_items"] = json.loads(expected["food_items"])
    assert legacy == {"entry_id": 11, "found": True, "entry": expected}
    assert set(legacy["entry"]) == set(columns)
    assert not ({"evidence_ref", "item_index", "provenance", "requested_fields"} & set(legacy["entry"]))
    assert all("evidence_ref" not in item and "item_index" not in item for item in legacy["entry"]["food_items"])


def test_kb_request_missing_section_is_partial_and_shared_packet_budget_is_global(monkeypatch, tmp_path):
    db = tmp_path / "kb-budget.duckdb"; migrate_database(db)
    root = tmp_path / "recipes"; root.mkdir()
    body = "\n".join(f"- ingredient {index}: {'界' * 30}" for index in range(80))
    for slug in ("one", "two", "three"):
        (root / f"{slug}.md").write_text(f"---\ntitle: {slug}\n---\n# {slug}\n\n## Formula\n{body}\n", encoding="utf-8")
    monkeypatch.setattr(neb, "KB_RECIPE_ROOT", root)
    monkeypatch.setattr(neb, "MAX_KB_PACKET_LINES", 20)
    monkeypatch.setattr(neb, "MAX_KB_PACKET_CHARS", 700)
    monkeypatch.setattr(neb, "MAX_KB_PACKET_BYTES", 900)
    request = {"db": str(db), "kb_recipe_queries": [
        {"query": "one", "sections": ["Formula", "Yield"]},
        {"query": "two", "sections": ["Formula"]},
        {"query": "three", "sections": ["Formula"]},
    ]}
    first = neb.nutrition_evidence_bundle(request)
    second = neb.nutrition_evidence_bundle(request)
    assert first == second and first["status"] == "partial"
    assert first["kb_recipes"][0]["status"] == "partial"
    assert "yield" in first["kb_recipes"][0]["missing_sections"]
    assert any(item["status"] == "partial" for item in first["kb_recipes"][1:])
    used = first["kb_packet_budget"]["used"]
    assert used["lines"] <= 20 and used["chars"] <= 700 and used["bytes"] <= 900
    assert sum(len(section["text"].encode("utf-8")) for response in first["kb_recipes"] for recipe in response["recipes"] for section in recipe["sections"]) <= 900
    assert "kb_recipes" in first["coverage"]["missing_classes"]


def test_cli_distinguishes_json_syntax_from_valid_but_invalid_request(tmp_path):
    db = tmp_path / "cli-errors.duckdb"; migrate_database(db)
    malformed = subprocess.run([sys.executable, str(SCRIPTS_DIR / "nutrition_evidence_bundle.py"), "--json", "{"], cwd=REPO_ROOT, capture_output=True, text=True)
    invalid = subprocess.run([
        sys.executable, str(SCRIPTS_DIR / "nutrition_evidence_bundle.py"), "--json",
        json.dumps({"db": str(db), "entry_requests": [{"entry_id": "eleven"}]})
    ], cwd=REPO_ROOT, capture_output=True, text=True)
    assert json.loads(malformed.stderr)["error"]["code"] == "invalid_json"
    assert json.loads(invalid.stderr)["error"]["code"] == "invalid_request"


def test_kb_split_sections_across_candidates_never_claim_query_complete(monkeypatch, tmp_path):
    db = tmp_path / "kb-split.duckdb"; migrate_database(db)
    root = tmp_path / "recipes"; root.mkdir()
    (root / "split-recipe-formula.md").write_text(
        "---\ntitle: Split Recipe Formula\n---\n# Split Recipe Formula\n\n## Formula\n- flour\n",
        encoding="utf-8",
    )
    (root / "split-recipe-yield.md").write_text(
        "---\ntitle: Split Recipe Yield\n---\n# Split Recipe Yield\n\n## Yield\none loaf\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(neb, "KB_RECIPE_ROOT", root)
    result = neb.nutrition_evidence_bundle({
        "db": str(db),
        "kb_recipe_queries": [{"query": "split recipe", "sections": ["Formula", "Yield"]}],
    })
    query = result["kb_recipes"][0]
    assert query["returned_sections"] == ["formula", "yield"]  # diagnostic union only
    assert query["complete_candidate_count"] == 0
    assert query["status"] == "partial" and query["missing_sections"] == ["formula", "yield"]
    assert result["status"] == "partial"
    by_slug = {candidate["slug"]: candidate for candidate in query["recipes"]}
    assert by_slug["split-recipe-formula"]["status"] == "partial"
    assert by_slug["split-recipe-formula"]["returned_sections"] == ["formula"]
    assert by_slug["split-recipe-formula"]["missing_sections"] == ["yield"]
    assert by_slug["split-recipe-formula"]["truncated_sections"] == []
    assert by_slug["split-recipe-yield"]["returned_sections"] == ["yield"]
    assert by_slug["split-recipe-yield"]["missing_sections"] == ["formula"]


def test_kb_one_full_candidate_makes_query_ok_without_hiding_partial_candidate(monkeypatch, tmp_path):
    db = tmp_path / "kb-full-control.duckdb"; migrate_database(db)
    root = tmp_path / "recipes"; root.mkdir()
    (root / "control-recipe-partial.md").write_text(
        "---\ntitle: Control Recipe Partial\n---\n# Control Recipe Partial\n\n## Formula\n- flour\n",
        encoding="utf-8",
    )
    (root / "control-recipe-full.md").write_text(
        "---\ntitle: Control Recipe Full\n---\n# Control Recipe Full\n\n## Formula\n- flour\n\n## Yield\none loaf\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(neb, "KB_RECIPE_ROOT", root)
    result = neb.nutrition_evidence_bundle({
        "db": str(db),
        "kb_recipe_queries": [{"query": "control recipe", "sections": ["Formula", "Yield"]}],
    })
    query = result["kb_recipes"][0]
    assert query["status"] == "ok" and query["complete_candidate_count"] == 1
    assert query["missing_sections"] == [] and result["status"] == "ok"
    by_slug = {candidate["slug"]: candidate for candidate in query["recipes"]}
    assert by_slug["control-recipe-full"]["status"] == "ok"
    assert by_slug["control-recipe-partial"]["status"] == "partial"


def test_empty_terms_and_omitted_fields_select_all_with_satisfiable_defaults(tmp_path):
    db = tmp_path / "entry-defaults.duckdb"; migrate_database(db)
    conn = duckdb.connect(str(db))
    conn.execute(
        """INSERT INTO nutrition_log
        (entry_id, meal_time, meal_type, meal_name, food_items, calories, protein_g, carbs_g, fat_total_g, source)
        VALUES (90, '2026-08-20', 'snack', 'ordinary', ?, 30, 3, 4, 1, 'fixture')""",
        [json.dumps([
            {"item": "one", "calories": 10, "protein_g": 1, "carbs_g": 2, "fat_total_g": 0},
            {"item": "two", "calories": 20, "protein_g": 2, "carbs_g": 2, "fat_total_g": 1},
        ])],
    )
    conn.close()
    result = neb.nutrition_evidence_bundle({"db": str(db), "entry_requests": [{"entry_id": 90, "item_terms": []}]})
    request = result["entries"][0]
    assert request["requested_fields"] == neb.DEFAULT_ENTRY_REQUEST_FIELDS
    assert [item["item"] for item in request["items"]] == ["one", "two"]
    assert request["status"] == "ok" and result["status"] == "ok"


def test_explicit_exhaustive_entry_fields_remain_honestly_partial(tmp_path):
    db = tmp_path / "entry-exhaustive.duckdb"; migrate_database(db)
    conn = duckdb.connect(str(db))
    conn.execute(
        """INSERT INTO nutrition_log
        (entry_id, meal_time, meal_type, meal_name, food_items, calories, protein_g, carbs_g, fat_total_g, source)
        VALUES (91, '2026-08-20', 'snack', 'ordinary', ?, 10, 1, 2, 0, 'fixture')""",
        [json.dumps([{"item": "one", "calories": 10, "protein_g": 1, "carbs_g": 2, "fat_total_g": 0}])],
    )
    conn.close()
    exhaustive = sorted(neb.ENTRY_REQUEST_FIELD_ALLOWLIST)
    result = neb.nutrition_evidence_bundle({
        "db": str(db), "entry_requests": [{"entry_id": 91, "fields": exhaustive}],
    })
    request = result["entries"][0]
    assert request["status"] == "partial" and result["status"] == "partial"
    assert request["requested_fields"] == exhaustive
    assert request["missing_fields"]["items"][0]["fields"]
