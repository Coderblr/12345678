"""
Builds the Txn index ONCE (at startup or on demand) so lookups are instant.

Result shape (saved to data/index.json):
{
  "Txn010450": {
    "module":  "loans",
    "feature": "C:/.../features/loans/Txn010450_LoanTransactionEnquiry.feature",
    "page":    "C:/.../page/loans/Txn010450_LoanTransactionEnquiryPage.java",
    "steps":   "C:/.../stepDefinitions/loans/Txn010450_LoanTransactionEnquirySteps.java"
  },
  ...
}
"""
import json
import re
import time
from pathlib import Path

from . import config

TXN_RE = re.compile(r"(Txn\d+)", re.IGNORECASE)

_index: dict = {}
_built_at: float = 0.0


def _module_of(path: Path, base: Path) -> str:
    """Return the first sub-folder under base (e.g. 'loans'), or '' if file sits at root."""
    try:
        rel = path.relative_to(base)
        return rel.parts[0].lower() if len(rel.parts) > 1 else ""
    except ValueError:
        return ""


def _canon(token: str) -> str:
    """Canonical txn key: digits with leading zeros stripped, so Txn450,
    Txn0450 and Txn000450 all merge into one entry."""
    digits = re.sub(r"\D", "", token)
    return digits.lstrip("0") or "0"


def build_index() -> dict:
    """Walk the framework ONCE and map every Txn id to its feature/page/steps
    files. Entries are keyed by the canonical number so zero-padding
    differences between feature and java filenames can't split a txn."""
    global _index, _built_at
    idx: dict = {}

    def scan(base: Path, key: str, suffix: str):
        if not base.exists():
            return
        for f in base.rglob(f"*{suffix}"):
            m = TXN_RE.search(f.stem)
            if not m:
                continue
            token = "Txn" + m.group(1)[3:]  # normalise casing -> TxnNNNN
            entry = idx.setdefault(_canon(token),
                {"txn": token, "module": "", "feature": None, "page": None, "steps": None})
            entry[key] = str(f)
            if key == "feature":
                entry["txn"] = token   # display name follows the FEATURE file's naming
            mod = _module_of(f, base)
            if mod and not entry["module"]:
                entry["module"] = mod

    scan(config.FEATURES_DIR, "feature", ".feature")
    scan(config.PAGES_DIR, "page", ".java")
    scan(config.STEPS_DIR, "steps", ".java")

    _index = idx
    _built_at = time.time()
    config.INDEX_FILE.write_text(json.dumps(idx, indent=2))
    return idx


def get_index() -> dict:
    """Load from memory, then disk cache, then rebuild."""
    global _index
    if _index:
        return _index
    if config.INDEX_FILE.exists():
        _index = json.loads(config.INDEX_FILE.read_text())
        return _index
    return build_index()


def lookup(txn_no: str) -> dict | None:
    """Find a txn regardless of typing style or zero padding:
    '450', '000450', 'txn0450', 'Txn000450' all resolve to the same entry."""
    idx = get_index()
    hit = idx.get(_canon(txn_no))
    return dict(hit) if hit else None


def detect_module(feature_text: str) -> str | None:
    """
    For an UPLOADED feature: figure out which module it belongs to.
    1. If it mentions a known Txn id, use that txn's module from the index.
    2. Else, look for a module name in tags / text (@loans, 'cash', ...).
    """
    m = TXN_RE.search(feature_text)
    if m:
        hit = lookup(m.group(1))
        if hit and hit.get("module"):
            return hit["module"]
    low = feature_text.lower()
    for mod in config.MODULES:
        if f"@{mod}" in low or f"/{mod}/" in low or f" {mod} " in low:
            return mod
    return None


def closest_feature(feature_text: str, module: str | None) -> dict | None:
    """
    Find the most similar EXISTING feature (used later as a style template
    for AI code generation). Cheap token-overlap similarity, searched only
    inside the detected module folder as your notes recommend.
    """
    base = config.FEATURES_DIR / module if module else config.FEATURES_DIR
    if not base.exists():
        base = config.FEATURES_DIR
    words = set(re.findall(r"[a-zA-Z]{4,}", feature_text.lower()))
    best, best_score = None, 0.0
    for f in base.rglob("*.feature"):
        try:
            other = set(re.findall(r"[a-zA-Z]{4,}", f.read_text(errors="ignore").lower()))
        except OSError:
            continue
        if not other:
            continue
        score = len(words & other) / len(words | other)
        if score > best_score:
            best, best_score = f, score
    if not best:
        return None
    txn = TXN_RE.search(best.stem)
    related = lookup(txn.group(1)) if txn else None
    return {"feature": str(best), "similarity": round(best_score, 3), "related": related}
