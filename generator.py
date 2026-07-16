"""
Phase 2: AI-driven framework updates.

Flow (called from executor._run when the user enables AI on an upload):
  1. dry-run already told us which steps are undefined
  2. build a context pack: uploaded feature + old feature + existing Steps/Page java
  3. ask Azure OpenAI for COMPLETE updated java files
  4. javac them into an isolated classes dir (compile errors go back to the
     model for up to AI_MAX_FIX_ROUNDS repair rounds)
  5. return {rel_path: code} + the classes dir; the executor prepends that dir
     to the classpath so the JVM uses the new classes WITHOUT touching the
     framework. Commit-to-framework only happens after a green run.
"""
import re
import subprocess
from pathlib import Path

import requests

from . import config

FILE_RE = re.compile(r"===FILE:\s*(.+?)\s*===\s*\n(.*?)===END===", re.S)
PKG_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
CLS_RE = re.compile(r"(?:public\s+)?class\s+(\w+)")

SYSTEM_PROMPT = """You are a senior Java test-automation engineer maintaining an enterprise
Cucumber + Selenium + TestNG framework (package root com.nbc).
Your job: update step-definition and/or page-object classes so a modified Gherkin feature executes.

Rules:
- Keep existing package declarations, imports, naming and coding style.
- REUSE existing methods, steps and locators wherever possible; change only what the feature requires.
- Cucumber matches steps by their exact sentence text - annotation strings must match the feature lines exactly.
- NEVER invent application locators. If a genuinely new UI element is needed, copy the locator style
  of the provided Page class and mark it with: // TODO QA: verify locator
- APPEND-ONLY mindset: PRESERVE every existing method, field, locator and import exactly as given;
  add the new step methods / page methods / locators needed by the new or changed scenarios.
- NEVER call a method (on a Page object, a CommonSteps/shared class, a utility class, or anything
  else) unless that exact method signature is EITHER shown to you verbatim in the context below OR
  defined by you, in full, within one of the files you return in this same response. If you need a
  helper action that doesn't exist yet, either reuse an EXACT existing method shown in the context,
  or write the new method's full implementation yourself in the appropriate file — never invent a
  call to a method you have not seen and are not also defining.
- CRITICAL: NEVER implement a step (a @Given/@When/@Then/@And/@But method) whose exact sentence
  already appears ANYWHERE in the EXISTING STEP SENTENCES catalog below (including CommonSteps and
  any other shared class) — even when returning a "complete" file that also needs that step to run.
  Cucumber throws a hard runtime error if the same step sentence is implemented in two classes.
  Only implement methods for the steps explicitly listed as UNDEFINED.
- This framework's shared/common classes (login helpers, property readers, file utilities) often do
  file I/O and throw CHECKED exceptions (e.g. IOException) from their constructors or methods. Every
  time you call such a constructor or method, wrap the call in try/catch (matching this framework's
  existing style, typically catching the exception and calling printStackTrace()) — do not let a
  checked exception propagate unhandled, and do not just add a blanket "throws Exception" unless the
  real code you were shown does the same.
- Return COMPLETE files (never fragments), and ONLY the files that change.
- Output format, with NOTHING else before, between, or after the blocks:
===FILE: com/nbc/stepDefinitions/<module>/<ClassName>.java===
<entire file content>
===END===
"""


