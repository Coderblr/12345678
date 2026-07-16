"""
BRD Processor — closes the one gap in the Normalization/Update Engine spec:
accepting a BUSINESS REQUIREMENT DOCUMENT (.pdf / .docx / .txt) instead of a
ready-made .feature file.

Flow: extract text -> Azure OpenAI drafts a feature file USING THE FRAMEWORK'S
OWN CONVENTIONS (existing step sentences via the knowledge catalog, tag styles,
a real feature as a style example) -> the draft then enters the EXISTING
pipeline (match -> normalize -> merge -> dry-run -> generate -> review) exactly
as if the user had uploaded that feature themselves. The BRD is never the
source of truth; the framework is — the draft exists only to be normalized and
merged like any other upload, with the same human review gate.
"""
import io
import re
from pathlib import Path

from . import config


def extract_text(filename: str, data: bytes) -> str:
    """Plain text from a BRD file. Raises ValueError with a user-facing
    message for unsupported/broken files."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".txt":
        return data.decode("utf-8", errors="ignore")
    if suffix == ".docx":
        try:
            import docx
            document = docx.Document(io.BytesIO(data))
            parts = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
            return "\n".join(parts)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Could not read .docx file: {e}")
    if suffix == ".pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(data))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Could not read .pdf file: {e}")
    if suffix == ".doc":
        raise ValueError("Legacy .doc format is not supported - save the BRD as .docx or .pdf and re-upload.")
    raise ValueError(f"Unsupported BRD type '{suffix}' - upload .pdf, .docx, or .txt (or a .feature file).")


BRD_SYSTEM_PROMPT = """You are a senior QA automation engineer converting a business requirement
document (BRD) into a Cucumber feature file for an existing enterprise banking automation framework.

STRICT RULES:
- Output ONLY the Gherkin feature file. No explanations, no markdown fences, no commentary.
- Start with tag line(s), then 'Feature:', then scenarios with Given/When/Then/And steps.
- REUSE the exact wording of EXISTING STEP SENTENCES provided below whenever the required behaviour
  matches - identical wording means the framework already automates that step with zero new code.
- Follow the tag conventions and formatting style of the EXAMPLE EXISTING FEATURE provided below.
- Write concrete, executable scenarios with real values from the BRD (amounts, codes, transaction
  numbers). Use Scenario Outline + Examples tables when the BRD lists multiple data variations.
- If the BRD references a transaction number, include it in scenario wording the same way the
  example feature does.
- Do not invent UI details the BRD doesn't mention; keep steps at the business-action level."""


def brd_to_feature(brd_text: str, filename: str | None = None, log=lambda m: None) -> dict:
    """Convert BRD text into a draft feature file written in the framework's
    own conventions. Returns {feature_text, txn, module}."""
    from . import framework_scanner, generator, indexer, knowledge

    if len(brd_text.strip()) < 40:
        raise ValueError("The BRD appears to be empty or unreadable after text extraction.")

    txn_m = re.search(r"(?:Txn|transaction)\s*[#:]?\s*0*(\d{3,6})", brd_text, re.I)
    txn = txn_m.group(1) if txn_m else None
    module = indexer.detect_module(brd_text)

    try:
        catalog = knowledge.step_catalog(module, cap=250)
    except Exception:  # noqa: BLE001
        catalog = "(knowledge base unavailable)"

    style_example = ""
    try:
        kb = framework_scanner.get_framework_knowledge()
        candidates = [f for f in kb["features"] if not module or f["module"] == module] or kb["features"]
        if candidates:
            style_example = Path(candidates[0]["path"]).read_text(encoding="utf-8", errors="ignore")[:3000]
    except Exception:  # noqa: BLE001
        pass

    log(">>> BRD: converting business requirements to a framework-convention feature draft...")
    user = f"""## BUSINESS REQUIREMENT DOCUMENT{f' ({filename})' if filename else ''}:
{brd_text[:12000]}

## EXISTING STEP SENTENCES in the framework (reuse EXACT wording where behaviour matches):
{catalog or '(none found)'}

## EXAMPLE EXISTING FEATURE (match this style, tags, and formatting):
```gherkin
{style_example or '(none available)'}
```

Produce the feature file now."""
    reply = generator._azure_chat([{"role": "system", "content": BRD_SYSTEM_PROMPT},
                                   {"role": "user", "content": user}])

    text = reply.strip()
    text = re.sub(r"^```(?:gherkin)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip() + "\n"

    parsed = framework_scanner.parse_feature_text(text)
    if not parsed or not parsed["scenarios"]:
        raise ValueError("The AI's BRD conversion did not produce valid Gherkin - "
                         "the BRD may be too vague to derive concrete scenarios. "
                         "Add explicit flows/validations to the BRD, or write the feature manually.")
    log(f">>> BRD: draft feature ready - {len(parsed['scenarios'])} scenario(s), "
        f"txn hint: {txn or 'none'}, module: {module or 'undetected'}.")
    return {"feature_text": text, "txn": txn, "module": module}
