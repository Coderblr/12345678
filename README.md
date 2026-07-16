# Txn Automation Console — Project Reference

> **Purpose of this document:** This README is written to be handed to another AI assistant
> (or a new engineer) with zero prior context, so it can understand the system's purpose,
> architecture, data flow, and every file's role well enough to modify or extend it correctly.

---

## 1. What this project is

A **Python FastAPI backend + single-file HTML/JS frontend** that lets a non-Java QA engineer
trigger an existing, unmodified **enterprise Java Cucumber/Selenium/TestNG test-automation
framework** (banking domain — SBI/NBC), execute it on Microsoft Edge, view live logs and a
graphical pass/fail report, and — in its advanced mode — use **Azure OpenAI with
Retrieval-Augmented Generation (RAG)** to automatically write, compile, and (after human review)
commit new Java step-definitions and page-object code when an uploaded feature file contains
scenarios the framework doesn't yet support.

**The person operating this system has no Java knowledge.** Every design decision follows from
that constraint: the Java framework is treated as a black box that is launched via command-line
overrides, never edited by hand, and any AI-generated code must compile and pass a human review
gate before it touches the real framework.

**Critical invariant, honor in all future changes:** the framework's own source files are
**never modified during a normal execution**. The *only* time framework files are written is in
the AI-update commit step, and even then only after (a) the generated Java compiles, (b) a human
clicks "Approve" in the browser, and (c) the original versions have been archived to a permanent,
timestamped, never-overwritten folder first.

---

## 2. The Java framework being orchestrated (facts, not assumptions)

