"""P0 ingest correctness tests; every database is isolated under pytest tmp_path."""
from __future__ import annotations
import ast
import importlib.util
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import duckdb
import pytest
REPO_ROOT = Path(__file__).parent.parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "log-nutrition" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
from log_nutrition import log_nutrition
from nutrition_ingest import (
    SCHEMA_VERSION, EntryIdExhaustedError, ReceiptIntegrityError, SchemaValidationError,
    _canonical_db, _canonical_row, _connect_with_retry, _digest, _legacy_v4_digest, _locked_database,
    canonical_identity, identity_from_payload, ingest_nutrition, migrate_database,
    replay_result, resolve_and_ingest_quick_text,
)
from quick_log_text import quick_log_text

def _data(name: str = "test meal") -> dict:
    return {"meal_time":"2026-08-17T08:00:00","meal_type":"breakfast","meal_name":name,
            "food_items":[{"item":"egg","portion_g":50,"calories":78}],"calories":78,
            "protein_g":6.3,"carbs_g":0.6,"fat_total_g":5.3,"source":"test"}


def _mutation_counts(db: Path) -> dict[str, int]:
    conn = duckdb.connect(str(db), read_only=True)
    counts = {
        "nutrition_rows": conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0],
        "receipts": conn.execute("SELECT COUNT(*) FROM nutrition_ingest_receipts").fetchone()[0],
        "ledgers": conn.execute("SELECT COUNT(*) FROM nutrition_ingest_identities").fetchone()[0],
        "anchors": conn.execute("SELECT COUNT(*) FROM nutrition_log WHERE ingest_provider IS NOT NULL OR ingest_message_id IS NOT NULL").fetchone()[0],
    }
    conn.close()
    return counts

@pytest.mark.parametrize("drift", [6, 100, 1000])
def test_stale_legacy_sequence_never_collides(tmp_path, drift):
    db = tmp_path / f"drift-{drift}.duckdb"; migrate_database(db)
    conn = duckdb.connect(str(db)); conn.execute("INSERT INTO nutrition_log (entry_id,meal_time,meal_name) SELECT n,'2026-01-01 08:00:00','legacy' FROM range(1, ?) AS t(n)",[drift+1]); conn.execute("UPDATE nutrition_entry_id_allocator SET next_entry_id=1"); conn.close()
    assert ingest_nutrition(db,_data())["result"]["entry"]["entry_id"] == drift + 1

def test_int32_entry_id_boundary_raises_dedicated_error_without_mutation(tmp_path):
    db=tmp_path/"limit.duckdb"; migrate_database(db)
    conn=duckdb.connect(str(db)); conn.execute("INSERT INTO nutrition_log (entry_id,meal_time) VALUES (2147483647,'2026-08-17 08:00')"); conn.close()
    with pytest.raises(EntryIdExhaustedError): ingest_nutrition(db,_data())
    conn=duckdb.connect(str(db),read_only=True); assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0] == 1; conn.close()

def test_receipt_replays_exact_committed_result_and_integrity_fails_closed(tmp_path):
    db=tmp_path/"receipt.duckdb"; migrate_database(db); identity=("discord","post-commit-1")
    first=ingest_nutrition(db,_data(),identity=identity,result_builder=lambda entry:{"status":"logged","mode":"custom","entry":entry,"reply":"persisted before delivery"})
    second=ingest_nutrition(db,_data("changed retry"),identity=identity)
    assert second["replayed"] is True and second["result"] == first["result"]
    conn=duckdb.connect(str(db)); conn.execute("UPDATE nutrition_ingest_receipts SET result_json='{bad'"); conn.close()
    with pytest.raises(ReceiptIntegrityError): replay_result(db,identity)

@pytest.mark.parametrize("damage", ["delete_receipt","delete_row","tamper_calories","tamper_name","mismatched_identity"])
def test_identity_ledger_and_canonical_row_tampering_fail_closed(tmp_path,damage):
    db=tmp_path/f"integrity-{damage}.duckdb"; migrate_database(db); identity=("discord","durable-1"); ingest_nutrition(db,_data("canonical"),identity=identity)
    conn=duckdb.connect(str(db))
    if damage == "delete_receipt": conn.execute("DELETE FROM nutrition_ingest_receipts")
    elif damage == "delete_row": conn.execute("DELETE FROM nutrition_log")
    elif damage == "tamper_calories": conn.execute("UPDATE nutrition_log SET calories=999")
    elif damage == "tamper_name": conn.execute("UPDATE nutrition_log SET meal_name='tampered'")
    else: conn.execute("UPDATE nutrition_ingest_receipts SET message_id='other'")
    conn.close()
    with pytest.raises(ReceiptIntegrityError): replay_result(db,identity)

def test_identity_replay_happens_before_invalid_retry_payload(tmp_path):
    db=tmp_path/"identity-first.duckdb"; migrate_database(db); identity=("discord","replay-invalid-payload")
    first=ingest_nutrition(db,_data("original"),identity=identity)
    retry=ingest_nutrition(db,{"provider":"discord","message_id":identity[1],"meal_time":"not-a-time"},identity=identity)
    assert retry["replayed"] is True and retry["result"] == first["result"]

def test_exact_reuse_copies_all_nutrients_preserving_null_and_zero(monkeypatch,tmp_path):
    db,data_dir=tmp_path/"exact.duckdb",tmp_path/"data"; data_dir.mkdir(); monkeypatch.setenv("HEALTH_DB_PATH",str(db)); monkeypatch.setenv("HEALTH_DATA_DIR",str(data_dir)); migrate_database(db)
    conn=duckdb.connect(str(db)); conn.execute("INSERT INTO nutrition_log (entry_id,meal_time,meal_name,food_items,calories,protein_g,carbs_g,fat_total_g,potassium_mg) VALUES (41,'2026-08-16 08:00','seed','[]',100,0,0,0,NULL)"); conn.close()
    result=quick_log_text({"meal_time":"2026-08-17T08:00:00","meal_type":"breakfast","discord_message_id":"exact-full","reuse_mode":"exact","reuse":{"mode":"exact","entry_id":"41"}})
    assert result["mode"] == "exact_reuse"
    conn=duckdb.connect(str(db),read_only=True); assert conn.execute("SELECT calories,protein_g,carbs_g,fat_total_g,potassium_mg FROM nutrition_log WHERE entry_id=?",[result["entry"]["entry_id"]]).fetchone() == (100.0,0.0,0.0,0.0,None); conn.close()

@pytest.mark.parametrize("selector", [True,False,1.0,"1.0","+1","-1"," 1","","01","999999999999999999999",0])
def test_exact_reuse_rejects_noncanonical_selectors(monkeypatch,tmp_path,selector):
    db,data_dir=tmp_path/"unsafe.duckdb",tmp_path/"data"; data_dir.mkdir(); monkeypatch.setenv("HEALTH_DB_PATH",str(db)); monkeypatch.setenv("HEALTH_DATA_DIR",str(data_dir)); migrate_database(db)
    assert quick_log_text({"meal_time":"2026-08-17T08:00:00","reuse_mode":"exact","reuse":{"mode":"exact","entry_id":selector},"discord_message_id":f"unsafe-{selector}"})["status"] == "needs_clarification"

@pytest.mark.parametrize("meal_time", ["2026-08-17","2026-08-17T08:00","2026-08-17T08:00:00.1234567","infinity"])
def test_noncanonical_timestamps_fail_closed(tmp_path,meal_time):
    db=tmp_path/"timestamp.duckdb"; migrate_database(db)
    with pytest.raises(ValueError,match="meal_time"): ingest_nutrition(db,{**_data(),"meal_time":meal_time})

def test_malformed_legacy_schema_fails_closed_without_version_marker(tmp_path):
    db=tmp_path/"bad.duckdb"; conn=duckdb.connect(str(db)); conn.execute("CREATE TABLE nutrition_log (entry_id BIGINT, meal_time DATE)"); conn.close()
    with pytest.raises(SchemaValidationError,match="supported legacy"): migrate_database(db)
    conn=duckdb.connect(str(db)); assert conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='nutrition_schema_migrations'").fetchone()[0] == 0; conn.close()

def test_bootstrap_style_initializer_compatibility(tmp_path):
    spec=importlib.util.spec_from_file_location("bootstrap_style_init",SCRIPTS_DIR/"init_nutrition.py"); module=importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(module)
    db=tmp_path/"bootstrap.duckdb"; module.init_nutrition_table(db); assert ingest_nutrition(db,_data())["result"]["entry"]["entry_id"] == 1

def test_subprocess_duplicate_delivery_uses_production_cli_once(tmp_path):
    db=tmp_path/"subprocess.duckdb"; migrate_database(db); payload=json.dumps({**_data(),"provider":"discord","message_id":"process-1"}); command=[sys.executable,str(SCRIPTS_DIR/"log_nutrition.py"),"--json",payload]; env={**os.environ,"HEALTH_DB_PATH":str(db),"PYTHONPATH":str(REPO_ROOT)}
    a=subprocess.Popen(command,cwd=REPO_ROOT,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE); b=subprocess.Popen(command,cwd=REPO_ROOT,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE); ao,ae=a.communicate(timeout=15); bo,be=b.communicate(timeout=15)
    assert (a.returncode,b.returncode)==(0,0),(ae,be); assert ao==bo
    conn=duckdb.connect(str(db),read_only=True); assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0] == 1; conn.close()



# Real historic shapes: v1 has only the meal table; v2 introduced receipt/
# ledger state; v3 added the digest.  All have the pre-v4 meal table without a
# row-level identity anchor.
_LEGACY_LOG_SQL = """CREATE TABLE nutrition_log (
 entry_id INTEGER PRIMARY KEY, meal_time TIMESTAMP NOT NULL, meal_type VARCHAR,
 meal_name VARCHAR, meal_description TEXT, food_items TEXT, calories DOUBLE,
 protein_g DOUBLE, carbs_g DOUBLE, fat_total_g DOUBLE, fat_saturated_g DOUBLE,
 fat_unsaturated_g DOUBLE, fat_trans_g DOUBLE, fiber_g DOUBLE, sugar_g DOUBLE,
 sodium_mg DOUBLE, potassium_mg DOUBLE, calcium_mg DOUBLE, iron_mg DOUBLE,
 magnesium_mg DOUBLE, vitamin_d_mcg DOUBLE, vitamin_b12_mcg DOUBLE,
 vitamin_c_mg DOUBLE, cholesterol_mg DOUBLE, source VARCHAR DEFAULT 'chat',
 logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, notes TEXT)"""


_LEGACY_RECEIPT_FIELDS = [
    "entry_id", "meal_time", "meal_type", "meal_name", "meal_description", "food_items",
    "calories", "protein_g", "carbs_g", "fat_total_g", "fat_saturated_g", "fat_unsaturated_g",
    "fat_trans_g", "fiber_g", "sugar_g", "sodium_mg", "potassium_mg", "calcium_mg", "iron_mg",
    "magnesium_mg", "vitamin_d_mcg", "vitamin_b12_mcg", "vitamin_c_mg", "cholesterol_mg", "source",
    "logged_at", "notes",
]


def _legacy_result(conn, entry_id: int) -> str:
    row = conn.execute(f"SELECT {', '.join(_LEGACY_RECEIPT_FIELDS)} FROM nutrition_log WHERE entry_id=?", [entry_id]).fetchone()
    entry = {field: (value.isoformat(sep=" ") if hasattr(value, "isoformat") else value) for field, value in zip(_LEGACY_RECEIPT_FIELDS, row)}
    return json.dumps({"version": 1, "result": {"status": "logged", "entry": entry}}, separators=(",", ":"))


