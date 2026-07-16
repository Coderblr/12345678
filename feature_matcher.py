"""
Phase 6 — Intelligent Feature Matching (single responsibility: given an
uploaded feature's text, find the best matching EXISTING feature in the
framework — never relying on filename alone).

Five signals, each 0..1, combined into a weighted, normalized score:
  txn_match           — canonical transaction number match (dominant signal)
  module_match        — same module folder
  tag_overlap         — Jaccard similarity of @tags (feature-level + scenario-level)
  scenario_similarity — lexical (token) overlap of scenario names + step text
  semantic_similarity — cosine similarity of Azure OpenAI embeddings, OPTIONAL:
                        only active if feature-level embeddings have been built
                        (POST /framework/embeddings/rebuild) on top of an
                        AZURE_OPENAI_EMBED_DEPLOYMENT. Weight is redistributed
                        across the other signals when unavailable, so matching
                        always works even with zero Azure configuration.

This module does NOT decide anything on its own and does NOT call the LLM —
it only ranks candidates. It is not yet wired into the live upload/AI-execute
endpoint (that hook-up belongs to Phase 7 Comparison / Phase 8 Normalization,
built on top of this), so it carries zero regression risk to what already works.
"""
import re
from pathlib import Path

from . import config, framework_scanner

TXN_RE = framework_scanner.TXN_RE
_TOKEN_RE = re.compile(r"[a-zA-Z]{4,}")

WEIGHTS = {
    "txn_match": 5.0,
    "module_match": 1.0,
    "tag_overlap": 1.5,
    "scenario_similarity": 2.0,
    "semantic_similarity": 2.5,
}


def _canon_txn(token: str | None) -> str | None:
    if not token:
        return None
    digits = re.sub(r"\D", "", token)
    return digits.lstrip("0") or "0" if digits else None


def _tokens(*texts: str) -> set[str]:
    return {t.lower() for text in texts for t in _TOKEN_RE.findall(text or "")}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def _all_tags(parsed: dict) -> set[str]:
    tags = set(t.lower() for t in parsed.get("tags", []))
    for sc in parsed.get("scenarios", []):
        tags |= {t.lower() for t in sc.get("tags", [])}
    return tags


def _scenario_tokens(parsed: dict) -> set[str]:
    parts = [parsed.get("feature_name", "")]
    for sc in parsed.get("scenarios", []):
        parts.append(sc["name"])
        parts += [s["text"] for s in sc["steps"]]
    return _tokens(*parts)


def _extract_txn(text: str, filename: str | None, txn_hint: str | None) -> str | None:
    if txn_hint:
        return txn_hint
    if filename:
        m = TXN_RE.search(Path(filename).stem)
        if m:
            return m.group(1)
    m = TXN_RE.search(text)
    return m.group(1) if m else None


# ─────────────────────── optional semantic layer ─────────────────────────────
def _emb_file():
    return config.DATA_DIR / "feature_embeddings.npz"


def feature_embeddings_available() -> bool:
    return bool(getattr(config, "AZURE_OPENAI_EMBED_DEPLOYMENT", "")) and _emb_file().exists()


def build_feature_embeddings() -> dict:
    """Embed (feature name + all scenario names) for every framework feature,
    once. Cached to data/feature_embeddings.npz. Separate from
    app/knowledge.py's step/page embeddings — different granularity, different
    purpose (whole-feature matching vs. step/locator reuse retrieval)."""
    import numpy as np
    from . import knowledge  # reuse the existing, already-tested Azure embed caller

    if not getattr(config, "AZURE_OPENAI_EMBED_DEPLOYMENT", ""):
        raise RuntimeError("Set AZURE_OPENAI_EMBED_DEPLOYMENT in config.py first.")

    kb = framework_scanner.get_framework_knowledge()
    keys, texts = [], []
    for f in kb["features"]:
        text = f["feature_name"] + " " + " ".join(sc["name"] for sc in f["scenarios"])
        keys.append({"txn": f["txn"], "module": f["module"], "path": f["path"],
                    "feature_name": f["feature_name"]})
        texts.append(text)
    if not texts:
        raise RuntimeError("No features found to embed — run POST /framework/scan first.")
    vecs = np.array(knowledge._azure_embed(texts), dtype="float32")
    vecs /= (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
    np.savez_compressed(_emb_file(), vectors=vecs,
                        keys=np.array([str(k) for k in keys], dtype=object),
                        meta=np.array(keys, dtype=object))
    return {"embedded_features": len(texts)}


def _semantic_scores(query_text: str) -> dict[str, float]:
    """{path: similarity} for every embedded feature, or {} if unavailable."""
    if not feature_embeddings_available():
        return {}
    import numpy as np
    from . import knowledge
    data = np.load(_emb_file(), allow_pickle=True)
    vecs, meta = data["vectors"], data["meta"]
    q = np.array(knowledge._azure_embed([query_text[:2000]]), dtype="float32")[0]
    q /= (np.linalg.norm(q) + 1e-9)
    sims = vecs @ q
    return {meta[i]["path"]: float(sims[i]) for i in range(len(meta))}


# ────────────────────────────── public API ────────────────────────────────────
def find_matches(uploaded_text: str, filename: str | None = None,
                 module_hint: str | None = None, txn_hint: str | None = None,
                 top_k: int = 5) -> list[dict]:
    """Rank every framework feature against the uploaded text. Returns a list
    of {txn, module, path, feature_name, score, signals: {...}}, best first."""
    parsed = framework_scanner.parse_feature_text(uploaded_text) or \
             {"feature_name": "", "tags": [], "scenarios": []}
    up_txn = _canon_txn(_extract_txn(uploaded_text, filename, txn_hint))
    up_tags = _all_tags(parsed)
    up_tokens = _scenario_tokens(parsed)
    sem_scores = _semantic_scores(uploaded_text)

    kb = framework_scanner.get_framework_knowledge()
    results = []
    for f in kb["features"]:
        signals = {
            "txn_match": 1.0 if up_txn and _canon_txn(f["txn"]) == up_txn else 0.0,
            "module_match": 1.0 if module_hint and f["module"] == module_hint else 0.0,
            "tag_overlap": _jaccard(up_tags, _all_tags(f)),
            "scenario_similarity": _jaccard(up_tokens, _scenario_tokens(f)),
        }
        if sem_scores:
            signals["semantic_similarity"] = max(0.0, sem_scores.get(f["path"], 0.0))

        used_weight = sum(WEIGHTS[s] for s in signals)
        score = sum(WEIGHTS[s] * v for s, v in signals.items()) / used_weight if used_weight else 0.0

        results.append({"txn": f["txn"], "module": f["module"], "path": f["path"],
                        "feature_name": f["feature_name"],
                        "score": round(score, 4), "signals": {k: round(v, 3) for k, v in signals.items()}})

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_k]


def best_match(uploaded_text: str, filename: str | None = None,
              module_hint: str | None = None, txn_hint: str | None = None,
              min_score: float = 0.05) -> dict | None:
    """Convenience: top match, or None if nothing scores above min_score
    (i.e. this is likely a genuinely new feature with no existing counterpart)."""
    matches = find_matches(uploaded_text, filename, module_hint, txn_hint, top_k=1)
    if matches and matches[0]["score"] >= min_score:
        return matches[0]
    return None
