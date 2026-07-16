"""
Runs the Java framework via Maven as a subprocess.

Key trick (why no Java knowledge is needed):
  Cucumber system properties OVERRIDE whatever is hardcoded in @CucumberOptions
  inside TestRunner.java. So we can point the SAME TestRunner at any single
  feature file — including one sitting in our own uploads folder — with:

      mvn test -Dtest=TestRunner
               -Dcucumber.features=<absolute path to .feature>
               -Dcucumber.plugin=json:<report path>
               -Dbrowser=edge

  Nothing inside the framework is modified, so nothing needs restoring.
"""
import json
import os
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config

JOBS: dict[str, "Job"] = {}

# The properties file is shared state -> only one execution at a time.
_RUN_LOCK = threading.Lock()

_SECRET_RE = re.compile(r"(-D\w*[Pp]assword=)(\S+)")


def _effective_classpath() -> str:
    """Assemble the classpath. If it involves recursively-scanned jar folders
    or would exceed the Windows command-line limit, pack everything into a
    'pathing jar' (a jar whose manifest Class-Path lists all entries) and
    return just that jar's path."""
    import glob
    from urllib.request import pathname2url

    entries: list[str] = []
    override = getattr(config, "CLASSPATH_OVERRIDE", "")
    if override:
        entries.extend(x for x in override.split(config.CP_SEP) if x.strip())
    else:
        entries.extend(config.CLASSPATH_ENTRIES)

    # expand wildcards like  lib/*  into real jar paths
    expanded: list[str] = []
    for e in entries:
        if e.endswith("*"):
            expanded.extend(glob.glob(e + ".jar") + glob.glob(e))
        else:
            expanded.append(e)

    # recursive jar folders (Maven cache, nested lib dirs)
    for d in getattr(config, "RECURSIVE_JAR_DIRS", []):
        base = Path(d)
        if base.is_dir():
            expanded.extend(str(j) for j in base.rglob("*.jar"))

    expanded = [e for e in dict.fromkeys(expanded) if Path(e).exists()]
    flat = config.CP_SEP.join(expanded)
    if len(flat) < 4000 and not any(Path(d).is_dir() for d in getattr(config, "RECURSIVE_JAR_DIRS", [])):
        return flat

    # build the pathing jar
    urls = []
    for e in expanded:
        pth = Path(e).resolve()
        u = "file:" + pathname2url(str(pth))
        if pth.is_dir() and not u.endswith("/"):
            u += "/"
        urls.append(u)
    raw = ("Manifest-Version: 1.0\r\nClass-Path: " + " ".join(urls) + "\r\n").encode("utf-8")
    # manifest lines max 72 bytes; continuation lines start with one space
    wrapped = bytearray()
    line = bytearray()
    for token in raw.split(b"\r\n"):
        line = bytearray(token)
        while len(line) > 70:
            wrapped += line[:70] + b"\r\n"
            line = bytearray(b" ") + line[70:]
        wrapped += line + b"\r\n"
    import zipfile
    jar = config.DATA_DIR / "classpath.jar"
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", bytes(wrapped))
    return str(jar)


def _mask(cmd: list[str]) -> str:
    return _SECRET_RE.sub(r"\1*****", " ".join(cmd))


def _patch_properties(run_params: dict | None) -> bytes | None:
    """Write url / maker / checker into the framework's properties file.
    Returns the ORIGINAL bytes so the caller can restore them afterwards."""
    if not run_params or not any(run_params.get(f) for f in config.RUNTIME_PROPERTY_KEYS):
        return None
    pf = config.PROPERTIES_FILE
    if not pf.exists():
        return None
    original = pf.read_bytes()
    lines = original.decode("utf-8", errors="ignore").splitlines()
    wanted = {key: run_params[fld]
              for fld, key in config.RUNTIME_PROPERTY_KEYS.items()
              if run_params.get(fld)}
    out, seen = [], set()
    for line in lines:
        stripped = line.strip()
        replaced = False
        if stripped and not stripped.startswith(("#", "!")) and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in wanted:
                out.append(f"{key}={wanted[key]}")
                seen.add(key)
                replaced = True
        if not replaced:
            out.append(line)
    for key, val in wanted.items():          # keys not present yet -> append
        if key not in seen:
            out.append(f"{key}={val}")
    pf.write_text("\n".join(out) + "\n", encoding="utf-8")
    return original