- Root folder example: `D:\NBC_Pankaj` (configurable, arbitrary location, **outside** this project's folder)
- No Maven, no Gradle — no build tool. Compiled by IntelliJ IDEA into `target/test-classes`.
- Layout:
  ```
  src/test/features/<module>/Txn<NNNN>_<Name>.feature      Gherkin features, ~1600+ files
  src/test/java/com/nbc/pages/<module>/...Page.java         Selenium page objects (locators)
  src/test/java/com/nbc/stepDefinitions/<module>/...Steps.java  Cucumber step definitions
  src/test/java/com/nbc/stepDefinitions/common/CommonSteps.java  Shared steps (login, logout...)
  src/test/java/com/nbc/runners/TestRunner.java              @CucumberOptions entry point
  src/test/java/com/nbc/hooks/Hooks.java                     @Before/@After (driver lifecycle)
  src/test/resources/data/<env>/<module>.properties          Per-environment config, incl. credentials
  target/test-classes/                                       Compiled .class output (IntelliJ writes here)
  ```
- Modules: `batch, cash, cif, common, deposits, fileUpload, gl, healthCheck, loans, ndvp, parameters, sdv`
- `TestRunner.java` uses **TestNG** (`extends AbstractTestNGCucumberTests`, NOT JUnit — the
  `@RunWith(Cucumber.class)` import is commented out in the real file), package `com.nbc.runners`,
  and hardcodes `tags = "@400_pass"` plus a hardcoded single feature path inside `@CucumberOptions`.
  Both are overridden per-run via Cucumber system properties (see §5).
- Cucumber version is 5/6/7 style (`io.cucumber.testng.*` imports), so the property-override
  mechanism is `-Dcucumber.features=...` / `-Dcucumber.filter.tags=...` (NOT the older
  `-Dcucumber.options=...` used by Cucumber 4.x — `config.CUCUMBER_STYLE` switches between them
  if ever needed for a different framework).
- `PropertiesUtil.java` loads `.properties` files directly from
  `System.getProperty("user.dir") + /src/test/resources/data/<env>/<module>.properties` at
  runtime — **not** from the compiled classpath copy. This means the orchestrator's working
  directory (`cwd`) for the java subprocess must be `FRAMEWORK_ROOT`, and patching the properties
  file requires no recompilation to take effect.
- Real property keys inside `common.properties`: `browser`, `baseUrl`, `makerID` (capital ID),
  `makerPassword`, `checkerID`, `checkerPassword`, plus many per-branch override keys
  (`makerID_UT01`, etc.) that are irrelevant to this project.
- Edge WebDriver: bundled in `drivers/edgeDriver` inside the framework repo; no manual driver-path
  configuration needed by the orchestrator in the current setup.

---

## 3. The core trick that makes "no Java knowledge" possible

Cucumber system properties **override** whatever is hardcoded inside `@CucumberOptions` in
`TestRunner.java`, without editing or recompiling that file:

```
java -Dcucumber.features=<absolute path to ANY .feature file, anywhere>
     -Dcucumber.plugin=json:<absolute path to a report file>
     -Dcucumber.filter.tags=<a tag expression>
     -Dbrowser=edge
     -cp <classpath>
     org.testng.TestNG -testclass com.nbc.runners.TestRunner
```

This single command works whether the feature file is one of the framework's own ~1600 files
*or* a file sitting in this project's own `data/uploads/` folder that was never copied into the
framework. `glue = "stepDefinitions"` inside `@CucumberOptions` is untouched, so all existing step
definitions remain discoverable regardless of which feature is injected.

**Tag-filter gotcha (real bug hit and fixed):** passing an *empty* string to
`-Dcucumber.filter.tags=` is silently ignored by this Cucumber version, letting the hardcoded
`tags = "@400_pass"` filter re-apply and match zero scenarios (`Total tests run: 0`). The fix
(`executor._cucumber_props`) substitutes an explicit match-all expression instead of empty:
`-Dcucumber.filter.tags=not @__orchestrator_no_filter__`.

---

## 4. High-level architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Browser (frontend/index.html — served BY the FastAPI app, same origin) │
│  Single-page app: no build step, no framework, plain HTML/CSS/JS         │
└───────────────────────────────┬───────────────────────────────────────┘
                                 │ fetch() to same-origin REST endpoints
┌───────────────────────────────▼───────────────────────────────────────┐
│                     FastAPI backend  (app/main.py)                     │
│  ┌────────────┐  ┌───────────┐  ┌───────────┐  ┌──────────┐  ┌──────┐ │
│  │ indexer.py │  │executor.py│  │generator.py│  │knowledge.py│ │config│ │
│  │ Txn→files  │  │ subprocess│  │ Azure LLM  │  │  RAG /    │ │ .py  │ │
│  │  lookup    │  │ lifecycle │  │ codegen +  │  │ knowledge  │ │(user │ │
│  │            │  │ + AI phases│  │  compile   │  │  base      │ │values)│
│  └────────────┘  └───────────┘  └───────────┘  └──────────┘  └──────┘ │
└───────────────────────────────┬───────────────────────────────────────┘
                                 │ subprocess.Popen (java / javac)
┌───────────────────────────────▼───────────────────────────────────────┐
│         The Java framework (untouched folder, arbitrary location)      │
│         TestNG + Cucumber + Selenium → Microsoft Edge                  │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 5. File-by-file reference

### `app/config.py` — the ONLY file with machine-specific / secret values
Everything else in the project is portable; this file is not. Sections, in order:
1. `FRAMEWORK_ROOT` and the three source folder paths (features/pages/steps)
2. `TEST_RUNNER_CLASS` — fully-qualified runner class name
3. `RUN_MODE` (`"java"` | `"gradle"` | `"maven"` — this deployment uses `"java"`, no build tool)
4. `JAVA` / `MVN` / `GRADLE` — launcher executable paths
5. `CLASSPATH_ENTRIES` / `CLASSPATH_OVERRIDE` / `RECURSIVE_JAR_DIRS` — how the classpath is
   assembled (see §7, "the classpath problem")
6. `RUNNER_KIND` (`"junit"` | `"testng"`), `CUCUMBER_STYLE` (`"properties"` | `"options"`),
   `CUCUMBER_TAG_FILTER`
7. `EXTRA_SYSTEM_PROPS` — fixed `-D` flags added to every run (e.g. `webdriver.edge.driver` if
   ever needed — currently unused since the driver is bundled in the framework)
8. `ENVIRONMENT`, `PROPERTIES_FILE`, `RUNTIME_PROPERTY_KEYS` — properties-file patch mechanism
   (currently dormant/unused by the UI, which sends no credentials — see §6 Flow 1/2)
9. `MODULES` — known module folder names, used for upload module-detection
10. `AZURE_OPENAI_*` — endpoint, key, chat deployment name, API version,
    `AZURE_OPENAI_EMBED_DEPLOYMENT` (optional, enables RAG)
11. `JAVA_SRC_ROOT`, `TEST_CLASSES_DIR`, `JAVAC`, `AI_MAX_FIX_ROUNDS`, `GEN_DIR`, `ARCHIVE_DIR` —
    AI-commit mechanics

### `app/indexer.py` — the Txn → files index
Built once at startup (`build_index()`), cached to `data/index.json`. Walks the three source
folders, regex-matches `Txn<digits>` in filenames, and merges entries by **canonical number with
leading zeros stripped** (`_canon()`) so `Txn450`, `Txn0450`, `Txn000450` collapse into one entry
even if the feature and Java filenames use different zero-padding — a real inconsistency found in
the actual framework. Each entry: `{txn, module, feature, page, steps}`.
- `lookup(txn_no)` — case/padding-insensitive lookup
- `detect_module(feature_text)` — for uploads: find the txn mentioned inside the feature, or
  match a module name/tag in the text
- `closest_feature(feature_text, module)` — token-overlap similarity search within one module
  folder only (never scans the whole framework), used as a style template — currently a fallback
  path, superseded in practice by `knowledge.py`'s richer retrieval

### `app/executor.py` — process lifecycle, the biggest file
- `Job` dataclass: one execution's full state (status, log file, report file, subprocess handle,
  `ai_ctx`, `pending` review payload)
