"""
Phase 1 — Framework Scanner (single responsibility: EXTRACTION only, no LLM calls,
no matching/comparison logic — those are separate future modules per the roadmap).

Produces a structured, richer knowledge dump than app/knowledge.py's catalogs:
this module understands FEATURE FILES at the scenario/step/examples/tags level,
not just step-definition sentences. It is purely ADDITIVE — nothing here is
called by the existing execute/generate pipeline yet, so it cannot regress
Run-by-Txn, Upload+Execute, or the existing AI-update flow.

Output: data/framework_knowledge.json, shape:
{
  "features":  [ {txn, module, path, feature_name, tags, scenarios: [...]} ],
  "step_defs": [ {class, module, path, methods: [{keyword, sentence, method, params}]} ],
  "pages":     [ {class, module, path, locators: [...], methods: [{name, params, kind}]} ],
  "utils":     [ {class, path, methods: [...]} ],
  "properties":[ {env, module, path, keys: [...]} ],   # KEYS ONLY — values never captured
  "stats": {...}
}
"""
import json
import re
from pathlib import Path

from . import config

TXN_RE = re.compile(r"(Txn\d+)", re.IGNORECASE)
TAG_LINE_RE = re.compile(r"^\s*(@\S+(?:\s+@\S+)*)\s*$", re.M)
FEATURE_HDR_RE = re.compile(r"^\s*Feature:\s*(.+)$", re.M)
SCENARIO_HDR_RE = re.compile(r"^([ \t]*)(Scenario Outline|Scenario):\s*(.+)$")
STEP_LINE_RE = re.compile(r"^\s*(Given|When|Then|And|But)\s+(.+?)\s*$")
EXAMPLES_HDR_RE = re.compile(r"^\s*Examples:\s*$")
TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")

STEP_ANN_RE = re.compile(
    r'@(Given|When|Then|And|But)\s*\(\s*"((?:[^"\\]|\\.)*)"\s*\)\s*'
    r'(?:public\s+)?[\w<>\[\], ]*\bvoid\s+(\w+)\s*\(([^)]*)\)', re.S)
CLASS_RE = re.compile(r'(?:public\s+)?class\s+(\w+)')
METHOD_RE = re.compile(r'^\s*(?:public|protected)\s+(?!class)[\w<>\[\], ]+\s+(\w+)\s*\(([^)]*)\)', re.M)
LOCATOR_RE = re.compile(r'\bBy\s+(\w+)\s*=|WebElement\s+(\w+)\s*;')
PROP_KEY_RE = re.compile(r'^\s*([A-Za-z][\w.]*)\s*=', re.M)

ACTION_HINTS = ("click", "enter", "select", "navigate", "type", "open", "submit",
               "choose", "upload", "logout", "login", "set", "clear")
VALID_HINTS = ("validate", "verify", "check", "assert", "is", "get", "should")


def _module_of(path: Path, base: Path) -> str:
    try:
        rel = path.relative_to(base)
        return rel.parts[0] if len(rel.parts) > 1 else "common"
    except ValueError:
        return "common"


def _param_names(params: str) -> list[str]:
    out = []
    for p in params.split(","):
        p = p.strip()
        if p:
            out.append(p.split()[-1].lstrip("*"))
    return out


def _classify_method(name: str) -> str:
    low = name.lower()
    if any(low.startswith(h) for h in VALID_HINTS):
        return "validation"
    if any(low.startswith(h) for h in ACTION_HINTS):
        return "action"
    return "other"