def _make_legacy_fixture(db: Path, version: int, *, log_sql: str = _LEGACY_LOG_SQL) -> tuple[str, str]:
    provider, message_id, entry_id = "discord", f"legacy-v{version}", 7
    conn = duckdb.connect(str(db))
    conn.execute("CREATE SEQUENCE seq_nutrition_entry START 1")
    conn.execute(log_sql)
    conn.execute("INSERT INTO nutrition_log (entry_id,meal_time,meal_name,calories,protein_g,carbs_g,fat_total_g) VALUES (?,?,?,?,?,?,?)", [entry_id, "2026-08-16 08:00", "historic meal", 321, 20, 30, 10])
    if version >= 2:
        conn.execute("CREATE TABLE nutrition_schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE nutrition_entry_id_allocator (allocator_name VARCHAR PRIMARY KEY, next_entry_id BIGINT NOT NULL CHECK (next_entry_id > 0))")
        conn.execute("INSERT INTO nutrition_entry_id_allocator VALUES ('nutrition_log', 8)")
        if version == 2:
            conn.execute("CREATE TABLE nutrition_ingest_receipts (provider VARCHAR NOT NULL, message_id VARCHAR NOT NULL, entry_id INTEGER NOT NULL, result_json TEXT NOT NULL, committed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(provider,message_id), UNIQUE(entry_id))")
        else:
            conn.execute("CREATE TABLE nutrition_ingest_receipts (provider VARCHAR NOT NULL, message_id VARCHAR NOT NULL, entry_id INTEGER NOT NULL, result_json TEXT NOT NULL, integrity_digest VARCHAR NOT NULL DEFAULT '', committed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(provider,message_id), UNIQUE(entry_id))")
        conn.execute("CREATE TABLE nutrition_ingest_identities (provider VARCHAR NOT NULL, message_id VARCHAR NOT NULL, entry_id INTEGER NOT NULL, PRIMARY KEY(provider,message_id), UNIQUE(entry_id))")
        if version == 2:
            conn.execute("INSERT INTO nutrition_ingest_receipts (provider,message_id,entry_id,result_json) VALUES (?,?,?,?)", [provider, message_id, entry_id, _legacy_result(conn, entry_id)])
        else:
            result_json = _legacy_result(conn, entry_id)
            row = conn.execute(f"SELECT {', '.join(_LEGACY_RECEIPT_FIELDS)} FROM nutrition_log WHERE entry_id=?", [entry_id]).fetchone()
            canonical_row = dict(zip(_LEGACY_RECEIPT_FIELDS, row))
            digest = hashlib.sha256(json.dumps({"row": canonical_row, "envelope": json.loads(result_json)}, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
            conn.execute("INSERT INTO nutrition_ingest_receipts (provider,message_id,entry_id,result_json,integrity_digest) VALUES (?,?,?,?,?)", [provider, message_id, entry_id, result_json, digest])
        conn.execute("INSERT INTO nutrition_ingest_identities VALUES (?,?,?)", [provider, message_id, entry_id])
        for marker in range(1, version + 1): conn.execute("INSERT INTO nutrition_schema_migrations VALUES (?,CURRENT_TIMESTAMP)", [marker])
    conn.close()
    return provider, message_id


def _make_legacy_v4_fixture(db: Path) -> tuple[str, str]:
    provider, message_id, entry_id = "discord", "legacy-v4-literal-message", 11
    conn = duckdb.connect(str(db))
    conn.execute("CREATE SEQUENCE seq_nutrition_entry START 1")
    conn.execute(_LEGACY_LOG_SQL.replace("notes TEXT)", "notes TEXT, ingest_provider VARCHAR, ingest_message_id VARCHAR, UNIQUE(ingest_provider, ingest_message_id))"))
    conn.execute("INSERT INTO nutrition_log (entry_id,meal_time,meal_name,calories,protein_g,carbs_g,fat_total_g,ingest_provider,ingest_message_id) VALUES (?,?,?,?,?,?,?,?,?)", [entry_id, "2026-08-16 08:00", "historic v4 meal", 321, 20, 30, 10, provider, message_id])
    conn.execute("INSERT INTO nutrition_log (entry_id,meal_time,meal_name,calories) VALUES (?,?,?,?)", [12, "2026-08-16 09:00", "unidentified historic meal", 99])
    fields = [column[1] for column in conn.execute("PRAGMA table_info('nutrition_log')").fetchall()]
    row = dict(zip(fields, conn.execute(f"SELECT {', '.join(fields)} FROM nutrition_log WHERE entry_id=?", [entry_id]).fetchone()))
    entry = {field: (value.isoformat(sep=" ") if hasattr(value, "isoformat") else value) for field, value in row.items()}
    envelope = {"version": 1, "result": {"status": "logged", "entry": entry}}
    result_json = json.dumps(envelope, separators=(",", ":"))
    conn.execute("CREATE TABLE nutrition_schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE nutrition_entry_id_allocator (allocator_name VARCHAR PRIMARY KEY, next_entry_id BIGINT NOT NULL CHECK (next_entry_id > 0))")
    conn.execute("INSERT INTO nutrition_entry_id_allocator VALUES ('nutrition_log', 13)")
    conn.execute("CREATE TABLE nutrition_ingest_receipts (provider VARCHAR NOT NULL, message_id VARCHAR NOT NULL, entry_id INTEGER NOT NULL, result_json TEXT NOT NULL, integrity_digest VARCHAR NOT NULL DEFAULT '', committed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(provider,message_id), UNIQUE(entry_id))")
    conn.execute("CREATE TABLE nutrition_ingest_identities (provider VARCHAR NOT NULL, message_id VARCHAR NOT NULL, entry_id INTEGER NOT NULL, PRIMARY KEY(provider,message_id), UNIQUE(entry_id))")
    conn.execute("INSERT INTO nutrition_ingest_receipts (provider,message_id,entry_id,result_json,integrity_digest) VALUES (?,?,?,?,?)", [provider, message_id, entry_id, result_json, ""])
    committed_at = conn.execute("SELECT committed_at FROM nutrition_ingest_receipts WHERE provider=? AND message_id=?", [provider, message_id]).fetchone()[0]
    conn.execute("UPDATE nutrition_ingest_receipts SET integrity_digest=? WHERE provider=? AND message_id=?", [_legacy_v4_digest(row, envelope, (provider, message_id), entry_id, committed_at), provider, message_id])
    conn.execute("INSERT INTO nutrition_ingest_identities VALUES (?,?,?)", [provider, message_id, entry_id])
    for marker in range(1, 5):
        conn.execute("INSERT INTO nutrition_schema_migrations VALUES (?,CURRENT_TIMESTAMP)", [marker])
    conn.close()
    return provider, message_id


@pytest.mark.parametrize("version", [1, 2, 3])
def test_realistic_v1_v2_v3_migration_rebuilds_and_replays(tmp_path, version):
    db = tmp_path / f"legacy-v{version}.duckdb"
    provider, message_id = _make_legacy_fixture(db, version)
    migrate_database(db)
    conn = duckdb.connect(str(db), read_only=True)
    columns = conn.execute("PRAGMA table_info('nutrition_log')").fetchall()
    assert {column[1] for column in columns} >= {"ingest_provider", "ingest_message_id"}
    if version >= 2:
        assert conn.execute("SELECT ingest_provider,ingest_message_id FROM nutrition_log WHERE entry_id=7").fetchone() == (provider, message_id)
    conn.close()
    if version == 1:
        first = ingest_nutrition(db, _data("v1 committed"), identity=(provider, message_id))
        replay = ingest_nutrition(db, {"meal_time": "invalid"}, identity=(provider, message_id))
        assert replay["replayed"] is True and replay["result"] == first["result"]
    else:
        replay = ingest_nutrition(db, {"meal_time": "invalid"}, identity=(provider, message_id))
        assert replay["replayed"] is True and replay["result"]["entry"]["entry_id"] == 7
    conn = duckdb.connect(str(db), read_only=True)
    assert {row[0] for row in conn.execute("SELECT version FROM nutrition_schema_migrations").fetchall()} == set(range(1, SCHEMA_VERSION + 1))
    conn.close()


def test_exact_current_v4_two_part_identity_shape_migrates_to_v5(tmp_path):
    db = tmp_path / "legacy-v4-production-shape.duckdb"
    provider, message_id = _make_legacy_v4_fixture(db)
    before = _mutation_counts(db)
    migrate_database(db)
    conn = duckdb.connect(str(db), read_only=True)
    try:
        assert {row[0] for row in conn.execute("SELECT version FROM nutrition_schema_migrations").fetchall()} == set(range(1, SCHEMA_VERSION + 1))
        assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0] == before["nutrition_rows"]
        assert conn.execute("SELECT COUNT(*) FROM nutrition_ingest_receipts").fetchone()[0] == before["receipts"]
        assert conn.execute("SELECT COUNT(*) FROM nutrition_ingest_identities").fetchone()[0] == before["ledgers"]
        assert conn.execute("SELECT ingest_provider,ingest_message_id,ingest_event_key FROM nutrition_log WHERE entry_id=11").fetchone() == (provider, message_id, "default")
        assert conn.execute("SELECT provider,message_id,event_key FROM nutrition_ingest_receipts").fetchone() == (provider, message_id, "default")
        assert conn.execute("SELECT provider,message_id,event_key FROM nutrition_ingest_identities").fetchone() == (provider, message_id, "default")
    finally:
        conn.close()
    replay = ingest_nutrition(db, {"provider": provider, "message_id": message_id, "meal_time": "invalid"})
    assert replay["replayed"] is True and replay["result"]["entry"]["ingest_event_key"] == "default"


@pytest.mark.parametrize("version", [1, 2, 3])
def test_realistic_v1_v2_v3_extra_check_constraint_rolls_back_without_logical_change(tmp_path, version):
    db = tmp_path / f"legacy-v{version}-extra-check.duckdb"
    _make_legacy_fixture(
        db,
        version,
        log_sql=_LEGACY_LOG_SQL.replace("notes TEXT)", "notes TEXT, CHECK (calories >= 0))"),
    )
    before = _legacy_state(db)
    with pytest.raises(SchemaValidationError, match="legacy constraint"):
        migrate_database(db)
    assert _legacy_state(db) == before


def test_exact_nutrition_log_schema_rejects_extras_defaults_and_nullability(tmp_path):
    db = tmp_path / "schema-contract.duckdb"; migrate_database(db)
    conn = duckdb.connect(str(db))
    conn.execute("ALTER TABLE nutrition_log ADD COLUMN unexpected VARCHAR")
    conn.close()
    with pytest.raises(SchemaValidationError, match="column set"):
        ingest_nutrition(db, _data())


@pytest.mark.parametrize("change", [
    "ALTER TABLE nutrition_log ALTER source SET DEFAULT 'wrong'",
    "ALTER TABLE nutrition_log ALTER logged_at SET DEFAULT '2000-01-01 00:00:00'",
    "ALTER TABLE nutrition_log ALTER meal_time DROP NOT NULL",
])
def test_exact_nutrition_log_schema_rejects_wrong_defaults_and_nullability(tmp_path, change):
    db = tmp_path / "schema-tuple.duckdb"; migrate_database(db)
    conn = duckdb.connect(str(db))
    conn.execute("DROP INDEX idx_nutrition_meal_time")
    conn.execute("DROP INDEX idx_nutrition_meal_type")
    conn.execute(change); conn.close()
    with pytest.raises(SchemaValidationError, match="contract"):
        ingest_nutrition(db, _data())


@pytest.mark.parametrize("column,value", [
    ("entry_id", 900), ("meal_time", "2026-08-18 08:00:00"), ("meal_type", "dinner"),
    ("meal_name", "tampered"), ("meal_description", "tampered"), ("food_items", "[]"),
    ("calories", 999.0), ("protein_g", 999.0), ("carbs_g", 999.0), ("fat_total_g", 999.0),
    ("fat_saturated_g", 999.0), ("fat_unsaturated_g", 999.0), ("fat_trans_g", 999.0),
    ("fiber_g", 999.0), ("sugar_g", 999.0), ("sodium_mg", 999.0), ("potassium_mg", 999.0),
    ("calcium_mg", 999.0), ("iron_mg", 999.0), ("magnesium_mg", 999.0),
    ("vitamin_d_mcg", 999.0), ("vitamin_b12_mcg", 999.0), ("vitamin_c_mg", 999.0),
    ("cholesterol_mg", 999.0), ("source", "tampered"), ("logged_at", "2026-08-18 08:00:00"),
    ("notes", "tampered"), ("ingest_provider", "other"), ("ingest_message_id", "other"),
])
def test_digest_covers_every_certified_nutrition_log_column(tmp_path, column, value):
    db = tmp_path / f"digest-{column}.duckdb"; migrate_database(db)
    identity = ("discord", f"all-columns-{column}")
    ingest_nutrition(db, {**_data("digest all columns"), "meal_description": "original", "notes": "original", "potassium_mg": 1}, identity=identity)
    conn = duckdb.connect(str(db))
    conn.execute(f"UPDATE nutrition_log SET {column}=? WHERE ingest_provider=? AND ingest_message_id=?", [value, *identity])
    conn.close()
    with pytest.raises(ReceiptIntegrityError):
        replay_result(db, identity)


def test_simultaneous_receipt_and_ledger_deletion_is_stopped_by_row_anchor(tmp_path):
    db = tmp_path / "coordinated-delete.duckdb"; migrate_database(db)
    identity = ("discord", "coordinated-delete")
    ingest_nutrition(db, _data(), identity=identity)
    conn = duckdb.connect(str(db))
    conn.execute("DELETE FROM nutrition_ingest_receipts")
    conn.execute("DELETE FROM nutrition_ingest_identities")
    conn.close()
    with pytest.raises(ReceiptIntegrityError, match="anchor"):
        ingest_nutrition(db, _data("must not duplicate"), identity=identity)
    conn = duckdb.connect(str(db), read_only=True)
    assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0] == 1
    conn.close()