def _azure_chat(messages: list[dict]) -> str:
    url = (f"{config.AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
           f"{config.AZURE_OPENAI_DEPLOYMENT}/chat/completions"
           f"?api-version={config.AZURE_OPENAI_API_VERSION}")
    if "YOUR-RESOURCE" in config.AZURE_OPENAI_ENDPOINT or "PASTE" in config.AZURE_OPENAI_KEY:
        raise RuntimeError("Azure OpenAI is not configured yet - fill AZURE_OPENAI_ENDPOINT / "
                           "AZURE_OPENAI_KEY / AZURE_OPENAI_DEPLOYMENT in app/config.py.")
    try:
        r = requests.post(url,
                          headers={"api-key": config.AZURE_OPENAI_KEY,
                                   "Content-Type": "application/json"},
                          json={"messages": messages, "temperature": 0.1, "max_tokens": 8000},
                          timeout=240)
    except requests.RequestException as e:
        raise RuntimeError(f"Cannot reach Azure OpenAI at {config.AZURE_OPENAI_ENDPOINT} "
                           f"({e.__class__.__name__}: {e}). Check the endpoint URL and whether "
                           "the office network/proxy allows it.")
    if r.status_code == 401:
        raise RuntimeError("Azure OpenAI rejected the API key (401). Re-check AZURE_OPENAI_KEY.")
    if r.status_code == 404:
        raise RuntimeError(f"Azure OpenAI returned 404 - deployment '{config.AZURE_OPENAI_DEPLOYMENT}' "
                           "not found on this resource. Use the DEPLOYMENT NAME from Azure Portal "
                           "(not the model name), and check AZURE_OPENAI_API_VERSION.")
    if r.status_code != 200:
        raise RuntimeError(f"Azure OpenAI error {r.status_code}: {r.text[:400]}")
    return r.json()["choices"][0]["message"]["content"]


def _read(path, cap=16000) -> str:
    try:
        t = Path(path).read_text(encoding="utf-8", errors="ignore")
        return t[:cap] + ("\n// ...truncated..." if len(t) > cap else "")
    except (OSError, TypeError):
        return ""


def _parse_files(text: str) -> dict[str, str]:
    """{relative java path: full code}. Path is normalised from the package line
    when the header path looks wrong."""
    out = {}
    for raw_path, code in FILE_RE.findall(text):
        code = code.strip()
        pkg = PKG_RE.search(code)
        cls = CLS_RE.search(code)
        if pkg and cls:
            rel = pkg.group(1).replace(".", "/") + "/" + cls.group(1) + ".java"
        else:
            rel = raw_path.strip().replace("\\", "/")
        out[rel] = code + "\n"
    return out


def _compile(files: dict[str, str], job_dir: Path, classpath: str) -> tuple[bool, str, Path]:
    """Write sources under job_dir/src, javac into job_dir/classes."""
    src, classes = job_dir / "src", job_dir / "classes"
    classes.mkdir(parents=True, exist_ok=True)
    paths = []
    for rel, code in files.items():
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(code, encoding="utf-8")
        paths.append(str(f))
    cmd = [config.JAVAC, "-encoding", "UTF-8", "-cp", classpath,
           "-d", str(classes), *paths]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        return False, ("javac not found at '" + config.JAVAC + "'. Point JAVAC_CMD (or config.JAVAC) "
                       "at a JDK's bin\\javac.exe - a plain JRE cannot compile."), classes
    return r.returncode == 0, (r.stdout + r.stderr).strip(), classes


def _common_steps_context() -> str:
    """Full source of every step-definition file in the 'common' module —
    login/logout/navigation helpers that nearly every generated step relies
    on. Sent in FULL (not just the sentence catalog) so the model calls real
    method names instead of guessing them."""
    try:
        from . import framework_scanner
        kb = framework_scanner.get_framework_knowledge()
        blobs = []
        for sd in kb["step_defs"]:
            if sd.get("module") == "common":
                blobs.append(_read(sd["path"], cap=6000))
        return "\n\n".join(blobs)
    except Exception:  # noqa: BLE001 — best-effort context, never blocks generation
        return ""


_MISSING_SYMBOL_RE = re.compile(
    r'cannot find symbol.*?symbol:\s*method\s+(\w+)\([^\n]*\n\s*location:\s*variable\s+\w+\s+of\s+type\s+(\w+)',
    re.S)


def _find_class_source(class_name: str) -> str | None:
    """Search steps/pages/utils folders for the file defining `class_name`
    and return its real current content, or None if not found."""
    for base in (getattr(config, "STEPS_DIR", None), getattr(config, "PAGES_DIR", None),
                getattr(config, "UTILS_DIR", None)):
        if not base or not Path(base).exists():
            continue
        for f in Path(base).rglob(f"{class_name}.java"):
            return f.read_text(encoding="utf-8", errors="ignore")
    return None