# ─────────────────────────── feature files ───────────────────────────────────
def parse_feature_text(text: str) -> dict | None:
    """Parse raw Gherkin text (no file/path/txn/module context needed) into
    {feature_name, tags, scenarios}. Reusable for both on-disk framework files
    AND an uploaded feature's raw text — this is what makes feature_matcher.py
    able to compare an upload against the framework without writing it to disk
    first."""
    hdr = FEATURE_HDR_RE.search(text)
    if not hdr:
        return None  # not a real feature (e.g. a stray non-Gherkin .feature)

    lines = text.splitlines()
    feature_tags: list[str] = []
    feature_line_idx = 0
    for i, line in enumerate(lines):
        if FEATURE_HDR_RE.match(line):
            feature_line_idx = i
            break
        m = TAG_LINE_RE.match(line)
        if m:
            feature_tags += m.group(1).split()

    scenarios = []
    pending_tags: list[str] = []
    cur = None
    in_examples = False
    table_headers = None

    def flush():
        if cur is not None:
            scenarios.append(cur)

    for line in lines[feature_line_idx + 1:]:
        m_tag = TAG_LINE_RE.match(line)
        m_sc = SCENARIO_HDR_RE.match(line)
        m_step = STEP_LINE_RE.match(line)
        m_ex = EXAMPLES_HDR_RE.match(line)
        m_row = TABLE_ROW_RE.match(line)

        if m_sc:
            flush()
            cur = {"type": m_sc.group(2), "name": m_sc.group(3).strip(),
                  "tags": pending_tags, "steps": [], "examples": []}
            pending_tags = []
            in_examples = False
            table_headers = None
            continue
        if m_tag:
            # A tag line always belongs to whichever scenario comes NEXT,
            # regardless of the previous scenario's step count.
            pending_tags += m_tag.group(1).split()
            continue
        if cur is None:
            continue
        if m_ex:
            in_examples = True
            table_headers = None
            continue
        if m_row:
            cells = [c.strip() for c in m_row.group(1).split("|")]
            if in_examples:
                if table_headers is None:
                    table_headers = cells
                    cur["examples"] = {"headers": cells, "rows": []}
                else:
                    cur["examples"]["rows"].append(cells)
            else:
                cur.setdefault("data_table_rows", []).append(cells)
            continue
        if m_step:
            cur["steps"].append({"keyword": m_step.group(1), "text": m_step.group(2)})
            continue
    flush()

    return {"feature_name": hdr.group(1).strip(), "tags": feature_tags, "scenarios": scenarios}


def _parse_feature_file(f: Path, base: Path) -> dict | None:
    text = f.read_text(encoding="utf-8", errors="ignore")
    parsed = parse_feature_text(text)
    if not parsed:
        return None
    txn_m = TXN_RE.search(f.stem)
    return {
        "txn": ("Txn" + txn_m.group(1)[3:]) if txn_m else None,
        "module": _module_of(f, base),
        "path": str(f),
        **parsed,
    }


# ───────────────────────── step definitions ──────────────────────────────────
def _parse_step_def_file(f: Path, base: Path) -> dict:
    text = f.read_text(encoding="utf-8", errors="ignore")
    cls = CLASS_RE.search(text)
    methods = [{"keyword": kw, "sentence": sentence, "method": method,
               "params": _param_names(params)}
              for kw, sentence, method, params in STEP_ANN_RE.findall(text)]
    return {"class": cls.group(1) if cls else f.stem, "module": _module_of(f, base),
           "path": str(f), "methods": methods}


# ─────────────────────────── page objects ─────────────────────────────────────
def _parse_page_file(f: Path, base: Path) -> dict:
    text = f.read_text(encoding="utf-8", errors="ignore")
    cls = CLASS_RE.search(text)
    locators = sorted({a or b for a, b in LOCATOR_RE.findall(text) if a or b})
    methods = [{"name": m, "params": _param_names(p), "kind": _classify_method(m)}
              for m, p in METHOD_RE.findall(text)]
    return {"class": cls.group(1) if cls else f.stem, "module": _module_of(f, base),
           "path": str(f), "locators": locators, "methods": methods}


# ───────────────────────────── utilities ───────────────────────────────────────
def _parse_util_file(f: Path) -> dict:
    text = f.read_text(encoding="utf-8", errors="ignore")
    cls = CLASS_RE.search(text)
    methods = [{"name": m, "params": _param_names(p)} for m, p in METHOD_RE.findall(text)]
    return {"class": cls.group(1) if cls else f.stem, "path": str(f), "methods": methods}


