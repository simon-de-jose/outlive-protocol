"""Versioned, fail-closed shared nutrition ingest.

The public APIs own an interprocess lock.  A receipt, identity ledger, and
identity anchor on the meal row make every identified retry replay-only.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from numbers import Real
from pathlib import Path
from typing import Any, Callable, Iterator

import duckdb

SCHEMA_VERSION = 5
INT32_MAX = 2_147_483_647
PERSISTED_FIELDS = [
    "meal_time", "meal_type", "meal_name", "meal_description", "food_items",
    "calories", "protein_g", "carbs_g", "fat_total_g", "fat_saturated_g",
    "fat_unsaturated_g", "fat_trans_g", "fiber_g", "sugar_g", "sodium_mg",
    "potassium_mg", "calcium_mg", "iron_mg", "magnesium_mg", "vitamin_d_mcg",
    "vitamin_b12_mcg", "vitamin_c_mg", "cholesterol_mg", "source", "notes",
]
NUTRIENT_FIELDS = PERSISTED_FIELDS[5:23]
ANCHOR_FIELDS = ("ingest_provider", "ingest_message_id", "ingest_event_key")
IDENTITY_PAYLOAD_FIELDS = frozenset({"provider", "message_id", "event_key", "discord_message_id", "ingest_identity"})
INGEST_PAYLOAD_FIELDS = frozenset(PERSISTED_FIELDS) | IDENTITY_PAYLOAD_FIELDS
QUICK_PAYLOAD_FIELDS = frozenset({
    "meal_time", "meal_type", "meal_name", "raw_text", "items", "reuse_mode",
    "reuse", "reuse_entry_id", "notes",
}) | IDENTITY_PAYLOAD_FIELDS

class NutritionIngestError(RuntimeError): pass
class SchemaValidationError(NutritionIngestError): pass
class ReceiptIntegrityError(NutritionIngestError): pass
class EntryIdExhaustedError(NutritionIngestError): pass

# These limits comfortably cover Discord snowflakes and ordinary provider names,
# while preventing an identity from becoming an unbounded receipt/anchor key.
IDENTITY_PROVIDER_MAX_CHARS = 128
IDENTITY_MESSAGE_ID_MAX_CHARS = 512
IDENTITY_PROVIDER_MAX_BYTES = 256
IDENTITY_MESSAGE_ID_MAX_BYTES = 1024
IDENTITY_EVENT_KEY_MAX_CHARS = 128
IDENTITY_EVENT_KEY_MAX_BYTES = 256
DEFAULT_EVENT_KEY = "default"


def canonical_identity(identity: Any, *, error_type: type[Exception] = ValueError, context: str = "identity") -> tuple[str, str, str]:
    """Validate canonical durable identity: literal provider/message_id plus event_key.

    Two-part legacy caller identities are accepted as the default event only; the
    persisted key is always three-dimensional so one source message can own
    multiple entries without synthetic message-id suffixes.
    """
    if not isinstance(identity, tuple) or len(identity) not in (2, 3) or not all(isinstance(value, str) for value in identity):
        raise error_type(f"{context} must be a tuple of two or three strings")
    provider, message_id = identity[:2]
    event_key = identity[2] if len(identity) == 3 else DEFAULT_EVENT_KEY
    for label, value, max_chars, max_bytes in (
        ("provider", provider, IDENTITY_PROVIDER_MAX_CHARS, IDENTITY_PROVIDER_MAX_BYTES),
        ("message_id", message_id, IDENTITY_MESSAGE_ID_MAX_CHARS, IDENTITY_MESSAGE_ID_MAX_BYTES),
        ("event_key", event_key, IDENTITY_EVENT_KEY_MAX_CHARS, IDENTITY_EVENT_KEY_MAX_BYTES),
    ):
        if not value or value != value.strip():
            raise error_type(f"{context} {label} must be non-empty and have no leading/trailing whitespace")
        try:
            byte_length = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise error_type(f"{context} {label} is not valid UTF-8 text") from exc
        if len(value) > max_chars or byte_length > max_bytes:
            raise error_type(f"{context} {label} is too long")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", event_key):
        raise error_type(f"{context} event_key must match [A-Za-z0-9][A-Za-z0-9_.:-]{{0,127}}")
    return provider, message_id, event_key

# (name, SQL type, NOT NULL, primary-key member, exact default as PRAGMA reports it)
_LOG_COLUMNS = (
    ("entry_id", "INTEGER", True, True, None),
    ("meal_time", "TIMESTAMP", True, False, None),
    ("meal_type", "VARCHAR", False, False, None),
    ("meal_name", "VARCHAR", False, False, None),
    ("meal_description", "VARCHAR", False, False, None),
    ("food_items", "VARCHAR", False, False, None),
    ("calories", "DOUBLE", False, False, None),
    ("protein_g", "DOUBLE", False, False, None),
    ("carbs_g", "DOUBLE", False, False, None),
    ("fat_total_g", "DOUBLE", False, False, None),
    ("fat_saturated_g", "DOUBLE", False, False, None),
    ("fat_unsaturated_g", "DOUBLE", False, False, None),
    ("fat_trans_g", "DOUBLE", False, False, None),
    ("fiber_g", "DOUBLE", False, False, None),
    ("sugar_g", "DOUBLE", False, False, None),
    ("sodium_mg", "DOUBLE", False, False, None),
    ("potassium_mg", "DOUBLE", False, False, None),
    ("calcium_mg", "DOUBLE", False, False, None),
    ("iron_mg", "DOUBLE", False, False, None),
    ("magnesium_mg", "DOUBLE", False, False, None),
    ("vitamin_d_mcg", "DOUBLE", False, False, None),
    ("vitamin_b12_mcg", "DOUBLE", False, False, None),
    ("vitamin_c_mg", "DOUBLE", False, False, None),
    ("cholesterol_mg", "DOUBLE", False, False, None),
    ("source", "VARCHAR", False, False, "'chat'"),
    ("logged_at", "TIMESTAMP", False, False, "CURRENT_TIMESTAMP"),
    ("notes", "VARCHAR", False, False, None),
    ("ingest_provider", "VARCHAR", False, False, None),
    ("ingest_message_id", "VARCHAR", False, False, None),
    ("ingest_event_key", "VARCHAR", False, False, None),
)
_LEGACY_LOG_COLUMNS = tuple(column for column in _LOG_COLUMNS if column[0] not in ANCHOR_FIELDS)
_LEGACY_V4_LOG_COLUMNS = tuple(column for column in _LOG_COLUMNS if column[0] != "ingest_event_key")
_LOG_CONSTRAINTS = {
    ("NOT NULL", ("entry_id",), None),
    ("NOT NULL", ("meal_time",), None),
    ("PRIMARY KEY", ("entry_id",), None),
    ("UNIQUE", ANCHOR_FIELDS, None),
}
_LEGACY_LOG_CONSTRAINTS = {
    ("NOT NULL", ("entry_id",), None),
    ("NOT NULL", ("meal_time",), None),
    ("PRIMARY KEY", ("entry_id",), None),
}
_LEGACY_V4_LOG_CONSTRAINTS = _LEGACY_LOG_CONSTRAINTS | {("UNIQUE", ("ingest_provider", "ingest_message_id"), None)}
_RECEIPT_COLUMNS = (
    ("provider", "VARCHAR", True, True, None),
    ("message_id", "VARCHAR", True, True, None),
    ("event_key", "VARCHAR", True, True, None),
    ("entry_id", "INTEGER", True, False, None),
    ("result_json", "VARCHAR", True, False, None),
    ("integrity_digest", "VARCHAR", True, False, "''"),
    ("committed_at", "TIMESTAMP", True, False, "CURRENT_TIMESTAMP"),
)
_RECEIPT_V2_COLUMNS = tuple(column for column in _RECEIPT_COLUMNS if column[0] != "integrity_digest")
_LEGACY_RECEIPT_V2_COLUMNS = tuple(column for column in _RECEIPT_V2_COLUMNS if column[0] != "event_key")
_LEGACY_RECEIPT_V3_COLUMNS = tuple(column for column in _RECEIPT_COLUMNS if column[0] != "event_key")
_RECEIPT_CONSTRAINTS = {
    ("NOT NULL", ("provider",), None),
    ("NOT NULL", ("message_id",), None),
    ("NOT NULL", ("event_key",), None),
    ("NOT NULL", ("entry_id",), None),
    ("NOT NULL", ("result_json",), None),
    ("NOT NULL", ("integrity_digest",), None),
    ("NOT NULL", ("committed_at",), None),
    ("PRIMARY KEY", ("provider", "message_id", "event_key"), None),
    ("UNIQUE", ("entry_id",), None),
}
_RECEIPT_V2_CONSTRAINTS = {constraint for constraint in _RECEIPT_CONSTRAINTS if constraint[1] != ("integrity_digest",)}
_LEGACY_RECEIPT_BASE_CONSTRAINTS = {constraint for constraint in _RECEIPT_CONSTRAINTS if constraint[1] not in (("event_key",), ("provider", "message_id", "event_key"))} | {("PRIMARY KEY", ("provider", "message_id"), None)}
_LEGACY_RECEIPT_V2_CONSTRAINTS = {constraint for constraint in _LEGACY_RECEIPT_BASE_CONSTRAINTS if constraint[1] != ("integrity_digest",)}
_LEGACY_RECEIPT_V3_CONSTRAINTS = _LEGACY_RECEIPT_BASE_CONSTRAINTS
_IDENTITY_LEDGER_COLUMNS = (
    ("provider", "VARCHAR", True, True, None),
    ("message_id", "VARCHAR", True, True, None),
    ("event_key", "VARCHAR", True, True, None),
    ("entry_id", "INTEGER", True, False, None),
)
_IDENTITY_LEDGER_CONSTRAINTS = {
    ("NOT NULL", ("provider",), None),
    ("NOT NULL", ("message_id",), None),
    ("NOT NULL", ("event_key",), None),
    ("NOT NULL", ("entry_id",), None),
    ("PRIMARY KEY", ("provider", "message_id", "event_key"), None),
    ("UNIQUE", ("entry_id",), None),
}
_LEGACY_IDENTITY_LEDGER_COLUMNS = tuple(column for column in _IDENTITY_LEDGER_COLUMNS if column[0] != "event_key")
_LEGACY_IDENTITY_LEDGER_CONSTRAINTS = {constraint for constraint in _IDENTITY_LEDGER_CONSTRAINTS if constraint[1] not in (("event_key",), ("provider", "message_id", "event_key"))} | {("PRIMARY KEY", ("provider", "message_id"), None)}
_ALLOCATOR_COLUMNS = (
    ("allocator_name", "VARCHAR", True, True, None),
    ("next_entry_id", "BIGINT", True, False, None),
)
_ALLOCATOR_CONSTRAINTS = {
    ("NOT NULL", ("allocator_name",), None),
    ("NOT NULL", ("next_entry_id",), None),
    ("PRIMARY KEY", ("allocator_name",), None),
    ("CHECK", ("next_entry_id",), "(next_entry_id > 0)"),
}
_MIGRATION_COLUMNS = (
    ("version", "INTEGER", True, True, None),
    ("applied_at", "TIMESTAMP", True, False, "CURRENT_TIMESTAMP"),
)
_MIGRATION_CONSTRAINTS = {
    ("NOT NULL", ("version",), None),
    ("NOT NULL", ("applied_at",), None),
    ("PRIMARY KEY", ("version",), None),
}


def _canonical_db(db_path: str | Path, *, allow_create: bool) -> tuple[Path, Path]:
    raw = Path(db_path).expanduser()
    if not allow_create and (not raw.exists() or not raw.is_file()):
        raise SchemaValidationError(f"nutrition database does not exist: {raw}; run nutrition_migrate.py explicitly")
    if not raw.parent.exists():
        if allow_create: raw.parent.mkdir(parents=True, exist_ok=True)
        else: raise SchemaValidationError(f"nutrition database directory does not exist: {raw.parent}")
    canonical = raw.resolve(strict=raw.exists())
    if canonical.exists():
        stat = canonical.stat()
        if stat.st_nlink > 1:
            raise SchemaValidationError("hard-linked nutrition databases are unsupported; use one canonical database path")
    # The lock identity must not change when DuckDB creates the file.  A
    # canonical-path digest is stable before and after creation, while still
    # making symlink aliases serialize on the target path.  Existing hardlinks
    # remain fail-closed above because a path digest cannot unify them safely.
    path_digest = hashlib.sha256(os.fsencode(str(canonical))).hexdigest()[:24]
    lock = canonical.parent / f".nutrition-ingest-{path_digest}.lock"
    return canonical, lock


@contextlib.contextmanager
def database_lock(db_path: str | Path, *, allow_create: bool) -> Iterator[Path]:
    db, lock_path = _canonical_db(db_path, allow_create=allow_create)
    try:
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try: yield db
            finally: fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise NutritionIngestError(f"could not acquire nutrition ingest lock: {exc}") from exc


@contextlib.contextmanager
def writable_database(db_path: str | Path, *, allow_create: bool = True) -> Iterator[duckdb.DuckDBPyConnection]:
    """Public locked raw connection for bootstrap-owned non-nutrition DDL.

    Nutrition meal writes must use :func:`ingest_nutrition`; this API exists so
    repository bootstrap code can create core tables/views in the same DuckDB
    file without opening an unlocked competing writer.
    """
    with database_lock(db_path, allow_create=allow_create) as db:
        conn = _connect_with_retry(db)
        try:
            yield conn
        finally:
            conn.close()


# These are intentionally full-string signatures from DuckDB's documented/file
# lock failures.  Do not broaden them: arbitrary transaction/IO errors are not
# safe to replay.
_RETRYABLE_LOCK_SIGNATURES = (
    re.compile(r"TransactionContext Error: Failed to commit: Conflicting lock is held in .+"),
    re.compile(r'IO Error: Could not set lock on file ".+": Conflicting lock is held in .+'),
)
def _retryable_connect_conflict(exc: BaseException) -> bool:
    return isinstance(exc, (duckdb.TransactionException, duckdb.IOException)) and any(
        pattern.fullmatch(str(exc)) for pattern in _RETRYABLE_LOCK_SIGNATURES
    )


def _connect_with_retry(db: Path) -> duckdb.DuckDBPyConnection:
    last: Exception | None = None
    for attempt in range(3):
        try:
            return duckdb.connect(str(db))
        except (duckdb.TransactionException, duckdb.IOException) as exc:
            if not _retryable_connect_conflict(exc):
                raise
            last = exc
            if attempt == 2:
                break
            time.sleep(0.05 * (attempt + 1))
    raise NutritionIngestError(f"could not connect to nutrition database after bounded lock-conflict retry: {last}") from last


def _table_exists(conn: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]).fetchone())


def _info(conn: duckdb.DuckDBPyConnection, table: str) -> dict[str, tuple[str, bool, bool, str | None]]:
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    if not rows: raise SchemaValidationError(f"nutrition schema missing table {table}; run nutrition_migrate.py")
    ordered = sorted(rows, key=lambda row: int(row[0]))
    if [int(row[0]) for row in ordered] != list(range(len(ordered))):
        raise SchemaValidationError(f"{table} has noncanonical column cids")
    return {row[1]: (row[2].upper(), bool(row[3]), bool(row[5]), _normalize_default(row[4])) for row in ordered}


def _normalize_default(value: str | None) -> str | None:
    if value is None: return None
    text = str(value).strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text.upper() if text.upper() == "CURRENT_TIMESTAMP" else text


def _matches_columns(info: dict[str, tuple[str, bool, bool, str | None]], expected: tuple[tuple[str, str, bool, bool, str | None], ...]) -> bool:
    return list(info) == [column[0] for column in expected] and all(
        info[name] == (type_name, not_null, primary_key, default)
        for name, type_name, not_null, primary_key, default in expected
    )


def _constraint_details(conn: duckdb.DuckDBPyConnection, table: str) -> Counter[tuple[str, tuple[str, ...], str | None]]:
    try:
        rows = conn.execute("SELECT constraint_type, constraint_column_names, expression FROM duckdb_constraints() WHERE table_name=?", [table]).fetchall()
    except duckdb.Error as exc:
        raise SchemaValidationError(f"cannot inspect constraints for {table}: {exc}") from exc
    return Counter((row[0], tuple(row[1]), row[2]) for row in rows)


def _reject_identity_collations(conn: duckdb.DuckDBPyConnection, table: str) -> None:
    row = conn.execute("SELECT sql FROM duckdb_tables() WHERE table_name=?", [table]).fetchone()
    if not row or not isinstance(row[0], str):
        raise SchemaValidationError(f"cannot inspect catalog DDL for {table}")
    identity_columns = {
        "nutrition_log": ANCHOR_FIELDS,
        "nutrition_ingest_receipts": ("provider", "message_id", "event_key"),
        "nutrition_ingest_identities": ("provider", "message_id", "event_key"),
    }.get(table, ())
    for column in identity_columns:
        pattern = rf'(?is)(?:"{re.escape(column)}"|\b{re.escape(column)}\b)\s+VARCHAR(?:\s*\([^)]*\))?\s+COLLATE\b'
        if re.search(pattern, row[0]):
            raise SchemaValidationError(f"{table}.{column} has a noncanonical collation")


def _matches_contract(conn: duckdb.DuckDBPyConnection, table: str, expected_columns: tuple[tuple[str, str, bool, bool, str | None], ...], expected_constraints: set[tuple[str, tuple[str, ...], str | None]]) -> bool:
    try:
        _reject_identity_collations(conn, table)
    except SchemaValidationError:
        return False
    return _matches_columns(_info(conn, table), expected_columns) and _constraint_details(conn, table) == Counter(expected_constraints)


def _require_contract(conn: duckdb.DuckDBPyConnection, table: str, expected_columns: tuple[tuple[str, str, bool, bool, str | None], ...], expected_constraints: set[tuple[str, tuple[str, ...], str | None]]) -> None:
    info = _info(conn, table)
    if list(info) != [column[0] for column in expected_columns]:
        raise SchemaValidationError(f"nutrition schema has an unsafe {table} column set or order")
    if not _matches_columns(info, expected_columns):
        raise SchemaValidationError(f"{table} exact type/nullability/PK/default contract is unsafe")
    _reject_identity_collations(conn, table)
    if _constraint_details(conn, table) != Counter(expected_constraints):
        raise SchemaValidationError(f"{table} exact constraint contract is unsafe")


def _constraints(conn: duckdb.DuckDBPyConnection, table: str) -> Counter[tuple[str, tuple[str, ...]]]:
    try:
        rows = conn.execute("SELECT constraint_type, constraint_column_names FROM duckdb_constraints() WHERE table_name=?", [table]).fetchall()
    except duckdb.Error as exc:
        raise SchemaValidationError(f"cannot inspect constraints for {table}: {exc}") from exc
    return Counter((row[0], tuple(row[1])) for row in rows)


def _create_nutrition_log(conn: duckdb.DuckDBPyConnection, name: str = "nutrition_log") -> None:
    conn.execute(f'''CREATE TABLE {name} (
      entry_id INTEGER PRIMARY KEY,
      meal_time TIMESTAMP NOT NULL,
      meal_type VARCHAR, meal_name VARCHAR, meal_description TEXT, food_items TEXT,
      calories DOUBLE, protein_g DOUBLE, carbs_g DOUBLE, fat_total_g DOUBLE,
      fat_saturated_g DOUBLE, fat_unsaturated_g DOUBLE, fat_trans_g DOUBLE,
      fiber_g DOUBLE, sugar_g DOUBLE, sodium_mg DOUBLE, potassium_mg DOUBLE,
      calcium_mg DOUBLE, iron_mg DOUBLE, magnesium_mg DOUBLE, vitamin_d_mcg DOUBLE,
      vitamin_b12_mcg DOUBLE, vitamin_c_mg DOUBLE, cholesterol_mg DOUBLE,
      source VARCHAR DEFAULT 'chat', logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      notes TEXT, ingest_provider VARCHAR, ingest_message_id VARCHAR, ingest_event_key VARCHAR,
      UNIQUE(ingest_provider, ingest_message_id, ingest_event_key)
    )''')


def _create_legacy_table(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_nutrition_entry START 1")
    _create_nutrition_log(conn)


_DEFAULT_RECIPE_ITEMS = [
    {"item": "cranberry sourdough", "portion": "40g", "fdc_id": None, "calories": 97, "protein_g": 3.0, "carbs_g": 18.0, "fat_g": 1.5},
    {"item": "avocado", "portion": "1/2", "fdc_id": "171716", "calories": 114, "protein_g": 1.3, "carbs_g": 6.0, "fat_g": 10.5},
    {"item": "hard-boiled egg", "portion": "50g", "fdc_id": "748967", "calories": 78, "protein_g": 6.3, "carbs_g": 0.6, "fat_g": 5.3},
    {"item": "black coffee", "portion": "240ml", "fdc_id": "171998", "calories": 2, "protein_g": 0.3, "carbs_g": 0.0, "fat_g": 0.0},
]


def _create_compatibility_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Restore bootstrap compatibility without polluting existing recipes."""
    conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_nutrition_entry START 1")
    recipes_existed = _table_exists(conn, "recipes")
    conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_recipe_id START 1")
    conn.execute('''CREATE TABLE IF NOT EXISTS recipes (
      id INTEGER PRIMARY KEY DEFAULT nextval('seq_recipe_id'),
      name VARCHAR NOT NULL, description VARCHAR, food_items JSON NOT NULL,
      total_calories DOUBLE, total_protein_g DOUBLE, total_carbs_g DOUBLE,
      total_fat_g DOUBLE, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
      updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, UNIQUE(name)
    )''')
    if not recipes_existed:
        conn.execute(
            """INSERT INTO recipes (
              name, description, food_items, total_calories, total_protein_g,
              total_carbs_g, total_fat_g
            ) VALUES (?, ?, ?::JSON, ?, ?, ?, ?)""",
            [
                "Example breakfast",
                "Cranberry sourdough, avocado, hard-boiled egg, and black coffee.",
                json.dumps(_DEFAULT_RECIPE_ITEMS),
                333, 11.3, 27.8, 20.7,
            ],
        )
    # Rebuilding nutrition_log drops its indexes.  Migration owns recreation
    # and runs this after the final table name is in place.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_meal_time ON nutrition_log(meal_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nutrition_meal_type ON nutrition_log(meal_type)")