_STEP_ANN_FULL_RE = re.compile(r'@(Given|When|Then|And|But)\s*\(\s*"((?:[^"\\]|\\.)*)"\s*\)')
_METHOD_SIG_RE = re.compile(
    r'(?:public|protected)\s+(?!class\b)[\w<>\[\], ]+?\s+(\w+)\s*\(([^)]*)\)\s*'
    r'(?:throws\s+[\w,.\s]+)?\s*\{')


def _remove_step_method(code: str, keyword: str, sentence: str) -> tuple[str, bool]:
    """Excise ONE step method (its annotation through its matching closing
    brace) from generated source, identified by its exact keyword+sentence."""
    ann_pat = re.compile(r'@' + re.escape(keyword) + r'\s*\(\s*"' + re.escape(sentence) + r'"\s*\)')
    am = ann_pat.search(code)
    if not am:
        return code, False
    mm = _METHOD_SIG_RE.search(code, am.end())
    if not mm:
        return code, False
    depth, i, n = 0, mm.end() - 1, len(code)
    while i < n:
        if code[i] == '{':
            depth += 1
        elif code[i] == '}':
            depth -= 1
            if depth == 0:
                return (code[:am.start()] + code[i + 1:]).strip() + "\n", True
        i += 1
    return code, False


def _strip_duplicate_steps(files: dict[str, str], log) -> dict[str, str]:
    """Safety net for a real failure mode: Cucumber throws
    DuplicateStepDefinitionException at RUNTIME (glue-loading, before any
    scenario executes) if the same step sentence is implemented in two
    classes — javac cannot catch this, so prompting alone is not reliable
    enough to prevent it. Deterministically detect and remove any generated
    step method that duplicates a sentence already owned by a DIFFERENT
    existing class in the framework. A generated file legitimately keeping/
    updating one of ITS OWN pre-existing steps is correctly left alone."""
    try:
        from . import framework_scanner
        kb = framework_scanner.get_framework_knowledge()
    except Exception:  # noqa: BLE001 — best-effort; never block generation
        return files

    existing_owner: dict[str, str] = {}
    for sd in kb["step_defs"]:
        for m in sd["methods"]:
            key = re.sub(r"\s+", " ", m["sentence"]).strip().lower()
            existing_owner.setdefault(key, sd["class"])

    out = {}
    for rel, code in files.items():
        cls_m = re.search(r'class\s+(\w+)', code)
        this_class = cls_m.group(1) if cls_m else None
        new_code = code
        for kw, sentence in _STEP_ANN_FULL_RE.findall(code):
            key = re.sub(r"\s+", " ", sentence).strip().lower()
            owner = existing_owner.get(key)
            if owner and owner != this_class:
                new_code, removed = _remove_step_method(new_code, kw, sentence)
                if removed:
                    log(f'>>> AI safety check: removed duplicate step "{sentence}" from {rel} '
                       f'(already implemented in {owner}) — prevents DuplicateStepDefinitionException')
        out[rel] = new_code
    return out


_UNREPORTED_EXCEPTION_RE = re.compile(
    r'unreported exception ([\w.]+); must be caught or declared to be thrown')


def _exception_handling_hint(err: str) -> str:
    """javac's 'unreported exception X; must be caught or declared to be
    thrown' is a purely mechanical Java rule the model keeps failing to
    satisfy blindly on retry — give it the exact fix instead of hoping it
    guesses. Different failure category from a missing/hallucinated method:
    the class and method here are REAL, the code just doesn't satisfy Java's
    checked-exception handling requirement."""
    exceptions = sorted(set(_UNREPORTED_EXCEPTION_RE.findall(err)))
    if not exceptions:
        return ""
    exc_list = ", ".join(e.rsplit(".", 1)[-1] for e in exceptions)
    return (f"\n\nJava CHECKED EXCEPTION error: your code throws {exc_list} without handling it. "
           f"Java requires every checked exception to be either caught or declared. Fix this by "
           f"wrapping ONLY the specific line(s) that throw it in a try/catch that catches "
           f"{exc_list} and rethrows as an unchecked exception, for example:\n"
           f"    try {{ ... }} catch ({exc_list} e) {{ throw new RuntimeException(e); }}\n"
           f"Do not add `throws {exc_list}` to a Cucumber step method's signature — Cucumber step "
           f"methods must not declare checked exceptions. Return the corrected COMPLETE file(s).")