# ───────────────────────────── properties ──────────────────────────────────────
def _parse_properties_file(f: Path, env: str) -> dict:
    """KEYS ONLY. Values (credentials, URLs, etc.) are never read into the
    knowledge base — this is a deliberate safety choice (Phase 22)."""
    text = f.read_text(encoding="utf-8", errors="ignore")
    keys = [k for k in PROP_KEY_RE.findall(text) if not k.strip().startswith("#")]
    return {"env": env, "module": f.stem, "path": str(f), "keys": keys}


# ───────────────────────────── orchestration ───────────────────────────────────
def scan_framework() -> dict:
    """Full-depth scan of the entire framework. Additive/read-only — writes
    only to data/framework_knowledge.json, never touches the framework itself."""
    features, step_defs, pages, utils, properties = [], [], [], [], []

    if config.FEATURES_DIR.exists():
        for f in config.FEATURES_DIR.rglob("*.feature"):
            parsed = _parse_feature_file(f, config.FEATURES_DIR)
            if parsed:
                features.append(parsed)

    if config.STEPS_DIR.exists():
        for f in config.STEPS_DIR.rglob("*.java"):
            step_defs.append(_parse_step_def_file(f, config.STEPS_DIR))

    if config.PAGES_DIR.exists():
        for f in config.PAGES_DIR.rglob("*.java"):
            pages.append(_parse_page_file(f, config.PAGES_DIR))

    utils_dir = getattr(config, "UTILS_DIR", None)
    if utils_dir and Path(utils_dir).exists():
        for f in Path(utils_dir).rglob("*.java"):
            utils.append(_parse_util_file(f))

    props_root = getattr(config, "PROPERTIES_ROOT", None)
    if props_root and Path(props_root).exists():
        for env_dir in Path(props_root).iterdir():
            if env_dir.is_dir():
                for f in env_dir.glob("*.properties"):
                    properties.append(_parse_properties_file(f, env_dir.name))

    stats = {
        "features": len(features),
        "scenarios": sum(len(f_["scenarios"]) for f_ in features),
        "steps_in_features": sum(len(sc["steps"]) for f_ in features for sc in f_["scenarios"]),
        "step_def_classes": len(step_defs),
        "step_def_methods": sum(len(s["methods"]) for s in step_defs),
        "page_classes": len(pages),
        "locators": sum(len(p["locators"]) for p in pages),
        "utility_classes": len(utils),
        "properties_files": len(properties),
    }
    out = {"features": features, "step_defs": step_defs, "pages": pages,
          "utils": utils, "properties": properties, "stats": stats}
    (config.DATA_DIR / "framework_knowledge.json").write_text(json.dumps(out, indent=1))
    global _cache
    _cache = out
    return out


_cache: dict = {}


def get_framework_knowledge() -> dict:
    global _cache
    if _cache:
        return _cache
    kf = config.DATA_DIR / "framework_knowledge.json"
    if kf.exists():
        _cache = json.loads(kf.read_text())
        return _cache
    return scan_framework()


def search_features(query: str, limit: int = 20) -> list[dict]:
    """Simple, dependency-free search across feature name / scenario names /
    step text / tags — the 'everything searchable' requirement for Phase 2,
    without yet requiring the embeddings pipeline (that's app/knowledge.py's job
    for the generation-time retrieval; this is for browsing/lookup)."""
    q = query.lower().strip()
    if not q:
        return []
    kb = get_framework_knowledge()
    hits = []
    for f in kb["features"]:
        haystack = " ".join([
            f["feature_name"], " ".join(f["tags"]),
            *(sc["name"] for sc in f["scenarios"]),
            *(st["text"] for sc in f["scenarios"] for st in sc["steps"]),
        ]).lower()
        if q in haystack:
            hits.append({"txn": f["txn"], "module": f["module"],
                        "feature_name": f["feature_name"], "path": f["path"],
                        "scenario_count": len(f["scenarios"])})
        if len(hits) >= limit:
            break
    return hits
