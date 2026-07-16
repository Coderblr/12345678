"""
Phase 8 — Feature Normalization (single responsibility: REWRITE the uploaded
feature's text to follow the framework's existing conventions — no LLM calls,
no Java generation, no file writes to the framework. Deterministic and
explainable, like feature_matcher.py and feature_comparator.py.)

"The uploaded feature is NOT the source of truth. The existing framework is."

Four normalization rules, each independently testable:
  1. TAG CASING     — rewrite tags to the framework's own most-common casing
                       for that tag (e.g. uploaded "@regression" -> "@Regression"
                       if that's how the framework actually spells it everywhere).
                       A tag never seen anywhere in the framework is left as-is
                       and reported under new_tags (not rejected — could be a
                       deliberate, legitimate new tag).
  2. STEP WORDING   — the highest-value rule. For every step, check the WHOLE
                       framework's existing step-definition sentences (not just
                       the matched feature's — reuse should be framework-wide).
                       If the uploaded step's fixed wording is IDENTICAL in
                       structure to an existing step, nothing changes — it
                       already binds to existing glue. If it's a near-duplicate
                       (a typo, an extra/missing word) with the same number of
                       parameters, rewrite it to the existing step's exact
                       wording, substituting in the uploaded step's own literal
                       values. This is what prevents Phase 11 from generating a
                       duplicate step definition for what is really the same
                       step, just typed slightly differently.
  3. SCENARIO NAMES — if an uploaded scenario's name doesn't exactly match any
                       existing scenario but is a close near-duplicate, rename
                       it to match the existing scenario's exact name. This is
                       what lets Phase 7's exact-name comparator correctly
                       classify it as "modified" instead of delete+add — a
                       documented limitation of Phase 7 that this phase closes.
  4. EXAMPLES HEADERS — if an uploaded Scenario Outline's Examples table has the
                       same set of column names as the existing scenario's
                       (same-name, post-rule-3) but different casing/order,
                       normalize to the existing header spelling and order,
                       reordering each row's values to match.

The normalized feature is reconstructed from the parsed structure (a clean
Gherkin renderer), not patched via fragile string search/replace. This is an
intermediate artifact for the pipeline (consumed next by Phase 9 Merge), so
exact original formatting/comments of the UPLOAD are not preserved — only the
EXISTING framework feature's formatting matters for the final merge, which is
Phase 9's job, not this one's.
"""
import difflib
import re
from collections import Counter

from . import framework_scanner

PLACEHOLDER = "\uE000"
_LITERAL_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\b\d+(?:\.\d+)?\b')
_CUKE_PLACEHOLDER_RE = re.compile(r'\{[^}]*\}')

FUZZY_STEP_THRESHOLD = 0.80
FUZZY_NAME_THRESHOLD = 0.75
FLAG_SIMILARITY_THRESHOLD = 0.45  # much lower bar — only used to WARN, never to auto-rewrite


# ─────────────────────────── tag vocabulary ──────────────────────────────────
def _build_tag_vocabulary() -> dict:
    """{tag lowercased: the framework's own most-common actual casing}."""
    kb = framework_scanner.get_framework_knowledge()
    counts = {}
    for f in kb["features"]:
        all_tags = list(f["tags"]) + [t for sc in f["scenarios"] for t in sc.get("tags", [])]
        for t in all_tags:
            counts.setdefault(t.lower(), Counter())[t] += 1
    return {low: counter.most_common(1)[0][0] for low, counter in counts.items()}


def _normalize_tags(tags, vocab, normalized_log, new_tags_log):
    out = []
    for t in tags:
        canon = vocab.get(t.lower())
        if canon is None:
            if t not in new_tags_log:
                new_tags_log.append(t)
            out.append(t)
        elif canon != t:
            normalized_log.append({"from": t, "to": canon})
            out.append(canon)
        else:
            out.append(t)
    return out