def _rebuild_log_with_anchors(conn: duckdb.DuckDBPyConnection) -> None:
    # DuckDB releases used in production reject ADD COLUMN ... NOT NULL DEFAULT.
    # Rebuild inside this migration transaction instead; it preserves all legacy
    # values and installs the exact current table contract atomically.
    _create_nutrition_log(conn, "nutrition_log__v5")
    columns = [column[0] for column in _LEGACY_LOG_COLUMNS]
    conn.execute(
        f"INSERT INTO nutrition_log__v5 ({', '.join(columns)}, ingest_provider, ingest_message_id, ingest_event_key) "
        f"SELECT {', '.join(columns)}, NULL, NULL, NULL FROM nutrition_log"
    )
    conn.execute("DROP TABLE nutrition_log")
    conn.execute("ALTER TABLE nutrition_log__v5 RENAME TO nutrition_log")


def _rebuild_log_with_event_key(conn: duckdb.DuckDBPyConnection) -> None:
    _create_nutrition_log(conn, "nutrition_log__v5")
    columns = [column[0] for column in _LEGACY_V4_LOG_COLUMNS]
    conn.execute(
        f"INSERT INTO nutrition_log__v5 ({', '.join(columns)}, ingest_event_key) "
        f"SELECT {', '.join(columns)}, CASE WHEN ingest_provider IS NULL THEN NULL ELSE ? END FROM nutrition_log",
        [DEFAULT_EVENT_KEY],
    )
    conn.execute("DROP TABLE nutrition_log")
    conn.execute("ALTER TABLE nutrition_log__v5 RENAME TO nutrition_log")


