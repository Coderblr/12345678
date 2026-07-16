"""
Phase 7 — Intelligent Feature Comparison (single responsibility: given two
parsed features, produce a structured comparison summary — no LLM calls, no
file writes, no merging/normalization logic; those are separate modules).

Detects, matching the spec exactly:
  - existing (unchanged) scenarios
  - new scenarios            (present in upload, not in existing)
  - deleted scenarios        (present in existing, not in upload)  <- new capability
  - modified scenarios, each broken down into:
      - tag changes
      - step changes (proper insert/delete/replace diff via difflib, not just
        "changed / not changed")
      - Examples table changes (Scenario Outline data)
      - inline data-table changes

Scenario matching is by exact name (framework convention: scenario names are
unique within one feature). A scenario RENAME will show as delete+add rather
than "modified" — this is a known, documented limitation (no fuzzy name
matching in v1) rather than a silent wrong answer.
"""
import difflib
from pathlib import Path

from . import framework_scanner


def _step_signature(sc: dict) -> tuple:
    return tuple((s["keyword"], s["text"]) for s in sc.get("steps", []))


def _examples_signature(sc: dict):
    ex = sc.get("examples") or {}
    return tuple(ex.get("headers", [])), tuple(tuple(r) for r in ex.get("rows", []))


def _data_table_signature(sc: dict):
    return tuple(tuple(r) for r in sc.get("data_table_rows", []))


def _scenario_unchanged(old: dict, new: dict) -> bool:
    return (sorted(old.get("tags", [])) == sorted(new.get("tags", []))
            and _step_signature(old) == _step_signature(new)
            and _examples_signature(old) == _examples_signature(new)
            and _data_table_signature(old) == _data_table_signature(new))


def _step_diff(old: dict, new: dict) -> list[dict]:
    before = [f"{s['keyword']} {s['text']}" for s in old.get("steps", [])]
    after = [f"{s['keyword']} {s['text']}" for s in new.get("steps", [])]
    sm = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    ops = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        ops.append({"op": tag, "before": before[i1:i2], "after": after[j1:j2]})
    return ops


def _examples_diff(old: dict, new: dict) -> dict | None:
    oh, orows = _examples_signature(old)
    nh, nrows = _examples_signature(new)
    if oh == nh and set(orows) == set(nrows):
        return None
    orow_set, nrow_set = set(orows), set(nrows)
    return {
        "headers_changed": oh != nh,
        "old_headers": list(oh), "new_headers": list(nh),
        "rows_added": [list(r) for r in nrows if r not in orow_set],
        "rows_removed": [list(r) for r in orows if r not in nrow_set],
    }


def _data_table_diff(old: dict, new: dict) -> dict | None:
    orows, nrows = _data_table_signature(old), _data_table_signature(new)
    if set(orows) == set(nrows):
        return None
    orow_set, nrow_set = set(orows), set(nrows)
    return {
        "rows_added": [list(r) for r in nrows if r not in orow_set],
        "rows_removed": [list(r) for r in orows if r not in nrow_set],
    }


def compare_features(existing_text: str, uploaded_text: str) -> dict:
    """The core comparison. existing_text may be empty ("" or no Feature:
    header) to represent a brand-new transaction with no prior feature —
    every uploaded scenario is then correctly classified as 'new'."""
    old = framework_scanner.parse_feature_text(existing_text) or \
          {"feature_name": "", "tags": [], "scenarios": []}
    new = framework_scanner.parse_feature_text(uploaded_text)
    if new is None:
        raise ValueError("Uploaded text is not valid Gherkin (no 'Feature:' header found).")

    old_by_name = {sc["name"]: sc for sc in old["scenarios"]}
    new_by_name = {sc["name"]: sc for sc in new["scenarios"]}

    unchanged, modified, new_names = [], [], []
    for name, new_sc in new_by_name.items():
        if name not in old_by_name:
            new_names.append(name)
        elif _scenario_unchanged(old_by_name[name], new_sc):
            unchanged.append(name)
        else:
            old_sc = old_by_name[name]
            modified.append({
                "name": name,
                "tags_changed": sorted(old_sc.get("tags", [])) != sorted(new_sc.get("tags", [])),
                "old_tags": old_sc.get("tags", []), "new_tags": new_sc.get("tags", []),
                "step_diff": _step_diff(old_sc, new_sc),
                "examples_diff": _examples_diff(old_sc, new_sc),
                "data_table_diff": _data_table_diff(old_sc, new_sc),
            })
    deleted_names = [n for n in old_by_name if n not in new_by_name]

    return {
        "existing_feature_name": old["feature_name"],
        "uploaded_feature_name": new["feature_name"],
        "feature_tags_changed": sorted(old.get("tags", [])) != sorted(new.get("tags", [])),
        "existing_tags": old.get("tags", []), "uploaded_tags": new.get("tags", []),
        "unchanged_scenarios": unchanged,
        "new_scenarios": new_names,
        "deleted_scenarios": deleted_names,
        "modified_scenarios": modified,
        "summary": {
            "unchanged": len(unchanged), "new": len(new_names),
            "deleted": len(deleted_names), "modified": len(modified),
            "total_existing": len(old["scenarios"]), "total_uploaded": len(new["scenarios"]),
            "has_changes": bool(new_names or deleted_names or modified),
        },
    }


def compare_with_best_match(uploaded_text: str, filename: str | None = None,
                            module_hint: str | None = None, txn_hint: str | None = None,
                            min_score: float = 0.05) -> dict:
    """Phase 6 -> Phase 7 hookup: auto-find the best existing feature via
    feature_matcher, then compare against it. If nothing scores above
    min_score, treats this as a brand-new transaction (existing_text = "")."""
    from . import feature_matcher  # local import avoids a hard circular dep at module load

    match = feature_matcher.best_match(uploaded_text, filename, module_hint, txn_hint, min_score)
    existing_text = ""
    if match:
        existing_text = Path(match["path"]).read_text(encoding="utf-8", errors="ignore")

    result = compare_features(existing_text, uploaded_text)
    result["matched_feature"] = match
    result["is_new_feature"] = match is None
    return result
