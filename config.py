"""
Central configuration. Everything you need to change for YOUR machine is here.
No other file should need editing to get started.
"""
from pathlib import Path
import os

# ── 1. Where the Java Cucumber framework lives (folder containing pom.xml) ──
FRAMEWORK_ROOT = Path(os.getenv("FRAMEWORK_ROOT", r"D:\NBC_Pankaj"))

# ── 2. Source folders inside the framework (adjust if yours differ) ─────────
# Common layouts: src/test/resources/features  OR  src/test/java/features
FEATURES_DIR = FRAMEWORK_ROOT / "src" / "test" / "features"
PAGES_DIR = FRAMEWORK_ROOT / "src" / "test" / "java" / "com" / "nbc" / "pages"
STEPS_DIR = FRAMEWORK_ROOT / "src" / "test" / "java" / "com" / "nbc" / "stepDefinitions"

# ── 3. The fully-qualified TestRunner class Maven should run ────────────────
# Find it with:  search the framework for a class containing @RunWith(Cucumber.class)
# If it's  src/test/java/runners/TestRunner.java  ->  "runners.TestRunner"
# If it has no package line at the top              ->  "TestRunner"
TEST_RUNNER_CLASS = os.getenv("TEST_RUNNER_CLASS", "com.nbc.runners.TestRunner")

# ── 4. How the framework is launched ────────────────────────────────────────
# "java"   -> no build tool; run TestRunner directly with a classpath (most likely for you)
# "gradle" -> framework has build.gradle / gradlew.bat
# "maven"  -> framework has pom.xml
RUN_MODE = os.getenv("RUN_MODE", "java")

MVN = os.getenv("MVN_CMD", "mvn.cmd" if os.name == "nt" else "mvn")
GRADLE = os.getenv("GRADLE_CMD", str(FRAMEWORK_ROOT / ("gradlew.bat" if os.name == "nt" else "gradlew")))
JAVA = os.getenv("JAVA_CMD", "java")

# For RUN_MODE="java": folders whose .class files and jars make up the classpath.
# Wildcard 'lib/*' means "every jar in lib". Adjust to where your jars live
# (look in Eclipse: right-click project -> Properties -> Java Build Path -> Libraries).
CLASSPATH_ENTRIES = [
    str(FRAMEWORK_ROOT / "target" / "test-classes"),
    str(FRAMEWORK_ROOT / "target" / "classes"),
    str(FRAMEWORK_ROOT / "target" / "nbc-automation-1.0.0.jar"),
]

# Folders to scan RECURSIVELY for dependency .jar files (e.g. a Maven cache
# copied from another machine, or a lib folder with subfolders). Every jar
# found is added to the classpath via an auto-generated "pathing jar", which
# also sidesteps the Windows command-line length limit.
RECURSIVE_JAR_DIRS = [
    os.path.expanduser(r"~\.m2\repository"),   # remove/adjust if not present
    str(FRAMEWORK_ROOT / "lib"),
]
CP_SEP = ";" if os.name == "nt" else ":"

# EASIEST OPTION (IntelliJ): run TestRunner once in IntelliJ, expand the first
# line of the Run console, copy everything after "-classpath", and paste it
# here as one raw string. If set, it wins over CLASSPATH_ENTRIES above.
CLASSPATH_OVERRIDE = os.getenv("CLASSPATH_OVERRIDE", r"")

CLASSPATH = CLASSPATH_OVERRIDE or CP_SEP.join(CLASSPATH_ENTRIES)

# For RUN_MODE="java": is TestRunner JUnit or TestNG?
#   "junit"  -> TestRunner has @RunWith(Cucumber.class)   <- yours
#   "testng" -> TestRunner extends AbstractTestNGCucumberTests
RUNNER_KIND = os.getenv("RUNNER_KIND", "testng")

# ── 4b. Cucumber version style ───────────────────────────────────────────────
# Check the cucumber jar filename in your lib folder (e.g. cucumber-java-7.11.0.jar):
#   version 5/6/7 -> "properties"  (uses -Dcucumber.features=...)
#   version 4.x   -> "options"     (uses -Dcucumber.options=...)
CUCUMBER_STYLE = os.getenv("CUCUMBER_STYLE", "properties")

# TestRunner hardcodes tags = "@400_pass". An injected feature without that tag
# would run 0 scenarios, so by default we CLEAR the tag filter ("" = run all
# scenarios of the injected feature). Set to e.g. "@smoke" to filter, or None
# to leave the annotation's @400_pass in force.
CUCUMBER_TAG_FILTER = os.getenv("CUCUMBER_TAG_FILTER", "")

# ── 4d. Fixed extra -D system properties added to every run ─────────────────
# Selenium natively reads "webdriver.edge.driver" — set the msedgedriver path
# here if the framework doesn't handle drivers itself (WebDriverManager /
# Selenium Manager 4.6+ need nothing). Add any other fixed -D values too.
EXTRA_SYSTEM_PROPS = {
    # "webdriver.edge.driver": r"C:\drivers\msedgedriver.exe",
}