# ─────────────────────────── step wording ────────────────────────────────────
def _uploaded_skeleton(text):
    tokens = _LITERAL_TOKEN_RE.findall(text)
    skeleton = _LITERAL_TOKEN_RE.sub(PLACEHOLDER, text)
    return re.sub(r"\s+", " ", skeleton).strip().lower(), tokens


def _candidate_parts(sentence):
    parts = _CUKE_PLACEHOLDER_RE.split(sentence)
    n_ph = len(_CUKE_PLACEHOLDER_RE.findall(sentence))
    skeleton = re.sub(r"\s+", " ", _CUKE_PLACEHOLDER_RE.sub(PLACEHOLDER, sentence)).strip().lower()
    return parts, n_ph, skeleton


def _match_known_step(keyword, text, known_steps):
    up_skel, up_tokens = _uploaded_skeleton(text)
    same_kw = [s for s in known_steps if s[0] == keyword]
    candidates = same_kw if keyword not in ("And", "But") else known_steps

    for kw, sentence in candidates:
        _, n_ph, cskel = _candidate_parts(sentence)
        if cskel == up_skel and n_ph == len(up_tokens):
            return {"confidence": "exact_noop"}

    best = None
    for kw, sentence in candidates:
        parts, n_ph, cskel = _candidate_parts(sentence)
        if n_ph != len(up_tokens):
            continue
        ratio = difflib.SequenceMatcher(a=up_skel, b=cskel, autojunk=False).ratio()
        if ratio >= FUZZY_STEP_THRESHOLD and (best is None or ratio > best[0]):
            rebuilt = parts[0]
            for i, tok in enumerate(up_tokens):
                rebuilt += tok
                if i + 1 < len(parts):
                    rebuilt += parts[i + 1]
            best = (ratio, rebuilt, sentence)
    if best:
        return {"confidence": "fuzzy", "rewritten_text": best[1],
               "matched_sentence": best[2], "similarity": round(best[0], 3)}
    return None


# ─────────────────────────── scenario naming ─────────────────────────────────
def _best_name_match(name, candidates):
    best = None
    for c in candidates:
        ratio = difflib.SequenceMatcher(a=name.lower(), b=c.lower(), autojunk=False).ratio()
        if best is None or ratio > best[1]:
            best = (c, ratio)
    return best


def _ci_index(headers, name):
    low = [h.lower() for h in headers]
    return low.index(name.lower())