- `_build_command()` — assembles the `java ... org.testng.TestNG -testclass ...` command line
  from config + per-run tag/params
- `_effective_classpath()` — see §7
- `_patch_properties()` / restore — temporary properties-file edit with guaranteed restore in a
  `finally` block, even on crash; currently unused in practice since the UI sends no credentials
  (Flow 1/2 rely on the framework's own `common.properties` as-is)
- `_parse_report()` — parses Cucumber's JSON report into a summary with **per-step detail**
  (keyword, sentence, status, duration, full error message) — this is what powers both the
  on-screen charts and the downloadable detailed HTML report
- `_dry_run_sync()` — synchronous dry-run (no browser) used inside the AI phase to get the
  ground-truth list of undefined steps directly from Cucumber, not from guessing
- `_ai_prepare()` → `job.status = "review"` → `_finalize()` — the three-phase AI flow (see §6 Flow 3)
- `_archive()` — copies current file versions to `data/archive/<Txn>/<timestamp>/` before any
  overwrite; **permanent, never cleaned up automatically**
- `_commit_pending()` — writes merged feature + approved Java into the real framework, compiles
  into `target/test-classes`
- `start_job()` / `stop_job()` (tree-kill via `taskkill /T /F` on Windows so java + msedgedriver +
  Edge all die together) / `approve_job()` / `discard_job()` / `read_log()` (byte-offset tail for
  live polling)
- One global `_RUN_LOCK`: only one execution at a time (shared Edge session, shared properties file)

### `app/generator.py` — Azure OpenAI code generation
- `SYSTEM_PROMPT` — instructs the model: reuse existing methods/locators, match framework style,
  never invent locators (mark `// TODO QA: verify locator` instead), append-only mindset, output
  in a strict `===FILE: <path>===\n<code>\n===END===` block format (parsed by `_parse_files()`)
- `_azure_chat()` — the Chat Completions REST call; raises specific, actionable `RuntimeError`s
  for each failure mode (unconfigured placeholders, network unreachable, 401 bad key, 404 wrong
  deployment name, other non-200) — these messages are what the user sees in the console/report
- `generate_and_compile()` — builds the full context (uploaded feature, old feature, undefined
  steps, the target txn's existing Steps+Page files, **plus retrieved framework knowledge from
  `knowledge.py`**), calls the model, `javac`-compiles the result into an isolated quarantine
  folder (`data/gen/<job_id>/`), and on compile failure sends the errors back to the model for up
  to `AI_MAX_FIX_ROUNDS` repair attempts
- `merge_feature()` — **deterministic Python, not LLM**: splits both feature files into scenario
  blocks (`_blocks()`, tag-aware line-based parser — a naive regex split broke on tag lines, hence
  the explicit line-walk implementation), then: new scenario name → appended at the end; existing
  name with a changed body → replaced in place; existing unchanged scenarios untouched. Returns
  `(merged_text, added_names, replaced_names)`.

### `app/knowledge.py` — the RAG layer ("training the AI on the framework")
Two retrieval strategies, automatically chosen by availability:

1. **Structural catalogs (always available, no extra setup).** `build_knowledge()` regex-scans
   *every* step-definition and page-object file in the framework (not just the target txn) into
   `data/knowledge.json`: every `@Given/@When/@Then/@And/@But` sentence with its class+module, and
   every Page class's locator field names + public method signatures (names/signatures only,
   never method bodies). `step_catalog(module)` / `page_catalog(module)` format a capped,
   module-prioritized slice for the prompt.