def test_connect_retry_is_limited_to_exact_duckdb_lock_signatures(monkeypatch, tmp_path):
    import nutrition_ingest
    db = tmp_path / "retry.duckdb"; migrate_database(db)
    real_connect, calls = nutrition_ingest.duckdb.connect, []
    def exact_conflict(path):
        calls.append(path)
        if len(calls) < 3:
            raise duckdb.TransactionException("TransactionContext Error: Failed to commit: Conflicting lock is held in test")
        return real_connect(path)
    monkeypatch.setattr(nutrition_ingest.duckdb, "connect", exact_conflict)
    conn = _connect_with_retry(db); conn.close()
    assert len(calls) == 3


@pytest.mark.parametrize("exc", [
    duckdb.TransactionException("TransactionContext Error: unrelated transaction failure"),
    duckdb.IOException("malformed header: Could not set lock but not a DuckDB lock conflict"),
])
def test_unrelated_connect_errors_raise_once_without_retry(monkeypatch, tmp_path, exc):
    import nutrition_ingest
    calls = []
    def fail_once(path):
        calls.append(path); raise exc
    monkeypatch.setattr(nutrition_ingest.duckdb, "connect", fail_once)
    with pytest.raises(type(exc)):
        _connect_with_retry(tmp_path / "not-used.duckdb")
    assert len(calls) == 1

def test_missing_meal_time_and_unmigrated_database_fail_closed(monkeypatch, tmp_path):
    db = tmp_path / "missing.duckdb"; monkeypatch.setenv("HEALTH_DB_PATH", str(db))
    with pytest.raises(Exception): log_nutrition(_data())
    migrate_database(db)
    with pytest.raises(ValueError, match="meal_time"):
        log_nutrition({**{k: v for k, v in _data().items() if k != "meal_time"}, "provider": "test", "message_id": "missing-time"})


def test_failed_migration_rolls_back_version_and_is_rerunnable(monkeypatch, tmp_path):
    import nutrition_ingest
    db = tmp_path / "migration-fault.duckdb"; original = nutrition_ingest._validate_tables
    monkeypatch.setattr(nutrition_ingest, "_validate_tables", lambda conn: (_ for _ in ()).throw(SchemaValidationError("injected migration failure")))
    with pytest.raises(SchemaValidationError, match="injected"): migrate_database(db)
    conn = duckdb.connect(str(db)); assert conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='nutrition_schema_migrations'").fetchone()[0] == 0; conn.close()
    monkeypatch.setattr(nutrition_ingest, "_validate_tables", original); migrate_database(db)
    with _locked_database(db): pass


def test_nutrient_text_type_schema_fails_closed(tmp_path):
    db = tmp_path / "bad-nutrient.duckdb"; conn = duckdb.connect(str(db)); conn.execute(_LEGACY_LOG_SQL.replace("calories DOUBLE", "calories VARCHAR")); conn.close()
    with pytest.raises(SchemaValidationError, match="column type"): migrate_database(db)
    conn = duckdb.connect(str(db)); assert conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='nutrition_schema_migrations'").fetchone()[0] == 0; conn.close()


def test_cross_directory_hardlinked_database_fails_closed(tmp_path):
    first_dir, second_dir = tmp_path / "one", tmp_path / "two"; first_dir.mkdir(); second_dir.mkdir()
    primary, alias = first_dir / "health.duckdb", second_dir / "health-alias.duckdb"; migrate_database(primary); os.link(primary, alias)
    with pytest.raises(SchemaValidationError, match="hard-linked nutrition databases are unsupported"): ingest_nutrition(alias, _data())


def test_cli_replay_renders_only_stored_result_for_invalid_changed_retry(tmp_path):
    db = tmp_path / "cli-fidelity.duckdb"; migrate_database(db); env = {**os.environ, "HEALTH_DB_PATH": str(db)}
    first_payload = json.dumps({**_data("committed"), "provider":"discord", "message_id":"cli-fidelity"})
    retry_payload = json.dumps({"provider":"discord", "message_id":"cli-fidelity", "meal_time":"invalid", "meal_name":"retry-name"})
    command = [sys.executable, str(SCRIPTS_DIR / "log_nutrition.py"), "--json"]
    first = subprocess.run([*command, first_payload], cwd=REPO_ROOT, env=env, text=True, capture_output=True, check=True)
    retry = subprocess.run([*command, retry_payload], cwd=REPO_ROOT, env=env, text=True, capture_output=True, check=True)
    assert retry.stdout == first.stdout


def _legacy_state(db: Path) -> tuple[str, dict[str, list[tuple]]]:
    """Both a byte fingerprint and a logical snapshot catch partial migrations."""
    conn = duckdb.connect(str(db), read_only=True)
    tables = [row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name").fetchall()]
    state = {table: conn.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall() for table in tables}
    conn.close()
    return hashlib.sha256(db.read_bytes()).hexdigest(), state


def _corrupt_legacy(db: Path, kind: str) -> None:
    version = 1 if kind == "v1_orphan_ledger" else 2 if kind in {"json_array", "semantic_object"} else 3
    provider, message_id = _make_legacy_fixture(db, version)
    conn = duckdb.connect(str(db))
    if kind == "v1_orphan_ledger":
        conn.execute("CREATE TABLE nutrition_ingest_identities (provider VARCHAR NOT NULL, message_id VARCHAR NOT NULL, entry_id INTEGER NOT NULL, PRIMARY KEY(provider,message_id), UNIQUE(entry_id))")
        conn.execute("INSERT INTO nutrition_ingest_identities VALUES ('discord','unexpected-v1-ledger',8)")
    elif kind == "json_array":
        conn.execute("UPDATE nutrition_ingest_receipts SET result_json='[]'")
    elif kind == "semantic_object":
        conn.execute("UPDATE nutrition_ingest_receipts SET result_json=?", [json.dumps({"version": 1, "result": {"status": "logged", "entry": {"entry_id": 7}}})])
    elif kind == "result_row_disagreement":
        envelope = json.loads(conn.execute("SELECT result_json FROM nutrition_ingest_receipts").fetchone()[0])
        envelope["result"]["entry"]["calories"] = 999
        conn.execute("UPDATE nutrition_ingest_receipts SET result_json=?", [json.dumps(envelope, separators=(",", ":"))])
    elif kind == "conflicting_ledger":
        conn.execute("UPDATE nutrition_ingest_identities SET entry_id=8 WHERE provider=? AND message_id=?", [provider, message_id])
    elif kind == "orphan_ledger":
        conn.execute("INSERT INTO nutrition_ingest_identities VALUES ('discord','orphan',8)")
    elif kind == "duplicate_entry_ownership":
        # A constraint-free receipt copy represents a physically possible old
        # corruption; the migration must refuse it, not deduplicate it.
        conn.execute("ALTER TABLE nutrition_ingest_receipts RENAME TO bad_receipts")
        conn.execute("CREATE TABLE nutrition_ingest_receipts AS SELECT * FROM bad_receipts")
        conn.execute("INSERT INTO nutrition_ingest_receipts SELECT 'discord','duplicate-owner',entry_id,result_json,integrity_digest,committed_at FROM bad_receipts")
        conn.execute("DROP TABLE bad_receipts")
    else:
        raise AssertionError(kind)
    conn.close()


@pytest.mark.parametrize("kind", ["v1_orphan_ledger", "json_array", "semantic_object", "result_row_disagreement", "conflicting_ledger", "orphan_ledger", "duplicate_entry_ownership"])
def test_legacy_receipt_or_ledger_corruption_rolls_back_without_byte_or_logical_change(tmp_path, kind):
    db = tmp_path / f"corrupt-{kind}.duckdb"
    _corrupt_legacy(db, kind)
    before = _legacy_state(db)
    with pytest.raises((ReceiptIntegrityError, SchemaValidationError)):
        migrate_database(db)
    assert _legacy_state(db) == before


def test_only_historical_v2_missing_ledger_is_reconstructed_after_valid_receipt_preflight(tmp_path):
    db = tmp_path / "v2-missing-ledger.duckdb"
    provider, message_id = _make_legacy_fixture(db, 2)
    conn = duckdb.connect(str(db)); conn.execute("DROP TABLE nutrition_ingest_identities"); conn.close()
    migrate_database(db)
    conn = duckdb.connect(str(db), read_only=True)
    assert conn.execute("SELECT entry_id FROM nutrition_ingest_identities WHERE provider=? AND message_id=?", [provider, message_id]).fetchone() == (7,)
    conn.close()
    assert replay_result(db, (provider, message_id))["entry"]["entry_id"] == 7


@pytest.mark.parametrize("markers", [set(), {1}, {2}, {0, 1, 2}, {-1, 1, 2}, {1, 3}, {2, 3}, {3}, {4}, {1, 2, 3}, {1, 2, 4}])
def test_missing_ledger_is_rejected_for_every_nonexact_v2_marker_set(tmp_path, markers):
    db = tmp_path / f"missing-ledger-{sorted(markers)}.duckdb"
    _make_legacy_fixture(db, 2)
    conn = duckdb.connect(str(db))
    conn.execute("DROP TABLE nutrition_ingest_identities")
    conn.execute("DELETE FROM nutrition_schema_migrations")
    for marker in markers:
        conn.execute("INSERT INTO nutrition_schema_migrations VALUES (?,CURRENT_TIMESTAMP)", [marker])
    conn.close()
    before = _legacy_state(db)
    with pytest.raises((ReceiptIntegrityError, SchemaValidationError)):
        migrate_database(db)
    assert _legacy_state(db) == before


@pytest.mark.parametrize("markers", [set(), {1}, {2}, {1, 2}, {1, 3}, {1, 2, 4}, {4}])
def test_current_shape_with_incompatible_marker_sets_is_rejected_without_mutation(tmp_path, markers):
    db = tmp_path / f"current-markers-{sorted(markers)}.duckdb"
    migrate_database(db)
    conn = duckdb.connect(str(db))
    conn.execute("DELETE FROM nutrition_schema_migrations")
    for marker in markers:
        conn.execute("INSERT INTO nutrition_schema_migrations VALUES (?,CURRENT_TIMESTAMP)", [marker])
    conn.close()
    before = _legacy_state(db)
    with pytest.raises(SchemaValidationError):
        migrate_database(db)
    assert _legacy_state(db) == before


@pytest.mark.parametrize("damage", ["delete_receipt", "delete_ledger", "tamper_receipt", "partial_anchor", "orphan_ledger"])
def test_any_current_integrity_corruption_blocks_unrelated_writes_before_mutation(tmp_path, damage):
    db = tmp_path / f"current-write-guard-{damage}.duckdb"
    migrate_database(db)
    ingest_nutrition(db, _data("guarded"), identity=("discord", "guarded-original"))
    conn = duckdb.connect(str(db))
    if damage == "delete_receipt":
        conn.execute("DELETE FROM nutrition_ingest_receipts")
    elif damage == "delete_ledger":
        conn.execute("DELETE FROM nutrition_ingest_identities")
    elif damage == "tamper_receipt":
        conn.execute("UPDATE nutrition_ingest_receipts SET result_json=?", [json.dumps({"version": 1, "result": {"status": "logged", "entry": {"entry_id": 1}}})])
    elif damage == "partial_anchor":
        conn.execute("UPDATE nutrition_log SET ingest_message_id=NULL")
    elif damage == "orphan_ledger":
        conn.execute("INSERT INTO nutrition_ingest_identities VALUES ('discord','orphan-ledger','default',2)")
    conn.close()
    before = _legacy_state(db)
    with pytest.raises((ReceiptIntegrityError, SchemaValidationError)):
        ingest_nutrition(db, _data("unrelated"), identity=("discord", "new-write"))
    assert _legacy_state(db) == before


def test_partial_v4_with_removed_marker_and_recomputed_malformed_receipt_is_not_recertified(tmp_path):
    db = tmp_path / "partial-v4-malformed.duckdb"; migrate_database(db)
    identity = ("discord", "partial-v4")
    ingest_nutrition(db, _data(), identity=identity)
    conn = duckdb.connect(str(db))
    conn.execute("DELETE FROM nutrition_schema_migrations WHERE version=4")
    row_fields = [column[1] for column in conn.execute("PRAGMA table_info('nutrition_log')").fetchall()]
    row = dict(zip(row_fields, conn.execute(f"SELECT {', '.join(row_fields)} FROM nutrition_log WHERE entry_id=1").fetchone()))
    malformed = {"version": 1, "result": {"status": "logged", "entry": {"entry_id": 1}}}
    committed_at = conn.execute("SELECT committed_at FROM nutrition_ingest_receipts").fetchone()[0]
    digest = _digest(row, malformed, identity, 1, committed_at)
    conn.execute("UPDATE nutrition_ingest_receipts SET result_json=?, integrity_digest=?", [json.dumps(malformed, separators=(",", ":")), digest])
    conn.close()
    before = _legacy_state(db)
    with pytest.raises(ReceiptIntegrityError, match="canonical nutrition row"):
        migrate_database(db)
    assert _legacy_state(db) == before


@pytest.mark.parametrize("payload", [
    pytest.param({"provider": "discord", "message_id": "payload-conflict"}, id="conflicting-top-level"),
    pytest.param({"provider": "discord"}, id="partial-top-level"),
    pytest.param({"ingest_identity": {"provider": "discord", "message_id": "explicit", "extra": "x"}}, id="malformed-nested-extra"),
    pytest.param({"discord_message_id": " explicit"}, id="padded-discord-id"),
])
def test_explicit_identity_never_suppresses_payload_identity_validation_before_mutation(tmp_path, payload):
    db = tmp_path / "explicit-payload-boundary.duckdb"; migrate_database(db)
    before = _mutation_counts(db)
    with pytest.raises(ValueError):
        ingest_nutrition(db, {**_data(), **payload}, identity=("discord", "explicit"))
    assert _mutation_counts(db) == before


@pytest.mark.parametrize("payload,identity", [
    pytest.param({"provider": "discord", "message_id": "equivalent"}, ("discord", "equivalent", "default"), id="equivalent-explicit-and-payload"),
    pytest.param({}, ("discord", "explicit-only"), id="absent-payload-explicit"),
])
def test_explicit_identity_equivalent_or_absent_payload_commits_exactly_once(tmp_path, payload, identity):
    db = tmp_path / "explicit-payload-success.duckdb"; migrate_database(db)
    before = _mutation_counts(db)
    result = ingest_nutrition(db, {**_data(), **payload}, identity=identity)
    assert result["replayed"] is False
    after = _mutation_counts(db)
    assert after == {key: before[key] + 1 for key in before}


@pytest.mark.parametrize("identity", [
    ("", "id"), (" discord", "id"), ("discord ", "id"), ("discord", " id"), ("discord", "id "),
    ("discord", 123), (123, "id"), ("x" * 129, "id"), ("discord", "x" * 513),
])
def test_public_identity_boundaries_reject_noncanonical_values_before_writes(tmp_path, identity):
    db = tmp_path / "bad-public-identity.duckdb"; migrate_database(db)
    with pytest.raises(ValueError):
        canonical_identity(identity)  # shared boundary validator itself
    with pytest.raises(ValueError):
        ingest_nutrition(db, _data(), identity=identity)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        replay_result(db, identity)  # type: ignore[arg-type]
    payload = {"meal_time": "2026-08-17T08:00:00", "provider": identity[0], "message_id": identity[1], "items": []}
    with pytest.raises(ValueError):
        resolve_and_ingest_quick_text(db, payload)
    conn = duckdb.connect(str(db), read_only=True)
    assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("payload,expected", [
    ({"discord_message_id": "m1"}, ("discord", "m1", "default")),
    ({"provider": "discord", "message_id": "m1"}, ("discord", "m1", "default")),
    ({"ingest_identity": {"provider": "discord", "message_id": "m1"}}, ("discord", "m1", "default")),
    ({"discord_message_id": "m1", "provider": "discord", "message_id": "m1", "ingest_identity": {"provider": "discord", "message_id": "m1"}}, ("discord", "m1", "default")),
])
def test_identity_from_payload_accepts_only_complete_equivalent_canonical_forms(payload, expected):
    assert identity_from_payload(payload) == expected


@pytest.mark.parametrize("payload", [
    {"discord_message_id": "m1", "provider": "", "message_id": "m1"},  # do not ignore malformed top-level form
    {"discord_message_id": "m1", "provider": "discord"},  # do not ignore partial top-level form
    {"discord_message_id": "m1", "provider": "slack", "message_id": "m1"},
    {"discord_message_id": "m1", "provider": "discord", "message_id": "m2"},
    {"provider": "discord"},
    {"message_id": "m1"},
    {"provider": "discord", "ingest_identity": {"message_id": "m1"}},  # no top/nested mixing
    {"provider": "discord", "message_id": "m1", "ingest_identity": {"provider": "discord"}},
    {"provider": "discord", "message_id": "m1", "ingest_identity": {"provider": "discord", "message_id": "m1", "extra": "x"}},
    {"provider": "discord", "message_id": "m1", "ingest_identity": {"provider": "discord", "message_id": "m2"}},
    {"ingest_identity": None},
    {"ingest_identity": []},
    {"discord_message_id": 123},
    {"discord_message_id": ""},
    {"discord_message_id": " m1"},
    {"discord_message_id": "m1 "},
    {"discord_message_id": "x" * 513},
    {"provider": 123, "message_id": "m1"},
    {"provider": "discord", "message_id": 123},
    {"provider": " discord", "message_id": "m1"},
    {"provider": "x" * 129, "message_id": "m1"},
])
def test_identity_from_payload_rejects_malformed_ambiguous_or_noncanonical_forms_before_mutation(tmp_path, payload):
    db = tmp_path / "identity-matrix.duckdb"; migrate_database(db)
    with pytest.raises(ValueError):
        identity_from_payload(payload)
    with pytest.raises(ValueError):
        ingest_nutrition(db, {**_data(), **payload}, identity=identity_from_payload({**_data(), **payload}))
    conn = duckdb.connect(str(db), read_only=True)
    assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0] == 0
    conn.close()