# ─────────────────────────── renderer ────────────────────────────────────────
def render_feature(feature_name, tags, scenarios):
    lines = []
    if tags:
        lines.append(" ".join(tags))
    lines.append(f"Feature: {feature_name}")
    lines.append("")
    for sc in scenarios:
        if sc.get("tags"):
            lines.append("  " + " ".join(sc["tags"]))
        hdr = "Scenario Outline" if sc["type"] == "Scenario Outline" else "Scenario"
        lines.append(f'  {hdr}: {sc["name"]}')
        for st in sc["steps"]:
            lines.append(f'    {st["keyword"]} {st["text"]}')
        for row in sc.get("data_table_rows", []):
            lines.append("      | " + " | ".join(row) + " |")
        ex = sc.get("examples")
        if ex and ex.get("headers"):
            lines.append("")
            lines.append("    Examples:")
            lines.append("      | " + " | ".join(ex["headers"]) + " |")
            for row in ex.get("rows", []):
                lines.append("      | " + " | ".join(row) + " |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ─────────────────────────────── main entry ──────────────────────────────────
def normalize_feature(uploaded_text, existing_text=""):
    new = framework_scanner.parse_feature_text(uploaded_text)
    if new is None:
        raise ValueError("Uploaded text is not valid Gherkin (no 'Feature:' header found).")
    old = framework_scanner.parse_feature_text(existing_text) if existing_text.strip() else \
          {"feature_name": "", "tags": [], "scenarios": []}

    vocab = _build_tag_vocabulary()
    kb = framework_scanner.get_framework_knowledge()
    known_steps = [(m["keyword"], m["sentence"]) for sd in kb["step_defs"] for m in sd["methods"]]
    old_scenario_names = [sc["name"] for sc in old["scenarios"]]

    tags_normalized = []
    new_tags = []
    scenarios_renamed = []
    steps_normalized = []
    new_steps = []
    examples_headers_normalized = []

    new_feature_tags = _normalize_tags(new["tags"], vocab, tags_normalized, new_tags)

    normalized_scenarios = []
    for sc in new["scenarios"]:
        name = sc["name"]
        if name not in old_scenario_names:
            best = _best_name_match(name, old_scenario_names) if old_scenario_names else None
            if best and best[1] >= FUZZY_NAME_THRESHOLD:
                scenarios_renamed.append({"from": name, "to": best[0], "similarity": round(best[1], 3)})
                name = best[0]

        sc_tags = _normalize_tags(sc.get("tags", []), vocab, tags_normalized, new_tags)

        new_steps_list = []
        for st in sc["steps"]:
            full_text = f'{st["keyword"]} {st["text"]}'
            match = _match_known_step(st["keyword"], st["text"], known_steps)
            if match is None:
                new_steps.append({"scenario": name, "step": full_text})
                new_steps_list.append(st)
            elif match["confidence"] == "exact_noop":
                new_steps_list.append(st)
            else:
                if match["rewritten_text"] != st["text"]:
                    steps_normalized.append({
                        "scenario": name, "from": full_text,
                        "to": f'{st["keyword"]} {match["rewritten_text"]}',
                        "confidence": match["confidence"], "similarity": match["similarity"],
                    })
                new_steps_list.append({"keyword": st["keyword"], "text": match["rewritten_text"]})

        examples = sc.get("examples")
        old_sc = next((s for s in old["scenarios"] if s["name"] == name), None)
        if examples and examples.get("headers") and old_sc:
            old_ex = old_sc.get("examples") or {}
            oh = old_ex.get("headers", [])
            if oh and {h.lower() for h in examples["headers"]} == {h.lower() for h in oh} \
                    and examples["headers"] != oh:
                examples_headers_normalized.append(
                    {"scenario": name, "from": examples["headers"], "to": oh})
                new_rows = [[row[_ci_index(examples["headers"], h)] for h in oh]
                           for row in examples.get("rows", [])]
                examples = {"headers": oh, "rows": new_rows}

        normalized_scenarios.append({**sc, "name": name, "tags": sc_tags,
                                    "steps": new_steps_list, "examples": examples})

    normalized_text = render_feature(new["feature_name"], new_feature_tags, normalized_scenarios)

    return {
        "normalized_text": normalized_text,
        "changes": {
            "tags_normalized": tags_normalized, "new_tags": new_tags,
            "scenarios_renamed": scenarios_renamed,
            "steps_normalized": steps_normalized, "new_steps": new_steps,
            "examples_headers_normalized": examples_headers_normalized,
        },
        "summary": {
            "tags_normalized": len(tags_normalized), "scenarios_renamed": len(scenarios_renamed),
            "steps_normalized": len(steps_normalized), "new_steps": len(new_steps),
            "examples_headers_normalized": len(examples_headers_normalized),
            "any_changes": bool(tags_normalized or scenarios_renamed or steps_normalized
                               or examples_headers_normalized),
        },
    }


def normalize_with_best_match(uploaded_text, filename=None, module_hint=None,
                              txn_hint=None, min_score=0.05):
    """Phase 6 -> Phase 8 hookup: auto-find the best existing feature, then
    normalize the upload against it. Mirrors feature_comparator's hookup."""
    from . import feature_matcher

    match = feature_matcher.best_match(uploaded_text, filename, module_hint, txn_hint, min_score)
    existing_text = ""
    if match:
        from pathlib import Path
        existing_text = Path(match["path"]).read_text(encoding="utf-8", errors="ignore")

    result = normalize_feature(uploaded_text, existing_text)
    result["matched_feature"] = match
    return result