2. **Semantic RAG (optional, requires an Azure embedding deployment).** `build_embeddings()`
   embeds every step sentence and page summary once via Azure's embeddings endpoint into
   `data/embeddings.npz` (plain NumPy, no vector DB — appropriate at this data scale). At
   generation time, `retrieve(query_texts)` embeds the uploaded feature + its undefined steps and
   returns the top-k **cosine-similarity** matches across the *entire* framework, regardless of
   module — this is what lets an uploaded "set a hold" scenario retrieve a semantically similar
   existing step even if it's phrased completely differently or lives in another module.
   Chosen over fine-tuning deliberately: fine-tuning would go stale after every framework commit,
   cost more, and raise more data-handling questions; RAG re-indexes in seconds and sends only
   interface-level text (sentences, class names, locator/method names) to Azure, never full
   method implementations except for the one target txn's files.

`generator.py` tries `knowledge.retrieve()` first and falls back to the plain catalogs if
embeddings aren't configured or the call fails — the AI phase can never be blocked by RAG being
absent.

### `app/main.py` — FastAPI routes (all of them)
```
POST /index/rebuild                    rescan Txn index
GET  /txns?q=&all=                     search / full list (dropdown data; java-only,
                                        featureless entries are excluded from the picker)
POST /knowledge/rebuild?embed=         rescan framework knowledge; optionally (re)build embeddings
GET  /knowledge/stats                  counts + whether RAG embeddings are active

POST /execute/txn/{txn_no}             Flow 1 — run by transaction number (tag-verified, see §6)
POST /execute/upload?dry_run=&ai=      Flow 2 (ai=false) / Flow 3 (ai=true) — upload a .feature

GET  /jobs/{job_id}                    status + summary (scenarios, steps, per-step detail)
GET  /jobs/{job_id}/logs?offset=       live log tail, byte-offset polling
POST /jobs/{job_id}/stop               hard-kill a running execution (tree-kill)

GET  /jobs/{job_id}/pending            the AI's proposed changes, for the review editor
POST /jobs/{job_id}/approve            commit (possibly user-edited) changes, then execute
POST /jobs/{job_id}/discard            abandon AI changes, framework untouched
```
Also mounts `frontend/` as static files at `/` (must stay registered LAST so it doesn't shadow
the API routes above it).