def _create_receipts(conn: duckdb.DuckDBPyConnection, name: str = "nutrition_ingest_receipts") -> None:
    conn.execute(f'''CREATE TABLE {name} (
      provider VARCHAR NOT NULL, message_id VARCHAR NOT NULL, event_key VARCHAR NOT NULL, entry_id INTEGER NOT NULL,
      result_json TEXT NOT NULL, integrity_digest VARCHAR NOT NULL DEFAULT '',
      committed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(provider, message_id,event_key), UNIQUE(entry_id)
    )''')


def _receipt_shape(conn: duckdb.DuckDBPyConnection) -> str:
    if not _table_exists(conn, "nutrition_ingest_receipts"): return "absent"
    if _matches_contract(conn, "nutrition_ingest_receipts", _RECEIPT_COLUMNS, _RECEIPT_CONSTRAINTS): return "current"
    if _matches_contract(conn, "nutrition_ingest_receipts", _RECEIPT_V2_COLUMNS, _RECEIPT_V2_CONSTRAINTS): return "v2"
    if _matches_contract(conn, "nutrition_ingest_receipts", _LEGACY_RECEIPT_V2_COLUMNS, _LEGACY_RECEIPT_V2_CONSTRAINTS): return "legacy_v2"
    if _matches_contract(conn, "nutrition_ingest_receipts", _LEGACY_RECEIPT_V3_COLUMNS, _LEGACY_RECEIPT_V3_CONSTRAINTS): return "legacy_v3"
    raise SchemaValidationError("nutrition_ingest_receipts schema is unsafe")


def _rebuild_receipts_v2(conn: duckdb.DuckDBPyConnection) -> None:
    _create_receipts(conn, "nutrition_ingest_receipts__v4")
    shape = _receipt_shape(conn)
    if shape == "v2":
        conn.execute("INSERT INTO nutrition_ingest_receipts__v4 (provider,message_id,event_key,entry_id,result_json,integrity_digest,committed_at) SELECT provider,message_id,event_key,entry_id,result_json,'',committed_at FROM nutrition_ingest_receipts")
    elif shape == "legacy_v2":
        conn.execute("INSERT INTO nutrition_ingest_receipts__v4 (provider,message_id,event_key,entry_id,result_json,integrity_digest,committed_at) SELECT provider,message_id,?,entry_id,result_json,'',committed_at FROM nutrition_ingest_receipts", [DEFAULT_EVENT_KEY])
    elif shape == "legacy_v3":
        conn.execute("INSERT INTO nutrition_ingest_receipts__v4 (provider,message_id,event_key,entry_id,result_json,integrity_digest,committed_at) SELECT provider,message_id,?,entry_id,result_json,integrity_digest,committed_at FROM nutrition_ingest_receipts", [DEFAULT_EVENT_KEY])
    conn.execute("DROP TABLE nutrition_ingest_receipts")
    conn.execute("ALTER TABLE nutrition_ingest_receipts__v4 RENAME TO nutrition_ingest_receipts")


def _create_support_tables(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS nutrition_schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS nutrition_entry_id_allocator (allocator_name VARCHAR PRIMARY KEY, next_entry_id BIGINT NOT NULL CHECK (next_entry_id > 0))")
    shape = _receipt_shape(conn)
    if shape == "absent": _create_receipts(conn)
    elif shape in {"v2", "legacy_v2", "legacy_v3"}: _rebuild_receipts_v2(conn)
    if _ledger_shape(conn) == "legacy":
        _rebuild_legacy_ledger(conn)
    conn.execute('''CREATE TABLE IF NOT EXISTS nutrition_ingest_identities (
      provider VARCHAR NOT NULL, message_id VARCHAR NOT NULL, event_key VARCHAR NOT NULL, entry_id INTEGER NOT NULL,
      PRIMARY KEY(provider,message_id,event_key), UNIQUE(entry_id))''')


def _migrate_nutrition_log(conn: duckdb.DuckDBPyConnection) -> None:
    if not _table_exists(conn, "nutrition_log"):
        _create_legacy_table(conn)
        return
    info = _info(conn, "nutrition_log")
    if _matches_columns(info, _LOG_COLUMNS):
        _reject_identity_collations(conn, "nutrition_log")
        if _constraint_details(conn, "nutrition_log") != Counter(_LOG_CONSTRAINTS):
            raise SchemaValidationError("nutrition_log exact constraint contract is unsafe")
        return
    if _matches_columns(info, _LEGACY_LOG_COLUMNS):
        if _constraint_details(conn, "nutrition_log") != Counter(_LEGACY_LOG_CONSTRAINTS):
            raise SchemaValidationError("nutrition_log exact supported legacy constraint contract is unsafe")
        _rebuild_log_with_anchors(conn)
        return
    if _matches_columns(info, _LEGACY_V4_LOG_COLUMNS):
        if _constraint_details(conn, "nutrition_log") != Counter(_LEGACY_V4_LOG_CONSTRAINTS):
            raise SchemaValidationError("nutrition_log exact supported v4 constraint contract is unsafe")
        _rebuild_log_with_event_key(conn)
        return
    raise SchemaValidationError("nutrition_log column type/nullability/default contract is not a supported legacy v1/v2/v3/v4 shape")


def _json_value(value: Any) -> Any:
    """The single JSON representation used for receipt entries and digests."""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return value


def _receipt_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {field: _json_value(value) for field, value in row.items()}


def _legacy_receipt_entry(row: dict[str, Any]) -> dict[str, Any]:
    """Receipt form before v4 added immutable identity anchors to meal rows."""
    return {field: _json_value(row[field]) for field in (column[0] for column in _LEGACY_LOG_COLUMNS)}


def _require_complete_envelope(envelope: Any, entry_id: int, expected_entry: dict[str, Any]) -> dict[str, Any]:
    """Reject JSON-valid but semantically incomplete committed-result receipts."""
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"version", "result"}
        or type(envelope.get("version")) is not int
        or envelope.get("version") != 1
    ):
        raise ReceiptIntegrityError("stored nutrition receipt has an invalid envelope")
    result = envelope.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("status"), str) or not result["status"]:
        raise ReceiptIntegrityError("stored nutrition receipt has invalid result status")
    entry = result.get("entry")
    if not isinstance(entry, dict) or isinstance(entry.get("entry_id"), bool) or entry.get("entry_id") != entry_id:
        raise ReceiptIntegrityError("stored nutrition receipt entry id does not match receipt")
    if not _strict_equal(entry, expected_entry):
        raise ReceiptIntegrityError("stored nutrition receipt entry differs from canonical nutrition row")
    return result