def _damage_support_contract(conn: duckdb.DuckDBPyConnection, damage: str) -> None:
    if damage == "allocator_default_drift":
        conn.execute("ALTER TABLE nutrition_entry_id_allocator ALTER next_entry_id SET DEFAULT 1")
    elif damage == "allocator_nullability_drift":
        conn.execute("ALTER TABLE nutrition_entry_id_allocator ALTER next_entry_id DROP NOT NULL")
    elif damage == "allocator_pk_missing":
        conn.execute("ALTER TABLE nutrition_entry_id_allocator RENAME TO bad_allocator")
        conn.execute("CREATE TABLE nutrition_entry_id_allocator (allocator_name VARCHAR NOT NULL, next_entry_id BIGINT NOT NULL CHECK (next_entry_id > 0))")
        conn.execute("INSERT INTO nutrition_entry_id_allocator SELECT allocator_name,next_entry_id FROM bad_allocator")
        conn.execute("DROP TABLE bad_allocator")
    elif damage == "allocator_check_missing":
        conn.execute("ALTER TABLE nutrition_entry_id_allocator RENAME TO bad_allocator")
        conn.execute("CREATE TABLE nutrition_entry_id_allocator (allocator_name VARCHAR PRIMARY KEY, next_entry_id BIGINT NOT NULL)")
        conn.execute("INSERT INTO nutrition_entry_id_allocator SELECT allocator_name,next_entry_id FROM bad_allocator")
        conn.execute("DROP TABLE bad_allocator")
    elif damage == "allocator_extra_constraint":
        conn.execute("ALTER TABLE nutrition_entry_id_allocator RENAME TO bad_allocator")
        conn.execute("CREATE TABLE nutrition_entry_id_allocator (allocator_name VARCHAR PRIMARY KEY CHECK (allocator_name='nutrition_log'), next_entry_id BIGINT NOT NULL CHECK (next_entry_id > 0))")
        conn.execute("INSERT INTO nutrition_entry_id_allocator SELECT allocator_name,next_entry_id FROM bad_allocator")
        conn.execute("DROP TABLE bad_allocator")
    elif damage == "allocator_extra_row":
        conn.execute("INSERT INTO nutrition_entry_id_allocator VALUES ('other', 1)")
    elif damage == "migration_default_drift":
        conn.execute("ALTER TABLE nutrition_schema_migrations ALTER applied_at SET DEFAULT '2000-01-01 00:00:00'")
    elif damage == "migration_nullability_drift":
        conn.execute("ALTER TABLE nutrition_schema_migrations ALTER applied_at DROP NOT NULL")
    elif damage == "migration_pk_missing":
        conn.execute("ALTER TABLE nutrition_schema_migrations RENAME TO bad_migrations")
        conn.execute("CREATE TABLE nutrition_schema_migrations (version INTEGER NOT NULL, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO nutrition_schema_migrations SELECT version,applied_at FROM bad_migrations")
        conn.execute("DROP TABLE bad_migrations")
    elif damage == "migration_extra_constraint":
        conn.execute("ALTER TABLE nutrition_schema_migrations RENAME TO bad_migrations")
        conn.execute("CREATE TABLE nutrition_schema_migrations (version INTEGER PRIMARY KEY CHECK (version > 0), applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("INSERT INTO nutrition_schema_migrations SELECT version,applied_at FROM bad_migrations")
        conn.execute("DROP TABLE bad_migrations")
    elif damage == "receipt_default_drift":
        conn.execute("ALTER TABLE nutrition_ingest_receipts ALTER integrity_digest SET DEFAULT 'bad'")
    elif damage == "ledger_extra_constraint":
        conn.execute("ALTER TABLE nutrition_ingest_identities RENAME TO bad_identities")
        conn.execute("CREATE TABLE nutrition_ingest_identities (provider VARCHAR NOT NULL CHECK (provider <> ''), message_id VARCHAR NOT NULL, entry_id INTEGER NOT NULL, PRIMARY KEY(provider,message_id), UNIQUE(entry_id))")
        conn.execute("INSERT INTO nutrition_ingest_identities SELECT provider,message_id,entry_id FROM bad_identities")
        conn.execute("DROP TABLE bad_identities")
    else:
        raise AssertionError(damage)


@pytest.mark.parametrize("damage", [
    "allocator_default_drift", "allocator_nullability_drift", "allocator_pk_missing",
    "allocator_check_missing", "allocator_extra_constraint", "allocator_extra_row",
    "migration_default_drift", "migration_nullability_drift", "migration_pk_missing",
    "migration_extra_constraint", "receipt_default_drift", "ledger_extra_constraint",
])
def test_support_table_contract_drift_blocks_write_replay_and_migration_without_nutrition_mutation(tmp_path, damage):
    db = tmp_path / f"support-contract-{damage}.duckdb"; migrate_database(db)
    identity = ("discord", f"support-{damage}")
    ingest_nutrition(db, _data("support seed"), identity=identity)
    conn = duckdb.connect(str(db)); _damage_support_contract(conn, damage); conn.close()
    before = _legacy_state(db)
    with pytest.raises((SchemaValidationError, ReceiptIntegrityError)):
        ingest_nutrition(db, _data("must not commit"), identity=("discord", f"new-{damage}"))
    assert _legacy_state(db) == before
    with pytest.raises((SchemaValidationError, ReceiptIntegrityError)):
        replay_result(db, identity)
    assert _legacy_state(db) == before
    with pytest.raises((SchemaValidationError, ReceiptIntegrityError)):
        migrate_database(db)
    assert _legacy_state(db) == before


@pytest.mark.parametrize("damage", ["receipt_whitespace", "ledger_blank", "ledger_nonstring", "anchor_partial", "anchor_overlong"])
def test_migration_rejects_noncanonical_receipt_ledger_and_anchor_identity_without_mutation(tmp_path, damage):
    db = tmp_path / f"bad-migration-identity-{damage}.duckdb"; migrate_database(db)
    identity = ("discord", "identity-preflight")
    ingest_nutrition(db, _data(), identity=identity)
    conn = duckdb.connect(str(db))
    if damage == "receipt_whitespace":
        conn.execute("UPDATE nutrition_ingest_receipts SET provider=' discord'")
    elif damage == "ledger_blank":
        conn.execute("UPDATE nutrition_ingest_identities SET message_id=''")
    elif damage == "ledger_nonstring":
        conn.execute("ALTER TABLE nutrition_ingest_identities RENAME TO bad_ledger")
        conn.execute("CREATE TABLE nutrition_ingest_identities (provider INTEGER NOT NULL, message_id VARCHAR NOT NULL, entry_id INTEGER NOT NULL, PRIMARY KEY(provider,message_id), UNIQUE(entry_id))")
        conn.execute("INSERT INTO nutrition_ingest_identities SELECT 1,message_id,entry_id FROM bad_ledger")
        conn.execute("DROP TABLE bad_ledger")
    elif damage == "anchor_partial":
        conn.execute("UPDATE nutrition_log SET ingest_message_id=NULL")
    else:
        conn.execute("UPDATE nutrition_log SET ingest_provider=?", ["x" * 129])
    conn.close()
    before = _legacy_state(db)
    with pytest.raises((ReceiptIntegrityError, SchemaValidationError)):
        migrate_database(db)
    assert _legacy_state(db) == before


P0_FIXTURE = REPO_ROOT / "skills" / "log-nutrition" / "evals" / "fixtures" / "p0-executable.json"
P0_FIXTURE_SHA256 = "fbe4901d25c0f86b84dd2d3ac7e000ea04af07dd67195d3d40d2beb0374a1abb"
P0_REQUIRED_CASE_IDS = frozenset({"clarification-no-items", "exact-reuse", "sequential-replay"})


def _p0_cases_from_bytes(raw: bytes) -> list[dict]:
    assert hashlib.sha256(raw).hexdigest() == P0_FIXTURE_SHA256, "p0 executable fixture changed without updating its external lock"
    fixture = json.loads(raw)
    assert set(fixture) == {"schema_version", "isolated_db_only", "cases"}
    assert fixture["schema_version"] == 2 and fixture["isolated_db_only"] is True
    assert isinstance(fixture["cases"], list) and fixture["cases"]
    assert all(isinstance(case, dict) for case in fixture["cases"])
    ids = [case.get("id") for case in fixture["cases"]]
    assert all(isinstance(case_id, str) and case_id for case_id in ids)
    assert len(ids) == len(fixture["cases"]) == len(set(ids)) and set(ids) == P0_REQUIRED_CASE_IDS
    for case in fixture["cases"]:
        assert isinstance(case.get("operation"), str)
        assert case["operation"] in {"quick", "ingest_twice"}
        expected_keys = {"id", "operation", "seed", "payload", "expected"}
        if case["operation"] == "ingest_twice":
            expected_keys.add("retry_payload")
        assert set(case) == expected_keys
        assert isinstance(case["seed"], list) and all(isinstance(seed, dict) and seed for seed in case["seed"])
        assert isinstance(case["payload"], dict) and case["payload"]
        assert set(case["expected"]) == {"result", "nutrition_rows", "receipts", "ledgers", "anchors"}
        assert isinstance(case["expected"]["result"], dict) and isinstance(case["expected"]["result"].get("status"), str)
        assert all(isinstance(case["expected"][key], int) and case["expected"][key] >= 0 for key in ("nutrition_rows", "receipts", "ledgers", "anchors"))
        if case["operation"] == "ingest_twice":
            assert isinstance(case["retry_payload"], dict) and case["retry_payload"]
    return fixture["cases"]


def _p0_cases() -> list[dict]:
    return _p0_cases_from_bytes(P0_FIXTURE.read_bytes())


def test_p0_executable_external_whole_file_lock_rejects_metadata_and_case_mutation():
    raw = P0_FIXTURE.read_bytes()
    assert _p0_cases_from_bytes(raw)
    with pytest.raises(AssertionError, match="external lock"):
        _p0_cases_from_bytes(raw.replace(b'"schema_version": 2', b'"schema_version": 3', 1))
    with pytest.raises(AssertionError, match="external lock"):
        _p0_cases_from_bytes(raw.replace(b'"fixture-replay"', b'"fixture-replax"', 1))


@pytest.mark.parametrize("case", _p0_cases(), ids=lambda case: case["id"])
def test_p0_executable_fixture_runs_on_isolated_db_and_replays_byte_identically(tmp_path, case):
    db = tmp_path / f"{case['id']}.duckdb"
    migrate_database(db)
    conn = duckdb.connect(str(db))
    for seed in case["seed"]:
        fields, values = list(seed), list(seed.values())
        conn.execute(f"INSERT INTO nutrition_log ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})", values)
    conn.close()
    payload = case["payload"]
    if case["operation"] == "quick":
        first = resolve_and_ingest_quick_text(db, payload)
        second = resolve_and_ingest_quick_text(db, payload)
    elif case["operation"] == "ingest_twice":
        identity = identity_from_payload(payload)
        first = ingest_nutrition(db, payload, identity=identity)["result"]
        second = ingest_nutrition(db, case["retry_payload"], identity=identity)
        assert second["replayed"] is True
        second = second["result"]
    else:
        raise AssertionError(f"unsupported fixture operation: {case['operation']}")
    assert json.dumps(second, sort_keys=True, separators=(",", ":")) == json.dumps(first, sort_keys=True, separators=(",", ":"))
    expected = case["expected"]
    conn = duckdb.connect(str(db), read_only=True)
    assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0] == expected["nutrition_rows"]
    assert conn.execute("SELECT COUNT(*) FROM nutrition_ingest_receipts").fetchone()[0] == expected["receipts"]
    assert conn.execute("SELECT COUNT(*) FROM nutrition_ingest_identities").fetchone()[0] == expected["ledgers"]
    assert conn.execute("SELECT COUNT(*) FROM nutrition_log WHERE ingest_provider IS NOT NULL OR ingest_message_id IS NOT NULL").fetchone()[0] == expected["anchors"]
    identity = identity_from_payload(payload)
    if identity and expected["receipts"]:
        entry_id = first["entry"]["entry_id"]
        assert conn.execute("SELECT entry_id FROM nutrition_ingest_receipts WHERE provider=? AND message_id=? AND event_key=?", list(identity)).fetchone() == (entry_id,)
        assert conn.execute("SELECT entry_id FROM nutrition_ingest_identities WHERE provider=? AND message_id=? AND event_key=?", list(identity)).fetchone() == (entry_id,)
        assert conn.execute("SELECT entry_id FROM nutrition_log WHERE ingest_provider=? AND ingest_message_id=? AND ingest_event_key=?", list(identity)).fetchone() == (entry_id,)
        fields = [column[1] for column in conn.execute("PRAGMA table_info('nutrition_log')").fetchall()]
        row = dict(zip(fields, conn.execute(f"SELECT {', '.join(fields)} FROM nutrition_log WHERE entry_id=?", [entry_id]).fetchone()))
        canonical_row = {key: (value.isoformat(sep=" ") if hasattr(value, "isoformat") else value) for key, value in row.items()}
        assert first["entry"] == canonical_row
        envelope, digest, committed_at = conn.execute("SELECT result_json,integrity_digest,committed_at FROM nutrition_ingest_receipts WHERE provider=? AND message_id=? AND event_key=?", list(identity)).fetchone()
        assert json.loads(envelope) == {"version": 1, "result": first}
        assert digest == _digest(row, json.loads(envelope), identity, entry_id, committed_at)
    actual = json.loads(json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))
    if "entry" in actual:
        assert isinstance(actual["entry"].get("logged_at"), str) and actual["entry"]["logged_at"]
        actual["entry"]["logged_at"] = "$persisted_timestamp"
    assert actual == expected["result"]
    conn.close()