@dataclass
class Job:
    id: str
    feature_path: str
    status: str = "queued"          # queued | running | passed | failed | error | stopped
    return_code: int | None = None
    log_file: Path = None
    report_file: Path = None
    summary: dict = field(default_factory=dict)
    proc: object = None
    stop_requested: bool = False
    ai_ctx: dict | None = None
    pending: dict | None = None          # proposed changes awaiting user review
    run_params: dict | None = None
    tag_filter: str | None = None

    def public(self) -> dict:
        return {
            "job_id": self.id,
            "feature": self.feature_path,
            "status": self.status,
            "return_code": self.return_code,
            "summary": self.summary,
            "needs_review": self.status == "review",
        }


def _cucumber_props(feature_path: str, report_file: Path, dry_run: bool,
                    run_params: dict | None, tag_filter: str | None = None) -> list[str]:
    """System properties that OVERRIDE @CucumberOptions in TestRunner.java."""
    if config.CUCUMBER_STYLE == "options":
        # Cucumber 4.x: one combined option string
        opts = f"{feature_path} --plugin json:{report_file}"
        if dry_run:
            opts += " --dry-run"
        props = [f"-Dcucumber.options={opts}"]
    else:
        # Cucumber 5/6/7
        props = [
            f"-Dcucumber.features={feature_path}",
            f"-Dcucumber.plugin=json:{report_file}",
        ]
        if dry_run:
            props.append("-Dcucumber.execution.dry-run=true")
    props.append(f"-D{config.BROWSER_SYSPROP}={config.BROWSER_VALUE}")
    tag = tag_filter if tag_filter is not None else getattr(config, "CUCUMBER_TAG_FILTER", None)
    if tag is not None:
        if not tag.strip():
            # An EMPTY value is ignored by some cucumber versions, letting the
            # hardcoded tags="@400_pass" in TestRunner re-apply and filter
            # everything out (Total tests run: 0). "not @<nonexistent>" is an
            # explicit expression that matches every scenario.
            tag = "not @__orchestrator_no_filter__"
        props.append(f"-Dcucumber.filter.tags={tag}")
    for k, v in getattr(config, "EXTRA_SYSTEM_PROPS", {}).items():
        props.append(f"-D{k}={v}")
    # Also expose url / maker / checker as system properties, in case
    # PropertiesUtil checks System.getProperty() before the file.
    for field, key in config.RUNTIME_PROPERTY_KEYS.items():
        if run_params and run_params.get(field):
            props.append(f"-D{key}={run_params[field]}")
    return props


def _build_command(feature_path: str, report_file: Path, dry_run: bool = False,
                   run_params: dict | None = None, tag_filter: str | None = None) -> list[str]:
    props = _cucumber_props(feature_path, report_file, dry_run, run_params, tag_filter)

    if config.RUN_MODE == "java":
        # No build tool: launch the already-compiled TestRunner directly.
        # NOTE: -D properties must come BEFORE the main class.
        base = [config.JAVA, *props, "-cp", _effective_classpath()]
        if config.RUNNER_KIND == "testng":
            return [*base, "org.testng.TestNG", "-testclass", config.TEST_RUNNER_CLASS]
        return [*base, "org.junit.runner.JUnitCore", config.TEST_RUNNER_CLASS]

    if config.RUN_MODE == "gradle":
        return [
            config.GRADLE, "test",
            "--tests", config.TEST_RUNNER_CLASS,
            *props,
        ]

    # maven
    return [
        config.MVN, "test",
        f"-Dtest={config.TEST_RUNNER_CLASS}",
        *props,
        "-DfailIfNoTests=false",
    ]