def _extra_context_for_compile_errors(err: str) -> str:
    """Parse javac's 'cannot find symbol: method X ... location: variable v
    of type ClassName' pattern and, for every ClassName we can find on disk,
    inject its REAL current source — this is what lets the retry round fix a
    hallucinated method call instead of just guessing again. Also appends a
    mechanical fix hint for unhandled checked exceptions, a separate and
    common failure category."""
    found = {}
    for method, cls in _MISSING_SYMBOL_RE.findall(err):
        if cls in found:
            continue
        src = _find_class_source(cls)
        if src:
            found[cls] = src
    parts = []
    if found:
        parts.append("\n\nThe methods you called do not exist. Here is the REAL current source of "
                     "the class(es) involved — use an exact existing method from here, or if the "
                     "needed behaviour genuinely doesn't exist yet, define it yourself in full:")
        for cls, src in found.items():
            parts.append(f"\n## Real current source of {cls}:\n```java\n{src}\n```")
    parts.append(_exception_handling_hint(err))
    return "".join(parts)


def generate_and_compile(ai_ctx: dict, undefined_steps: list[str],
                         classpath: str, job_dir: Path, log) -> tuple[dict, Path]:
    """Returns ({rel_path: code}, classes_dir). Raises RuntimeError with a
    readable message on unrecoverable failure."""
    hit = ai_ctx.get("hit") or {}
    old_feature = _read(hit.get("feature")) if hit.get("feature") else ""
    steps_java = _read(hit.get("steps")) if hit.get("steps") else ""
    page_java = _read(hit.get("page")) if hit.get("page") else ""

    try:
        from . import recorder as _rec
        _captured_locators = _rec.repository_for_prompt()
    except Exception:  # noqa: BLE001 — repository is best-effort
        _captured_locators = ""
    try:
        from . import knowledge
        # RAG: semantic retrieval when embeddings exist, module catalogs otherwise
        hits = None
        try:
            hits = knowledge.retrieve([ai_ctx["uploaded_text"], *undefined_steps])
        except Exception as e:  # noqa: BLE001 — embeddings are best-effort
            log(f">>> RAG retrieval unavailable ({e}); using module catalogs.")
        if hits:
            cat_steps, cat_pages = hits
            log(">>> RAG: retrieved the most similar existing steps/pages for context.")
        else:
            cat_steps = knowledge.step_catalog(ai_ctx.get("module"))
            cat_pages = knowledge.page_catalog(ai_ctx.get("module"))
    except Exception:  # noqa: BLE001 — knowledge is best-effort
        cat_steps = cat_pages = "(knowledge base unavailable)"

    user = f"""The QA engineer uploaded a modified feature file. Update the framework so it runs.

## Uploaded feature (the one that must execute):
```gherkin
{ai_ctx['uploaded_text'][:12000]}
```

## Previous version of this feature in the framework{' (none - this txn had no feature)' if not old_feature else ''}:
```gherkin
{old_feature or '(none)'}
```

## Steps reported UNDEFINED by a cucumber dry run (these have no matching @Given/@When/@Then):
{chr(10).join('- ' + u for u in undefined_steps) or '- (none - only wording/data changed)'}

## EXISTING STEP SENTENCES across the framework (module + common) — REUSE these
## (by keeping the feature wording identical) instead of writing new step methods:
{cat_steps or '(none found)'}

## EXISTING PAGE CLASSES in this module (locator names + method signatures) —
## call these instead of inventing duplicates:
{cat_pages or '(none found)'}

## Existing step-definition class for this transaction:
```java
{steps_java or '(none exists yet - create one in com.nbc.stepDefinitions.<module>)'}
```

## Existing page-object class for this transaction:
```java
{page_java or '(none provided - reuse steps/pages patterns and add TODO markers for locators)'}
```

## CAPTURED LOCATORS from recorded manual sessions (REAL locators verified on
## the live application by a human - ALWAYS use these instead of a TODO guess
## when the element matches; keep the suggested field name):
{_captured_locators or '(none recorded yet)'}

## FULL SOURCE of shared/common step definitions (login, logout, navigation —
## call these EXACT method names if you need this behaviour; do not invent your own):
```java
{_common_steps_context() or '(none found)'}
```

Produce the updated file(s) now, in the required ===FILE / ===END format."""

    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user}]

    log(f">>> AI: sending context to Azure OpenAI ({config.AZURE_OPENAI_DEPLOYMENT})...")
    reply = _azure_chat(messages)
    files = _parse_files(reply)
    if not files:
        raise RuntimeError("AI returned no file blocks - response did not follow the required format.")
    log(">>> AI: received " + ", ".join(files.keys()))
    files = _strip_duplicate_steps(files, log)

    for attempt in range(config.AI_MAX_FIX_ROUNDS + 1):
        ok, err, classes = _compile(files, job_dir, classpath)
        if ok:
            log(">>> AI: compilation OK")
            return files, classes
        if attempt == config.AI_MAX_FIX_ROUNDS:
            raise RuntimeError("Generated java failed to compile after "
                               f"{config.AI_MAX_FIX_ROUNDS + 1} attempts:\n{err[:1500]}")
        undefined_method_hint = bool(_MISSING_SYMBOL_RE.search(err))
        exception_hint = bool(_UNREPORTED_EXCEPTION_RE.search(err))
        extra = _extra_context_for_compile_errors(err)
        if undefined_method_hint and exception_hint:
            log(f">>> AI: compile errors include an undefined method AND an unhandled checked "
               f"exception — injecting real source + a fix hint (round {attempt + 1})...")
        elif undefined_method_hint:
            log(f">>> AI: compile errors reference undefined methods — injecting the REAL source "
               f"of the class(es) involved, then asking the model to fix (round {attempt + 1})...")
        elif exception_hint:
            log(f">>> AI: compile errors are an unhandled checked exception — injecting a "
               f"mechanical fix hint (round {attempt + 1})...")
        else:
            log(f">>> AI: compile errors, asking the model to fix (round {attempt + 1})...")
        messages += [{"role": "assistant", "content": reply},
                     {"role": "user", "content":
                      "javac failed with these errors. Fix them and return the corrected "
                      "complete files in the same ===FILE/===END format:\n\n" + err[:6000] + extra}]
        reply = _azure_chat(messages)
        new_files = _parse_files(reply)
        if new_files:
            files.update(_strip_duplicate_steps(new_files, log))
    raise RuntimeError("unreachable")


