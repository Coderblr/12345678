"""
Framework knowledge base — the practical form of "training the AI on the framework".

A scan of the ENTIRE framework extracts, per module:
  - every step sentence (@Given/@When/@Then/@And/@But annotation text) and the
    class+method implementing it
  - every page class with its locator field names and public method signatures

Saved to data/knowledge.json (rebuilt at startup and via POST /knowledge/rebuild).
At generation time the relevant slice (target module + common) is included in
the Azure OpenAI prompt so the model REUSES existing steps/locators instead of
inventing duplicates. Only interface-level information (sentences, names,
signatures) is included — not method bodies.
"""
import json
import re
from pathlib import Path

from . import config

STEP_ANN = re.compile(r'@(Given|When|Then|And|But)\s*\(\s*"((?:[^"\\]|\\.)*)"', re.S)
CLASS_RE = re.compile(r'(?:public\s+)?class\s+(\w+)')
METHOD_RE = re.compile(r'^\s*(?:public|protected)\s+(?!class)[\w<>\[\], ]+\s+(\w+)\s*\(([^)]*)\)', re.M)
LOCATOR_RE = re.compile(r'\bBy\s+(\w+)\s*=|WebElement\s+(\w+)\s*;')

_kb: dict = {}


def _module_of(path: Path, base: Path) -> str:
    try:
        rel = path.relative_to(base)
        return rel.parts[0] if len(rel.parts) > 1 else "common"
    except ValueError:
        return "common"


def build_knowledge() -> dict:
    """Scan the whole framework once. Returns and caches the knowledge dict."""
    global _kb
    steps, pages = [], []

    if config.STEPS_DIR.exists():
        for f in config.STEPS_DIR.rglob("*.java"):
            text = f.read_text(encoding="utf-8", errors="ignore")
            cls = CLASS_RE.search(text)
            cls_name = cls.group(1) if cls else f.stem
            module = _module_of(f, config.STEPS_DIR)
            for kw, sentence in STEP_ANN.findall(text):
                steps.append({"keyword": kw, "sentence": sentence,
                              "class": cls_name, "module": module})

    if config.PAGES_DIR.exists():
        for f in config.PAGES_DIR.rglob("*.java"):
            text = f.read_text(encoding="utf-8", errors="ignore")
            cls = CLASS_RE.search(text)
            locators = sorted({a or b for a, b in LOCATOR_RE.findall(text) if a or b})
            methods = [f"{m}({', '.join(p.strip().split()[-1] for p in ps.split(',') if p.strip())})"
                       for m, ps in METHOD_RE.findall(text)]
            pages.append({"class": cls.group(1) if cls else f.stem,
                          "module": _module_of(f, config.PAGES_DIR),
                          "locators": locators, "methods": methods})

    _kb = {"steps": steps, "pages": pages,
           "stats": {"step_sentences": len(steps), "page_classes": len(pages),
                     "locators": sum(len(p["locators"]) for p in pages)}}
    (config.DATA_DIR / "knowledge.json").write_text(json.dumps(_kb, indent=1))
    return _kb


def get_knowledge() -> dict:
    global _kb
    if _kb:
        return _kb
    kf = config.DATA_DIR / "knowledge.json"
    if kf.exists():
        _kb = json.loads(kf.read_text())
        return _kb
    return build_knowledge()


def step_catalog(module: str | None, cap: int = 350) -> str:
    """Existing step sentences for the target module + common, one per line,
    formatted for the LLM prompt."""
    kb = get_knowledge()
    wanted = {m for m in [module, "common"] if m}
    lines, seen = [], set()
    ordered = [s for s in kb["steps"] if s["module"] in wanted] + \
              [s for s in kb["steps"] if s["module"] not in wanted]
    for s in ordered:
        key = s["sentence"]
        if key in seen:
            continue
        seen.add(key)
        lines.append(f'{s["keyword"]} "{s["sentence"]}"   [{s["class"]}]')
        if len(lines) >= cap:
            break
    return "\n".join(lines)