def _parse_report(report_file: Path) -> dict:
    """Turn cucumber's JSON report into a small pass/fail summary + undefined steps."""
    if not report_file.exists():
        return {}
    try:
        data = json.loads(report_file.read_text(errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return {}
    total = passed = failed = skipped = 0
    undefined: list[str] = []
    failures: list[dict] = []
    scenarios: list[dict] = []
    steps_passed = steps_failed = 0
    for feat in data:
        for el in feat.get("elements", []):
            if el.get("type") == "background":
                continue
            total += 1
            scenario_failed = False
            duration_ns = 0
            step_rows = []
            for step in el.get("steps", []):
                res = step.get("result", {})
                st = res.get("status")
                duration_ns += res.get("duration", 0) or 0
                step_rows.append({
                    "keyword": (step.get("keyword") or "").strip(),
                    "name": step.get("name", ""),
                    "status": st or "unknown",
                    "duration_sec": round((res.get("duration", 0) or 0) / 1e9, 2),
                    "error": (res.get("error_message") or "")[:2000],
                })
                if st == "passed":
                    steps_passed += 1
                if st == "undefined":
                    undefined.append(step.get("name", ""))
                if st in ("failed", "undefined"):
                    scenario_failed = True
                    steps_failed += 1
                    if st == "failed":
                        failures.append({
                            "scenario": el.get("name"),
                            "step": step.get("name"),
                            "error": (res.get("error_message") or "")[:500],
                        })
                if st == "skipped":
                    skipped += 1
            if scenario_failed:
                failed += 1
            else:
                passed += 1
            scenarios.append({
                "name": el.get("name") or "(unnamed scenario)",
                "status": "failed" if scenario_failed else "passed",
                "duration_sec": round(duration_ns / 1e9, 2),
                "steps": step_rows,
            })
    return {
        "scenarios_total": total,
        "scenarios_passed": passed,
        "scenarios_failed": failed,
        "steps_passed": steps_passed,
        "steps_failed": steps_failed,
        "steps_skipped": skipped,
        "scenarios": scenarios,
        "undefined_steps": sorted(set(undefined)),
        "failures": failures,
    }


def _dry_run_sync(feature_path: str, log) -> list[str]:
    """Run cucumber's dry-run (no browser) and return the undefined steps."""
    rep = config.REPORTS_DIR / ("dry_" + uuid.uuid4().hex[:8] + ".json")
    cmd = _build_command(feature_path, rep, dry_run=True)
    log(">>> Phase 1/3 — dry run (no browser): checking which steps have java glue...")
    r = subprocess.run(cmd, cwd=str(config.FRAMEWORK_ROOT),
                       capture_output=True, text=True, timeout=600)
    summary = _parse_report(rep)
    und = summary.get("undefined_steps", [])
    log(f">>> Dry run done: {len(und)} undefined step(s)" + (":" if und else "."))
    for u in und:
        log("      - " + u)
    return und


def _archive(paths: dict, txn_label: str, log) -> str:
    """Copy the CURRENT versions of the given files into a permanent,
    timestamped reference folder before they are modified."""
    import shutil
    from datetime import datetime
    dest = config.ARCHIVE_DIR / txn_label / datetime.now().strftime("%Y%m%d_%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    for kind, src in paths.items():
        if src and Path(src).exists():
            shutil.copy2(src, dest / Path(src).name)
            log(f">>> Archived old {kind}: {Path(src).name}  ->  {dest}")
    return str(dest)


def _ai_prepare(job: Job, log) -> str | None:
    """Compare + normalize + merge + generate + compile-verify, then STOP for
    user review. Returns a feature path to run immediately when nothing
    changed, or None when job.pending was filled and approval is required.

    Integrates Phases 6-9 (feature_matcher, feature_normalizer, merge_engine,
    feature_comparator) into the live pipeline:
      - feature_matcher is used ONLY as a FALLBACK — it never overrides an
        existing hit found by the simple filename/content txn lookup (main.py),
        it only fires when that lookup found NOTHING. This means the path that
        already worked cannot regress; the new capability (finding a likely
        existing feature even without a clean txn number) is strictly additive.
        A higher confidence threshold (0.25) is used here than the module's
        general-purpose default, because a wrong match here means committing
        to the wrong framework file — mitigated further by the mandatory
        human review gate downstream, which shows the match score.
      - feature_normalizer runs BEFORE the dry-run (not after), so wording/tag
        fixes are reflected in an ACCURATE undefined-steps count. Running the
        dry-run on the raw upload (the old order) would wrongly flag a step
        that's really just typo'd/differently-phrased as "undefined" and send
        it to the LLM to regenerate as an unwanted duplicate.
      - merge_engine replaces the old text-block merge, adding visibility into
        scenarios present in the existing feature but untouched by this
        upload (never deleted, only reported).
      - feature_comparator supplies a richer summary for job.pending["meta"]
        (additive keys only — the three original keys/semantics the frontend
        already reads are unchanged, so the existing review screen keeps
        working without modification).
    """
    from . import generator, feature_matcher, feature_normalizer, merge_engine, feature_comparator, indexer

    ctx = job.ai_ctx
    hit = ctx.get("hit") or {}
    uploaded = ctx["uploaded_text"]
    filename = ctx.get("filename")
    module_hint = ctx.get("module")

    match = None
    if not hit:
        # The simple filename/content txn lookup (main.py) found nothing —
        # try the smarter multi-signal matcher as a fallback ONLY (never as
        # an override of an already-found hit).
        try:
            match = feature_matcher.best_match(uploaded, filename, module_hint, min_score=0.25)
        except Exception as e:  # noqa: BLE001 — matching is best-effort, never fatal
            log(f">>> Feature matching unavailable ({e}).")
        if match:
            matched_hit = indexer.lookup(match["txn"]) if match.get("txn") else None
            if matched_hit:
                hit = matched_hit
                log(f">>> No exact transaction number found in the upload, but matching found a "
                   f"likely existing feature: {hit['txn']} (module={hit['module']}, "
                   f"confidence={match['score']}). Using it as the comparison baseline.")

    old_text = Path(hit["feature"]).read_text(encoding="utf-8", errors="ignore") if hit.get("feature") else ""

    try:
        norm = feature_normalizer.normalize_feature(uploaded, old_text)
        normalized_text, norm_changes = norm["normalized_text"], norm["changes"]
        s = norm["summary"]
        log(f">>> Normalization: {s['tags_normalized']} tag(s), {s['steps_normalized']} step(s), "
           f"{s['scenarios_renamed']} scenario name(s) aligned to framework conventions.")
    except Exception as e:  # noqa: BLE001 — normalization is best-effort, never fatal
        log(f">>> Normalization unavailable ({e}); using the upload as-is.")
        normalized_text, norm_changes = uploaded, {}

    # dry-run the NORMALIZED text via a sibling temp file — the accurate
    # undefined-steps count reflects the wording/tag fixes above.
    norm_path = str(Path(job.feature_path).with_name(Path(job.feature_path).stem + "__normalized.feature"))
    Path(norm_path).write_text(normalized_text, encoding="utf-8")
    try:
        undefined = _dry_run_sync(norm_path, log)
    finally:
        Path(norm_path).unlink(missing_ok=True)

    merge_result = merge_engine.merge(old_text, normalized_text)
    added, replaced, unreferenced = (merge_result["appended_scenarios"],
                                     merge_result["replaced_scenarios"],
                                     merge_result["unreferenced_scenarios"])
    log(f">>> Merge: {len(added)} new scenario(s) {added or ''}, "
       f"{len(replaced)} changed scenario(s) {replaced or ''}, "
       f"{len(unreferenced)} existing scenario(s) untouched by this upload {unreferenced or ''} "
       f"(kept, not deleted).")
    merged = merge_result["merged_text"]

    if not undefined and not added and not replaced and old_text:
        # Nothing to do — run the REAL existing framework file directly
        # (not the raw/possibly-typo'd upload: the dry-run above only proved
        # the NORMALIZED wording has glue, not necessarily the raw upload's).
        log(">>> No differences and all steps defined — executing the existing framework feature as-is.")
        return hit["feature"]

    comparison_summary = None
    try:
        comparison_summary = feature_comparator.compare_features(old_text, normalized_text)["summary"]
    except Exception:  # noqa: BLE001 — comparison summary is a nice-to-have only
        pass

    gen_files = {}
    if undefined:
        log(">>> Generating framework updates with Azure OpenAI...")
        gen_files, _ = generator.generate_and_compile(
            {**ctx, "hit": hit, "uploaded_text": normalized_text}, undefined,
            _effective_classpath(), config.GEN_DIR / job.id, log)

    job.pending = {
        "feature_text": merged,
        "files": gen_files,
        "meta": {
            # unchanged keys/semantics — the existing review screen keeps working as-is
            "undefined_steps": undefined, "scenarios_added": added, "scenarios_replaced": replaced,
            # new, additive keys for a future review-screen upgrade
            "scenarios_unreferenced": unreferenced, "normalization": norm_changes,
            "matched_via_fallback_score": match["score"] if match else None,
            "comparison_summary": comparison_summary,
        },
        "txn_label": hit.get("txn") or Path(ctx.get("filename", "feature")).stem,
        "hit": hit, "module": ctx.get("module"), "filename": ctx.get("filename"),
    }
    log(">>> Changes are ready for REVIEW — edit or approve them in the browser to continue.")
    return None


def _commit_pending(job: Job, log):
    """Archive current versions, then write the (possibly user-edited)
    feature + java into the framework and compile the java."""
    from . import indexer
    pen = job.pending
    hit = pen["hit"]

    to_archive = {"feature": hit.get("feature")}
    for rel in pen["files"]:
        tgt = config.JAVA_SRC_ROOT / rel
        if tgt.exists():
            to_archive[Path(rel).name] = str(tgt)
    if hit.get("page"):
        to_archive.setdefault("page", hit["page"])
    if hit.get("steps"):
        to_archive.setdefault("steps", hit["steps"])
    archive_dir = _archive(to_archive, pen["txn_label"], log)

    if hit.get("feature"):
        feature_target = Path(hit["feature"])
    else:
        module = pen.get("module") or "common"
        feature_target = config.FEATURES_DIR / module / (pen.get("filename") or (pen["txn_label"] + ".feature"))
        feature_target.parent.mkdir(parents=True, exist_ok=True)
    feature_target.write_text(pen["feature_text"], encoding="utf-8")
    log(f">>> Updated feature file: {feature_target}")

    for rel, code in pen["files"].items():
        tgt = config.JAVA_SRC_ROOT / rel
        tgt.parent.mkdir(parents=True, exist_ok=True)
        tgt.write_text(code, encoding="utf-8")
        log(f">>> Updated java source: {tgt}")
    if pen["files"]:
        srcs = [str(config.JAVA_SRC_ROOT / rel) for rel in pen["files"]]
        config.TEST_CLASSES_DIR.mkdir(parents=True, exist_ok=True)
        rc = subprocess.run([config.JAVAC, "-encoding", "UTF-8",
                             "-cp", _effective_classpath(),
                             "-d", str(config.TEST_CLASSES_DIR), *srcs],
                            capture_output=True, text=True, timeout=300)
        if rc.returncode != 0:
            raise RuntimeError("commit compile failed:\n" + (rc.stdout + rc.stderr)[:1500])
        log(">>> Compiled updated classes into target/test-classes")

    job.summary["ai"] = {**pen["meta"],
                         "java_files_updated": list(pen["files"].keys()),
                         "archive": archive_dir}
    indexer.build_index()
    return str(feature_target)


def _execute(job: Job, log_mode: str = "w", dry_run: bool = False):
    """Launch the JVM for job.feature_path, stream to the log, parse the report."""
    original_props = None
    try:
        with _RUN_LOCK:
            original_props = _patch_properties(job.run_params)
            try:
                with open(job.log_file, log_mode, encoding="utf-8", errors="ignore") as log:
                    cmd = _build_command(job.feature_path, job.report_file,
                                         dry_run, job.run_params, job.tag_filter)
                    log.write("\n>>> " + _mask(cmd) + "\n\n")   # never log passwords
                    log.flush()
                    proc = subprocess.Popen(cmd, cwd=str(config.FRAMEWORK_ROOT),
                                            stdout=log, stderr=subprocess.STDOUT, shell=False)
                    job.proc = proc
                    job.return_code = proc.wait()
            finally:
                if original_props is not None:
                    config.PROPERTIES_FILE.write_bytes(original_props)
        _ai_info = job.summary.get("ai")
        job.summary = _parse_report(job.report_file)
        if _ai_info:
            job.summary["ai"] = _ai_info
        if job.stop_requested:
            job.status = "stopped"
            return
        if job.summary.get("scenarios_total", 0) == 0:
            job.status = "failed"
            job.summary["error"] = ("Cucumber found 0 scenarios in this feature. The file is "
                "probably not valid Gherkin (check for a missing 'Feature:' header, prose text, "
                "or Word formatting), or every scenario was filtered out. Scroll up in the "
                "console for gherkin parse errors.")
            return
        if job.summary.get("undefined_steps"):
            job.status = "failed"
        elif job.return_code == 0 and job.summary.get("scenarios_failed", 0) == 0:
            job.status = "passed"
        else:
            job.status = "failed"
    except FileNotFoundError as e:
        job.status = "error"
        job.summary = {"error": f"Could not start the launcher ({e}). Check JAVA_CMD/MVN_CMD and PATH."}
    except Exception as e:  # noqa: BLE001
        job.status = "error"
        job.summary = {"error": str(e)}
        _log_error(job, str(e))


def _log_error(job: Job, msg: str):
    try:
        with open(job.log_file, "a", encoding="utf-8", errors="ignore") as f:
            f.write("\n>>> ERROR: " + msg + "\n")
    except OSError:
        pass


def _run(job: Job, dry_run: bool, run_params: dict | None, tag_filter: str | None = None):
    job.status = "running"
    job.run_params, job.tag_filter = run_params, tag_filter
    if not job.ai_ctx:
        _execute(job, "w", dry_run)
        return
    # AI flow: prepare (dry-run + merge + generate) -> pause for review
    try:
        with _RUN_LOCK:
            with open(job.log_file, "w", encoding="utf-8", errors="ignore") as log:
                def L(m):
                    log.write(m + "\n"); log.flush()
                run_now = _ai_prepare(job, L)
        if run_now is not None:            # nothing changed -> straight to execution
            job.feature_path = run_now
            _execute(job, "a")
        else:
            job.status = "review"          # frontend shows the editor; approval resumes
    except Exception as e:  # noqa: BLE001
        job.status = "error"
        job.summary = {"error": str(e)}
        _log_error(job, str(e))


def _finalize(job: Job):
    """Runs after user approval: commit the (edited) changes, then execute."""
    job.status = "running"
    try:
        with _RUN_LOCK:
            with open(job.log_file, "a", encoding="utf-8", errors="ignore") as log:
                def L(m):
                    log.write(m + "\n"); log.flush()
                L("\n>>> Changes APPROVED by user — committing to the framework...")
                job.feature_path = _commit_pending(job, L)
        _execute(job, "a")
    except Exception as e:  # noqa: BLE001
        job.status = "error"
        job.summary = {"error": str(e)}
        _log_error(job, str(e))


def approve_job(job_id: str, feature_text: str | None, files: dict | None) -> Job:
    """Accept (possibly edited) changes. Edited java is compile-checked in
    quarantine first; compile errors raise ValueError and the job STAYS in review."""
    from . import generator
    job = JOBS.get(job_id)
    if not job or job.status != "review" or not job.pending:
        raise KeyError("Job is not awaiting review.")
    if feature_text is not None:
        job.pending["feature_text"] = feature_text
    if files:
        merged_files = dict(job.pending["files"])
        merged_files.update({k: v for k, v in files.items() if v and v.strip()})
        ok, err, _ = generator._compile(merged_files, config.GEN_DIR / job.id / "approved",
                                        _effective_classpath())
        if not ok:
            raise ValueError(err[:2000])
        job.pending["files"] = merged_files
    threading.Thread(target=_finalize, args=(job,), daemon=True).start()
    return job


def discard_job(job_id: str) -> bool:
    job = JOBS.get(job_id)
    if not job or job.status != "review":
        return False
    job.status = "discarded"
    with open(job.log_file, "a", encoding="utf-8", errors="ignore") as log:
        log.write("\n>>> Changes DISCARDED by user. The framework was not modified.\n")
    job.pending = None
    return True


def start_job(feature_path: str, dry_run: bool = False,
              run_params: dict | None = None, tag_filter: str | None = None,
              ai_ctx: dict | None = None) -> Job:
    job_id = uuid.uuid4().hex[:12]
    job = Job(
        id=job_id,
        feature_path=str(feature_path),
        log_file=config.LOGS_DIR / f"{job_id}.log",
        report_file=config.REPORTS_DIR / f"{job_id}.json",
        ai_ctx=ai_ctx,
    )
    job.run_params, job.tag_filter = run_params, tag_filter
    JOBS[job_id] = job
    threading.Thread(target=_run, args=(job, dry_run, run_params, tag_filter), daemon=True).start()
    return job


def stop_job(job_id: str) -> bool:
    """Kill a running execution (java + its Edge/driver children)."""
    job = JOBS.get(job_id)
    if not job or job.status != "running" or job.proc is None:
        return False
    job.stop_requested = True
    try:
        if os.name == "nt":
            # /T kills the whole tree: java -> msedgedriver -> Edge
            subprocess.run(["taskkill", "/PID", str(job.proc.pid), "/T", "/F"],
                           capture_output=True)
        else:
            job.proc.kill()
    except Exception:  # noqa: BLE001
        pass
    return True


def read_log(job_id: str, offset: int = 0) -> tuple[str, int]:
    """Return log text from byte offset, plus the new offset (for live polling)."""
    job = JOBS.get(job_id)
    if not job or not job.log_file.exists():
        return "", offset
    with open(job.log_file, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(offset)
        chunk = f.read()
        return chunk, f.tell()