# ─────────────────────── deterministic feature merge ────────────────────────
SC_LINE = re.compile(r"\s*(?:Scenario Outline:|Scenario:)\s*(.+)")
TAG_LINE = re.compile(r"\s*@")


def _blocks(text: str):
    """Split a feature into (head, {scenario_name: block_text}, order).
    A block starts at its tag lines (if any) and runs to the next scenario."""
    lines = text.splitlines(keepends=True)
    starts = []           # (block_start_line, scenario_name)
    for i, l in enumerate(lines):
        m = SC_LINE.match(l)
        if m:
            j = i
            while j > 0 and TAG_LINE.match(lines[j - 1]):
                j -= 1
            starts.append((j, m.group(1).strip()))
    if not starts:
        return text, {}, []
    head = "".join(lines[:starts[0][0]])
    blocks, order = {}, []
    for n, (b, name) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        blocks[name] = "".join(lines[b:end])
        order.append(name)
    return head, blocks, order


def merge_feature(old_text: str, new_text: str) -> tuple[str, list[str], list[str]]:
    """Merge the uploaded feature into the existing one.
    - scenario with a NEW name -> appended at the end
    - scenario with an EXISTING name but different body -> replaced in place
    Returns (merged_text, added_names, replaced_names)."""
    if not old_text.strip():
        _, sc, order = _blocks(new_text)
        return new_text, order, []
    old_head, old_sc, old_order = _blocks(old_text)
    _, new_sc, new_order = _blocks(new_text)
    added, replaced = [], []
    for name in new_order:
        if name not in old_sc:
            added.append(name)
        elif old_sc[name].strip() != new_sc[name].strip():
            old_sc[name] = new_sc[name]
            replaced.append(name)
    merged = old_head + "".join(old_sc[n] for n in old_order)
    for name in added:
        merged = merged.rstrip("\n") + "\n\n" + new_sc[name].rstrip("\n") + "\n"
    return merged, added, replaced
