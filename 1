"""
Locator Intelligence Engine — Phase A: Human-Workflow Recorder.

Not a video recorder: a BROWSER EVENT recorder (the Selenium-IDE/Playwright-
codegen approach). We launch Edge with a small JavaScript listener injected;
a human performs the workflow manually in that window; every click and every
field the human types into captures the target element's full DOM identity
(id / name / data-* / aria-label / placeholder / text / ancestor chain).
From those snapshots, stable locators are derived deterministically using the
spec's priority order, reviewed by the human, saved to the Locator Knowledge
Repository (data/locator_repository.json), and injected into every future AI
generation so "// TODO QA: verify locator" guesses become REAL, captured
locators.

SECURITY (deliberate, non-negotiable for a banking app): the recorder NEVER
captures what was typed — no values, no lengths. Only that a field received
input, plus the field's DOM metadata. Passwords and account numbers never
leave the browser.

v1 limitation, stated plainly: iframe capture covers same-origin frames one
level deep, re-checked on every poll. If the NBC app nests frames deeper,
extend _drain_all_contexts — the symptom would be clicks inside a frame not
appearing in the captured list.
"""
import json
import re
import threading
import time
from pathlib import Path

from . import config

# ─────────────────────── injected recorder script ────────────────────────────
RECORDER_JS = r"""
if (!window.__liRec) {
  window.__liRec = true;
  window.__liRecEvents = [];
  var snap = function (el, kind) {
    try {
      var attrs = {};
      for (var i = 0; i < el.attributes.length; i++) {
        var a = el.attributes[i];
        if (a.name !== 'value') attrs[a.name] = (a.value || '').slice(0, 120);
      }
      var chain = [], p = el.parentElement, d = 0;
      while (p && d < 4) {
        chain.push({tag: p.tagName.toLowerCase(), id: p.id || null,
                    cls: (p.className && p.className.split ? p.className.split(/\s+/)[0] : null) || null});
        p = p.parentElement; d++;
      }
      window.__liRecEvents.push({
        kind: kind,
        tag: el.tagName.toLowerCase(),
        type: (el.getAttribute('type') || '').toLowerCase(),
        attrs: attrs,
        text: (el.innerText || el.textContent || '').trim().slice(0, 80),
        ancestors: chain,
        url: location.href.slice(0, 300),
        title: (document.title || '').slice(0, 120),
        ts: Date.now()
      });
    } catch (e) {}
  };
  document.addEventListener('click', function (e) { snap(e.target, 'click'); }, true);
  document.addEventListener('change', function (e) { snap(e.target, 'input'); }, true);
}
"""
DRAIN_JS = "var e = window.__liRecEvents || []; window.__liRecEvents = []; return e;"


# ─────────────────────── locator derivation (pure) ───────────────────────────
_AUTOGEN_ID = re.compile(r"\d{4,}|^(ext-|gwt-|j_id|__)|--\d+$")


def derive_locators(ev: dict) -> list[dict]:
    """Ordered stable-locator candidates for one captured element, following
    the spec priority: data-testid > data-qa > id > name > aria-label >
    stable CSS > relative XPath. Every candidate includes a ready Java line."""
    attrs = ev.get("attrs", {})
    tag = ev.get("tag", "*")
    out = []

    def add(loc_type, value, java_by, note=""):
        out.append({"locator_type": loc_type, "locator_value": value,
                    "java": java_by, "note": note})

    for da in ("data-testid", "data-qa"):
        if attrs.get(da):
            add(da, attrs[da], f'By.cssSelector("[{da}=\'{attrs[da]}\']")')
    if attrs.get("id"):
        note = "id looks auto-generated - verify it is stable across sessions" \
            if _AUTOGEN_ID.search(attrs["id"]) else ""
        add("id", attrs["id"], f'By.id("{attrs["id"]}")', note)
    if attrs.get("name"):
        add("name", attrs["name"], f'By.name("{attrs["name"]}")')
    if attrs.get("aria-label"):
        add("aria-label", attrs["aria-label"],
            f'By.cssSelector("{tag}[aria-label=\'{attrs["aria-label"]}\']")')
    if attrs.get("placeholder"):
        add("css-placeholder", attrs["placeholder"],
            f'By.cssSelector("{tag}[placeholder=\'{attrs["placeholder"]}\']")')
    text = (ev.get("text") or "").strip()
    if text and len(text) <= 40 and tag in ("button", "a", "span", "td", "th", "label", "option", "div"):
        safe = text.replace('"', "'")
        add("xpath-text", text, f'By.xpath("//{tag}[normalize-space()=\\"{safe}\\"]")')
    # last resort: relative xpath anchored on the nearest ancestor WITH an id
    for anc in ev.get("ancestors", []):
        if anc.get("id") and not _AUTOGEN_ID.search(anc["id"]):
            suffix = f'[@type=\'{ev["type"]}\']' if ev.get("type") else ""
            add("xpath-relative", f'#{anc["id"]} >> {tag}',
                f'By.xpath("//*[@id=\\"{anc["id"]}\\"]//{tag}{suffix}")',
                "anchored on nearest stable ancestor id")
            break
    if not out:
        add("xpath-fragile", tag, f'By.xpath("//{tag}")',
            "NO stable attributes found - this element needs a dev-added id/data-testid")
    return out


_ROLE_SUFFIX = {"button": "Button", "a": "Link", "select": "Dropdown",
               "textarea": "Textbox", "table": "Table", "img": "Image"}