CACHE_DB_PATH_ALLOWLIST = frozenset({"cache_path", "cache_db_path", "CACHE_DB_PATH", "usda_cache_path"})
CACHE_PATH_WRAPPERS = frozenset({"str", "Path"})


class _RawConnectScope:
    def __init__(
        self,
        duckdb_modules: set[str],
        duckdb_connects: set[str],
        migrate_modules: set[str],
        migrate_calls: set[str],
        connector_factories: set[str] | None = None,
    ):
        self.duckdb_modules = set(duckdb_modules)
        self.duckdb_connects = set(duckdb_connects)
        self.migrate_modules = set(migrate_modules)
        self.migrate_calls = set(migrate_calls)
        self.connector_factories = set(connector_factories or set())

    def clone(self) -> "_RawConnectScope":
        return _RawConnectScope(
            self.duckdb_modules,
            self.duckdb_connects,
            self.migrate_modules,
            self.migrate_calls,
            self.connector_factories,
        )


def _is_duckdb_module_expr(expr: ast.AST, scope: _RawConnectScope) -> bool:
    return isinstance(expr, ast.Name) and expr.id in scope.duckdb_modules


def _is_migrate_func_expr(expr: ast.AST, scope: _RawConnectScope) -> bool:
    if isinstance(expr, ast.Name):
        return expr.id in scope.migrate_calls
    if isinstance(expr, ast.Attribute) and expr.attr in scope.migrate_calls:
        return True
    return isinstance(expr, ast.Attribute) and expr.attr == "migrate_database" and isinstance(expr.value, ast.Name) and expr.value.id in scope.migrate_modules


def _is_duckdb_connect_expr(expr: ast.AST, scope: _RawConnectScope) -> bool:
    if isinstance(expr, ast.Name):
        return expr.id in scope.duckdb_connects
    if isinstance(expr, ast.Attribute) and expr.attr == "connect":
        return _is_duckdb_module_expr(expr.value, scope)
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id == "getattr"
        and len(expr.args) >= 2
        and _is_duckdb_module_expr(expr.args[0], scope)
        and isinstance(expr.args[1], ast.Constant)
        and expr.args[1].value == "connect"
    ):
        return True
    if isinstance(expr, ast.IfExp):
        return _is_duckdb_connect_expr(expr.body, scope) or _is_duckdb_connect_expr(expr.orelse, scope)
    if isinstance(expr, ast.Call):
        return _call_returns_connector(expr, scope)
    return False


def _call_returns_connector(call: ast.Call, scope: _RawConnectScope) -> bool:
    return bool(_helper_callee_names(call) & scope.connector_factories)


def _is_migrate_call(call: ast.Call, scope: _RawConnectScope) -> bool:
    return _is_migrate_func_expr(call.func, scope)


def _helper_callee_names(call: ast.Call) -> set[str]:
    func = call.func
    if isinstance(func, ast.Name):
        return {func.id}
    if isinstance(func, ast.Attribute):
        return {func.attr}
    if isinstance(func, ast.Call):
        return _helper_callee_names(func)
    return set()


def _has_read_only_true(call: ast.Call) -> bool:
    return any(keyword.arg == "read_only" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True for keyword in call.keywords)


def _connect_path_expr(call: ast.Call) -> ast.AST | None:
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg in {"database", "path"}:
            return keyword.value
    return None


def _is_cache_name(expr: ast.AST) -> bool:
    return isinstance(expr, ast.Name) and expr.id in CACHE_DB_PATH_ALLOWLIST


def _is_safe_path_wrapper(func: ast.AST) -> bool:
    return (
        isinstance(func, ast.Name)
        and func.id in CACHE_PATH_WRAPPERS
    ) or (
        isinstance(func, ast.Attribute)
        and func.attr == "Path"
        and isinstance(func.value, ast.Name)
        and func.value.id == "pathlib"
    )


def _references_allowed_cache_path(expr: ast.AST | None) -> bool:
    if expr is None:
        return False
    if _is_cache_name(expr):
        return True
    if isinstance(expr, ast.Call) and _is_safe_path_wrapper(expr.func) and len(expr.args) == 1 and not expr.keywords:
        return _is_cache_name(expr.args[0])
    return False


def _is_writable_connect_call(call: ast.Call, scope: _RawConnectScope) -> bool:
    return _is_duckdb_connect_expr(call.func, scope) and not _has_read_only_true(call) and not _references_allowed_cache_path(_connect_path_expr(call))


def _target_names(targets: list[ast.expr]) -> set[str]:
    return {node.id for target in targets for node in ast.walk(target) if isinstance(node, ast.Name)}


def _aliases_connector_factory(value: ast.AST, scope: _RawConnectScope) -> bool:
    if isinstance(value, ast.Name):
        return value.id in scope.connector_factories
    if isinstance(value, ast.IfExp):
        return _aliases_connector_factory(value.body, scope) or _aliases_connector_factory(value.orelse, scope)
    return False


def _update_raw_connect_aliases(stmt: ast.stmt, scope: _RawConnectScope) -> None:
    if isinstance(stmt, ast.Import):
        for alias in stmt.names:
            if alias.name == "duckdb":
                scope.duckdb_modules.add(alias.asname or alias.name)
            elif alias.name == "nutrition_ingest":
                scope.migrate_modules.add(alias.asname or alias.name)
    elif isinstance(stmt, ast.ImportFrom):
        if stmt.module == "duckdb":
            for alias in stmt.names:
                if alias.name == "connect":
                    scope.duckdb_connects.add(alias.asname or alias.name)
        elif stmt.module == "nutrition_ingest":
            for alias in stmt.names:
                if alias.name == "migrate_database":
                    scope.migrate_calls.add(alias.asname or alias.name)

    assignments: list[ast.Assign | ast.AnnAssign] = []
    if isinstance(stmt, ast.Assign):
        assignments.append(stmt)
    elif isinstance(stmt, ast.AnnAssign):
        assignments.append(stmt)
    for assignment in assignments:
        value = assignment.value
        if value is None:
            continue
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        aliases_duckdb_module = _is_duckdb_module_expr(value, scope)
        aliases_duckdb_connect = _is_duckdb_connect_expr(value, scope)
        aliases_migrate = _is_migrate_func_expr(value, scope)
        aliases_factory = _aliases_connector_factory(value, scope)
        for name in _target_names(targets):
            if aliases_duckdb_module:
                scope.duckdb_modules.add(name)
            if aliases_duckdb_connect:
                scope.duckdb_connects.add(name)
            if aliases_migrate:
                scope.migrate_calls.add(name)
            if aliases_factory:
                scope.connector_factories.add(name)