def _strict_equal(left: Any, right: Any) -> bool:
    """Recursive JSON-semantic equality with exact scalar types and signed zero."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(_strict_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_strict_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, float):
        if math.isnan(left) or math.isnan(right):
            return False
        if left == 0.0 and right == 0.0:
            return math.copysign(1.0, left) == math.copysign(1.0, right)
    return left == right


def _legacy_versions(conn: duckdb.DuckDBPyConnection) -> set[int]:
    if not _table_exists(conn, "nutrition_schema_migrations"):
        return set()
    _preflight_migration_metadata(conn)
    return {int(row[0]) for row in conn.execute("SELECT version FROM nutrition_schema_migrations").fetchall()}


def _preflight_allocator(conn: duckdb.DuckDBPyConnection) -> None:
    if not _table_exists(conn, "nutrition_entry_id_allocator"):
        raise SchemaValidationError("nutrition allocator is missing")
    _require_contract(conn, "nutrition_entry_id_allocator", _ALLOCATOR_COLUMNS, _ALLOCATOR_CONSTRAINTS)
    rows = conn.execute("SELECT allocator_name,next_entry_id FROM nutrition_entry_id_allocator ORDER BY allocator_name").fetchall()
    if len(rows) != 1 or rows[0][0] != "nutrition_log" or isinstance(rows[0][1], bool) or int(rows[0][1]) <= 0:
        raise SchemaValidationError("nutrition allocator is invalid")


def _preflight_migration_metadata(conn: duckdb.DuckDBPyConnection) -> None:
    _require_contract(conn, "nutrition_schema_migrations", _MIGRATION_COLUMNS, _MIGRATION_CONSTRAINTS)


def _ledger_shape(conn: duckdb.DuckDBPyConnection) -> str:
    if not _table_exists(conn, "nutrition_ingest_identities"):
        return "absent"
    if _matches_contract(conn, "nutrition_ingest_identities", _IDENTITY_LEDGER_COLUMNS, _IDENTITY_LEDGER_CONSTRAINTS):
        return "current"
    if _matches_contract(conn, "nutrition_ingest_identities", _LEGACY_IDENTITY_LEDGER_COLUMNS, _LEGACY_IDENTITY_LEDGER_CONSTRAINTS):
        return "legacy"
    raise SchemaValidationError("nutrition_ingest_identities schema is unsafe")


def _rebuild_legacy_ledger(conn: duckdb.DuckDBPyConnection) -> None:
    if _ledger_shape(conn) != "legacy":
        return
    conn.execute("CREATE TABLE nutrition_ingest_identities__v5 (provider VARCHAR NOT NULL, message_id VARCHAR NOT NULL, event_key VARCHAR NOT NULL, entry_id INTEGER NOT NULL, PRIMARY KEY(provider,message_id,event_key), UNIQUE(entry_id))")
    conn.execute("INSERT INTO nutrition_ingest_identities__v5 SELECT provider,message_id,?,entry_id FROM nutrition_ingest_identities", [DEFAULT_EVENT_KEY])
    conn.execute("DROP TABLE nutrition_ingest_identities")
    conn.execute("ALTER TABLE nutrition_ingest_identities__v5 RENAME TO nutrition_ingest_identities")


def _preflight_ledger(conn: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str, int]]:
    if not _table_exists(conn, "nutrition_ingest_identities"):
        raise ReceiptIntegrityError("nutrition receipts have a missing identity ledger")
    shape = _ledger_shape(conn)
    if shape == "legacy":
        rows = conn.execute("SELECT provider,message_id,entry_id FROM nutrition_ingest_identities ORDER BY provider,message_id").fetchall()
        resolved = [(provider, message_id, DEFAULT_EVENT_KEY, int(entry_id)) for provider, message_id, entry_id in rows]
    else:
        rows = conn.execute("SELECT provider,message_id,event_key,entry_id FROM nutrition_ingest_identities ORDER BY provider,message_id,event_key").fetchall()
        resolved = [(provider, message_id, event_key, int(entry_id)) for provider, message_id, event_key, entry_id in rows]
    for provider, message_id, event_key, _ in resolved:
        canonical_identity((provider, message_id, event_key), error_type=ReceiptIntegrityError, context="stored ledger identity")
    return resolved


def _legacy_digest(row: dict[str, Any], envelope: dict[str, Any]) -> str:
    """The pre-anchor digest format used by the historical v3 receipt."""
    material = json.dumps({"row": row, "envelope": envelope}, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(material).hexdigest()


def _legacy_v4_digest(
    row: dict[str, Any],
    envelope: dict[str, Any],
    identity: tuple[str, str],
    entry_id: int,
    committed_at: Any,
) -> str:
    """The v4 digest format before event_key became part of durable identity."""
    material = json.dumps(
        {
            "identity": {"provider": identity[0], "message_id": identity[1]},
            "entry_id": entry_id,
            "committed_at": _json_value(committed_at),
            "row": row,
            "envelope": envelope,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(material).hexdigest()


def _digest(
    row: dict[str, Any],
    envelope: dict[str, Any],
    identity: tuple[str, ...],
    entry_id: int,
    committed_at: Any,
) -> str:
    identity = canonical_identity(identity)
    material = json.dumps(
        {
            "identity": {"provider": identity[0], "message_id": identity[1], "event_key": identity[2]},
            "entry_id": entry_id,
            "committed_at": _json_value(committed_at),
            "row": row,
            "envelope": envelope,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(material).hexdigest()


def _preflight_current_integrity(conn: duckdb.DuckDBPyConnection) -> None:
    """Validate the fully anchored v4 receipt/ledger/row bijection."""
    _require_contract(conn, "nutrition_log", _LOG_COLUMNS, _LOG_CONSTRAINTS)
    if _receipt_shape(conn) != "current":
        raise SchemaValidationError("current nutrition schema requires the exact current receipt schema")
    ledger = _preflight_ledger(conn)
    anchors: set[tuple[str, str, str, int]] = set()
    for entry_id, provider, message_id, event_key in conn.execute("SELECT entry_id,ingest_provider,ingest_message_id,ingest_event_key FROM nutrition_log").fetchall():
        present = [provider is not None, message_id is not None, event_key is not None]
        if any(present) and not all(present):
            raise ReceiptIntegrityError("nutrition row identity anchors must be all NULL or all present")
        if provider is not None:
            identity = canonical_identity((provider, message_id, event_key), error_type=ReceiptIntegrityError, context="nutrition row identity anchor")
            anchors.add((*identity, int(entry_id)))
    receipt_keys: set[tuple[str, str, str, int]] = set()
    for provider, message_id, event_key, raw_entry_id, result_json, stored_digest, committed_at in conn.execute("SELECT provider,message_id,event_key,entry_id,result_json,integrity_digest,committed_at FROM nutrition_ingest_receipts ORDER BY provider,message_id,event_key").fetchall():
        identity = canonical_identity((provider, message_id, event_key), error_type=ReceiptIntegrityError, context="stored receipt identity")
        entry_id = int(raw_entry_id)
        key = (*identity, entry_id)
        if key in receipt_keys:
            raise ReceiptIntegrityError("nutrition receipts have duplicate identity ownership")
        row = _canonical_row(conn, entry_id)
        if row is None:
            raise ReceiptIntegrityError("stored nutrition receipt references a missing nutrition row")
        try:
            envelope = json.loads(result_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ReceiptIntegrityError("stored nutrition receipt is not valid JSON") from exc
        if stored_digest != _digest(row, envelope, identity, entry_id, committed_at):
            raise ReceiptIntegrityError("stored nutrition receipt integrity digest mismatch")
        _require_complete_envelope(envelope, entry_id, _receipt_entry(row))
        receipt_keys.add(key)
    if len(receipt_keys) != len(ledger) or receipt_keys != set(ledger) or receipt_keys != anchors or len({entry_id for *_, entry_id in receipt_keys}) != len(receipt_keys):
        raise ReceiptIntegrityError("nutrition receipt, identity ledger, and row anchors are not a complete one-to-one bijection")


def _migration_receipt_preflight(conn: duckdb.DuckDBPyConnection) -> tuple[str, list[tuple[str, str, str, int, dict[str, Any]]]]:
    """Validate every upgradeable state before changing a single table or marker."""
    has_log = _table_exists(conn, "nutrition_log")
    versions = _legacy_versions(conn)
    has_receipts = _table_exists(conn, "nutrition_ingest_receipts")
    has_ledger = _table_exists(conn, "nutrition_ingest_identities")
    has_allocator = _table_exists(conn, "nutrition_entry_id_allocator")
    has_metadata = _table_exists(conn, "nutrition_schema_migrations")
    if has_metadata:
        _preflight_migration_metadata(conn)
    if not has_log:
        if versions or has_receipts or has_ledger or has_allocator or has_metadata:
            raise SchemaValidationError("nutrition support tables or markers exist without nutrition_log")
        return "absent", []

    info = _info(conn, "nutrition_log")
    legacy = _matches_columns(info, _LEGACY_LOG_COLUMNS)
    legacy_v4 = _matches_columns(info, _LEGACY_V4_LOG_COLUMNS)
    current = _matches_columns(info, _LOG_COLUMNS)
    if not legacy and not legacy_v4 and not current:
        raise SchemaValidationError("nutrition_log column type/nullability/default contract is not a supported legacy v1/v2/v3/v4 shape")
    if legacy and _constraint_details(conn, "nutrition_log") != Counter(_LEGACY_LOG_CONSTRAINTS):
        raise SchemaValidationError("nutrition_log exact supported legacy constraint contract is unsafe")
    if legacy_v4 and _constraint_details(conn, "nutrition_log") != Counter(_LEGACY_V4_LOG_CONSTRAINTS):
        raise SchemaValidationError("nutrition_log exact supported v4 constraint contract is unsafe")

    if legacy:
        # The only supported historical states are: v1 bare meal table; exact
        # v2 metadata/receipt state; and exact v3 digest+ledger state.
        if not versions:
            if has_receipts or has_ledger or has_allocator or has_metadata:
                raise SchemaValidationError("legacy v1 nutrition state has inconsistent support tables or markers")
            return "legacy", []
        if versions not in ({1, 2}, {1, 2, 3}):
            raise SchemaValidationError("legacy nutrition migration marker set is incompatible with its schema")
        if not has_receipts or not has_allocator:
            raise SchemaValidationError("legacy nutrition migration state is incomplete")
        _preflight_allocator(conn)
        receipt_shape = _receipt_shape(conn)
        if versions == {1, 2} and receipt_shape not in {"v2", "legacy_v2"}:
            raise SchemaValidationError("historical v2 requires the exact v2 receipt schema")
        if versions == {1, 2, 3} and receipt_shape not in {"current", "legacy_v3"}:
            raise SchemaValidationError("historical v3 requires the exact digest receipt schema")
        receipts = conn.execute("SELECT provider,message_id,entry_id,result_json,integrity_digest FROM nutrition_ingest_receipts ORDER BY provider,message_id").fetchall() if receipt_shape in {"current", "legacy_v3"} else [(*row, None) for row in conn.execute("SELECT provider,message_id,entry_id,result_json FROM nutrition_ingest_receipts ORDER BY provider,message_id").fetchall()]
        if has_ledger:
            ledger = _preflight_ledger(conn)
        elif versions == {1, 2} and receipt_shape in {"v2", "legacy_v2"}:
            # This is the sole documented missing-ledger reconstruction state.
            ledger = [(provider, message_id, DEFAULT_EVENT_KEY, int(entry_id)) for provider, message_id, entry_id, _, _ in receipts]
        else:
            raise ReceiptIntegrityError("nutrition receipts have a missing identity ledger")
        receipt_keys: set[tuple[str, str, str, int]] = set()
        validated: list[tuple[str, str, str, int, dict[str, Any]]] = []
        for provider, message_id, raw_entry_id, result_json, stored_digest in receipts:
            identity = canonical_identity((provider, message_id), error_type=ReceiptIntegrityError, context="stored receipt identity")
            entry_id = int(raw_entry_id)
            key = (*identity, entry_id)
            if key in receipt_keys:
                raise ReceiptIntegrityError("nutrition receipts have duplicate identity ownership")
            receipt_keys.add(key)
            row = _canonical_legacy_row(conn, entry_id)
            if row is None:
                raise ReceiptIntegrityError("cannot migrate receipt with missing nutrition row")
            try:
                envelope = json.loads(result_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ReceiptIntegrityError("cannot migrate malformed receipt") from exc
            _require_complete_envelope(envelope, entry_id, _legacy_receipt_entry(row))
            if receipt_shape in {"current", "legacy_v3"} and stored_digest != _legacy_digest(row, envelope):
                raise ReceiptIntegrityError("stored historical nutrition receipt integrity digest mismatch")
            validated.append((identity[0], identity[1], identity[2], entry_id, envelope))
        if len(receipt_keys) != len(ledger) or receipt_keys != set(ledger) or len({entry_id for *_, entry_id in receipt_keys}) != len(receipt_keys):
            raise ReceiptIntegrityError("nutrition receipt and identity ledger are not a complete one-to-one bijection")
        return "legacy", validated

    if legacy_v4:
        # Production v4 had literal provider/message_id anchors and digest-backed
        # two-part receipt/ledger keys, but no event_key dimension.  This is
        # intentionally the only supported two-anchor state.
        if versions != {1, 2, 3, 4} or not has_receipts or not has_ledger or not has_allocator:
            raise SchemaValidationError("legacy v4 nutrition migration state is incomplete or incompatible")
        _preflight_allocator(conn)
        if _receipt_shape(conn) != "legacy_v3":
            raise SchemaValidationError("legacy v4 requires the exact two-part digest receipt schema")
        ledger = _preflight_ledger(conn)
        anchors: set[tuple[str, str, str, int]] = set()
        for entry_id, provider, message_id in conn.execute("SELECT entry_id,ingest_provider,ingest_message_id FROM nutrition_log").fetchall():
            present = [provider is not None, message_id is not None]
            if any(present) and not all(present):
                raise ReceiptIntegrityError("legacy v4 nutrition row identity anchors must be both NULL or both present")
            if provider is not None:
                identity = canonical_identity((provider, message_id), error_type=ReceiptIntegrityError, context="legacy v4 nutrition row identity anchor")
                anchors.add((*identity, int(entry_id)))
        receipt_keys: set[tuple[str, str, str, int]] = set()
        validated: list[tuple[str, str, str, int, dict[str, Any]]] = []
        for provider, message_id, raw_entry_id, result_json, stored_digest, committed_at in conn.execute("SELECT provider,message_id,entry_id,result_json,integrity_digest,committed_at FROM nutrition_ingest_receipts ORDER BY provider,message_id").fetchall():
            identity = canonical_identity((provider, message_id), error_type=ReceiptIntegrityError, context="stored legacy v4 receipt identity")
            entry_id = int(raw_entry_id)
            key = (*identity, entry_id)
            if key in receipt_keys:
                raise ReceiptIntegrityError("legacy v4 nutrition receipts have duplicate identity ownership")
            receipt_keys.add(key)
            row = _canonical_legacy_v4_row(conn, entry_id)
            if row is None:
                raise ReceiptIntegrityError("cannot migrate legacy v4 receipt with missing nutrition row")
            try:
                envelope = json.loads(result_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ReceiptIntegrityError("cannot migrate malformed legacy v4 receipt") from exc
            _require_complete_envelope(envelope, entry_id, _receipt_entry(row))
            if stored_digest != _legacy_v4_digest(row, envelope, (identity[0], identity[1]), entry_id, committed_at):
                raise ReceiptIntegrityError("stored legacy v4 nutrition receipt integrity digest mismatch")
            validated.append((identity[0], identity[1], identity[2], entry_id, envelope))
        if len(receipt_keys) != len(ledger) or receipt_keys != set(ledger) or receipt_keys != anchors or len({entry_id for *_, entry_id in receipt_keys}) != len(receipt_keys):
            raise ReceiptIntegrityError("legacy v4 nutrition receipt, identity ledger, and row anchors are not a complete one-to-one bijection")
        return "legacy", validated

    # A v5-shaped table can only be the fully marked v5 database or the one
    # recoverable post-rebuild/pre-marker state.  Both must already be a fully
    # certified current receipt/ledger/anchor bijection before marker 4 is set.
    if versions not in ({1, 2, 3}, {1, 2, 3, 4}, {1, 2, 3, 5}, set(range(1, SCHEMA_VERSION + 1))) or not has_receipts or not has_ledger or not has_allocator:
        raise SchemaValidationError("current nutrition schema has incompatible migration markers or support tables")
    _preflight_allocator(conn)
    _preflight_current_integrity(conn)
    return "current", []


def _canonical_legacy_row(conn: duckdb.DuckDBPyConnection, entry_id: int) -> dict[str, Any] | None:
    fields = [column[0] for column in _LEGACY_LOG_COLUMNS]
    row = conn.execute(f"SELECT {', '.join(fields)} FROM nutrition_log WHERE entry_id=?", [entry_id]).fetchone()
    return dict(zip(fields, row)) if row else None


def _canonical_legacy_v4_row(conn: duckdb.DuckDBPyConnection, entry_id: int) -> dict[str, Any] | None:
    fields = [column[0] for column in _LEGACY_V4_LOG_COLUMNS]
    row = conn.execute(f"SELECT {', '.join(fields)} FROM nutrition_log WHERE entry_id=?", [entry_id]).fetchone()
    return dict(zip(fields, row)) if row else None


def migrate_database(db_path: str | Path) -> None:
    """Explicit transactional migration. The current marker is written last."""
    with database_lock(db_path, allow_create=True) as db:
        conn = _connect_with_retry(db)
        try:
            conn.execute("BEGIN TRANSACTION")
            # This must precede rebuilding tables, adding anchors, updating
            # digests, or creating a version marker; failure rolls back intact.
            migration_state, validated_receipts = _migration_receipt_preflight(conn)
            _migrate_nutrition_log(conn)
            _create_support_tables(conn)
            _create_compatibility_schema(conn)
            if not conn.execute("SELECT 1 FROM nutrition_entry_id_allocator WHERE allocator_name='nutrition_log'").fetchone():
                conn.execute("INSERT INTO nutrition_entry_id_allocator (allocator_name,next_entry_id) SELECT 'nutrition_log', CAST(COALESCE(MAX(entry_id), 0) AS BIGINT) + 1 FROM nutrition_log")
            for provider, message_id, event_key, entry_id, envelope in validated_receipts:
                anchored = conn.execute("SELECT ingest_provider,ingest_message_id,ingest_event_key FROM nutrition_log WHERE entry_id=?", [entry_id]).fetchone()
                if anchored != (None, None, None) and anchored != (provider, message_id, event_key):
                    raise ReceiptIntegrityError("cannot migrate conflicting nutrition identity anchor")
                conn.execute("UPDATE nutrition_log SET ingest_provider=?, ingest_message_id=?, ingest_event_key=? WHERE entry_id=?", [provider, message_id, event_key, entry_id])
                existing = conn.execute("SELECT entry_id FROM nutrition_ingest_identities WHERE provider=? AND message_id=? AND event_key=?", [provider, message_id, event_key]).fetchone()
                if existing is None:
                    conn.execute("INSERT INTO nutrition_ingest_identities (provider,message_id,event_key,entry_id) VALUES (?,?,?,?)", [provider, message_id, event_key, entry_id])
                elif int(existing[0]) != entry_id:
                    raise ReceiptIntegrityError("cannot migrate conflicting nutrition identity ledger")
                # Upgrade a validated legacy result to the v4 full canonical
                # entry only after all legacy state has passed preflight.
                envelope["result"]["entry"] = _receipt_entry(_canonical_row(conn, entry_id) or {})
                encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
                committed_at = conn.execute("SELECT committed_at FROM nutrition_ingest_receipts WHERE provider=? AND message_id=? AND event_key=?", [provider, message_id, event_key]).fetchone()[0]
                conn.execute("UPDATE nutrition_ingest_receipts SET result_json=?, integrity_digest=? WHERE provider=? AND message_id=? AND event_key=?", [encoded, _digest(_canonical_row(conn, entry_id) or {}, envelope, (provider, message_id, event_key), entry_id, committed_at), provider, message_id, event_key])
            _validate_tables(conn)
            for version in range(1, SCHEMA_VERSION + 1):
                if not conn.execute("SELECT 1 FROM nutrition_schema_migrations WHERE version=?", [version]).fetchone():
                    conn.execute("INSERT INTO nutrition_schema_migrations (version,applied_at) VALUES (?,CURRENT_TIMESTAMP)", [version])
            validate_schema(conn)
            conn.execute("COMMIT")
        except Exception:
            try: conn.execute("ROLLBACK")
            except duckdb.Error: pass
            raise
        finally:
            conn.close()


def _validate_tables(conn: duckdb.DuckDBPyConnection) -> None:
    _require_contract(conn, "nutrition_log", _LOG_COLUMNS, _LOG_CONSTRAINTS)
    if _receipt_shape(conn) != "current":
        raise SchemaValidationError("nutrition_ingest_receipts schema is unsafe")
    _preflight_ledger(conn)
    _preflight_allocator(conn)
    _preflight_migration_metadata(conn)
    index_rows = conn.execute(
        "SELECT index_name FROM duckdb_indexes() WHERE table_name='nutrition_log'"
    ).fetchall()
    indexes = {row[0] for row in index_rows}
    required_indexes = {"idx_nutrition_meal_time", "idx_nutrition_meal_type"}
    if not required_indexes.issubset(indexes):
        raise SchemaValidationError("nutrition_log compatibility indexes are missing")


def validate_schema(conn: duckdb.DuckDBPyConnection) -> None:
    _validate_tables(conn)
    versions = [row[0] for row in conn.execute("SELECT version FROM nutrition_schema_migrations").fetchall()]
    if any(version > SCHEMA_VERSION for version in versions): raise SchemaValidationError("nutrition schema has an unknown future version")
    if set(versions) != set(range(1, SCHEMA_VERSION + 1)):
        raise SchemaValidationError(f"nutrition schema version {SCHEMA_VERSION} is not fully applied; run nutrition_migrate.py")
    _preflight_current_integrity(conn)


@contextlib.contextmanager
def _locked_database(db_path: str | Path) -> Iterator[duckdb.DuckDBPyConnection]:
    with database_lock(db_path, allow_create=False) as db:
        conn = _connect_with_retry(db)
        try:
            validate_schema(conn)
            yield conn
        finally:
            conn.close()


def identity_from_payload(payload: dict[str, Any]) -> tuple[str, str, str] | None:
    if not isinstance(payload, dict): raise ValueError("nutrition payload must be an object")
    identities: list[tuple[str, str]] = []
    if "discord_message_id" in payload:
        identities.append(canonical_identity(("discord", payload.get("discord_message_id"), payload.get("event_key", DEFAULT_EVENT_KEY)), context="payload discord_message_id identity"))
    if "provider" in payload or "message_id" in payload:
        if "provider" not in payload or "message_id" not in payload:
            raise ValueError("top-level payload identity must include both provider and message_id")
        identities.append(canonical_identity((payload.get("provider"), payload.get("message_id"), payload.get("event_key", DEFAULT_EVENT_KEY)), context="payload top-level identity"))
    if "ingest_identity" in payload:
        nested = payload.get("ingest_identity")
        if not isinstance(nested, dict) or set(nested) - {"provider", "message_id", "event_key"} or not {"provider", "message_id"}.issubset(nested):
            raise ValueError("ingest_identity must be an object with provider and message_id and optional event_key")
        identities.append(canonical_identity((nested.get("provider"), nested.get("message_id"), nested.get("event_key", DEFAULT_EVENT_KEY)), context="payload ingest_identity"))
    if not identities: return None
    first = identities[0]
    if any(identity != first for identity in identities[1:]):
        raise ValueError("payload identity forms conflict")
    return first


def _normalize_data(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("nutrition payload must be an object")
    _reject_unknown_keys(data, INGEST_PAYLOAD_FIELDS, "nutrition")
    result = dict(data); value = result.get("meal_time")
    if isinstance(value, datetime): pass
    elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?", value):
        try: datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc: raise ValueError("meal_time must be a finite ISO timestamp with time component") from exc
    else: raise ValueError("meal_time must be a finite ISO timestamp with time component")
    if isinstance(result.get("food_items"), (list, dict)): result["food_items"] = json.dumps(result["food_items"], ensure_ascii=False)
    for field in NUTRIENT_FIELDS:
        if field not in result or result[field] is None:
            continue
        numeric = result[field]
        if isinstance(numeric, bool) or not isinstance(numeric, Real) or not math.isfinite(float(numeric)):
            raise ValueError(f"{field} must be a finite real number, not bool")
    return result


def _reject_unknown_keys(data: dict[str, Any], allowed: frozenset[str], context: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"{context} payload has unknown keys: {', '.join(sorted(unknown))}")


def _canonical_row(conn: duckdb.DuckDBPyConnection, entry_id: int) -> dict[str, Any] | None:
    fields = [column[0] for column in _LOG_COLUMNS]
    row = conn.execute(f"SELECT {', '.join(fields)} FROM nutrition_log WHERE entry_id=?", [entry_id]).fetchone()
    return dict(zip(fields, row)) if row else None


def _validate_result(result: Any, entry_id: int, conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    row = _canonical_row(conn, entry_id)
    if row is None:
        raise ReceiptIntegrityError("stored nutrition receipt references a missing nutrition row")
    # The complete row, including NULL nutrient semantics and identity anchors,
    # is persisted in every committed result rather than a lossy six-field view.
    return _require_complete_envelope({"version": 1, "result": result}, entry_id, _receipt_entry(row))


def _read_receipt(conn: duckdb.DuckDBPyConnection, identity: tuple[str, str]) -> dict[str, Any] | None:
    anchor = conn.execute("SELECT entry_id FROM nutrition_log WHERE ingest_provider=? AND ingest_message_id=? AND ingest_event_key=?", list(identity)).fetchone()
    ledger = conn.execute("SELECT entry_id FROM nutrition_ingest_identities WHERE provider=? AND message_id=? AND event_key=?", list(identity)).fetchone()
    receipt = conn.execute("SELECT entry_id,result_json,integrity_digest,committed_at FROM nutrition_ingest_receipts WHERE provider=? AND message_id=? AND event_key=?", list(identity)).fetchone()
    if not anchor and not ledger and not receipt: return None
    if not anchor or not ledger or not receipt or int(anchor[0]) != int(ledger[0]) or int(anchor[0]) != int(receipt[0]):
        raise ReceiptIntegrityError("nutrition identity anchor, ledger, and receipt disagree")
    try: envelope = json.loads(receipt[1])
    except (TypeError, json.JSONDecodeError) as exc: raise ReceiptIntegrityError("stored nutrition receipt is not valid JSON") from exc
    row = _canonical_row(conn, int(receipt[0]))
    if row is None or receipt[2] != _digest(row, envelope, identity, int(receipt[0]), receipt[3]): raise ReceiptIntegrityError("stored nutrition receipt integrity digest mismatch")
    return _require_complete_envelope(envelope, int(receipt[0]), _receipt_entry(row))


def _allocate_entry_id(conn: duckdb.DuckDBPyConnection) -> int:
    maximum = int(conn.execute("SELECT COALESCE(CAST(MAX(entry_id) AS BIGINT),0) FROM nutrition_log").fetchone()[0])
    cursor = int(conn.execute("SELECT next_entry_id FROM nutrition_entry_id_allocator WHERE allocator_name='nutrition_log'").fetchone()[0])
    entry_id = max(maximum + 1, cursor)
    if entry_id > INT32_MAX: raise EntryIdExhaustedError("nutrition_log.entry_id INTEGER range exhausted; perform an explicit BIGINT migration")
    conn.execute("UPDATE nutrition_entry_id_allocator SET next_entry_id=? WHERE allocator_name='nutrition_log'", [entry_id + 1])
    return entry_id


def _ingest_locked(conn: duckdb.DuckDBPyConnection, data: dict[str, Any], identity: tuple[str, str] | None, result_builder: Callable[[dict[str, Any]], dict[str, Any]] | None) -> dict[str, Any]:
    try:
        conn.execute("BEGIN TRANSACTION")
        if identity:
            prior = _read_receipt(conn, identity)
            if prior is not None:
                conn.execute("COMMIT")
                return {"replayed": True, "result": prior}
        normalized = _normalize_data(data)
        fields = [field for field in PERSISTED_FIELDS if field in normalized]
        entry_id = _allocate_entry_id(conn)
        insert_fields = ["entry_id", *fields]
        values: list[Any] = [entry_id, *[normalized[field] for field in fields]]
        if identity:
            insert_fields.extend(ANCHOR_FIELDS); values.extend(identity)
        conn.execute(f"INSERT INTO nutrition_log ({', '.join(insert_fields)}) VALUES ({', '.join('?' for _ in values)})", values)
        entry = _receipt_entry(_canonical_row(conn, entry_id) or {})
        result = _validate_result(result_builder(entry) if result_builder else {"status": "logged", "entry": entry}, entry_id, conn)
        if identity:
            envelope = {"version": 1, "result": result}
            encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                "INSERT INTO nutrition_ingest_receipts (provider,message_id,event_key,entry_id,result_json,integrity_digest,committed_at) VALUES (?,?,?,?,?,?,?)",
                [identity[0], identity[1], identity[2], entry_id, encoded, "", conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]],
            )
            # Read the value back from its TIMESTAMP column before hashing so
            # timezone/precision coercion cannot change digest material.
            committed_at = conn.execute(
                "SELECT committed_at FROM nutrition_ingest_receipts WHERE provider=? AND message_id=? AND event_key=?",
                list(identity),
            ).fetchone()[0]
            digest = _digest(_canonical_row(conn, entry_id) or {}, envelope, identity, entry_id, committed_at)
            conn.execute(
                "UPDATE nutrition_ingest_receipts SET integrity_digest=? WHERE provider=? AND message_id=? AND event_key=?",
                [digest, identity[0], identity[1], identity[2]],
            )
            conn.execute("INSERT INTO nutrition_ingest_identities (provider,message_id,event_key,entry_id) VALUES (?,?,?,?)", [identity[0], identity[1], identity[2], entry_id])
        conn.execute("COMMIT")
        return {"replayed": False, "result": result}
    except Exception:
        try: conn.execute("ROLLBACK")
        except duckdb.Error: pass
        raise


def _resolve_identity(data: dict[str, Any], identity: tuple[str, ...] | None, context: str = "nutrition") -> tuple[str, str, str] | None:
    _reject_unknown_keys(data, INGEST_PAYLOAD_FIELDS, context)
    payload_identity = identity_from_payload(data)
    if identity is not None:
        resolved = canonical_identity(identity)
        if payload_identity is not None and payload_identity != resolved:
            raise ValueError("explicit identity conflicts with payload identity")
        return resolved
    return payload_identity


def ingest_nutrition(db_path: str | Path, data: dict[str, Any], *, identity: tuple[str, ...] | None = None, result_builder: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("nutrition payload must be an object")
    # Key grammar is validated even on replay; value validation remains after
    # identity lookup so a committed retry can ignore changed/invalid values.
    identity = _resolve_identity(data, identity)
    with _locked_database(db_path) as conn:
        return _ingest_locked(conn, data, identity, result_builder)


def ingest_nutrition_many(db_path: str | Path, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Atomically ingest multiple event-keyed entries from one or more sources."""
    if not isinstance(entries, list) or not entries:
        raise ValueError("entries must be a non-empty list")
    prepared: list[tuple[dict[str, Any], tuple[str, str, str]]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each nutrition entry must be an object")
        identity = _resolve_identity(entry, None, "nutrition entry")
        if identity is None:
            raise ValueError("multi-entry nutrition writes require provider, message_id, and event_key/default identity")
        if identity in seen:
            raise ValueError("multi-entry payload contains duplicate provider/message_id/event_key")
        seen.add(identity)
        prepared.append((entry, identity))
    with _locked_database(db_path) as conn:
        try:
            conn.execute("BEGIN TRANSACTION")
            results = []
            for entry, identity in prepared:
                prior = _read_receipt(conn, identity)
                if prior is not None:
                    results.append({"replayed": True, "result": prior})
                    continue
                normalized = _normalize_data(entry)
                fields = [field for field in PERSISTED_FIELDS if field in normalized]
                entry_id = _allocate_entry_id(conn)
                insert_fields = ["entry_id", *fields, *ANCHOR_FIELDS]
                values: list[Any] = [entry_id, *[normalized[field] for field in fields], *identity]
                conn.execute(f"INSERT INTO nutrition_log ({', '.join(insert_fields)}) VALUES ({', '.join('?' for _ in values)})", values)
                row = _canonical_row(conn, entry_id) or {}
                result = _validate_result({"status": "logged", "entry": _receipt_entry(row)}, entry_id, conn)
                envelope = {"version": 1, "result": result}
                encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
                now = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
                conn.execute(
                    "INSERT INTO nutrition_ingest_receipts (provider,message_id,event_key,entry_id,result_json,integrity_digest,committed_at) VALUES (?,?,?,?,?,?,?)",
                    [identity[0], identity[1], identity[2], entry_id, encoded, "", now],
                )
                committed_at = conn.execute("SELECT committed_at FROM nutrition_ingest_receipts WHERE provider=? AND message_id=? AND event_key=?", list(identity)).fetchone()[0]
                digest = _digest(_canonical_row(conn, entry_id) or {}, envelope, identity, entry_id, committed_at)
                conn.execute("UPDATE nutrition_ingest_receipts SET integrity_digest=? WHERE provider=? AND message_id=? AND event_key=?", [digest, *identity])
                conn.execute("INSERT INTO nutrition_ingest_identities (provider,message_id,event_key,entry_id) VALUES (?,?,?,?)", [*identity, entry_id])
                results.append({"replayed": False, "result": result})
            conn.execute("COMMIT")
            return {"replayed": all(item["replayed"] for item in results), "results": results}
        except Exception:
            try: conn.execute("ROLLBACK")
            except duckdb.Error: pass
            raise


def replay_result(db_path: str | Path, identity: tuple[str, str] | None) -> dict[str, Any] | None:
    if identity is None: return None
    identity = canonical_identity(identity)
    with _locked_database(db_path) as conn:
        conn.execute("BEGIN TRANSACTION")
        try:
            result = _read_receipt(conn, identity)
            conn.execute("COMMIT")
            return result
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _merge_nutrition_row(existing_row: dict[str, Any], normalized: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing_row)
    for field in PERSISTED_FIELDS:
        if field in normalized:
            merged[field] = normalized[field]
    return merged


def _replace_nutrition_row(
    conn: duckdb.DuckDBPyConnection,
    entry_id: int,
    row: dict[str, Any],
    identity: tuple[str, str],
) -> None:
    conn.execute(
        f"UPDATE nutrition_log SET {', '.join(f'{field}=?' for field in PERSISTED_FIELDS)}, ingest_provider=?, ingest_message_id=?, ingest_event_key=? WHERE entry_id=?",
        [*(row.get(field) for field in PERSISTED_FIELDS), identity[0], identity[1], identity[2], entry_id],
    )


def _correct_locked(
    conn: duckdb.DuckDBPyConnection,
    data: dict[str, Any],
    identity: tuple[str, str],
    result_builder: Callable[[dict[str, Any]], dict[str, Any]] | None,
) -> dict[str, Any]:
    try:
        conn.execute("BEGIN TRANSACTION")
        prior = _read_receipt(conn, identity)
        if prior is None:
            raise ReceiptIntegrityError("nutrition correction requires an existing committed nutrition identity")
        receipt_row = conn.execute(
            "SELECT entry_id,committed_at FROM nutrition_ingest_receipts WHERE provider=? AND message_id=? AND event_key=?",
            list(identity),
        ).fetchone()
        if receipt_row is None:
            raise ReceiptIntegrityError("nutrition correction requires an existing committed nutrition identity")
        entry_id = int(receipt_row[0])
        committed_at = receipt_row[1]
        current_row = _canonical_row(conn, entry_id)
        if current_row is None:
            raise ReceiptIntegrityError("stored nutrition receipt references a missing nutrition row")
        normalized = _normalize_data(data)
        merged_row = _merge_nutrition_row(current_row, normalized)
        _replace_nutrition_row(conn, entry_id, merged_row, identity)
        entry = _receipt_entry(_canonical_row(conn, entry_id) or {})
        result = _validate_result(result_builder(entry) if result_builder else {"status": "logged", "entry": entry}, entry_id, conn)
        envelope = {"version": 1, "result": result}
        encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
            "UPDATE nutrition_ingest_receipts SET result_json=?, integrity_digest=? WHERE provider=? AND message_id=? AND event_key=?",
            [encoded, _digest(_canonical_row(conn, entry_id) or {}, envelope, identity, entry_id, committed_at), identity[0], identity[1], identity[2]],
        )
        conn.execute("COMMIT")
        return {"replayed": False, "corrected": True, "result": result}
    except Exception:
        try: conn.execute("ROLLBACK")
        except duckdb.Error: pass
        raise


