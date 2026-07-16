"""
Phase 3 — Knowledge Graph (single responsibility: build and query the
relationship chain Feature -> Scenario -> Step -> Step Definition -> Page
Method -> Locator -> Test Data. No LLM calls, no file writes to the
framework.)

Everything built in Phases 1/4/6/7/8/9 works on method/step SIGNATURES —
sentences, names, parameter lists. This module is the first to open method
BODIES, because signatures alone cannot answer "which locator does this step
actually touch" — that requires seeing what a step-definition method calls,
and what a page method's own body references.

Approach (regex + brace-matching, not a real Java parser — same honestly-
scoped heuristic style as the rest of this project):
  1. Extract each step-definition method's BODY (framework_scanner only
     captures signatures; this module does its own pass over the same files).
  2. Within a step-def method's body, find `identifier.methodName(` call
     sites. If methodName matches a KNOWN page-object method name anywhere in
     the framework, record an edge: step-def method -> page class + method.
     (Heuristic limitation, stated plainly: this matches by METHOD NAME, not
     by resolved variable type — if two unrelated page classes happen to
     share a method name like `clickSubmit()`, both are recorded as possible
     targets. For enterprise page-object frameworks, method names are
     typically screen-specific enough that this rarely produces false edges
     in practice, but it is not a guarantee.)
  3. Within a page method's OWN body, find references to that SAME class's
     own locator field names (whole-word match) -> edge: page method -> locator.
  4. Within any method body, find `getProperty("KEY")`-style calls -> edge to
     a known properties KEY (never the value — consistent with
     framework_scanner's key-only safety policy).
  5. For every feature/scenario/step (from framework_scanner's feature scan),
     resolve which step-definition method it binds to using the same
     skeleton-matching technique as feature_normalizer (reused via import,
     not duplicated), then follow that method's edges to produce the full
     chain for that step.

Two practical query functions on top of the graph:
  - trace_step(text)     — "what does this step actually do, all the way down
                            to locators and test-data keys?"
  - impact_of_locator(n) — reverse lookup: "what breaks if I change this
                            locator?" (which page methods / step defs /
                            scenarios / features touch it)
"""
import json
import re

from . import config, framework_scanner
from .feature_normalizer import _uploaded_skeleton, _candidate_parts  # reuse, don't duplicate

CALL_RE = re.compile(r'\b(\w+)\s*\.\s*(\w+)\s*\(')
GET_PROPERTY_RE = re.compile(r'getProperty\s*\(\s*"([\w.]+)"\s*\)')
METHOD_START_RE = re.compile(
    r'(?:public|protected)\s+(?!class\b)[\w<>\[\], ]+?\s+(\w+)\s*\(([^)]*)\)\s*'
    r'(?:throws\s+[\w,.\s]+)?\s*\{')
STEP_ANNOTATION_RE = re.compile(r'@(Given|When|Then|And|But)\s*\(\s*"((?:[^"\\]|\\.)*)"\s*\)')


def _extract_body(text: str, open_brace_idx: int) -> tuple[str, int]:
    """Brace-match from an opening '{' to its closing '}'. Does not special-case
    braces inside string/char literals (a real limitation, shared by nearly
    every regex-based Java tool of this kind) — acceptable because step-def
    and page-object method bodies overwhelmingly don't contain literal braces."""
    depth = 0
    i = open_brace_idx
    n = len(text)
    while i < n:
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[open_brace_idx + 1:i], i + 1
        i += 1
    return text[open_brace_idx + 1:], n  # unterminated — return what we have


def _methods_with_bodies(java_text: str) -> list[dict]:
    """Every public/protected method in the file, with its full body text.
    Step annotations are found INDEPENDENTLY and each attached to its nearest
    FOLLOWING method signature — the standard Cucumber convention (annotation
    directly above its method) — rather than guessing from a lookback window,
    which mis-attributes when methods sit close together."""
    methods = []
    for m in METHOD_START_RE.finditer(java_text):
        name, params = m.group(1), m.group(2)
        body, _ = _extract_body(java_text, m.end() - 1)
        methods.append({"name": name, "params": params, "body": body,
                       "sig_start": m.start(), "step_keyword": None, "step_sentence": None})
    for am in STEP_ANNOTATION_RE.finditer(java_text):
        candidates = [meth for meth in methods if meth["sig_start"] >= am.end()]
        if candidates:
            target = min(candidates, key=lambda x: x["sig_start"])
            target["step_keyword"] = am.group(1)
            target["step_sentence"] = am.group(2)
    return methods