def page_catalog(module: str | None, cap: int = 40) -> str:
    """Page classes of the target module: locator names + method signatures."""
    kb = get_knowledge()
    out = []
    for p in kb["pages"]:
        if module and p["module"] != module:
            continue
        out.append(f'{p["class"]}: locators={p["locators"][:25]} methods={p["methods"][:25]}')
        if len(out) >= cap:
            break
    return "\n".join(out)


# ══════════════════ RAG: semantic retrieval over the framework ══════════════
# Every step sentence and page summary is embedded ONCE (data/embeddings.npz).
# At generation time the uploaded feature's steps are embedded and the most
# SIMILAR framework items are retrieved - semantic matching, across modules.

def _azure_embed(texts: list[str]) -> "list[list[float]]":
    import requests
    url = (f"{config.AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
           f"{config.AZURE_OPENAI_EMBED_DEPLOYMENT}/embeddings"
           f"?api-version={config.AZURE_OPENAI_API_VERSION}")
    out = []
    for i in range(0, len(texts), 256):
        r = requests.post(url, headers={"api-key": config.AZURE_OPENAI_KEY,
                                        "Content-Type": "application/json"},
                          json={"input": texts[i:i + 256]}, timeout=180)
        if r.status_code != 200:
            raise RuntimeError(f"Azure embeddings error {r.status_code}: {r.text[:300]}")
        out.extend(d["embedding"] for d in r.json()["data"])
    return out


def _emb_file():
    return config.DATA_DIR / "embeddings.npz"


def embeddings_available() -> bool:
    return bool(config.AZURE_OPENAI_EMBED_DEPLOYMENT) and _emb_file().exists()


def build_embeddings() -> dict:
    """Embed the whole knowledge base. Run via POST /knowledge/rebuild?embed=1."""
    import numpy as np
    if not config.AZURE_OPENAI_EMBED_DEPLOYMENT:
        raise RuntimeError("Set AZURE_OPENAI_EMBED_DEPLOYMENT in config.py first "
                           "(deploy text-embedding-3-small on your Azure resource).")
    kb = get_knowledge()
    items, texts = [], []
    for s_ in kb["steps"]:
        items.append(("step", f'{s_["keyword"]} "{s_["sentence"]}"   [{s_["class"]}]'))
        texts.append(f'{s_["keyword"]} {s_["sentence"]}')
    for p_ in kb["pages"]:
        line = f'{p_["class"]}: locators={p_["locators"][:25]} methods={p_["methods"][:25]}'
        items.append(("page", line))
        texts.append(f'{p_["class"]} {" ".join(p_["locators"])} {" ".join(p_["methods"])}')
    vecs = np.array(_azure_embed(texts), dtype="float32")
    vecs /= (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)
    np.savez_compressed(_emb_file(),
                        vectors=vecs,
                        kinds=np.array([k for k, _ in items]),
                        lines=np.array([l for _, l in items], dtype=object))
    return {"embedded_items": len(items)}


def retrieve(query_texts: list[str], k_steps: int = 30, k_pages: int = 10):
    """Semantic top-k: the framework steps/pages most similar to the queries.
    Returns (steps_text, pages_text) or None if embeddings are unavailable."""
    if not embeddings_available():
        return None
    import numpy as np
    data = np.load(_emb_file(), allow_pickle=True)
    vecs, kinds, lines = data["vectors"], data["kinds"], data["lines"]
    q = np.array(_azure_embed([t[:1000] for t in query_texts if t.strip()]), dtype="float32")
    q /= (np.linalg.norm(q, axis=1, keepdims=True) + 1e-9)
    sims = (q @ vecs.T).max(axis=0)          # best similarity per item over all queries
    order = np.argsort(-sims)
    steps, pages = [], []
    for idx in order:
        if kinds[idx] == "step" and len(steps) < k_steps:
            steps.append(str(lines[idx]))
        elif kinds[idx] == "page" and len(pages) < k_pages:
            pages.append(str(lines[idx]))
        if len(steps) >= k_steps and len(pages) >= k_pages:
            break
    return "\n".join(steps), "\n".join(pages)