def correct_nutrition(
    db_path: str | Path,
    data: dict[str, Any],
    *,
    identity: tuple[str, str] | None = None,
    result_builder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("nutrition payload must be an object")
    _reject_unknown_keys(data, INGEST_PAYLOAD_FIELDS, "nutrition correction")
    payload_identity = identity_from_payload(data)
    if identity is not None:
        identity = canonical_identity(identity)
        if payload_identity is not None and payload_identity != identity:
            raise ValueError("explicit identity conflicts with payload identity")
    else:
        identity = payload_identity
    if identity is None:
        raise ValueError("nutrition corrections require provider and message_id")
    with _locked_database(db_path) as conn:
        return _correct_locked(conn, data, identity, result_builder)


def correct_nutrition_result(
    db_path: str | Path,
    data: dict[str, Any],
    *,
    identity: tuple[str, str] | None = None,
    result_builder: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return correct_nutrition(db_path, data, identity=identity, result_builder=result_builder)["result"]


# Deterministic quick-text resolution lives here, rather than in the CLI
# adapter, so the central ingest layer owns the only connection and write path.
QUICK_NUTRIENT_FIELDS = [
    "calories", "protein_g", "carbs_g", "fat_total_g", "fat_saturated_g",
    "fat_unsaturated_g", "fat_trans_g", "fiber_g", "sugar_g", "sodium_mg",
    "cholesterol_mg",
]
QUICK_PORTION_FIELDS = ("portion_g", "grams", "quantity_g", "amount_g", "quantity", "portion")
QUICK_ALIASES = {"牛油果": "avocado", "鳄梨": "avocado", "白煮蛋": "hard-boiled egg", "水煮蛋": "hard-boiled egg", "煮蛋": "hard-boiled egg", "鸡蛋": "egg", "黑咖啡": "black coffee", "咖啡": "coffee", "法棍": "baguette"}
QUICK_DEFAULT_PORTION_G = {"baguette": 30.0, "avocado": 150.0, "hard-boiled egg": 50.0, "egg": 50.0, "black coffee": 240.0, "coffee": 240.0}
QUICK_UNIT_TO_G = {"g": 1.0, "gram": 1.0, "grams": 1.0, "ml": 1.0, "slice": 30.0, "slices": 30.0, "片": 30.0, "egg": 50.0, "eggs": 50.0, "个": 50.0, "cup": 240.0, "cups": 240.0}
QUICK_NUMBER_WORDS = {"a": 1.0, "an": 1.0, "one": 1.0, "two": 2.0, "three": 3.0, "两": 2.0, "二": 2.0, "一": 1.0}


@dataclass
class QuickResolvedItem:
    name: str
    portion_g: float
    nutrients: dict[str, float]
    source: str


def _quick_normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower() if (name or "").strip().lower() not in QUICK_ALIASES else QUICK_ALIASES[(name or "").strip().lower()])


def _quick_json_loads(value: Any) -> Any:
    if value is None: return None
    if isinstance(value, (list, dict)): return value
    if isinstance(value, str):
        try: return json.loads(value)
        except json.JSONDecodeError: return None
    return None


def _quick_float(value: Any) -> float | None:
    if value is None: return None
    if isinstance(value, bool): return None
    if isinstance(value, Real):
        try: parsed = float(value)
        except OverflowError: return None
        return parsed if math.isfinite(parsed) else None
    if not isinstance(value, str): return None
    text = value.strip().lower()
    if text in QUICK_NUMBER_WORDS: return QUICK_NUMBER_WORDS[text]
    if "/" in text:
        try:
            numerator, denominator = text.split("/", 1)
            parsed = float(numerator) / float(denominator)
            return parsed if math.isfinite(parsed) else None
        except (ValueError, ZeroDivisionError, OverflowError): return None
    try: parsed = float(text)
    except (ValueError, OverflowError): return None
    return parsed if math.isfinite(parsed) else None


def _quick_invalid_numeric_fields(item: dict[str, Any]) -> list[str]:
    """Return explicit quick inputs that cannot safely represent finite numbers.

    Quantity/portion text may include units, so ordinary free-form strings keep
    the existing parser behavior.  Booleans, containers, non-finite literals,
    overflowing numeric text, and zero-denominator fractions are never allowed
    to be ignored and replaced by a default or prior nutrition value.
    """
    invalid: list[str] = []
    numeric_fields = (*QUICK_PORTION_FIELDS, *QUICK_NUTRIENT_FIELDS, "fat_g")
    for field in numeric_fields:
        if field not in item or item[field] is None:
            continue
        value = item[field]
        if isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
            invalid.append(field)
            continue
        if isinstance(value, Real):
            try: finite = math.isfinite(float(value))
            except OverflowError: finite = False
            if not finite:
                invalid.append(field)
            continue
        if not isinstance(value, str):
            invalid.append(field)
            continue
        text = value.strip().lower()
        if re.search(r"(?<![a-z0-9_])[-+]?(?:nan|inf(?:inity)?)(?![a-z0-9_])", text):
            invalid.append(field)
            continue
        number_tokens = re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?", text)
        if any(not math.isfinite(float(token)) for token in number_tokens):
            invalid.append(field)
            continue
        fraction = re.search(r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*/\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))", text)
        if fraction and float(fraction.group(2)) == 0:
            invalid.append(field)
            continue
        if field in (*QUICK_NUTRIENT_FIELDS, "fat_g", "portion_g", "grams", "quantity_g", "amount_g") and _quick_float(value) is None:
            invalid.append(field)
    return invalid


