"""
Phase 9 — Intelligent Merge Engine (single responsibility: merge a NORMALIZED
upload — Phase 8's output — into the existing framework feature's text. No LLM
calls, no file writes, no comparison/normalization logic of its own.)

Rules, exactly per spec:
  - Keep unchanged scenarios AS-IS (their original text, formatting, comments —
    byte-identical to the existing file).
  - Replace modified scenarios (with the normalized upload's version of that
    scenario).
  - Append new scenarios (at the end, from the normalized upload).
  - Never duplicate scenarios (name-keyed; each name appears exactly once in
    the merged output).
  - Preserve formatting / comments of the EXISTING feature file for anything
    not touched by this merge.
  - Preserve tags — for kept/unreferenced scenarios, the existing file's own
    tags are untouched; for replaced/appended scenarios, the normalized
    upload's (already-conventions-corrected, per Phase 8) tags are used.

DELETION POLICY (deliberate — matches the letter of the Phase 9 spec, which
lists keep/replace/append but never lists "remove"): a scenario present in the
existing feature but ABSENT from the normalized upload is NOT deleted from the
merged output. Deleting test coverage is materially more dangerous than
adding/updating it, and this module has no basis to decide whether an absence
means "intentionally removed" versus "the tester only touched a subset of
scenarios in this edit." Such scenarios ARE reported by name (mirroring Phase
7's comparator) so a human reviewer sees them; an actual removal is a
deliberate, separate, explicit action — never an automatic side effect of a
merge.

Independent of app/generator.py's existing merge_feature() (left completely
unchanged — still exactly what the live AI-execute pipeline calls today). This
module reuses generator._blocks(), the same proven, already-tested
text-block splitter, purely to avoid re-implementing that regex logic a
second time — it does not touch generator.py's public behavior or callers.
"""
from . import framework_scanner
from .generator import _blocks  # reuse the proven, already-tested text-block splitter


def merge(existing_text: str, normalized_text: str) -> dict:
    """
    Returns:
      {
        "merged_text": str,
        "kept_scenarios":         [names present in both, byte-identical -> untouched],
        "replaced_scenarios":     [names present in both, different -> existing block swapped],
        "appended_scenarios":     [names new to the upload -> added at the end],
        "unreferenced_scenarios": [names in the existing feature, absent from the
                                   upload -> KEPT, NOT deleted; reported for visibility],
        "duplicate_guard_triggered": bool,  # defensive: true if the normalized
                                            # upload itself somehow contained a
                                            # repeated scenario name
      }
    """
    if not existing_text.strip():
        # Brand-new transaction: nothing to merge INTO — the "merged" feature
        # is simply the normalized upload, and every scenario in it is new.
        parsed = framework_scanner.parse_feature_text(normalized_text) or {"scenarios": []}
        names = [sc["name"] for sc in parsed["scenarios"]]
        return {
            "merged_text": normalized_text,
            "kept_scenarios": [], "replaced_scenarios": [],
            "appended_scenarios": names, "unreferenced_scenarios": [],
            "duplicate_guard_triggered": len(names) != len(set(names)),
        }

    old_head, old_blocks, old_order = _blocks(existing_text)
    _, new_blocks, new_order = _blocks(normalized_text)

    duplicate_guard = len(new_order) != len(set(new_order))

    kept, replaced, appended = [], [], []
    merged_blocks = dict(old_blocks)      # start from existing -> preserves formatting/comments
    merged_order = list(old_order)

    for name in new_order:
        if name in old_blocks:
            if old_blocks[name].strip() != new_blocks[name].strip():
                merged_blocks[name] = new_blocks[name]   # REPLACE — never duplicated, same slot
                replaced.append(name)
            else:
                kept.append(name)                        # identical -> nothing to do
        else:
            merged_blocks[name] = new_blocks[name]
            merged_order.append(name)                    # APPEND — new slot at the end
            appended.append(name)

    unreferenced = [n for n in old_order if n not in new_order]  # present before, untouched here

    merged_text = old_head + "".join(merged_blocks[n] for n in merged_order[:len(old_order)])
    for name in appended:
        merged_text = merged_text.rstrip("\n") + "\n\n" + merged_blocks[name].rstrip("\n") + "\n"

    return {
        "merged_text": merged_text,
        "kept_scenarios": kept,
        "replaced_scenarios": replaced,
        "appended_scenarios": appended,
        "unreferenced_scenarios": unreferenced,
        "duplicate_guard_triggered": duplicate_guard,
    }


def merge_pipeline(uploaded_text: str, filename: str | None = None,
                   module_hint: str | None = None, txn_hint: str | None = None,
                   min_score: float = 0.05) -> dict:
    """Convenience: runs Phase 6 (match) -> Phase 8 (normalize) -> Phase 9
    (this merge) in one call, for callers that just want the final merged
    text plus full transparency into every step that produced it. Still not
    wired into /execute/upload — that final hookup is the next integration
    step once all pipeline phases are individually proven."""
    from . import feature_matcher, feature_normalizer
    from pathlib import Path

    match = feature_matcher.best_match(uploaded_text, filename, module_hint, txn_hint, min_score)
    existing_text = ""
    if match:
        existing_text = Path(match["path"]).read_text(encoding="utf-8", errors="ignore")

    normalization = feature_normalizer.normalize_feature(uploaded_text, existing_text)
    merge_result = merge(existing_text, normalization["normalized_text"])

    return {
        "matched_feature": match,
        "normalization": normalization,
        "merge": merge_result,
    }