### `frontend/index.html` — the entire UI, one file, no build step
Vanilla HTML/CSS/JS, no framework, no bundler — served directly by FastAPI. Sections: New
Execution card (Upload-first tabs, Txn combo with a fixed "Txn" label + scrollable/searchable
dropdown of every indexed feature), Execution card (4-stage pipeline rail, live-polled console,
Stop button), Review card (hidden until an AI job pauses — editable `<textarea>` per proposed
file, Approve/Discard), Result Report card (8 KPI cards, 3 SVG-drawn charts — pass-rate donut,
steps-breakdown donut, pass/fail column chart — a downloadable **self-contained HTML report**
button that renders per-scenario, per-step tables with full failure reasons client-side, no
server round-trip).

### `requirements.txt`
`fastapi, uvicorn[standard], python-multipart, requests, numpy` — `numpy` is only exercised by
the optional RAG embeddings path; everything else is required.

---

## 6. The three user-facing workflows

### Flow 1 — Run by transaction number
1. User types a number next to a fixed "Txn" label (autocomplete from the full index)
2. `indexer.lookup()` resolves it to the feature file instantly (no filesystem scan at request time)
3. Backend opens that feature file and validates a matching **tag** exists inside it, accepting
   three styles: exact `@Txn012000`, prefix `@Txn012000_LoansCreateLoanDetails`, or bare
   `@12000` — because the real framework uses all three inconsistently. If no matching tag is
   found, the run is refused up front with the exact tags that ARE present, rather than silently
   running zero scenarios.
4. Executes with `-Dcucumber.filter.tags=<that exact tag>` — this achieves the effect of "inject
   this transaction's scenarios only" without ever editing `TestRunner.java`.

### Flow 2 — Upload feature, no AI (`ai=false`)
1. Upload validated as real Gherkin (`Feature:` header + `Scenario:` + `Given/When/Then` all
   required — a prose "solution document" saved with a `.feature` extension is rejected with an
   explanation, a real failure mode that was hit in practice)
2. Saved to `data/uploads/`, executed directly from there via `-Dcucumber.features=<that path>`
   with the match-all tag filter (§3 gotcha) — the framework source is never touched
3. Optional dry-run mode (`-Dcucumber.execution.dry-run=true`) checks glue-code coverage in
   seconds without opening a browser

### Flow 3 — Upload feature with AI update (`ai=true`) — the RAG/codegen pipeline
This is a **paused, human-approved** pipeline, not a fire-and-forget one:
1. Dry-run → Cucumber reports which steps are undefined (ground truth, not guessed)
2. `generator.merge_feature()` deterministically diffs the upload against the existing feature for
   this txn (new scenarios appended, changed scenarios replaced, everything else untouched)
3. If there's nothing new and nothing undefined → runs immediately, no AI call, no review needed
4. Otherwise → `generator.generate_and_compile()` builds context (target txn's files + RAG-retrieved
   similar steps/pages from the WHOLE framework) → Azure OpenAI generates Java → `javac`-compiled
   in an isolated quarantine folder (never in the framework) → up to 2 auto-repair rounds on
   compile failure