def suggest_name(ev: dict) -> str:
    """A meaningful Java field name per the spec (customerNameTextbox, not field1)."""
    attrs = ev.get("attrs", {})
    base = (attrs.get("aria-label") or attrs.get("placeholder") or attrs.get("name")
            or attrs.get("id") or ev.get("text") or ev.get("tag", "element"))
    # split on non-letters AND on camelCase boundaries, so "holdReason" -> hold, Reason
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+", base)[:5] or ["element"]
    # drop a leading UI-hungarian prefix (btnSetHold -> setHoldButton, not btnSetHoldButton)
    if len(words) > 1 and words[0].lower() in ("btn", "txt", "lbl", "ddl", "chk", "img", "lnk", "tbl"):
        words = words[1:]
    words = words[:4]
    camel = words[0].lower() + "".join(w.capitalize() for w in words[1:])
    tag, typ = ev.get("tag", ""), ev.get("type", "")
    if tag == "input":
        suffix = {"checkbox": "Checkbox", "radio": "Radio", "submit": "Button",
                 "button": "Button"}.get(typ, "Textbox")
    else:
        suffix = _ROLE_SUFFIX.get(tag, "Element")
    return camel if camel.lower().endswith(suffix.lower()) else camel + suffix


# ─────────────────────── locator repository ──────────────────────────────────
def _repo_file() -> Path:
    return config.DATA_DIR / "locator_repository.json"


def load_repository() -> list[dict]:
    f = _repo_file()
    return json.loads(f.read_text()) if f.exists() else []


def save_entries(entries: list[dict]) -> int:
    repo = load_repository()
    existing = {(e.get("name"), e.get("java")) for e in repo}
    added = 0
    for e in entries:
        if (e.get("name"), e.get("java")) not in existing:
            e.setdefault("saved_at", time.strftime("%Y-%m-%d %H:%M:%S"))
            repo.append(e)
            added += 1
    _repo_file().write_text(json.dumps(repo, indent=1))
    return added


def repository_for_prompt(cap: int = 40) -> str:
    """Captured locators formatted for the AI generation prompt — real,
    human-verified locators the model must prefer over TODO guesses."""
    lines = []
    for e in reversed(load_repository()):          # newest first
        lines.append(f'{e.get("name")}: {e.get("java")}   '
                     f'[screen: {e.get("screen", e.get("title", "?"))}]')
        if len(lines) >= cap:
            break
    return "\n".join(lines)


# ─────────────────────── live session management ─────────────────────────────
_session: dict = {"driver": None, "events": [], "thread": None, "stop": False}
_LOCK = threading.Lock()


def _new_driver(url: str):
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options
    from selenium.webdriver.edge.service import Service
    opts = Options()
    opts.add_argument("--start-maximized")
    driver_path = getattr(config, "EDGE_DRIVER_PATH", "")
    service = Service(executable_path=driver_path) if driver_path and Path(driver_path).exists() else Service()
    driver = webdriver.Edge(service=service, options=opts)
    if url:
        driver.get(url)
    return driver


def _drain_all_contexts(driver) -> list[dict]:
    """Inject-if-missing and drain events from the top document and every
    same-origin first-level iframe."""
    collected = []

    def drain_current():
        try:
            driver.execute_script(RECORDER_JS)
            collected.extend(driver.execute_script(DRAIN_JS) or [])
        except Exception:  # noqa: BLE001 — page mid-navigation; next poll catches up
            pass

    try:
        driver.switch_to.default_content()
    except Exception:  # noqa: BLE001
        return collected
    drain_current()
    try:
        from selenium.webdriver.common.by import By
        frames = driver.find_elements(By.TAG_NAME, "iframe")
        for i in range(len(frames)):
            try:
                driver.switch_to.default_content()
                driver.switch_to.frame(i)
                drain_current()
            except Exception:  # noqa: BLE001 — cross-origin or vanished frame
                continue
        driver.switch_to.default_content()
    except Exception:  # noqa: BLE001
        pass
    return collected


def _poll_loop():
    while not _session["stop"]:
        driver = _session["driver"]
        if driver is None:
            break
        for ev in _drain_all_contexts(driver):
            ev["derived"] = derive_locators(ev)
            ev["suggested_name"] = suggest_name(ev)
            _session["events"].append(ev)
        time.sleep(0.8)


def start(url: str = "") -> dict:
    with _LOCK:
        if _session["driver"] is not None:
            raise RuntimeError("A recording session is already running - stop it first.")
        try:
            driver = _new_driver(url)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"Could not launch Edge for recording ({e.__class__.__name__}: {e}). "
                "Check EDGE_DRIVER_PATH in config.py points at msedgedriver.exe "
                "and that its version matches the installed Edge browser.")
        _session.update(driver=driver, events=[], stop=False)
        t = threading.Thread(target=_poll_loop, daemon=True)
        _session["thread"] = t
        t.start()
    return {"running": True, "url": url}


def status() -> dict:
    events = _session["events"]
    return {"running": _session["driver"] is not None, "captured": len(events),
            "recent": [{"kind": e["kind"], "tag": e["tag"],
                        "name": e["suggested_name"],
                        "top_locator": e["derived"][0]["java"] if e["derived"] else None}
                       for e in events[-5:]]}


def stop() -> dict:
    with _LOCK:
        driver = _session["driver"]
        _session["stop"] = True
        _session["driver"] = None
        if driver is not None:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001
                pass
        events = _session["events"]
        _session["events"] = []
    return {"captured": len(events), "events": [
        {"kind": e["kind"], "tag": e["tag"], "type": e.get("type", ""),
         "text": e.get("text", ""), "title": e.get("title", ""), "url": e.get("url", ""),
         "suggested_name": e["suggested_name"], "candidates": e["derived"]}
        for e in events]}