def _calls_in(body: str) -> list[tuple[str, str]]:
    return [(recv, meth) for recv, meth in CALL_RE.findall(body)]


def _property_keys_in(body: str) -> list[str]:
    return sorted(set(GET_PROPERTY_RE.findall(body)))


def build_graph() -> dict:
    """Scan the whole framework and build the relationship graph. Writes to
    data/knowledge_graph.json."""
    # ── pass 1: every page method (with body) + that class's locators ──
    page_methods_by_name: dict[str, list[dict]] = {}   # method name -> [{class, module, locators_used}]
    page_locators_by_class: dict[str, list[str]] = {}

    if config.PAGES_DIR.exists():
        for f in config.PAGES_DIR.rglob("*.java"):
            text = f.read_text(encoding="utf-8", errors="ignore")
            cls_m = re.search(r'(?:public\s+)?class\s+(\w+)', text)
            cls = cls_m.group(1) if cls_m else f.stem
            module = framework_scanner._module_of(f, config.PAGES_DIR)
            locators = sorted({a or b for a, b in framework_scanner.LOCATOR_RE.findall(text)})
            page_locators_by_class[cls] = locators
            for meth in _methods_with_bodies(text):
                used = [loc for loc in locators
                       if re.search(r'\b' + re.escape(loc) + r'\b', meth["body"])]
                keys = _property_keys_in(meth["body"])
                page_methods_by_name.setdefault(meth["name"], []).append({
                    "class": cls, "module": module, "locators_used": used,
                    "property_keys_used": keys,
                })

    # ── pass 2: every step-definition method (with body) ──
    step_def_methods = []   # [{class, module, keyword, sentence, method, calls_page, property_keys}]
    if config.STEPS_DIR.exists():
        for f in config.STEPS_DIR.rglob("*.java"):
            text = f.read_text(encoding="utf-8", errors="ignore")
            cls_m = re.search(r'(?:public\s+)?class\s+(\w+)', text)
            cls = cls_m.group(1) if cls_m else f.stem
            module = framework_scanner._module_of(f, config.STEPS_DIR)
            for meth in _methods_with_bodies(text):
                if not meth["step_sentence"]:
                    continue  # not a step method (a helper method in the same class)
                calls = _calls_in(meth["body"])
                page_targets = []
                for recv, called_name in calls:
                    for hit in page_methods_by_name.get(called_name, []):
                        page_targets.append({"receiver_var": recv, "page_class": hit["class"],
                                            "page_method": called_name,
                                            "locators_used": hit["locators_used"]})
                step_def_methods.append({
                    "class": cls, "module": module,
                    "keyword": meth["step_keyword"], "sentence": meth["step_sentence"],
                    "method": meth["name"],
                    "calls_page_methods": page_targets,
                    "property_keys_used": _property_keys_in(meth["body"]),
                })

    # ── pass 3: resolve every feature/scenario/step to a step-def method ──
    kb = framework_scanner.get_framework_knowledge()
    features_out = []
    resolved_count = total_steps = 0
    for feat in kb["features"]:
        scenarios_out = []
        for sc in feat["scenarios"]:
            steps_out = []
            for st in sc["steps"]:
                total_steps += 1
                resolved = _resolve_step(st["keyword"], st["text"], step_def_methods)
                if resolved:
                    resolved_count += 1
                    locs, keys = set(), set(resolved["property_keys_used"])
                    for pt in resolved["calls_page_methods"]:
                        locs.update(pt["locators_used"])
                    steps_out.append({
                        "keyword": st["keyword"], "text": st["text"], "resolved": True,
                        "step_def_class": resolved["class"], "step_def_method": resolved["method"],
                        "calls_page_methods": [{"page_class": p["page_class"], "method": p["page_method"]}
                                               for p in resolved["calls_page_methods"]],
                        "uses_locators": sorted(locs), "uses_test_data_keys": sorted(keys),
                    })
                else:
                    steps_out.append({"keyword": st["keyword"], "text": st["text"], "resolved": False})
            scenarios_out.append({"name": sc["name"], "steps": steps_out})
        features_out.append({"txn": feat["txn"], "module": feat["module"],
                            "feature_name": feat["feature_name"], "scenarios": scenarios_out})

    graph = {
        "features": features_out,
        "step_def_methods": step_def_methods,
        "page_locators_by_class": page_locators_by_class,
        "stats": {
            "total_steps": total_steps, "resolved_steps": resolved_count,
            "unresolved_steps": total_steps - resolved_count,
            "step_def_methods": len(step_def_methods),
            "page_classes": len(page_locators_by_class),
            "resolution_rate": round(resolved_count / total_steps, 3) if total_steps else 0.0,
        },
    }
    (config.DATA_DIR / "knowledge_graph.json").write_text(json.dumps(graph, indent=1))
    return graph


