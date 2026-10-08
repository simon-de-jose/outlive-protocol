#!/usr/bin/env python3
"""Read-only local nutrition retrieval for agent-assisted meal logging.

This module recalls prior meals, ingredient nutrition bases, recipes, aliases,
and explicitly supplied user defaults.  It never writes, calls the network, or
turns free text into a committed nutrition payload.  The caller/agent remains
responsible for interpreting the original message and choosing any write.
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import re
import sys
import unicodedata
from datetime import date, datetime, time as datetime_time
from pathlib import Path
from typing import Any, Iterable

import duckdb

SCHEMA_VERSION = 1
DEFAULT_TOP_K = 8
MAX_TOP_K = 50
MIN_CANDIDATE_SCORE = 0.50
ACTIONABLE_SCORE = 0.68

NUTRIENT_FIELDS = (
    "calories",
    "protein_g",
    "carbs_g",
    "fat_total_g",
    "fat_saturated_g",
    "fat_unsaturated_g",
    "fat_trans_g",
    "fiber_g",
    "sugar_g",
    "sodium_mg",
    "potassium_mg",
    "calcium_mg",
    "iron_mg",
    "magnesium_mg",
    "vitamin_d_mcg",
    "vitamin_b12_mcg",
    "vitamin_c_mg",
    "cholesterol_mg",
)
NUTRIENT_ALIASES = {
    "energy_kcal": "calories",
    "kcal": "calories",
    "fat_g": "fat_total_g",
    "total_fat_g": "fat_total_g",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+", re.IGNORECASE)
_LATIN_RE = re.compile(r"^[a-z0-9]+$")
_QUERY_STOPWORDS = {
    "a", "an", "and", "ate", "for", "had", "have", "i", "log", "meal",
    "my", "of", "please", "some", "the", "this", "today", "with",
    "份", "吃", "吃了", "和", "还有", "一份", "一个", "两片", "片",
}
_BRAND_RESTAURANT_TERMS = {
    "cava", "chipotle", "costco", "ikea", "innout", "mcdonalds", "nijiya",
    "pandaexpress", "starbucks", "subway", "sweetgreen", "tacobell", "traderjoes", "wendys",
}
_BRAND_RESTAURANT_CLASSIFIERS = {
    "brand", "branded", "chain", "packaged", "restaurant",
}
_PHOTO_TERMS = {"image", "photo", "picture", "照片", "图片"}
_AMBIGUOUS_TERMS = {
    "about", "approximately", "bit", "handful", "maybe", "perhaps", "some",
    "unclear", "unknown", "unsure", "大概", "一些", "不确定",
}
_MIXED_DISH_TERMS = {"casserole", "curry", "mixed dish", "stew", "大杂烩", "咖喱", "炖菜"}
_REUSE_TRIGGER_PHRASES = (
    "same as before",
    "same as last time",
    "same as yesterday",
    "same thing",
    "same meal",
    "same breakfast again",
    "same lunch again",
    "same dinner again",
    "same snack again",
    "same breakfast",
    "same lunch",
    "same dinner",
    "same snack",
    "the usual",
    "as usual",
    "usual breakfast",
    "usual lunch",
    "usual dinner",
    "usual snack",
    "usual",
    "again",
    "和以前一样",
    "跟之前一样",
    "照旧",
)
_CONTEXT_BOOLEAN_FIELDS = {
    "ambiguous",
    "brand_or_restaurant",
    "has_image",
    "has_photo",
    "image",
    "mixed_dish",
    "photo",
}
_CONTEXT_BRAND_FIELDS = {"brand", "chain", "packaged_product", "restaurant"}
_PUBLIC_CONTEXT_ENUMS = {
    "meal_type": {"breakfast", "lunch", "dinner", "snack", "other", "unknown"},
    "input_kind": {"text", "photo", "image", "mixed", "unknown"},
}
_CANDIDATE_KEYS = (
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
)


class RetrievalError(RuntimeError):
    """Safe, path-free retrieval failure suitable for CLI reporting."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class JsonArgumentParser(argparse.ArgumentParser):
    """Argparse variant whose failures are handled by the JSON CLI boundary."""

    def error(self, message: str) -> None:
        raise RetrievalError("invalid_arguments", "command arguments are invalid")


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _loads_json(value: str) -> Any:
    return json.loads(value, parse_constant=_reject_json_constant)


def _validate_json_like(value: Any, *, code: str, message: str) -> None:
    """Accept only values representable by strict JSON (finite numbers only)."""
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise RetrievalError(code, message)
    if isinstance(value, list):
        for item in value:
            _validate_json_like(item, code=code, message=message)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise RetrievalError(code, message)
            _validate_json_like(item, code=code, message=message)
        return
    raise RetrievalError(code, message)


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("’", "'").replace("&", " and ")
    return " ".join(_TOKEN_RE.findall(text))