def _function_defs(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _contains_migration(stmt: ast.stmt, scope: _RawConnectScope) -> bool:
    return any(isinstance(node, ast.Call) and _is_migrate_call(node, scope) for node in ast.walk(stmt))


def _direct_writable_connects(stmt: ast.stmt, scope: _RawConnectScope) -> list[ast.Call]:
    return [node for node in ast.walk(stmt) if isinstance(node, ast.Call) and _is_writable_connect_call(node, scope)]


def _function_summaries(
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    module_scope: _RawConnectScope,
) -> tuple[set[str], set[str], set[str], dict[str, set[str]]]:
    migrates: set[str] = set()
    writable_openers: set[str] = set()
    connector_factories: set[str] = set()
    call_graph: dict[str, set[str]] = {name: set() for name in functions}

    changed = True
    while changed:
        changed = False
        for name, function in functions.items():
            scope = module_scope.clone()
            scope.migrate_calls.update(migrates)
            scope.connector_factories.update(connector_factories)
            function_migrates = False
            function_opens = False
            function_returns_connector = False
            callees: set[str] = set()
            for stmt in function.body:
                for call in [node for node in ast.walk(stmt) if isinstance(node, ast.Call)]:
                    names = _helper_callee_names(call) & set(functions)
                    callees.update(name for name in names if name != function.name)
                if _contains_migration(stmt, scope):
                    function_migrates = True
                if _direct_writable_connects(stmt, scope):
                    function_opens = True
                if any(isinstance(node, ast.Call) and (_helper_callee_names(node) & writable_openers) for node in ast.walk(stmt)):
                    function_opens = True
                if isinstance(stmt, ast.Return) and stmt.value is not None and _is_duckdb_connect_expr(stmt.value, scope):
                    function_returns_connector = True
                _update_raw_connect_aliases(stmt, scope)
            if not callees.issubset(call_graph[name]):
                call_graph[name].update(callees)
                changed = True
            if function_migrates and name not in migrates:
                migrates.add(name)
                changed = True
            if function_opens and name not in writable_openers:
                writable_openers.add(name)
                changed = True
            if function_returns_connector and name not in connector_factories:
                connector_factories.add(name)
                changed = True
    return migrates, writable_openers, connector_factories, call_graph


def _offenders_in_ordered_body(
    body: list[ast.stmt],
    scope: _RawConnectScope,
    filename: str,
    context: str,
    writable_helpers: set[str],
    connector_factories: set[str],
) -> list[str]:
    offenders: list[str] = []
    migrated = False
    unsafe_helpers = writable_helpers | connector_factories
    scope.connector_factories.update(connector_factories)
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _update_raw_connect_aliases(stmt, scope)
            continue
        direct_connects = _direct_writable_connects(stmt, scope)
        helper_calls = [
            call for call in ast.walk(stmt)
            if isinstance(call, ast.Call) and _helper_callee_names(call) & unsafe_helpers
        ]
        contains_migration = _contains_migration(stmt, scope)
        for _ in direct_connects:
            offenders.append(f"{filename}:{context}:writable raw DuckDB connect bypasses shared path lock")
        for call in helper_calls:
            helper = sorted(_helper_callee_names(call) & unsafe_helpers)[0]
            offenders.append(f"{filename}:{context}:helper {helper} may open or return writable DuckDB connector")
        if contains_migration:
            migrated = True
        _update_raw_connect_aliases(stmt, scope)
    return offenders


def _raw_connect_policy_offenders(source: str, filename: str = "<snippet>") -> list[str]:
    tree = ast.parse(source, filename=filename)
    module_scope = _RawConnectScope({"duckdb"}, set(), {"nutrition_ingest"}, {"migrate_database"})
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "nutrition_ingest":
            offenders.extend(f"{filename}:{alias.name}" for alias in node.names if alias.name.startswith("_"))
    for stmt in tree.body if isinstance(tree, ast.Module) else []:
        _update_raw_connect_aliases(stmt, module_scope)

    functions = _function_defs(tree)
    migrating_helpers: set[str] = set()
    writable_helpers: set[str] = set()
    connector_factories: set[str] = set()
    call_graph: dict[str, set[str]] = {}
    changed = True
    while changed:
        before = (
            frozenset(module_scope.migrate_calls),
            frozenset(module_scope.connector_factories),
            frozenset(migrating_helpers),
            frozenset(writable_helpers),
            frozenset(connector_factories),
        )
        module_scope.migrate_calls.update(migrating_helpers)
        module_scope.connector_factories.update(connector_factories)
        alias_changed = True
        while alias_changed:
            alias_before = (
                frozenset(module_scope.duckdb_modules),
                frozenset(module_scope.duckdb_connects),
                frozenset(module_scope.migrate_calls),
                frozenset(module_scope.connector_factories),
            )
            for stmt in tree.body if isinstance(tree, ast.Module) else []:
                _update_raw_connect_aliases(stmt, module_scope)
            alias_after = (
                frozenset(module_scope.duckdb_modules),
                frozenset(module_scope.duckdb_connects),
                frozenset(module_scope.migrate_calls),
                frozenset(module_scope.connector_factories),
            )
            alias_changed = alias_after != alias_before
        migrating_helpers, writable_helpers, connector_factories, call_graph = _function_summaries(functions, module_scope)
        after = (
            frozenset(module_scope.migrate_calls),
            frozenset(module_scope.connector_factories),
            frozenset(migrating_helpers),
            frozenset(writable_helpers),
            frozenset(connector_factories),
        )
        changed = after != before
    analysis_scope = module_scope.clone()
    analysis_scope.migrate_calls.update(migrating_helpers)
    analysis_scope.connector_factories.update(connector_factories)
    if isinstance(tree, ast.Module):
        offenders.extend(_offenders_in_ordered_body(tree.body, analysis_scope.clone(), filename, "<module>", writable_helpers, connector_factories))
    for function in functions.values():
        function_scope = analysis_scope.clone()
        offenders.extend(_offenders_in_ordered_body(function.body, function_scope, filename, function.name, writable_helpers, connector_factories))
    offenders.extend(f"{filename}:{name}:returns writable DuckDB connector factory" for name in sorted(connector_factories))
    return list(dict.fromkeys(offenders))


@pytest.mark.parametrize("source", [
    pytest.param(
        "import duckdb\nfrom nutrition_ingest import migrate_database\ndef f(db_path):\n    migrate_database(db_path)\n    duckdb.connect(str(db_path))\n",
        id="direct-module-call",
    ),
    pytest.param(
        "import duckdb as dbapi\nfrom nutrition_ingest import migrate_database\ndef f(db_path):\n    migrate_database(db_path)\n    dbapi.connect(str(db_path))\n",
        id="import-duckdb-as",
    ),
    pytest.param(
        "from duckdb import connect\nfrom nutrition_ingest import migrate_database\ndef f(db_path):\n    migrate_database(db_path)\n    connect(str(db_path))\n",
        id="from-import-connect",
    ),
    pytest.param(
        "from duckdb import connect as open_db\nfrom nutrition_ingest import migrate_database\ndef f(db_path):\n    migrate_database(db_path)\n    open_db(str(db_path))\n",
        id="from-import-connect-as",
    ),
    pytest.param(
        "import duckdb\nfrom nutrition_ingest import migrate_database\nopen_db = duckdb.connect\nopen_again = open_db\ndef f(db_path):\n    migrate_database(db_path)\n    open_again(str(db_path))\n",
        id="callable-alias-chain",
    ),
    pytest.param(
        "import duckdb\nfrom nutrition_ingest import migrate_database\ndef helper(db_path):\n    return duckdb.connect(str(db_path))\ndef f(db_path):\n    migrate_database(db_path)\n    return helper(db_path)\n",
        id="helper-return-cross-function",
    ),
    pytest.param(
        "import duckdb\nfrom nutrition_ingest import migrate_database\ndef helper(db_path):\n    conn = duckdb.connect(str(db_path))\n    return conn.execute('select 1')\ndef f(db_path):\n    migrate_database(db_path)\n    helper(db_path)\n",
        id="helper-call-cross-function",
    ),
    pytest.param(
        "import duckdb\nfrom nutrition_ingest import migrate_database\nclass Runner:\n    def helper(self, db_path):\n        return duckdb.connect(str(db_path))\n    def f(self, db_path):\n        migrate_database(db_path)\n        return self.helper(db_path)\n",
        id="method-helper-after-migration",
    ),
    pytest.param(
        "import duckdb\nfrom nutrition_ingest import migrate_database\ndb_path = build_db()\nmigrate_database(db_path)\nduckdb.connect(str(db_path))\n",
        id="module-level-statement-ordering",
    ),
    pytest.param(
        "import duckdb\nfrom nutrition_ingest import migrate_database\ndbapi = duckdb\nother = dbapi\ndef f(db_path):\n    migrate_database(db_path)\n    other.connect(str(db_path))\n",
        id="arbitrary-db-module-alias-chain",
    ),
    pytest.param(
        "import duckdb\nfrom nutrition_ingest import migrate_database\nopen_db = getattr(duckdb, 'connect')\ndef f(db_path):\n    migrate_database(db_path)\n    open_db(str(db_path))\n",
        id="dynamic-getattr-connect-alias",
    ),
])
def test_raw_connect_policy_catches_writable_connects_after_migration(source):
    assert _raw_connect_policy_offenders(source)


@pytest.mark.parametrize("source", [
    pytest.param(
        "import duckdb\nfrom nutrition_ingest import migrate_database\ndef f(db_path):\n    migrate_database(db_path)\n    duckdb.connect(str(db_path), read_only=True)\n",
        id="read-only-connect",
    ),
    pytest.param(
        "import duckdb\nfrom nutrition_ingest import migrate_database\ndef f(db_path, cache_path):\n    migrate_database(db_path)\n    duckdb.connect(str(cache_path))\n",
        id="explicit-cache-path-connect",
    ),
    pytest.param(
        "from duckdb import connect as open_db\nfrom nutrition_ingest import migrate_database\ndef f(db_path, CACHE_DB_PATH):\n    migrate_database(db_path)\n    open_db(database=str(CACHE_DB_PATH))\n",
        id="explicit-cache-keyword-connect",
    ),
])
def test_raw_connect_policy_permits_pre_migration_readonly_and_cache_paths(source):
    assert _raw_connect_policy_offenders(source) == []


def test_raw_connect_policy_rejects_writable_primary_connect_even_before_migration():
    source = "import duckdb\nfrom nutrition_ingest import migrate_database\ndef f(db_path):\n    duckdb.connect(str(db_path))\n    migrate_database(db_path)\n"
    assert _raw_connect_policy_offenders(source)


def test_raw_connect_policy_still_catches_private_ingest_imports_inside_callers():
    source = "def f():\n    from nutrition_ingest import _locked_database\n    return _locked_database\n"
    assert _raw_connect_policy_offenders(source) == ["<snippet>:_locked_database"]


def test_raw_connect_policy_catches_helper_migration_before_direct_connect():
    source = """import duckdb
from nutrition_ingest import migrate_database
def do_migrate(db_path):
    migrate_database(db_path)
def f(db_path):
    do_migrate(db_path)
    duckdb.connect(str(db_path))
"""
    assert _raw_connect_policy_offenders(source)


def test_raw_connect_policy_catches_factory_returning_connect_invoked_after_migration():
    source = """import duckdb
from nutrition_ingest import migrate_database
def factory():
    return duckdb.connect
def f(db_path):
    migrate_database(db_path)
    factory()(str(db_path))
"""
    assert _raw_connect_policy_offenders(source)


def test_raw_connect_policy_rejects_conditional_primary_cache_expression():
    source = """import duckdb
from nutrition_ingest import migrate_database
def f(db_path, cache_path, use_primary):
    migrate_database(db_path)
    duckdb.connect(str(db_path if use_primary else cache_path))
"""
    assert _raw_connect_policy_offenders(source)


@pytest.mark.parametrize("source", [
    pytest.param(
        """import duckdb
from nutrition_ingest import migrate_database
def do_migrate(db_path):
    migrate_database(db_path)
def migrate_again(db_path):
    do_migrate(db_path)
def open_db(db_path):
    return duckdb.connect(str(db_path))
def f(db_path):
    migrate_again(db_path)
    return open_db(db_path)
""",
        id="migration-and-open-helper-chains",
    ),
    pytest.param(
        """import duckdb
from nutrition_ingest import migrate_database
def make_factory():
    return duckdb.connect
factory_alias = make_factory
def f(db_path):
    migrate_database(db_path)
    opener = factory_alias()
    opener(str(db_path))
""",
        id="factory-alias-chain-assigned-opener",
    ),
    pytest.param(
        """import duckdb
from nutrition_ingest import migrate_database
class Runner:
    def do_migrate(self, db_path):
        migrate_database(db_path)
    def factory(self):
        return duckdb.connect
    def f(self, db_path):
        self.do_migrate(db_path)
        self.factory()(str(db_path))
""",
        id="method-migration-and-factory",
    ),
])
def test_raw_connect_policy_catches_helper_chains_and_factory_aliases(source):
    assert _raw_connect_policy_offenders(source)


@pytest.mark.parametrize("source", [
    pytest.param(
        """import duckdb
from pathlib import Path
from nutrition_ingest import migrate_database
def f(db_path, cache_path):
    migrate_database(db_path)
    duckdb.connect(str(cache_path))
    duckdb.connect(Path(cache_path))
""",
        id="str-and-path-wrapper-cache-name",
    ),
    pytest.param(
        """from duckdb import connect
from nutrition_ingest import migrate_database
def f(db_path, CACHE_DB_PATH):
    migrate_database(db_path)
    connect(database=CACHE_DB_PATH)
""",
        id="bare-cache-keyword",
    ),
])
def test_raw_connect_policy_accepts_structural_cache_allowlist(source):
    assert _raw_connect_policy_offenders(source) == []


@pytest.mark.parametrize("source", [
    pytest.param(
        """import duckdb
from nutrition_ingest import migrate_database
def f(db_path, cache_path, use_primary):
    migrate_database(db_path)
    duckdb.connect(db_path if use_primary else cache_path)
""",
        id="cache-ternary-with-primary",
    ),
    pytest.param(
        """import duckdb
from nutrition_ingest import migrate_database
def f(db_path, cache_path):
    migrate_database(db_path)
    duckdb.connect(cache_path or db_path)
""",
        id="cache-boolop-with-primary",
    ),
    pytest.param(
        """import duckdb
from nutrition_ingest import migrate_database
def f(db_path, cache_path):
    migrate_database(db_path)
    duckdb.connect(cache_path / 'child.duckdb')
""",
        id="cache-arithmetic-path-expression",
    ),
    pytest.param(
        """import duckdb
from nutrition_ingest import migrate_database
def f(db_path, config):
    migrate_database(db_path)
    duckdb.connect(str(config.cache_path))
""",
        id="cache-attribute",
    ),
    pytest.param(
        """import duckdb
from nutrition_ingest import migrate_database
def f(db_path, cache_path):
    migrate_database(db_path)
    duckdb.connect(normalize(cache_path))
""",
        id="unknown-wrapper-call",
    ),
])
def test_raw_connect_policy_rejects_non_structural_cache_expressions(source):
    assert _raw_connect_policy_offenders(source)


def test_production_and_eval_callers_do_not_import_private_ingest_helpers_or_open_quick_db_connections():
    roots = [REPO_ROOT / "skills" / "log-nutrition" / "scripts", REPO_ROOT / "skills" / "log-nutrition" / "evals"]
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            if path.name == "nutrition_ingest.py":
                continue  # This is the owning central implementation.
            text = path.read_text()
            if path.name == "quick_log_text.py" and ("import duckdb" in text or ".connect(" in text):
                offenders.append(f"{path}:raw DuckDB access")
            offenders.extend(_raw_connect_policy_offenders(text, str(path)))
    bootstrap = REPO_ROOT / "bootstrap" / "init_db.py"
    offenders.extend(_raw_connect_policy_offenders(bootstrap.read_text(), str(bootstrap)))
    assert not offenders, "production/eval callers bypass central ingest API: " + ", ".join(offenders)


def test_benchmark_default_does_not_rewrite_tracked_results():
    benchmark = REPO_ROOT / "skills" / "log-nutrition" / "evals" / "run_benchmark.py"
    tracked_results = REPO_ROOT / "skills" / "log-nutrition" / "evals" / "results.json"
    before_bytes = tracked_results.read_bytes()
    before_mtime_ns = tracked_results.stat().st_mtime_ns
    completed = subprocess.run(
        [sys.executable, str(benchmark)],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Results JSON not written by default" in completed.stdout
    assert tracked_results.read_bytes() == before_bytes
    assert tracked_results.stat().st_mtime_ns == before_mtime_ns

def test_subprocess_quick_replay_is_concurrent_and_byte_identical(tmp_path):
    db = tmp_path / "quick-subprocess.duckdb"; migrate_database(db)
    conn = duckdb.connect(str(db))
    conn.execute("INSERT INTO nutrition_log (entry_id,meal_time,meal_name,food_items,calories,protein_g,carbs_g,fat_total_g) VALUES (1,'2026-08-16 08:00','seed','[{\"item\":\"egg\",\"portion_g\":50,\"calories\":78,\"protein_g\":6.3,\"carbs_g\":0.6,\"fat_total_g\":5.3}]',78,6.3,0.6,5.3)")
    conn.close()
    payload = json.dumps({"meal_time":"2026-08-17T08:00:00","discord_message_id":"quick-process","items":[{"name":"egg","quantity":1,"unit":"egg"}]})
    command = [sys.executable, str(SCRIPTS_DIR / "quick_log_text.py"), "--json", payload]
    env = {**os.environ, "HEALTH_DB_PATH": str(db), "HEALTH_DATA_DIR": str(tmp_path / "data")}
    first = subprocess.Popen(command, cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    second = subprocess.Popen(command, cwd=REPO_ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    first_out, first_err = first.communicate(timeout=15); second_out, second_err = second.communicate(timeout=15)
    assert (first.returncode, second.returncode) == (0, 0), (first_err, second_err)
    assert first_out == second_out
    conn = duckdb.connect(str(db), read_only=True)
    assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM nutrition_ingest_receipts").fetchone()[0] == 1
    conn.close()


def test_canonical_lock_identity_is_stable_before_after_creation_and_for_symlink(tmp_path):
    db = tmp_path / "fresh.duckdb"
    before_db, before_lock = _canonical_db(db, allow_create=True)
    migrate_database(db)
    after_db, after_lock = _canonical_db(db, allow_create=True)
    alias = tmp_path / "alias.duckdb"
    alias.symlink_to(db)
    alias_db, alias_lock = _canonical_db(alias, allow_create=False)
    assert before_db == after_db == alias_db == db.resolve()
    assert before_lock == after_lock == alias_lock


def test_ten_concurrent_fresh_migrations_share_one_bootstrap_lock_and_restore_compatibility(tmp_path):
    db = tmp_path / "fresh-concurrent.duckdb"
    command = [sys.executable, str(SCRIPTS_DIR / "nutrition_migrate.py"), "--db", str(db)]
    processes = [subprocess.Popen(command, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(10)]
    outputs = [process.communicate(timeout=30) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs
    conn = duckdb.connect(str(db), read_only=True)
    assert conn.execute("SELECT COUNT(*) FROM recipes WHERE name=?", ["Example breakfast"]).fetchone() == (1,)
    assert {row[0] for row in conn.execute("SELECT sequence_name FROM duckdb_sequences()").fetchall()} >= {"seq_nutrition_entry", "seq_recipe_id"}
    assert {row[0] for row in conn.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name='nutrition_log'").fetchall()} >= {"idx_nutrition_meal_time", "idx_nutrition_meal_type"}
    assert conn.execute("SELECT COUNT(*) FROM nutrition_schema_migrations").fetchone() == (SCHEMA_VERSION,)
    conn.close()


_RECIPE_SQL = """CREATE TABLE recipes (
  id INTEGER PRIMARY KEY DEFAULT nextval('seq_recipe_id'),
  name VARCHAR NOT NULL, description VARCHAR, food_items JSON NOT NULL,
  total_calories DOUBLE, total_protein_g DOUBLE, total_carbs_g DOUBLE,
  total_fat_g DOUBLE, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, UNIQUE(name)
)"""


def _recipe_snapshot(conn) -> list[tuple]:
    return conn.execute("""
      SELECT id, name, description, CAST(food_items AS VARCHAR), total_calories,
             total_protein_g, total_carbs_g, total_fat_g,
             CAST(created_at AS VARCHAR), CAST(updated_at AS VARCHAR)
      FROM recipes ORDER BY id
    """).fetchall()


def _make_legacy_fixture_with_recipes(db: Path, recipes: list[tuple]) -> None:
    conn = duckdb.connect(str(db))
    conn.execute("CREATE SEQUENCE seq_recipe_id START 100")
    conn.execute(_LEGACY_LOG_SQL)
    conn.execute(_RECIPE_SQL)
    conn.executemany(
        """INSERT INTO recipes (
          id, name, description, food_items, total_calories, total_protein_g,
          total_carbs_g, total_fat_g, created_at, updated_at
        ) VALUES (?, ?, ?, ?::JSON, ?, ?, ?, ?, ?, ?)""",
        recipes,
    )
    conn.close()


def test_migration_preserves_existing_production_recipes_without_example_seed(tmp_path):
    db = tmp_path / "legacy-user-recipes.duckdb"
    recipes = [
        (10, "User tofu bowl", "real recipe", '[{"item":"tofu"}]', 510, 32, 55, 18, "2026-08-01 08:00:00-07", "2026-08-02 09:30:00-07"),
        (11, "User oats", "real recipe", '[{"item":"oats"}]', 420, 20, 60, 12, "2026-08-03 08:00:00-07", "2026-08-04 09:30:00-07"),
    ]
    _make_legacy_fixture_with_recipes(db, recipes)
    conn = duckdb.connect(str(db), read_only=True)
    before = _recipe_snapshot(conn)
    conn.close()

    migrate_database(db)

    conn = duckdb.connect(str(db), read_only=True)
    assert _recipe_snapshot(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM recipes WHERE name=?", ["Example breakfast"]).fetchone() == (0,)
    conn.close()


def test_migration_does_not_duplicate_existing_example_breakfast_recipe(tmp_path):
    db = tmp_path / "legacy-existing-example.duckdb"
    recipes = [
        (20, "Example breakfast", "user kept/imported example", '[{"item":"custom"}]', 300, 10, 30, 10, "2026-08-01 08:00:00-07", "2026-08-02 09:30:00-07"),
        (21, "User dinner", "real recipe", '[{"item":"beans"}]', 600, 25, 80, 20, "2026-08-03 18:00:00-07", "2026-08-04 19:30:00-07"),
    ]
    _make_legacy_fixture_with_recipes(db, recipes)
    conn = duckdb.connect(str(db), read_only=True)
    before = _recipe_snapshot(conn)
    conn.close()

    migrate_database(db)
    migrate_database(db)

    conn = duckdb.connect(str(db), read_only=True)
    assert _recipe_snapshot(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM recipes WHERE name=?", ["Example breakfast"]).fetchone() == (1,)
    conn.close()


def test_fresh_bootstrap_restores_recipe_seed_sequences_and_nutrition_indexes(tmp_path):
    db = tmp_path / "compat.duckdb"
    migrate_database(db)
    conn = duckdb.connect(str(db), read_only=True)
    tables = {row[0] for row in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}
    assert {"nutrition_log", "recipes"}.issubset(tables)
    recipe = conn.execute("SELECT total_calories,total_protein_g,total_carbs_g,total_fat_g FROM recipes WHERE name=?", ["Example breakfast"]).fetchone()
    assert recipe == (333.0, 11.3, 27.8, 20.7)
    assert {row[0] for row in conn.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name='nutrition_log'").fetchall()} >= {"idx_nutrition_meal_time", "idx_nutrition_meal_type"}
    conn.close()


def test_rebuild_migration_recreates_nutrition_indexes(tmp_path):
    db = tmp_path / "legacy-indexes.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute(_LEGACY_LOG_SQL)
    conn.execute("CREATE INDEX idx_nutrition_meal_time ON nutrition_log(meal_time)")
    conn.execute("CREATE INDEX idx_nutrition_meal_type ON nutrition_log(meal_type)")
    conn.close()
    migrate_database(db)
    conn = duckdb.connect(str(db), read_only=True)
    assert {row[0] for row in conn.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name='nutrition_log'").fetchall()} >= {"idx_nutrition_meal_time", "idx_nutrition_meal_type"}
    conn.close()


def test_log_cli_requires_identity_unless_explicit_anonymous_import_flag(tmp_path):
    db = tmp_path / "cli-anonymous.duckdb"
    migrate_database(db)
    payload = json.dumps(_data("anonymous import"))
    command = [sys.executable, str(SCRIPTS_DIR / "log_nutrition.py"), "--json", payload]
    env = {**os.environ, "HEALTH_DB_PATH": str(db)}
    rejected = subprocess.run(command, cwd=REPO_ROOT, env=env, text=True, capture_output=True)
    assert rejected.returncode != 0 and "require provider and message_id" in rejected.stderr
    allowed = subprocess.run([*command, "--allow-anonymous"], cwd=REPO_ROOT, env=env, text=True, capture_output=True)
    assert allowed.returncode == 0, allowed.stderr
    assert "non-idempotent" in allowed.stderr
    conn = duckdb.connect(str(db), read_only=True)
    assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM nutrition_ingest_receipts").fetchone() == (0,)
    conn.close()


def test_quick_cli_requires_identity_for_write_and_allows_explicit_anonymous_import(tmp_path):
    db = tmp_path / "quick-cli-anonymous.duckdb"
    migrate_database(db)
    ingest_nutrition(db, {**_data("seed"), "food_items": [{"item": "egg", "portion_g": 50, "calories": 78, "protein_g": 6.3, "carbs_g": 0.6, "fat_total_g": 5.3}]})
    payload = json.dumps({"meal_time": "2026-08-18T08:00:00", "items": [{"name": "egg", "quantity": 1, "unit": "egg"}]})
    command = [sys.executable, str(SCRIPTS_DIR / "quick_log_text.py"), "--json", payload]
    env = {**os.environ, "HEALTH_DB_PATH": str(db), "HEALTH_DATA_DIR": str(tmp_path / "data")}
    rejected = subprocess.run(command, cwd=REPO_ROOT, env=env, text=True, capture_output=True)
    assert rejected.returncode == 1 and "require provider and message_id" in rejected.stderr
    allowed = subprocess.run([*command, "--allow-anonymous"], cwd=REPO_ROOT, env=env, text=True, capture_output=True)
    assert allowed.returncode == 0, allowed.stderr
    assert "non-idempotent" in allowed.stderr


def test_quick_missing_quantity_requires_clarification_and_writes_nothing(monkeypatch, tmp_path):
    db = tmp_path / "quick-missing-quantity.duckdb"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("HEALTH_DB_PATH", str(db))
    monkeypatch.setenv("HEALTH_DATA_DIR", str(data_dir))
    migrate_database(db)
    ingest_nutrition(
        db,
        {
            **_data("seed egg"),
            "food_items": [
                {"item": "egg", "portion_g": 50, "calories": 78, "protein_g": 6.3, "carbs_g": 0.6, "fat_total_g": 5.3},
            ],
        },
    )
    before = _mutation_counts(db)

    result = quick_log_text({
        "meal_time": "2026-08-18T08:00:00",
        "discord_message_id": "missing-quantity",
        "items": [{"name": "egg"}],
    })

    assert result["status"] == "needs_clarification"
    assert result["reasons"] == [{"item": "egg", "reason": "missing_or_unknown_quantity"}]
    assert _mutation_counts(db) == before


def test_quick_explicit_count_uses_canonical_unit_mass_and_reuses_prior_nutrition(monkeypatch, tmp_path):
    db = tmp_path / "quick-count.duckdb"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("HEALTH_DB_PATH", str(db))
    monkeypatch.setenv("HEALTH_DATA_DIR", str(data_dir))
    migrate_database(db)
    ingest_nutrition(
        db,
        {
            **_data("seed egg"),
            "food_items": [
                {"item": "egg", "portion_g": 50, "calories": 78, "protein_g": 6.3, "carbs_g": 0.6, "fat_total_g": 5.3},
            ],
        },
    )
    before = _mutation_counts(db)

    result = quick_log_text({
        "meal_time": "2026-08-18T08:00:00",
        "discord_message_id": "count-2-eggs",
        "items": [{"name": "egg", "quantity": 2, "unit": "egg"}],
    })

    after = _mutation_counts(db)
    assert result["status"] == "logged"
    assert result["mode"] == "ingredient_reuse"
    assert result["items"][0]["portion_g"] == 100.0
    assert result["totals"]["calories"] == 156
    assert after["nutrition_rows"] == before["nutrition_rows"] + 1
    assert after["receipts"] == before["receipts"] + 1
    assert after["ledgers"] == before["ledgers"] + 1
    assert after["anchors"] == before["anchors"] + 1


def test_bootstrap_init_db_uses_shared_locked_writer_api_at_runtime(monkeypatch, tmp_path):
    import bootstrap.init_db as init_db
    db = tmp_path / "bootstrap-core.duckdb"
    monkeypatch.setattr(init_db, "DB_PATH", db)
    assert init_db.init_database() is True
    migrate_database(db)
    conn = duckdb.connect(str(db), read_only=True)
    assert conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='readings'").fetchone() == (1,)
    assert conn.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name='nutrition_log'").fetchone() == (1,)
    conn.close()


def test_reordered_legacy_columns_are_not_schema_certified(tmp_path):
    db = tmp_path / "reordered-columns.duckdb"
    reordered = _LEGACY_LOG_SQL.replace(
        "entry_id INTEGER PRIMARY KEY, meal_time TIMESTAMP NOT NULL, meal_type VARCHAR,",
        "entry_id INTEGER PRIMARY KEY, meal_type VARCHAR, meal_time TIMESTAMP NOT NULL,",
    )
    conn = duckdb.connect(str(db)); conn.execute(reordered); conn.close()
    with pytest.raises(SchemaValidationError, match="supported legacy"):
        migrate_database(db)


def test_duplicate_constraint_multiplicity_is_rejected(tmp_path):
    db = tmp_path / "duplicate-constraint.duckdb"; migrate_database(db)
    conn = duckdb.connect(str(db))
    conn.execute("ALTER TABLE nutrition_entry_id_allocator RENAME TO old_allocator")
    conn.execute("CREATE TABLE nutrition_entry_id_allocator (allocator_name VARCHAR PRIMARY KEY, next_entry_id BIGINT NOT NULL CHECK(next_entry_id > 0), CHECK(next_entry_id > 0))")
    conn.execute("INSERT INTO nutrition_entry_id_allocator (allocator_name,next_entry_id) SELECT allocator_name,next_entry_id FROM old_allocator")
    conn.execute("DROP TABLE old_allocator")
    conn.close()
    with pytest.raises(SchemaValidationError, match="constraint contract"):
        ingest_nutrition(db, _data(), identity=("discord", "duplicate-constraint"))


def test_identity_column_nocase_collation_is_rejected_from_catalog_ddl(tmp_path):
    db = tmp_path / "nocase-identity.duckdb"; migrate_database(db)
    conn = duckdb.connect(str(db))
    conn.execute("ALTER TABLE nutrition_ingest_receipts RENAME TO old_receipts")
    conn.execute("""CREATE TABLE nutrition_ingest_receipts (
      provider VARCHAR COLLATE NOCASE NOT NULL, message_id VARCHAR NOT NULL,
      entry_id INTEGER NOT NULL, result_json TEXT NOT NULL,
      integrity_digest VARCHAR NOT NULL DEFAULT '',
      committed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(provider,message_id), UNIQUE(entry_id))""")
    conn.execute("DROP TABLE old_receipts")
    conn.close()
    with pytest.raises(SchemaValidationError, match="receipt|collation"):
        ingest_nutrition(db, _data(), identity=("discord", "nocase"))


@pytest.mark.parametrize("kind", ["version-bool", "float-as-int", "bool-as-zero", "signed-zero"])
def test_receipt_preflight_uses_recursive_type_strict_exact_v1_equality(tmp_path, kind):
    db = tmp_path / f"strict-{kind}.duckdb"; migrate_database(db)
    identity = ("discord", kind)
    ingest_nutrition(db, {**_data(), "fat_trans_g": 0.0}, identity=identity)
    conn = duckdb.connect(str(db))
    result_json, committed_at, entry_id = conn.execute(
        "SELECT result_json,committed_at,entry_id FROM nutrition_ingest_receipts WHERE provider=? AND message_id=?",
        list(identity),
    ).fetchone()
    envelope = json.loads(result_json)
    if kind == "version-bool": envelope["version"] = True
    elif kind == "float-as-int": envelope["result"]["entry"]["calories"] = 78
    elif kind == "bool-as-zero": envelope["result"]["entry"]["fat_trans_g"] = False
    else: envelope["result"]["entry"]["fat_trans_g"] = -0.0
    row = _canonical_row(conn, entry_id)
    encoded = json.dumps(envelope, separators=(",", ":"))
    conn.execute(
        "UPDATE nutrition_ingest_receipts SET result_json=?,integrity_digest=? WHERE provider=? AND message_id=?",
        [encoded, _digest(row or {}, envelope, identity, entry_id, committed_at), *identity],
    )
    conn.close()
    with pytest.raises(ReceiptIntegrityError, match="envelope|canonical nutrition row"):
        replay_result(db, identity)


def test_unknown_payload_key_is_rejected_before_write(tmp_path):
    db = tmp_path / "unknown-key.duckdb"; migrate_database(db)
    before = _mutation_counts(db)
    with pytest.raises(ValueError, match="unknown keys.*caloires"):
        ingest_nutrition(db, {**_data(), "caloires": 999}, identity=("discord", "unknown-key"))
    assert _mutation_counts(db) == before
    identity = ("discord", "known-replay")
    ingest_nutrition(db, _data(), identity=identity)
    after_commit = _mutation_counts(db)
    with pytest.raises(ValueError, match="unknown keys.*caloires"):
        ingest_nutrition(db, {**_data(), "caloires": 999}, identity=identity)
    assert _mutation_counts(db) == after_commit
    with pytest.raises(ValueError, match="unknown keys.*itmes"):
        resolve_and_ingest_quick_text(db, {"meal_time": "2026-08-18T08:00:00", "discord_message_id": "quick-typo", "itmes": []})


@pytest.mark.parametrize("field,value", [("calories", True), ("protein_g", float("nan")), ("carbs_g", float("inf")), ("fat_total_g", float("-inf"))])
def test_numeric_persisted_fields_require_finite_real_non_bool(tmp_path, field, value):
    db = tmp_path / f"nonfinite-{field}.duckdb"; migrate_database(db)
    before = _mutation_counts(db)
    with pytest.raises(ValueError, match=f"{field} must be a finite real number"):
        ingest_nutrition(db, {**_data(), field: value}, identity=("discord", f"nonfinite-{field}"))
    assert _mutation_counts(db) == before


def test_receipt_committed_at_tampering_is_digest_covered(tmp_path):
    db = tmp_path / "committed-at.duckdb"; migrate_database(db)
    identity = ("discord", "committed-at")
    ingest_nutrition(db, _data(), identity=identity)
    conn = duckdb.connect(str(db))
    conn.execute("UPDATE nutrition_ingest_receipts SET committed_at=committed_at + INTERVAL 1 SECOND")
    conn.close()
    with pytest.raises(ReceiptIntegrityError, match="digest mismatch"):
        replay_result(db, identity)


def test_all_support_table_inserts_use_explicit_column_lists():
    source = (SCRIPTS_DIR / "nutrition_ingest.py").read_text()
    for table in ("nutrition_entry_id_allocator", "nutrition_schema_migrations", "nutrition_ingest_identities", "nutrition_ingest_receipts"):
        assert f"INSERT INTO {table} VALUES" not in source


def test_static_boundary_scans_skill_references_evals_scripts_and_bootstrap():
    roots = [
        REPO_ROOT / "skills" / "log-nutrition" / "SKILL.md",
        REPO_ROOT / "skills" / "log-nutrition" / "references",
        REPO_ROOT / "skills" / "log-nutrition" / "evals",
        REPO_ROOT / "skills" / "log-nutrition" / "scripts",
        REPO_ROOT / "bootstrap" / "init_db.py",
    ]
    paths: list[Path] = []
    for root in roots:
        paths.extend([root] if root.is_file() else [path for path in root.rglob("*") if path.suffix in {".py", ".md", ".sh"}])
    offenders = []
    for path in paths:
        if path.name == "nutrition_ingest.py":
            continue
        text = path.read_text()
        compact = " ".join(text.lower().split())
        if "insert into nutrition_log" in compact or "nextval('seq_nutrition_entry')" in compact:
            offenders.append(str(path))
    assert offenders == []
    assert "duckdb.connect" not in (REPO_ROOT / "bootstrap" / "init_db.py").read_text()
    db_schema = (REPO_ROOT / "skills" / "log-nutrition" / "references" / "db-schema.md").read_text()
    assert "duckdb.connect(str(db_path()), read_only=True)" in db_schema


def test_event_key_allows_multiple_entries_with_literal_message_id_and_replay(tmp_path):
    db = tmp_path / "event-key.duckdb"
    migrate_database(db)
    first = ingest_nutrition(db, {**_data("peach"), "provider": "discord", "message_id": "literal-msg", "event_key": "snack-1"})
    second = ingest_nutrition(db, {**_data("watermelon"), "provider": "discord", "message_id": "literal-msg", "event_key": "snack-2"})
    replay = ingest_nutrition(db, {"provider": "discord", "message_id": "literal-msg", "event_key": "snack-1", "meal_time": "invalid"})
    assert replay["replayed"] is True and replay["result"] == first["result"]
    assert first["result"]["entry"]["entry_id"] != second["result"]["entry"]["entry_id"]
    conn = duckdb.connect(str(db), read_only=True)
    try:
        assert conn.execute("SELECT DISTINCT ingest_message_id FROM nutrition_log").fetchall() == [("literal-msg",)]
        assert conn.execute("SELECT ingest_event_key FROM nutrition_log ORDER BY entry_id").fetchall() == [("snack-1",), ("snack-2",)]
    finally:
        conn.close()


def test_duplicate_event_key_protects_same_entry_not_duplicate_row(tmp_path):
    db = tmp_path / "event-key-duplicate.duckdb"
    migrate_database(db)
    payload = {**_data("first"), "provider": "discord", "message_id": "literal-msg", "event_key": "only"}
    first = ingest_nutrition(db, payload)
    retry = ingest_nutrition(db, {**payload, "meal_name": "changed"})
    assert retry["replayed"] is True and retry["result"] == first["result"]
    conn = duckdb.connect(str(db), read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0] == 1
    finally:
        conn.close()