def _resolve_step(keyword: str, text: str, step_def_methods: list[dict]) -> dict | None:
    """Same skeleton-equality technique feature_normalizer uses to decide a
    step already binds to existing glue — reused here (not duplicated) to
    decide WHICH step-def method it binds to."""
    up_skel, up_tokens = _uploaded_skeleton(text)
    same_kw = [m for m in step_def_methods if m["keyword"] == keyword]
    candidates = same_kw if keyword not in ("And", "But") else step_def_methods
    for m in candidates:
        _, n_ph, cskel = _candidate_parts(m["sentence"])
        if cskel == up_skel and n_ph == len(up_tokens):
            return m
    return None


_cache: dict = {}


def get_graph() -> dict:
    global _cache
    if _cache:
        return _cache
    gf = config.DATA_DIR / "knowledge_graph.json"
    if gf.exists():
        _cache = json.loads(gf.read_text())
        return _cache
    return build_graph()


def trace_step(step_text: str, keyword: str | None = None) -> dict | None:
    """Find the full chain for a step sentence: step-def method -> page
    methods called -> locators used -> test-data keys used. Also searches
    every already-scanned feature for scenarios using this exact step, for
    context ('who else uses this')."""
    graph = get_graph()
    kw_candidates = [keyword] if keyword else ["Given", "When", "Then", "And", "But"]
    hit = None
    for kw in kw_candidates:
        hit = _resolve_step(kw, step_text, graph["step_def_methods"])
        if hit:
            break
    if not hit:
        return None

    used_in = []
    for feat in graph["features"]:
        for sc in feat["scenarios"]:
            for st in sc["steps"]:
                if st["resolved"] and st["step_def_method"] == hit["method"] \
                        and st["step_def_class"] == hit["class"]:
                    used_in.append({"txn": feat["txn"], "feature": feat["feature_name"],
                                   "scenario": sc["name"]})
    return {
        "step_def_class": hit["class"], "step_def_method": hit["method"],
        "module": hit["module"],
        "calls_page_methods": hit["calls_page_methods"],
        "test_data_keys": hit["property_keys_used"],
        "used_in_scenarios": used_in,
    }


def impact_of_locator(locator_name: str) -> dict:
    """Reverse lookup: everything that touches a given locator name — page
    methods, the step definitions that call them, and every scenario that
    would be affected if this locator's element changed or broke."""
    graph = get_graph()
    affected_step_defs = [m for m in graph["step_def_methods"]
                          if any(locator_name in p["locators_used"] for p in m["calls_page_methods"])]
    affected_scenarios = []
    for feat in graph["features"]:
        for sc in feat["scenarios"]:
            for st in sc["steps"]:
                if st.get("resolved") and locator_name in st.get("uses_locators", []):
                    affected_scenarios.append({"txn": feat["txn"], "feature": feat["feature_name"],
                                              "scenario": sc["name"], "step": st["text"]})
    page_classes = sorted({cls for cls, locs in graph["page_locators_by_class"].items()
                          if locator_name in locs})
    return {
        "locator": locator_name,
        "declared_in_page_classes": page_classes,
        "step_definitions_affected": [{"class": m["class"], "method": m["method"]} for m in affected_step_defs],
        "scenarios_affected": affected_scenarios,
        "impact_count": len(affected_scenarios),
    }