def _tokens(value: Any) -> list[str]:
    return [token for token in _normalize(value).split() if token not in _QUERY_STOPWORDS and not token.isdigit()]


def _safe_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date, datetime_time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in value.items()
            if isinstance(key, str)
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return None


def _public_context(context: dict[str, Any]) -> dict[str, Any]:
    """Return only validated routing/classification fields, never caller blobs."""
    public: dict[str, Any] = {}
    for field, allowed in _PUBLIC_CONTEXT_ENUMS.items():
        if field not in context:
            continue
        value = context[field]
        if not isinstance(value, str) or value.casefold() not in allowed:
            raise RetrievalError("invalid_context_value", "context classification values are invalid")
        public[field] = value.casefold()

    if any(context.get(field) is True for field in ("has_photo", "has_image", "photo", "image")):
        public["has_photo"] = True
    if context.get("brand_or_restaurant") is True or any(
        context.get(field) is True
        or (isinstance(context.get(field), str) and bool(context.get(field).strip()))
        for field in _CONTEXT_BRAND_FIELDS
    ):
        public["brand_or_restaurant"] = True
    for field in ("mixed_dish", "ambiguous"):
        if context.get(field) is True:
            public[field] = True
    return public


def _json_object(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _loads_json(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _normalized_nutrients(source: dict[str, Any], *, recipe_totals: bool = False) -> dict[str, float]:
    nutrients: dict[str, float] = {}
    for raw_key, raw_value in source.items():
        key = str(raw_key)
        if recipe_totals and key.startswith("total_"):
            key = key.removeprefix("total_")
        key = NUTRIENT_ALIASES.get(key, key)
        if key not in NUTRIENT_FIELDS:
            continue
        number = _safe_number(raw_value)
        if number is not None:
            nutrients[key] = round(number, 6)
    return nutrients


def _item_name(item: dict[str, Any]) -> str:
    return str(item.get("item") or item.get("name") or item.get("normalized_name") or "").strip()


def _item_phrases(item: dict[str, Any]) -> list[str]:
    phrases = [_item_name(item)]
    for key in ("aliases", "alias", "names"):
        raw = _json_object(item.get(key)) or item.get(key)
        if isinstance(raw, list):
            phrases.extend(str(value) for value in raw)
        elif isinstance(raw, str):
            phrases.extend(part.strip() for part in re.split(r"[,;|]", raw))
    return [phrase for phrase in phrases if phrase]


def _per_item_basis(item: dict[str, Any]) -> dict[str, Any] | None:
    name = _item_name(item)
    nested = None
    for key in ("per_100g", "nutrients_per_100g", "per100g"):
        value = _json_object(item.get(key)) or item.get(key)
        if isinstance(value, dict):
            nested = value
            break

    direct_nutrients = _normalized_nutrients(item)
    portion_g = next(
        (number for number in (_safe_number(item.get(key)) for key in ("portion_g", "grams", "quantity_g", "amount_g")) if number is not None and number > 0),
        None,
    )
    portion_description = str(item.get("portion") or "").strip()

    result: dict[str, Any] = {"item": name or "unnamed item"}
    source_identifier = item.get("fdc_id") or item.get("source_id")
    if source_identifier is not None:
        result["source_identifier"] = str(source_identifier)

    if nested is not None:
        per_100g = _normalized_nutrients(nested)
        if per_100g:
            result["basis"] = {"kind": "per_100g", "amount": 100.0, "unit": "g"}
            result["nutrients"] = per_100g
            if direct_nutrients:
                result["logged_portion_nutrients"] = direct_nutrients
            return result

    if direct_nutrients and portion_g is not None:
        result["basis"] = {"kind": "logged_portion", "amount": round(portion_g, 6), "unit": "g"}
        result["nutrients"] = direct_nutrients
        result["per_100g_nutrients"] = {
            key: round(value * 100.0 / portion_g, 6) for key, value in direct_nutrients.items()
        }
        return result

    if direct_nutrients and portion_description:
        result["basis"] = {"kind": "logged_portion", "description": portion_description}
        result["nutrients"] = direct_nutrients
        return result

    return None


def _term_similarity(query: str, candidate: str) -> float:
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0
    if min(len(query), len(candidate)) <= 2:
        return 0.0
    ratio = difflib.SequenceMatcher(None, query, candidate, autojunk=False).ratio()
    if _LATIN_RE.fullmatch(query) and _LATIN_RE.fullmatch(candidate):
        if query[0] != candidate[0]:
            ratio *= 0.67
        if abs(len(query) - len(candidate)) > max(3, int(max(len(query), len(candidate)) * 0.4)):
            ratio *= 0.72
    return ratio


def _score_phrases(query_terms: list[str], phrases: Iterable[str], *, whole: bool) -> tuple[float, str]:
    """Score phrase/term coverage before isolated token collisions.

    ``whole=False`` used to take the best single token, so "homemade latte"
    tied "Fior di Latte" on ``latte``. Multi-token queries now pay a coverage
    penalty while single-token typo/alias behavior remains unchanged.
    """
    normalized_phrases = [_normalize(phrase) for phrase in phrases if _normalize(phrase)]
    if not normalized_phrases or not query_terms:
        return 0.0, "none"
    query_phrase = " ".join(query_terms)
    if query_phrase in normalized_phrases:
        return 1.0, "exact_name"

    candidate_tokens = [token for phrase in normalized_phrases for token in phrase.split()]
    scores = [max((_term_similarity(term, token) for token in candidate_tokens), default=0.0) for term in query_terms]
    if not scores:
        return 0.0, "none"
    exact_count = sum(score == 1.0 for score in scores)
    strong = [score for score in scores if score >= 0.56]
    if whole or len(scores) > 1:
        # Additional weaker matches must not lower an otherwise stronger
        # multi-token match. Score the best prefix of strongest evidence so
        # exact core-token coverage can still use recency as a tie-breaker.
        ordered = sorted(strong, reverse=True)
        score = max(
            (
                (sum(ordered[:count]) / count)
                * (0.55 + 0.45 * (count / len(scores)))
                for count in range(1, len(ordered) + 1)
            ),
            default=0.0,
        )
        # Preserve a modest phrase signal when the requested phrase occurs
        # within a longer candidate label/alias.
        if any(query_phrase and query_phrase in phrase for phrase in normalized_phrases):
            score = max(score, 0.97)
    else:
        score = scores[0]

    if exact_count == len(scores):
        match_type = "token_exact"
    elif exact_count:
        match_type = "token_overlap"
    else:
        match_type = "fuzzy_name"
    return min(score, 0.985), match_type


def _table_names(conn: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
    }


def _rows(conn: duckdb.DuckDBPyConnection, table: str) -> list[dict[str, Any]]:
    cursor = conn.execute(f'SELECT * FROM "{table}"')
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _read_defaults(profile_path: str | Path | None) -> tuple[dict[str, Any], str]:
    if profile_path is None:
        return {}, "not_supplied"
    path = Path(profile_path).expanduser()
    if not path.is_file():
        return {}, "unavailable"
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}, "unavailable"
    defaults = data.get("nutrition_defaults", {}) if isinstance(data, dict) else {}
    return (defaults, "available") if isinstance(defaults, dict) else ({}, "unavailable")


def _recipe_aliases(
    tables: set[str],
    recipe_rows: list[dict[str, Any]],
    alias_rows: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    aliases: dict[str, list[dict[str, Any]]] = {}
    for recipe in recipe_rows:
        recipe_id = recipe.get("id", recipe.get("recipe_id"))
        key = str(recipe_id)
        raw = _json_object(recipe.get("aliases")) or recipe.get("aliases")
        values: list[Any] = []
        if isinstance(raw, list):
            values = raw
        elif isinstance(raw, str):
            values = [part.strip() for part in re.split(r"[,;|]", raw)]
        aliases[key] = [
            {"alias": str(value), "table": "recipes"} for value in values if str(value).strip()
        ]

    count = 0
    if "recipe_aliases" not in tables:
        return aliases, count
    by_name = {_normalize(recipe.get("name")): recipe for recipe in recipe_rows}
    for row in alias_rows:
        alias = row.get("alias", row.get("name"))
        if not alias:
            continue
        recipe_id = row.get("recipe_id", row.get("id"))
        if recipe_id is None and row.get("recipe_name"):
            recipe = by_name.get(_normalize(row.get("recipe_name")))
            recipe_id = recipe.get("id", recipe.get("recipe_id")) if recipe else None
        if recipe_id is None:
            continue
        aliases.setdefault(str(recipe_id), []).append(
            {
                "alias": str(alias),
                "table": "recipe_aliases",
                "alias_id": row.get("alias_id"),
            }
        )
        count += 1
    for values in aliases.values():
        values.sort(
            key=lambda item: (
                _normalize(item.get("alias")),
                str(item.get("table") or ""),
                str(item.get("alias_id") or ""),
            )
        )
    return aliases, count


def _safety_reasons(text: str, context: dict[str, Any]) -> list[str]:
    normalized = _normalize(text)
    compact = normalized.replace(" ", "")
    tokens = set(normalized.split())
    reasons: list[str] = []

    if (
        any(context.get(key) is True for key in ("has_photo", "has_image", "photo", "image"))
        or (
            isinstance(context.get("input_kind"), str)
            and context["input_kind"].casefold() in {"photo", "image", "mixed"}
        )
        or tokens.intersection(_PHOTO_TERMS)
    ):
        reasons.append("visual_input_requires_agent")
    explicit_brand = context.get("brand_or_restaurant") is True or any(
        context.get(key) is True or (isinstance(context.get(key), str) and bool(context.get(key).strip()))
        for key in _CONTEXT_BRAND_FIELDS
    )
    if (
        explicit_brand
        or tokens.intersection(_BRAND_RESTAURANT_CLASSIFIERS)
        or any(term in compact for term in _BRAND_RESTAURANT_TERMS)
    ):
        reasons.append("brand_or_restaurant_requires_external_source")
    if context.get("mixed_dish") is True or any(term in normalized for term in _MIXED_DISH_TERMS):
        reasons.append("mixed_dish_requires_agent")
    if context.get("ambiguous") is True or tokens.intersection(_AMBIGUOUS_TERMS):
        reasons.append("ambiguous_input_requires_agent")
    return reasons


def _clear_reuse_intent(text: str, structured_terms: Iterable[str]) -> bool:
    normalized = _normalize(" ".join([text, *structured_terms]))
    compact = normalized.replace(" ", "")
    if not normalized:
        return False
    for phrase in _REUSE_TRIGGER_PHRASES:
        normalized_phrase = _normalize(phrase)
        if " " in normalized_phrase:
            if normalized_phrase in normalized:
                return True
        elif normalized_phrase in compact:
            # Keep single-token triggers conservative: only fire when the
            # normalized text is itself the trigger or a very close variant.
            if normalized_phrase == "again" and compact == "again":
                return True
            if normalized_phrase in {"usual", "same", "照旧"} and compact == normalized_phrase:
                return True
            if normalized_phrase in {"和以前一样", "跟之前一样"} and normalized_phrase in compact:
                return True
    return False


def _meal_time_sort_text(value: Any) -> str:
    text = _json_value(value)
    return str(text or "")


def _meal_entry_id(row: dict[str, Any]) -> Any:
    entry_id = row.get("entry_id")
    if entry_id is None:
        entry_id = row.get("id")
    return entry_id


def _historical_meal_label(row: dict[str, Any]) -> str:
    for field in ("meal_name", "meal_description"):
        value = str(row.get(field) or "").strip()
        if value:
            return value
    meal_time = _meal_time_sort_text(row.get("meal_time"))
    entry_id = _meal_entry_id(row)
    if meal_time:
        return f"Meal {meal_time}"
    if entry_id is not None:
        return f"Meal {entry_id}"
    return "Historical meal"


def _meal_type_filter(context: dict[str, Any]) -> str | None:
    meal_type = context.get("meal_type")
    if not isinstance(meal_type, str):
        return None
    meal_type = meal_type.casefold().strip()
    return meal_type if meal_type and meal_type != "unknown" else None


def _recent_meal_candidates(
    rows: list[dict[str, Any]],
    *,
    meal_type: str | None,
    used_entry_ids: set[Any],
) -> list[dict[str, Any]]:
    recent_rows = [row for row in rows if _meal_entry_id(row) is not None]
    if meal_type is not None:
        recent_rows = [
            row for row in recent_rows
            if str(row.get("meal_type") or "").casefold().strip() == meal_type
        ]
    recent_rows.sort(
        key=lambda row: (
            _meal_time_sort_text(row.get("meal_time")),
            str(_meal_entry_id(row)),
        ),
        reverse=True,
    )
    candidates: list[dict[str, Any]] = []
    for index, row in enumerate(recent_rows):
        entry_id = _meal_entry_id(row)
        if entry_id in used_entry_ids:
            continue
        raw_items = _json_object(row.get("food_items"))
        items = raw_items if isinstance(raw_items, list) else []
        candidates.append(
            _candidate(
                candidate_type="historical_meal",
                label=_historical_meal_label(row),
                score=max(MIN_CANDIDATE_SCORE, round(0.995 - (index * 0.001), 6)),
                match_type="historical_recent_baseline",
                suggested_action="needs_agent_decision",
                provenance={"table": "nutrition_log", "entry_id": entry_id},
                meal_time=row.get("meal_time"),
                nutrients=_normalized_nutrients(row),
                per_item_basis=[basis for item in items if isinstance(item, dict) and (basis := _per_item_basis(item))],
                reason_codes=[
                    "historical_baseline_requires_confirmation",
                    *( ["meal_type_filter_applied"] if meal_type is not None else [] ),
                ],
            )
        )
        used_entry_ids.add(entry_id)
    return candidates


def _validate_context(context: Any) -> dict[str, Any]:
    if context is None:
        return {}
    if not isinstance(context, dict):
        raise RetrievalError("invalid_context", "context must be a JSON-like object")
    _validate_json_like(
        context,
        code="invalid_context_value",
        message="context must contain only finite JSON values",
    )
    for field in _CONTEXT_BOOLEAN_FIELDS:
        if field in context and not isinstance(context[field], bool):
            raise RetrievalError("invalid_context_value", "context classification values are invalid")
    for field in _CONTEXT_BRAND_FIELDS:
        if field not in context:
            continue
        value = context[field]
        if not isinstance(value, (bool, str)) or (isinstance(value, str) and not value.strip()):
            raise RetrievalError("invalid_context_value", "context classification values are invalid")
    # Validate the public enum fields before any database access.
    _public_context(context)
    return dict(context)


def _candidate(
    *,
    candidate_type: str,
    label: str,
    score: float,
    match_type: str,
    suggested_action: str,
    provenance: dict[str, Any],
    meal_time: Any = None,
    nutrients: dict[str, float] | None = None,
    per_item_basis: list[dict[str, Any]] | None = None,
    reason_codes: list[str],
) -> dict[str, Any]:
    """Build the fixed public candidate schema; per_item_basis is always a list."""
    return {
        "rank": 0,
        "candidate_type": candidate_type,
        "label": label,
        "score": round(score, 6),
        "match_type": match_type,
        "suggested_action": suggested_action,
        "provenance": provenance,
        "meal_time": _json_value(meal_time),
        "nutrients": nutrients or {},
        "per_item_basis": per_item_basis or [],
        "reason_codes": reason_codes,
    }


def _raw_vs_cooked_basis(query_text: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized = _normalize(query_text)
    grain_terms = {"rice", "米", "米饭", "糙米", "白米", "quinoa", "oats", "oatmeal"}
    if not any(term in normalized for term in grain_terms):
        return None
    observed: dict[str, int] = {"raw": 0, "cooked": 0, "unspecified": 0}
    examples: list[dict[str, Any]] = []
    for candidate in candidates:
        text_parts = [str(candidate.get("label") or "")]
        for basis in candidate.get("per_item_basis") or []:
            if isinstance(basis, dict):
                text_parts.extend(str(basis.get(key) or "") for key in ("item", "portion", "basis", "source"))
        joined = _normalize(" ".join(text_parts))
        if not any(term in joined for term in grain_terms):
            continue
        state = "unspecified"
        if re.search(r"\b(raw|dry|uncooked)\b", joined):
            state = "raw"
        elif re.search(r"\b(cooked|steamed)\b", joined) or "米饭" in joined:
            state = "cooked"
        observed[state] += 1
        if len(examples) < 3:
            examples.append({
                "label": candidate.get("label"),
                "basis": state,
                "entry_id": (candidate.get("provenance") or {}).get("entry_id") if isinstance(candidate.get("provenance"), dict) else None,
            })
    if not any(observed.values()):
        return {"status": "unresolved", "instruction": "grain amount mentioned; resolve raw vs cooked before writing"}
    likely = max(observed, key=observed.get)
    return {"status": "resolved_from_history" if observed[likely] else "unresolved", "likely_basis": likely, "observed_counts": observed, "examples": examples, "instruction": "use historical routine basis for usual grains; ask if current wording conflicts"}


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    type_priority = {
        "recipe": 0,
        "historical_meal": 1,
        "ingredient_basis": 2,
        "user_default": 3,
    }
    exact_priority = 0 if candidate["match_type"] in {"recipe_exact_name", "recipe_exact_alias"} else 1
    provenance_key = json.dumps(candidate["provenance"], ensure_ascii=False, sort_keys=True, allow_nan=False)
    context_priority = 0 if "meal_type_context_match" in candidate.get("reason_codes", []) else 1
    return (
        -candidate["score"],
        exact_priority,
        context_priority,
        type_priority.get(candidate["candidate_type"], 9),
        # Context and recency are signals only after lexical score/exactness.
        tuple(-ord(char) for char in (candidate["meal_time"] or "")),
        candidate["label"].casefold(),
        provenance_key,
    )


def retrieve_nutrition(
    db_path: str | Path,
    query: str | dict[str, Any] | None = None,
    *,
    text: str | None = None,
    terms: list[str] | tuple[str, ...] | None = None,
    meal_time: str | None = None,
    context: dict[str, Any] | None = None,
    profile_path: str | Path | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """Return deterministic, read-only nutrition retrieval candidates.

    ``db_path`` is mandatory and has no live-data default.  ``query`` may be a
    raw string or an object with ``text``, ``terms``, ``meal_time``, ``context``,
    ``profile_path``, and ``top_k`` fields.  Every candidate uses the same fixed
    keys and ``per_item_basis`` is always a list.  Filesystem paths are never
    emitted.
    """
    if isinstance(query, dict):
        text = query.get("text", query.get("raw_text", text))
        terms = query.get("terms", query.get("query_terms", terms))
        meal_time = query.get("meal_time", meal_time)
        context = query.get("context", context)
        profile_path = query.get("profile_path", profile_path)
        top_k = query.get("top_k", top_k)
    elif isinstance(query, str) and text is None:
        text = query
    elif query is not None:
        raise RetrievalError("invalid_query", "query must be text or an object")

    if not isinstance(db_path, (str, Path)) or isinstance(db_path, bool):
        raise RetrievalError("database_unavailable", "the explicitly supplied database is unavailable")
    if text is not None and not isinstance(text, str):
        raise RetrievalError("invalid_text", "text must be a string")
    if terms is not None and not isinstance(terms, (list, tuple)):
        raise RetrievalError("invalid_query_terms", "terms must be a list of strings")
    if terms is not None and any(not isinstance(term, str) for term in terms):
        raise RetrievalError("invalid_query_terms", "terms must be a list of strings")
    structured_terms = [term.strip() for term in (terms or []) if term.strip()]
    text = (text or "").strip()
    if not text and not structured_terms:
        raise RetrievalError("empty_query", "text or at least one query term is required")
    if meal_time is not None and not isinstance(meal_time, str):
        raise RetrievalError("invalid_meal_time", "meal_time must be a string")
    context = _validate_context(context)
    if profile_path is not None and (
        not isinstance(profile_path, (str, Path)) or isinstance(profile_path, bool)
    ):
        raise RetrievalError("invalid_profile_path", "profile_path must be a string")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
        raise RetrievalError("invalid_top_k", f"top_k must be an integer from 1 to {MAX_TOP_K}")

    path = Path(db_path).expanduser()
    if not path.is_file():
        raise RetrievalError("database_unavailable", "the explicitly supplied database is unavailable")

    query_terms = [token for term in structured_terms for token in _tokens(term)] or _tokens(text)
    if not query_terms:
        # Safety/pass-through requests such as "log this" with an image still
        # need a valid empty-candidate response even if every word is retrieval
        # noise.  These fallback tokens cannot create an exact recipe label.
        query_terms = _normalize(" ".join([text, *structured_terms])).split()
    if not query_terms:
        query_terms = ["__context_only__"]
    full_query = _normalize(" ".join([text, *structured_terms]))

    candidates: list[dict[str, Any]] = []
    source_counts = {
        "nutrition_log": 0,
        "food_items": 0,
        "recipes": 0,
        "recipe_aliases": 0,
        "user_defaults": 0,
    }
    reuse_intent = _clear_reuse_intent(text, structured_terms)
    meal_type_filter = _meal_type_filter(context)

    try:
        conn = duckdb.connect(str(path), read_only=True)
    except Exception as exc:
        raise RetrievalError("database_unavailable", "the explicitly supplied database cannot be opened read-only") from exc

    try:
        tables = _table_names(conn)
        nutrition_rows = _rows(conn, "nutrition_log") if "nutrition_log" in tables else []
        recipe_rows = _rows(conn, "recipes") if "recipes" in tables else []
        alias_rows = _rows(conn, "recipe_aliases") if "recipe_aliases" in tables else []
        source_counts["nutrition_log"] = len(nutrition_rows)
        source_counts["recipes"] = len(recipe_rows)
        aliases_by_recipe, alias_count = _recipe_aliases(tables, recipe_rows, alias_rows)
        source_counts["recipe_aliases"] = alias_count
        historical_entry_ids: set[Any] = set()

        # Exact recipe names and explicit aliases receive precedence.  Fuzzy
        # recipe retrieval remains candidate-only by construction.
        for recipe in recipe_rows:
            recipe_id = recipe.get("id", recipe.get("recipe_id"))
            canonical = str(recipe.get("name") or "").strip()
            if not canonical:
                continue
            aliases = aliases_by_recipe.get(str(recipe_id), [])
            canonical_normalized = _normalize(canonical)
            exact_alias = next((alias for alias in aliases if _normalize(alias["alias"]) == full_query), None)
            if canonical_normalized == full_query:
                score, match_type, action = 1.0, "recipe_exact_name", "recipe_exact_candidate"
                reasons = ["exact_recipe_name", "agent_must_confirm_recipe_use"]
            elif exact_alias is not None:
                score, match_type, action = 0.999, "recipe_exact_alias", "recipe_exact_candidate"
                reasons = ["exact_recipe_alias", "agent_must_confirm_recipe_use"]
            else:
                score, _ = _score_phrases(query_terms, [canonical, *[alias["alias"] for alias in aliases]], whole=True)
                match_type, action = "recipe_fuzzy", "needs_agent_decision"
                reasons = ["fuzzy_recipe_requires_agent"]
                score = min(score, 0.94)
            if score < MIN_CANDIDATE_SCORE:
                continue
            food_items = _json_object(recipe.get("food_items"))
            item_bases = [basis for item in (food_items if isinstance(food_items, list) else []) if isinstance(item, dict) and (basis := _per_item_basis(item))]
            provenance: dict[str, Any] = {"table": "recipes", "recipe_id": recipe_id}
            if exact_alias is not None:
                provenance["matched_alias_table"] = exact_alias["table"]
                if exact_alias.get("alias_id") is not None:
                    provenance["alias_id"] = exact_alias["alias_id"]
            candidates.append(
                _candidate(
                    candidate_type="recipe",
                    label=canonical,
                    score=score,
                    match_type=match_type,
                    suggested_action=action,
                    provenance=provenance,
                    nutrients=_normalized_nutrients(recipe, recipe_totals=True),
                    per_item_basis=item_bases,
                    reason_codes=reasons,
                )
            )

        seen_ingredients: dict[str, dict[str, Any]] = {}
        for row in nutrition_rows:
            entry_id = _meal_entry_id(row)
            meal_name = str(row.get("meal_name") or "").strip()
            raw_items = _json_object(row.get("food_items"))
            items = raw_items if isinstance(raw_items, list) else []
            item_phrases = [phrase for item in items if isinstance(item, dict) for phrase in _item_phrases(item)]
            meal_phrases = [meal_name, str(row.get("meal_description") or ""), " ".join(item_phrases)]
            score, match_type = _score_phrases(query_terms, meal_phrases, whole=True)
            meal_type_matches = meal_type_filter is not None and str(row.get("meal_type") or "").casefold().strip() == meal_type_filter
            if meal_type_matches and score < 0.985:
                score = min(0.985, score + 0.01)
            if score >= MIN_CANDIDATE_SCORE and meal_name:
                # A one-token ingredient overlap can score strongly against a
                # multi-item meal, but only a full normalized meal-name query
                # may be surfaced as an exact reuse candidate.
                action = "exact_reuse_candidate" if match_type == "exact_name" and score >= 0.95 else "needs_agent_decision"
                reasons = ["prior_meal_candidate_only"]
                if meal_type_matches:
                    reasons.append("meal_type_context_match")
                if action == "exact_reuse_candidate":
                    reasons.append("agent_must_confirm_reuse_intent")
                item_bases = [basis for item in items if isinstance(item, dict) and (basis := _per_item_basis(item))]
                candidates.append(
                    _candidate(
                        candidate_type="historical_meal",
                        label=meal_name,
                        score=min(score, 0.985),
                        match_type="historical_" + match_type,
                        suggested_action=action,
                        provenance={"table": "nutrition_log", "entry_id": row.get("entry_id")},
                        meal_time=row.get("meal_time"),
                        nutrients=_normalized_nutrients(row),
                        per_item_basis=item_bases,
                        reason_codes=reasons,
                    )
                )
                if entry_id is not None:
                    historical_entry_ids.add(entry_id)

            for item in items:
                if not isinstance(item, dict):
                    continue
                basis = _per_item_basis(item)
                if basis is None:
                    continue
                source_counts["food_items"] += 1
                phrases = _item_phrases(item)
                item_score, item_match = _score_phrases(query_terms, phrases, whole=False)
                if meal_type_matches and item_score < 0.985:
                    item_score = min(0.985, item_score + 0.01)
                if item_score < MIN_CANDIDATE_SCORE:
                    continue
                key = _normalize(_item_name(item))
                ingredient = _candidate(
                    candidate_type="ingredient_basis",
                    label=_item_name(item),
                    score=min(item_score, 0.985),
                    match_type="ingredient_" + item_match,
                    suggested_action="ingredient_basis_candidate" if item_score >= ACTIONABLE_SCORE else "needs_agent_decision",
                    provenance={"table": "nutrition_log", "entry_id": row.get("entry_id")},
                    meal_time=row.get("meal_time"),
                    per_item_basis=[basis],
                    reason_codes=[
                        "historical_ingredient_candidate_only",
                        "agent_must_validate_portion",
                        *(["meal_type_context_match"] if meal_type_matches else []),
                    ],
                )
                previous = seen_ingredients.get(key)
                if previous is None or _candidate_sort_key(ingredient) < _candidate_sort_key(previous):
                    seen_ingredients[key] = ingredient
        candidates.extend(seen_ingredients.values())

        if reuse_intent:
            recent_baselines = _recent_meal_candidates(
                nutrition_rows,
                meal_type=meal_type_filter,
                used_entry_ids=historical_entry_ids,
            )
            if recent_baselines:
                candidates.extend(recent_baselines)
    except duckdb.Error as exc:
        raise RetrievalError("database_read_failed", "nutrition sources could not be read") from exc
    finally:
        conn.close()

    defaults, defaults_status = _read_defaults(profile_path)
    source_counts["user_defaults"] = len(defaults)
    for key, value in defaults.items():
        key_text = str(key)
        score, match_type = _score_phrases(query_terms, [key_text], whole=False)
        if score < MIN_CANDIDATE_SCORE:
            continue
        candidates.append(
            _candidate(
                candidate_type="user_default",
                label=key_text,
                score=min(score, 0.985),
                match_type="default_" + match_type,
                suggested_action="needs_agent_decision",
                provenance={"table": "user_defaults", "key": key_text},
                per_item_basis=[
                    {
                        "item": key_text,
                        "basis": {"kind": "user_default"},
                        "value": _json_value(value),
                    }
                ],
                reason_codes=["user_default_available", "agent_must_apply_default_to_original_text"],
            )
        )

    safety_reasons = _safety_reasons(" ".join([text, *structured_terms]), context)
    if safety_reasons:
        for candidate in candidates:
            candidate["suggested_action"] = "needs_agent_decision"
            candidate["reason_codes"] = list(dict.fromkeys([*candidate["reason_codes"], *safety_reasons]))

    candidates.sort(key=_candidate_sort_key)
    candidates = candidates[:top_k]
    for rank, candidate in enumerate(candidates, 1):
        candidate["rank"] = rank

    if "visual_input_requires_agent" in safety_reasons:
        suggestion, reason_codes = "pass_through", safety_reasons
    elif safety_reasons:
        suggestion, reason_codes = "needs_agent_decision", safety_reasons
    elif reuse_intent and any(candidate["candidate_type"] == "historical_meal" for candidate in candidates):
        suggestion, reason_codes = "needs_agent_decision", ["historical_baseline_requires_confirmation"]
    elif not candidates:
        suggestion, reason_codes = "no_match", ["no_confident_local_candidate"]
    else:
        suggestion, reason_codes = "needs_agent_decision", ["local_candidates_require_agent_judgment"]

    raw_cooked_basis = _raw_vs_cooked_basis(" ".join([text, *structured_terms]), candidates)
    result = {
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "query": {
            "text": text or None,
            "terms": structured_terms,
            "meal_time": meal_time,
            "context": _public_context(context),
        },
        "suggested_action": suggestion,
        "reason_codes": reason_codes,
        "candidates": candidates,
        "sources": {
            "counts": source_counts,
            "defaults_status": defaults_status,
        },
        "read_only": True,
    }
    if raw_cooked_basis is not None:
        result["raw_vs_cooked_basis"] = raw_cooked_basis
    if any(tuple(candidate) != _CANDIDATE_KEYS for candidate in candidates):
        raise RetrievalError("unsupported_result_value", "local retrieval produced an unsupported value")
    try:
        json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RetrievalError("unsupported_result_value", "local retrieval produced an unsupported value") from exc
    return result


def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="Read-only local nutrition retrieval")
    parser.add_argument("--db", required=True, help="Explicit DuckDB path (never included in output)")
    parser.add_argument("--json", dest="json_payload", help="Query object JSON, or '-' for stdin")
    parser.add_argument("--text", help="Raw text to search")
    parser.add_argument("--term", action="append", default=[], help="Structured query term (repeatable)")
    parser.add_argument("--meal-time", help="Optional meal timestamp/context")
    parser.add_argument("--context-json", help="Optional context object JSON")
    parser.add_argument("--profile", help="Optional explicit user-profile YAML path")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        payload: dict[str, Any] = {}
        if args.json_payload is not None:
            raw = sys.stdin.read() if args.json_payload == "-" else args.json_payload
            loaded = _loads_json(raw)
            if not isinstance(loaded, dict):
                raise RetrievalError("invalid_query", "--json must contain an object")
            payload.update(loaded)
        if args.text is not None:
            payload["text"] = args.text
        if args.term:
            payload["terms"] = args.term
        if args.meal_time is not None:
            payload["meal_time"] = args.meal_time
        if args.context_json is not None:
            parsed_context = _loads_json(args.context_json)
            if not isinstance(parsed_context, dict):
                raise RetrievalError("invalid_context", "--context-json must contain an object")
            payload["context"] = parsed_context
        if args.profile is not None:
            payload["profile_path"] = args.profile
        if "top_k" not in payload:
            payload["top_k"] = args.top_k
        result = retrieve_nutrition(args.db, payload)
        print(json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
            allow_nan=False,
        ))
        return 0
    except (json.JSONDecodeError, ValueError):
        error = {"status": "error", "error": {"code": "invalid_json", "message": "query JSON is invalid"}}
    except RetrievalError as exc:
        error = {"status": "error", "error": {"code": exc.code, "message": exc.message}}
    except Exception:
        error = {"status": "error", "error": {"code": "retrieval_failed", "message": "local retrieval failed safely"}}
    print(json.dumps(error, ensure_ascii=False, sort_keys=True, allow_nan=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