def _quick_portion_g(item: dict[str, Any], canonical_name: str) -> float | None:
    for key in ("portion_g", "grams", "quantity_g", "amount_g"):
        value = _quick_float(item.get(key))
        if value is not None: return value
    quantity = _quick_float(item.get("quantity"))
    unit = str(item.get("unit") or "").strip().lower()
    if quantity is not None and unit in QUICK_UNIT_TO_G: return quantity * QUICK_UNIT_TO_G[unit]
    phrase = " ".join(str(item.get(key) or "") for key in ("quantity", "portion", "raw_text"))
    match = re.search(r"(\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?)", phrase)
    if match and canonical_name in QUICK_DEFAULT_PORTION_G:
        parsed = _quick_float(match.group(1).replace(" ", ""))
        if parsed is not None: return parsed * QUICK_DEFAULT_PORTION_G[canonical_name]
    if quantity is not None and canonical_name in QUICK_DEFAULT_PORTION_G: return quantity * QUICK_DEFAULT_PORTION_G[canonical_name]
    return None


def _quick_per_gram(item: dict[str, Any]) -> tuple[float, dict[str, float]] | None:
    if _quick_invalid_numeric_fields(item): return None
    grams = _quick_float(item.get("portion_g") or item.get("grams") or item.get("quantity_g") or item.get("amount_g"))
    if grams is None:
        match = re.search(r"(\d+(?:\.\d+)?)\s*g\b", str(item.get("portion") or "").lower())
        if match: grams = float(match.group(1))
    if not grams or grams <= 0: return None
    nutrients: dict[str, float] = {}
    for field in QUICK_NUTRIENT_FIELDS:
        value = _quick_float(item.get(field))
        if value is None and field == "fat_total_g": value = _quick_float(item.get("fat_g"))
        if value is not None: nutrients[field] = value / grams
    return (grams, nutrients) if nutrients and "calories" in nutrients else None