# ── 4c. Runtime inputs: target URL + maker/checker credentials ──────────────
# The framework reads these from a properties file via PropertiesUtil.java.
# Open PropertiesUtil.java to find (a) which file it loads and (b) the key names,
# then correct the two settings below.
# The framework keeps one properties folder PER ENVIRONMENT (preprod / uat).
# PropertiesUtil reads these SOURCE files directly at runtime (via user.dir),
# so patching them needs NO recompile. Credentials/URL live in common.properties.
ENVIRONMENT = os.getenv("NBC_ENV", "preprod")   # "preprod" or "uat"
PROPERTIES_FILE = FRAMEWORK_ROOT / "src" / "test" / "resources" / "data" / ENVIRONMENT / "common.properties"

# Map: API field name -> property key name used INSIDE the properties file.
# Only the right-hand side needs to match your framework.
RUNTIME_PROPERTY_KEYS = {
    "url": "baseUrl",
    "maker_id": "makerID",          # capital ID — matches common.properties
    "maker_password": "makerPassword",
    "checker_id": "checkerID",
    "checker_password": "checkerPassword",
}

# ── 5. How your framework selects the browser ───────────────────────────────
# Look inside PropertiesUtil.java / config.properties for the property name.
# It is usually something like  browser=chrome . We override it per run:
BROWSER_SYSPROP = os.getenv("BROWSER_SYSPROP", "browser")   # the KEY name
BROWSER_VALUE = os.getenv("BROWSER_VALUE", "edge")          # the VALUE

# ── 6. Where this backend stores its own data ───────────────────────────────
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
UPLOADS_DIR = DATA_DIR / "uploads"        # uploaded .feature files
LOGS_DIR = DATA_DIR / "logs"              # live console logs per job
REPORTS_DIR = DATA_DIR / "reports"        # cucumber JSON reports per job
INDEX_FILE = DATA_DIR / "index.json"      # the Txn -> files index

for d in (DATA_DIR, UPLOADS_DIR, LOGS_DIR, REPORTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── 6b. Phase 1 scanner: extra folders (optional — skipped gracefully if absent)
UTILS_DIR = FRAMEWORK_ROOT / "src" / "test" / "java" / "com" / "nbc" / "utils"
PROPERTIES_ROOT = FRAMEWORK_ROOT / "src" / "test" / "resources" / "data"  # <env>/<module>.properties

# ── 7. Known modules (used to detect which folder an uploaded feature targets)
MODULES = ["batch", "cash", "cif", "common", "deposits", "fileUpload", "gl",
           "healthCheck", "loans", "ndvp", "parameters", "sdv"]


# ── 8. Phase 2: AI framework updates (Azure OpenAI) ──────────────────────────
# Fill these from your Azure OpenAI resource (Portal -> Keys and Endpoint).
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "https://YOUR-RESOURCE.openai.azure.com")
AZURE_OPENAI_KEY = os.getenv("AZURE_OPENAI_KEY", "PASTE-YOUR-KEY")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")  # your deployment NAME
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-06-01")

# Locator Recorder: path to msedgedriver.exe (the framework bundles one) and
# the URL the recording browser opens on (empty = user types it in the UI).
EDGE_DRIVER_PATH = os.getenv("EDGE_DRIVER_PATH",
                             str(FRAMEWORK_ROOT / "drivers" / "edgeDriver" / "msedgedriver.exe"))
RECORDER_DEFAULT_URL = os.getenv("RECORDER_DEFAULT_URL", "")

# RAG embeddings (optional but recommended): deploy an embedding model on the
# SAME Azure resource (Deployments -> Create -> text-embedding-3-small) and put
# its DEPLOYMENT NAME here. Empty = fall back to module catalogs (no embeddings).
AZURE_OPENAI_EMBED_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBED_DEPLOYMENT", "")

# Where java sources live (for committing updated classes after a green run)
JAVA_SRC_ROOT = FRAMEWORK_ROOT / "src" / "test" / "java"
TEST_CLASSES_DIR = FRAMEWORK_ROOT / "target" / "test-classes"

# javac: derived from JAVA by default (JDK required; a bare JRE has no javac)
JAVAC = os.getenv("JAVAC_CMD", "")
if not JAVAC:
    JAVAC = JAVA[:-len("java.exe")] + "javac.exe" if JAVA.lower().endswith("java.exe") \
            else (JAVA[:-4] + "javac" if JAVA.endswith("java") else "javac")

AI_MAX_FIX_ROUNDS = 2          # compile-error repair attempts
GEN_DIR = DATA_DIR / "gen"         # per-job generated sources/classes (quarantine)
ARCHIVE_DIR = DATA_DIR / "archive" # permanent reference copies of replaced files
for _d in (GEN_DIR, ARCHIVE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