5. **Execution pauses.** `job.status = "review"`. The browser shows the merged feature text and
   every generated Java file in editable code panels, with a summary of what changed and a
   reminder to check for `// TODO QA: verify locator` markers (unavoidable when a scenario touches
   a UI element with no existing locator anywhere in the framework — no AI can know a real
   application's DOM without seeing it)
6. User edits anything (or nothing) and clicks **Approve** → edited Java is compile-checked again
   before acceptance (a broken edit is rejected with the compiler error, staying in review) →
   **archive current versions permanently** → write merged feature + approved Java into the real
   framework → compile into `target/test-classes` → execute on Edge
   — OR clicks **Discard** → framework is left byte-for-byte untouched

---

## 7. Two hard problems solved, worth knowing if extending this project

**The classpath problem.** The framework has no build tool, so there's no single command that
resolves dependencies. Three layered solutions in `config.py`/`executor._effective_classpath()`:
(a) explicit `CLASSPATH_ENTRIES` / a full `CLASSPATH_OVERRIDE` string copied from IntelliJ's own
run-console command line, (b) `RECURSIVE_JAR_DIRS` — folders (e.g. a Maven cache, a `lib/`
directory) scanned recursively for every `.jar`, (c) if the resulting classpath is long or
involves recursive scanning, everything is packed into an auto-generated **pathing jar**
(`data/classpath.jar`, a jar containing only a manifest with a `Class-Path:` header) — this both
sidesteps the Windows command-line length limit and lets the JVM resolve jars from arbitrarily
deep folder structures. Verified against a real `javac`/`java` toolchain during development.

**No Maven/Gradle, TestNG not JUnit, launcher discovery.** `RUN_MODE="java"` launches
`org.testng.TestNG -testclass <TEST_RUNNER_CLASS>` directly (vs. `org.junit.runner.JUnitCore` for
JUnit frameworks, or `mvn test -Dtest=...` / `gradlew test --tests ...` if a build tool exists —
all three code paths exist in `executor._build_command()`). `JAVA` and `JAVAC` point at explicit
`.exe` paths rather than relying on PATH, because the office environment's PATH did not reliably
expose `java` to a Python subprocess even when it worked in an interactive terminal.

---

## 8. Known limitations / deliberate non-goals (do not "fix" without discussion)

- **One execution at a time**, by design — the framework has one shared Edge session and one
  shared properties file per environment; concurrency was explicitly rejected, not overlooked.
- **Stop is a hard kill.** If a maker-side transaction was mid-submission in the banking UI when
  stopped, that state is left exactly as the real application leaves it — this is inherent to
  stopping any UI automation, not a bug in this tool.
- **AI-generated locators for genuinely new UI elements are placeholders**, not real. The
  system's honesty mechanism is the `// TODO QA: verify locator` marker plus the mandatory human
  review gate — it does not and should not attempt to scrape a live DOM to guess real locators
  (that would be a legitimate but separate future project: capture `driver.getPageSource()` on a
  failed run and feed it back for a second AI pass).
- **RAG embeddings are a snapshot**, rebuilt only on request (`POST /knowledge/rebuild?embed=1`),
  not live — must be re-run after significant framework Java changes.
- **The knowledge/structural catalog has a line cap** (~350 steps) prioritized by module; on a
  framework this large, a step reuse opportunity in a distant, unrelated module could in theory be
  missed by the non-embedding fallback path (semantic RAG does not have this limitation, since it
  ranks by relevance across the whole base rather than truncating by module proximity).
- **Credentials/URL are NOT sent from the frontend** — by explicit product decision, the framework
  reads `url`/`makerID`/`checkerID`/etc. entirely from its own `common.properties`. The backend
  mechanism to override them per-request via API still exists (`RunParams`, `_patch_properties`)
  but is currently dormant/unused by the UI.

---

## 9. Glossary (for an AI picking this project up cold)

- **Txn** — a banking transaction number; the framework's unit of test organization. One Txn ⇒
  one `.feature` + one Page class + one Steps class (in the common case).
- **The index** — `indexer.py`'s in-memory/`data/index.json` map of Txn number → file paths.
  Built once at startup so lookups never re-scan ~1600+ files.
- **The knowledge base** — `knowledge.py`'s extraction of step sentences + page locators/methods
  across the *entire* framework, used to give the AI breadth beyond one txn's files.
- **RAG** here specifically means: embed the framework's step/page catalog once, embed the
  incoming request, retrieve by cosine similarity, inject the retrieved text into the LLM prompt.
  Not fine-tuning; nothing about the LLM's weights changes.
- **Review gate** — the mandatory pause (`job.status == "review"`) between AI code generation and
  it touching the real framework, with an in-browser editable diff and explicit Approve/Discard.
- **Archive** (`data/archive/`) — permanent, timestamped copies of any file the AI-commit step is
  about to overwrite. Never auto-deleted. This is the project's audit trail / undo mechanism.
- **Quarantine** (`data/gen/`) — where AI-generated Java is compiled to *verify* it builds, before
  any decision is made about whether it ever reaches the real framework.