def _quick_find_prior(conn: duckdb.DuckDBPyConnection, canonical_name: str) -> dict[str, Any] | None:
    candidates = {canonical_name, *[key for key, value in QUICK_ALIASES.items() if value == canonical_name]}
    rows = conn.execute("SELECT entry_id,food_items FROM nutrition_log WHERE food_items IS NOT NULL AND lower(food_items) LIKE ? ORDER BY meal_time DESC, entry_id DESC LIMIT 20", [f"%{canonical_name.lower()}%"]).fetchall()
    for entry_id, raw_items in rows:
        items = _quick_json_loads(raw_items)
        if not isinstance(items, list): continue
        for food in items:
            if not isinstance(food, dict): continue
            food_name = _quick_normalize_name(str(food.get("item") or food.get("name") or ""))
            if food_name == canonical_name or food_name in candidates or canonical_name in food_name or food_name in canonical_name:
                basis = _quick_per_gram(food)
                if basis: return {"entry_id": entry_id, "per_g": basis[1]}
    return None


def _quick_resolve_item(conn: duckdb.DuckDBPyConnection, item: dict[str, Any]) -> QuickResolvedItem | dict[str, Any]:
    canonical = _quick_normalize_name(str(item.get("normalized_name") or item.get("name") or item.get("item") or ""))
    if not canonical: return {"item": item, "reason": "missing_name"}
    invalid_numeric_fields = _quick_invalid_numeric_fields(item)
    if invalid_numeric_fields:
        return {"item": canonical, "reason": "invalid_numeric_value", "fields": invalid_numeric_fields}
    portion_g = _quick_portion_g(item, canonical)
    if portion_g is None or portion_g <= 0: return {"item": canonical, "reason": "missing_or_unknown_quantity"}
    explicit = _quick_per_gram({**item, "portion_g": portion_g})
    required = ("calories", "protein_g", "carbs_g", "fat_total_g")
    if explicit and all(_quick_float(item.get(field)) is not None or (field == "fat_total_g" and _quick_float(item.get("fat_g")) is not None) for field in required):
        return QuickResolvedItem(canonical, portion_g, {field: round(explicit[1].get(field, 0.0) * portion_g, 3) for field in QUICK_NUTRIENT_FIELDS}, "payload")
    prior = _quick_find_prior(conn, canonical)
    if not prior: return {"item": canonical, "reason": "no_prior_nutrition"}
    return QuickResolvedItem(canonical, portion_g, {field: round(prior["per_g"].get(field, 0.0) * portion_g, 3) for field in QUICK_NUTRIENT_FIELDS}, f"prior_entry:{prior['entry_id']}")


def _quick_exact_reuse(conn: duckdb.DuckDBPyConnection, payload: dict[str, Any]) -> dict[str, Any] | None:
    reuse = payload.get("reuse") if isinstance(payload.get("reuse"), dict) else {}
    if (payload.get("reuse_mode") or reuse.get("mode")) != "exact": return None
    entry_id = reuse.get("entry_id", payload.get("reuse_entry_id"))
    if isinstance(entry_id, bool): return None
    if isinstance(entry_id, str) and re.fullmatch(r"[1-9][0-9]*", entry_id): entry_id = int(entry_id)
    if not isinstance(entry_id, int) or entry_id <= 0 or entry_id > INT32_MAX: return None
    fields = ["entry_id", "meal_name", "food_items", *NUTRIENT_FIELDS]
    row = conn.execute(f"SELECT {', '.join(fields)} FROM nutrition_log WHERE entry_id=?", [entry_id]).fetchone()
    return dict(zip(fields, row)) if row else None


def _quick_entry_data(payload: dict[str, Any], food_items: list[dict[str, Any]], totals: dict[str, Any], source_suffix: str) -> dict[str, Any]:
    return {"meal_time": payload.get("meal_time"), "meal_type": payload.get("meal_type"), "meal_name": payload.get("meal_name") or ", ".join(item["item"] for item in food_items[:4]) or "Quick logged meal", "meal_description": payload.get("raw_text"), "food_items": food_items, **totals, "source": f"quick-log-text:{source_suffix}", "notes": payload.get("notes")}


def resolve_and_ingest_quick_text(
    db_path: str | Path,
    payload: dict[str, Any],
    *,
    defaults_loaded: bool = False,
    data_dir: str | None = None,
    allow_anonymous: bool = False,
) -> dict[str, Any]:
    """Public path-based quick resolver/ingester; owns lock, replay, and commit."""
    if not isinstance(payload, dict):
        raise ValueError("quick nutrition payload must be an object")
    _reject_unknown_keys(payload, QUICK_PAYLOAD_FIELDS, "quick nutrition")
    identity = identity_from_payload(payload)
    with _locked_database(db_path) as conn:
        if identity:
            replayed = _read_receipt(conn, identity)
            if replayed is not None: return replayed
        exact = _quick_exact_reuse(conn, payload)
        if exact:
            if identity is None and not allow_anonymous:
                raise ValueError("quick nutrition writes require provider and message_id; anonymous writes are non-idempotent")
            totals = {field: exact.get(field) for field in NUTRIENT_FIELDS}
            food_items = _quick_json_loads(exact.get("food_items")) or []
            data = _quick_entry_data({**payload, "meal_name": payload.get("meal_name") or exact.get("meal_name")}, food_items, totals, f"exact-reuse:{exact['entry_id']}")
            def exact_result(entry: dict[str, Any]) -> dict[str, Any]:
                calories, protein = entry["calories"] if entry["calories"] is not None else 0, entry["protein_g"] if entry["protein_g"] is not None else 0
                return {"status": "logged", "mode": "exact_reuse", "entry": entry, "reused_entry_id": exact["entry_id"], "summary": f"Logged #{entry['entry_id']} from exact reuse of #{exact['entry_id']}: {calories:.0f} kcal, {protein:.1f}g protein"}
            return _ingest_locked(conn, data, identity, exact_result)["result"]
        if payload.get("reuse_mode") == "exact" or (isinstance(payload.get("reuse"), dict) and payload["reuse"].get("mode") == "exact"):
            return {"status": "needs_clarification", "reasons": [{"reason": "explicit_reuse_entry_id_required"}], "summary": "Exact reuse requires an explicit prior entry_id."}
        items = payload.get("items") or []
        if not isinstance(items, list) or not items: return {"status": "needs_clarification", "reasons": [{"reason": "no_items"}], "defaults_loaded": defaults_loaded}
        resolved: list[QuickResolvedItem] = []; unresolved: list[dict[str, Any]] = []
        for item in items:
            outcome = _quick_resolve_item(conn, item) if isinstance(item, dict) else {"item": item, "reason": "item_not_object"}
            (resolved if isinstance(outcome, QuickResolvedItem) else unresolved).append(outcome)  # type: ignore[arg-type]
        if unresolved:
            result: dict[str, Any] = {"status": "needs_clarification", "reasons": unresolved, "resolved_items": [item.name for item in resolved], "summary": "Need nutrition/portion clarification for: " + ", ".join(str(item.get("item")) for item in unresolved), "defaults_loaded": defaults_loaded}
            if data_dir is not None: result["data_dir"] = data_dir
            result["db_path"] = str(db_path)
            return result
        if identity is None and not allow_anonymous:
            raise ValueError("quick nutrition writes require provider and message_id; anonymous writes are non-idempotent")
        totals = {field: round(sum(item.nutrients.get(field, 0.0) for item in resolved), 1) for field in QUICK_NUTRIENT_FIELDS}; totals["calories"] = round(totals["calories"])
        food_items = [{"item": item.name, "portion_g": round(item.portion_g, 1), "source": item.source, **{field: round(item.nutrients.get(field, 0.0), 1) for field in QUICK_NUTRIENT_FIELDS}} for item in resolved]
        data = _quick_entry_data(payload, food_items, totals, "ingredient-reuse")
        def ingredient_result(entry: dict[str, Any]) -> dict[str, Any]:
            return {"status": "logged", "mode": "ingredient_reuse", "entry": entry, "items": food_items, "totals": totals, "summary": f"Logged #{entry['entry_id']}: {totals['calories']:.0f} kcal, {totals['protein_g']:.1f}g protein, {totals['carbs_g']:.1f}g carbs, {totals['fat_total_g']:.1f}g fat", "defaults_loaded": defaults_loaded}
        return _ingest_locked(conn, data, identity, ingredient_result)["result"]
