from pathlib import Path
from dotenv import load_dotenv
import os
from typing import Dict, Any

# ============================================================================
# DIRECTORIES & PATHS
# ============================================================================
ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data"
OUTPUT_DIR = DATA_DIR / "outputs"
PROMPTS_DIR = ROOT_DIR / "prompts"
LOGS_DIR = ROOT_DIR / "logs"



# ============================================================================
# ENVIRONMENT SETUP
# ============================================================================
# Tests opt in before imports; never load developer credentials during collection.
_TEST_MODE = os.getenv("WM_DB_TEST_MODE") == "1" or bool(os.getenv("PYTEST_CURRENT_TEST"))
from src.core.environment_guard import validate_environment, validate_path
validate_path(ROOT_DIR)
if not _TEST_MODE:
    _env_file = validate_path(os.getenv("WM_ENV_FILE", str(ROOT_DIR / ".env")))
    # A real deployment (APP_ENV=production, Render or equivalent) injects its own environment
    # variables (Dashboard/secrets) — those must never be silently clobbered by a `.env`-shaped file
    # that happens to exist in the deployed filesystem. `override=False` here means "process
    # environment wins"; local/dev usage (APP_ENV unset/development) keeps the existing behaviour
    # (`.env` always wins) unchanged.
    _deployment_mode = os.getenv("APP_ENV", "development").strip().lower() == "production"
    load_dotenv(_env_file, override=not _deployment_mode)
    validate_environment(os.environ, ROOT_DIR)
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT_DIR / "data"))).resolve()
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(DATA_DIR / "outputs"))).resolve()
LOGS_DIR = Path(os.getenv("LOGS_DIR", str(ROOT_DIR / "logs"))).resolve()
for _directory in (DATA_DIR, OUTPUT_DIR, LOGS_DIR):
    validate_path(_directory)
    _directory.mkdir(parents=True, exist_ok=True)

# ============================================================================
# API KEYS & CREDENTIALS
# ============================================================================
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-small-latest")
PAPPERS_API_TOKEN = os.getenv("PAPPERS_API_TOKEN", "")

# ============================================================================
# LLM CONFIGURATION
# ============================================================================
LLM_ENABLED = os.getenv("LLM_ENABLED", "true").lower() == "true"

# Timeouts
LLM_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))

# Retry policy
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
LLM_RETRY_BACKOFF = float(os.getenv("LLM_RETRY_BACKOFF", "2.0"))
LLM_RETRY_INITIAL_DELAY = float(os.getenv("LLM_RETRY_INITIAL_DELAY", "1.0"))

# Temperature & creativity
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))
LLM_TEMPERATURE_FACTUAL = float(os.getenv("LLM_TEMPERATURE_FACTUAL", "0.1"))
LLM_TEMPERATURE_GENERATION = float(os.getenv("LLM_TEMPERATURE_GENERATION", "0.3"))
LLM_TOP_P = float(os.getenv("LLM_TOP_P", "1.0"))
LLM_PROVIDER_PRIORITY = [item.strip().lower() for item in os.getenv(
    "LLM_PROVIDER_PRIORITY", "anthropic,openai,mistral"
).split(",") if item.strip()]

# ============================================================================
# RAG CONFIGURATION
# ============================================================================
RAG_ENABLED = os.getenv("RAG_ENABLED", "true").lower() == "true"
RAG_MIN_SCORE_THRESHOLD = float(os.getenv("RAG_MIN_SCORE_THRESHOLD", "0.15"))
RAG_TOP_K_RESULTS = int(os.getenv("RAG_TOP_K_RESULTS", "5"))
RAG_CACHE_TTL_SECONDS = int(os.getenv("RAG_CACHE_TTL_SECONDS", "3600"))

# Embedding model (if using semantic search)
RAG_EMBEDDING_MODEL = os.getenv("RAG_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
RAG_USE_EMBEDDINGS = os.getenv("RAG_USE_EMBEDDINGS", "false").lower() == "true"

# B18-T6 (complement to B18-T5, related to B15): character-budget limits
# for the reference-SELECTION prompt's candidate/document-text assembly
# step only (src/rag/semantic_rerank.py::semantic_rerank via
# src/rag/context_budget.py) — a text-length cap, never a token count or a
# real cost figure. Ticket defaults below; validated in validate_config()
# before use, never trusted blindly (see src/rag/context_budget.py for the
# full allocation logic these three feed).
RAG_SELECTION_MAX_CANDIDATES = int(os.getenv("RAG_SELECTION_MAX_CANDIDATES", "6"))
RAG_SELECTION_MAX_EXCERPT_CHARS = int(os.getenv("RAG_SELECTION_MAX_EXCERPT_CHARS", "800"))
RAG_SELECTION_MAX_DOCUMENT_BLOCK_CHARS = int(os.getenv("RAG_SELECTION_MAX_DOCUMENT_BLOCK_CHARS", "4800"))

# ============================================================================
# SAAS V3 — DATABASE, SESSION, STORAGE, EMAIL
# ============================================================================
# The only coupling to a Postgres provider is this URL — works unchanged with
# a local Postgres, Docker, Supabase, Neon or any Postgres-compatible host.
DATABASE_URL = os.getenv("DATABASE_URL", "")

SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "wm_session")
SESSION_MAX_AGE_SECONDS = int(os.getenv("SESSION_MAX_AGE_SECONDS", str(30 * 24 * 3600)))  # 30 days

APP_ENV = os.getenv("APP_ENV", "development")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8056")

STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")  # local | object
LOCAL_STORAGE_PATH = Path(os.getenv("LOCAL_STORAGE_PATH", str(DATA_DIR)))

# B02/B03 boundary — HISTORICAL, now vestigial for the SaaS routes.
# `MULTI_CLIENT_MODE`/`Organization.corpus_access` used to gate the ONE
# global RAG corpus + capacity file before B03 existed. B03 makes every
# SaaS route (`/api/analyze`, `/api/capacity`, `/api/knowledge*`,
# `/app/base-connaissances`) read exclusively from a private, per
# (organization_id, owner_user_id) corpus/capacity — there is no longer a
# shared resource for these flags to gate, in any combination of their
# values (see docs/architecture/B03_PRIVATE_KNOWLEDGE.md and
# tests/test_b03_private_knowledge.py::test_legacy_flags_cannot_reach_shared_corpus_in_any_combination).
# They are kept, unused by any SaaS route, only as a placeholder for a
# possible *explicit* future shared-library feature — never re-wire them to
# bypass privacy without a new, explicitly reviewed feature.
MULTI_CLIENT_MODE = os.getenv("MULTI_CLIENT_MODE", "false").lower() == "true"

# ============================================================================
# B03 — PRIVATE KNOWLEDGE (documentation, RAG index, capacity)
# ============================================================================
# Provisional development limits — NOT a measured production capacity, see
# docs/architecture/B03_PRIVATE_KNOWLEDGE.md for the reasoning.
KNOWLEDGE_MAX_FILE_SIZE_MB = int(os.getenv("KNOWLEDGE_MAX_FILE_SIZE_MB", "10"))
KNOWLEDGE_MAX_ACTIVE_DOCUMENTS_PER_CORPUS = int(os.getenv("KNOWLEDGE_MAX_ACTIVE_DOCUMENTS_PER_CORPUS", "100"))
# Lot 59: how many knowledge ingestions (extraction + local embeddings + DB writes) may run at the same time
# in this process. Each one is CPU-bound; 1 matches the single-CPU demo instance and keeps uploads that were
# sequential before (they blocked the event loop) sequential now. Extra uploads wait asynchronously.
KNOWLEDGE_INGEST_MAX_CONCURRENCY = int(os.getenv("KNOWLEDGE_INGEST_MAX_CONCURRENCY", "1"))
KNOWLEDGE_MAX_PDF_PAGES = int(os.getenv("KNOWLEDGE_MAX_PDF_PAGES", "200"))
KNOWLEDGE_MAX_EXTRACTED_CHARS = int(os.getenv("KNOWLEDGE_MAX_EXTRACTED_CHARS", "2_000_000"))
KNOWLEDGE_MAX_DOCX_PARAGRAPHS = int(os.getenv("KNOWLEDGE_MAX_DOCX_PARAGRAPHS", "20_000"))
# Bounded in-memory snapshot cache (per-process) — an entry per
# (organization_id, owner_user_id), evicted oldest-first past this size.
KNOWLEDGE_INDEX_CACHE_MAX_ENTRIES = int(os.getenv("KNOWLEDGE_INDEX_CACHE_MAX_ENTRIES", "64"))
KNOWLEDGE_EXTRACTOR_VERSION = "b03-v1"

# ============================================================================
# LOT 51 — HYBRID RAG (lexical + vector, PostgreSQL/pgvector only)
# ============================================================================
# Structural gate: vector storage/search is only ever attempted on a
# PostgreSQL target with the pgvector extension available (see
# src/rag/hybrid_search.py). This flag additionally requires an EXPLICIT
# opt-in — a real deployment switching its DATABASE_URL to PostgreSQL does
# NOT silently gain hybrid search; per the lot 51 ticket ("activation du
# mode hybride explicite ... pas silencieuse dans la configuration
# utilisateur réelle"), an operator must set this env var themselves.
RAG_HYBRID_MODE_ENABLED = os.getenv("RAG_HYBRID_MODE_ENABLED", "false").lower() == "true"

# fastembed/ONNX, no torch — chosen for this project's modest CPU-only
# Windows dev machine (see docs/qa/lot_51_.../RAPPORT.md §1 for the full
# justification): sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
# via qdrant's ONNX conversion, Apache-2.0, ~0.22 Go, no query/passage prefix
# asymmetry to get wrong, real French support among ~50 languages. Changing
# any of the four values below invalidates every existing vector (never
# mixed silently — see embedding_model_id/_revision/_dimension columns on
# KnowledgeDocumentVersion) and requires an explicit reindex.
EMBEDDING_MODEL_ID = os.getenv("EMBEDDING_MODEL_ID", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
# Lot 51 bis: bumped ("chunking-v2-tokens") — the character-based windowing
# scheme this label used to imply was found to silently truncate most of a
# window's content (verified: real effective limit is 128 tokens including
# 2 special tokens, not a safe match for the old 1200-character default —
# see src/rag/embeddings.py's module docstring for the reproduction).
# Bumping this string is what makes every passage indexed under the OLD,
# broken scheme "stale" (src/rag/hybrid_index.py::version_is_stale) and
# excluded from vector candidates until reindexed — the SAME mechanism
# already used for an actual model change, deliberately reused rather than
# adding a second staleness dimension.
EMBEDDING_MODEL_REVISION = os.getenv("EMBEDDING_MODEL_REVISION", "fastembed-0.8.1:qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q:chunking-v2-tokens")
EMBEDDING_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "384"))
# Never the OS temp dir (would force a redownload every time it's cleared) —
# a stable, gitignored cache directory outside the repo's tracked tree.
EMBEDDING_CACHE_DIR = os.getenv("EMBEDDING_CACHE_DIR", str(ROOT_DIR / ".model-cache"))

# Windowing applied ON TOP OF the existing paragraph/table-level KnowledgeChunk
# (itself unchanged, still the lexical/TF-IDF granularity) — a passage is a
# window of a section's content, never a re-chunking of the whole document,
# so `content == chunk.content[start_char:end_char]` always holds. Lot 51
# bis: the window SIZE is no longer a configurable character count — it is
# ALWAYS derived from the real, loaded tokenizer's own effective token
# budget (src/rag/embeddings.py::EmbeddingAdapter.max_content_tokens_per_
# window), so it can never silently exceed what the model actually sees.
# Only the overlap remains a tunable, expressed in tokens (not characters).
EMBEDDING_CHUNK_OVERLAP_TOKENS = int(os.getenv("EMBEDDING_CHUNK_OVERLAP_TOKENS", "32"))

# Reciprocal Rank Fusion constant (standard default from the original RRF
# paper — Cormack et al. 2009); not exposed as a per-account setting, this
# is a technical fusion parameter, not a scoring policy value.
RAG_RRF_K = int(os.getenv("RAG_RRF_K", "60"))
RAG_HYBRID_TOP_K_LEXICAL = int(os.getenv("RAG_HYBRID_TOP_K_LEXICAL", "20"))
RAG_HYBRID_TOP_K_VECTOR = int(os.getenv("RAG_HYBRID_TOP_K_VECTOR", "20"))

# ============================================================================
# B13-T1 — AO ANALYSIS INPUT VALIDATION (/api/analyze, /api/scoring-config/simulate)
# ============================================================================
# Bounds applied by src/web/analyze_input_service.py BEFORE any costly
# processing (LLM extraction, RAG, scoring) — see
# docs/api/B13_T1_INPUT_VALIDATION_CONTRACT.md.
#
# Same default as KNOWLEDGE_MAX_FILE_SIZE_MB: an AO upload and a knowledge
# upload are the same class of input, so they get the same bound until a
# product decision says otherwise.
ANALYZE_MAX_UPLOAD_MB = int(os.getenv("ANALYZE_MAX_UPLOAD_MB", "10"))
# Pasted text: generous for a real tender (a 500k-char AO is already an
# outlier) but bounded — the full untruncated string is stored as
# ao.texte_source and regex-scanned repeatedly by scoring_engine.py.
ANALYZE_MAX_PASTE_CHARS = int(os.getenv("ANALYZE_MAX_PASTE_CHARS", "500_000"))
# Defence in depth on the JOINED extracted text. extraction.extract_chunks
# already enforces KNOWLEDGE_MAX_EXTRACTED_CHARS on the SUM of chunk
# lengths; joining chunks adds a "\n\n" separator per boundary, so the
# string the analyze path actually forwards can exceed what extraction
# counted. Kept at the same default so the two cannot drift apart silently
# — the effective bound is min(KNOWLEDGE_MAX_EXTRACTED_CHARS, this).
ANALYZE_MAX_EXTRACTED_CHARS = int(os.getenv("ANALYZE_MAX_EXTRACTED_CHARS", "2_000_000"))
# Zip-bomb guard for DOCX (a DOCX is a ZIP): python-docx decompresses the
# whole archive into memory before any paragraph/char cap can fire, so the
# UNCOMPRESSED size is bounded from the archive directory first. 50 MiB is
# far above any real DOCX that could also pass KNOWLEDGE_MAX_DOCX_PARAGRAPHS
# (20k paragraphs of prose is single-digit MiB of text plus XML markup),
# while capping a malicious one at a bounded allocation.
ANALYZE_DOCX_MAX_UNCOMPRESSED_MB = int(os.getenv("ANALYZE_DOCX_MAX_UNCOMPRESSED_MB", "50"))
# B13-T2: the B13-T1 guard above trusted zf.infolist()'s declared
# info.file_size — attacker-controlled central-directory metadata, never
# cross-checked by zipfile against real decompressed output. Enforcement now
# happens against actual bytes read from zf.open(info) in bounded chunks
# (src/web/knowledge/extraction.py::_extract_docx); this constant only caps
# the NUMBER of ZIP entries iterated before that. A real .docx has on the
# order of 10-40 internal XML parts (document.xml, styles, numbering,
# core/app props, media, _rels...). 2000 is generously above any legitimate
# document while bounding the cost of opening/iterating an archive crafted
# with a huge number of tiny/empty entries.
ANALYZE_DOCX_MAX_ZIP_ENTRIES = int(os.getenv("ANALYZE_DOCX_MAX_ZIP_ENTRIES", "2000"))

# ============================================================================
# B13-T2 — RAW REQUEST-BODY RECEPTION BOUND (ASGI middleware)
# ============================================================================
# Enforced by src/web/body_limit_middleware.py by wrapping the ASGI
# `receive` callable and counting bytes as they actually arrive off the
# wire — BEFORE Starlette's MultiPartParser (invoked with hardcoded
# defaults by fastapi/routing.py's `await request.form()`) ever spools an
# uploaded file part to disk. Starlette's own `max_part_size` (1 MiB
# default) only bounds plain text form FIELDS, never a part carrying a
# `filename` — i.e. an actual file upload currently has NO size limit
# whatsoever at the multipart-parsing layer, making this the only gate that
# runs before the full body is received.
#
# Sized comfortably above the largest legitimate guarded request: the file
# payload itself (ANALYZE_MAX_UPLOAD_MB / KNOWLEDGE_MAX_FILE_SIZE_MB = 10
# MiB) plus realistic multipart overhead (boundary markers + per-part
# headers, on the order of a few hundred bytes per part; browsers do not
# base64-encode file parts) plus a handful of small accompanying text
# fields. 32 MiB gives >3x headroom over the largest single-file bound
# configured today while still capping any one guarded request to a
# bounded allocation instead of "however much disk is free".
MAX_REQUEST_BODY_MB = int(os.getenv("MAX_REQUEST_BODY_MB", "32"))

# ============================================================================
# LOT 47 BIS — AO DOSSIER (RC, CCTP, CCAP, acte d'engagement, annexes -> ONE analysis)
# ============================================================================
# User-set requirements, in DECIMAL units (1 Mo = 1 000 000 octets; never mixed with
# the Mio limits of the RAG corpus above, which are unchanged): four named slots of one
# file each plus at most DOSSIER_MAX_ANNEXES annexes, and at most DOSSIER_MAX_TOTAL_BYTES
# for the SUM of the bytes actually received (not declared sizes, not the multipart
# envelope). Equality is allowed, one byte more is refused (413). A file may use the whole
# remaining capacity: no implicit per-file cap on this journey.
DOSSIER_MAX_TOTAL_BYTES = int(os.getenv("DOSSIER_MAX_TOTAL_BYTES", "100_000_000"))
DOSSIER_MAX_ANNEXES = 3
DOSSIER_MAIN_SLOTS = 4  # rc, cctp, ccap, acte_engagement — one file each
DOSSIER_MAX_FILES = DOSSIER_MAIN_SLOTS + DOSSIER_MAX_ANNEXES
# Wire-level guard of /api/analyze ONLY (other guarded routes keep MAX_REQUEST_BODY_MB):
# the files' bytes plus a BOUNDED multipart envelope (boundaries, per-part headers, the
# small text fields, a pasted text of the legacy mode).
DOSSIER_MULTIPART_MARGIN_BYTES = int(os.getenv("DOSSIER_MULTIPART_MARGIN_BYTES", "4_000_000"))
# Accepting the binary size does not promise to analyse unlimited text: the CUMULATIVE
# extracted text of all pieces is bounded and refused explicitly (413) before any
# generation — never truncated silently. Every character is then read by the extractor,
# window by window (DOSSIER_WINDOW_CHARS, below the extractor's own per-call prompt caps).
DOSSIER_MAX_EXTRACTED_CHARS = int(os.getenv("DOSSIER_MAX_EXTRACTED_CHARS", "300_000"))
DOSSIER_WINDOW_CHARS = 15_000
DOSSIER_MAX_LIST_ITEMS = 50
DOSSIER_MAX_OBSERVATIONS = 300

# Lot 50 — free-form "autres pièces" (§1) share the SAME slots/bytes/chars budgets above, no extra
# allocation; and the preview/admission staging area (§3) a submitted-but-not-yet-confirmed dossier lives
# in for a bounded time before it must be re-confirmed (hashes/rights re-verified) or it expires.
DOSSIER_STAGING_TTL_SECONDS = int(os.getenv("DOSSIER_STAGING_TTL_SECONDS", str(30 * 60)))
# Lot 50 bis §4 — the actual background sweep of staging rows past DOSSIER_STAGING_TTL_SECONDS (see
# src/web/ao_dossier/expiry_sweeper.py): how often it wakes up, and how many expired dossiers it removes per
# pass (a large abandoned backlog is drained over several passes, never one unbounded transaction).
DOSSIER_STAGING_SWEEP_INTERVAL_SECONDS = int(os.getenv("DOSSIER_STAGING_SWEEP_INTERVAL_SECONDS", str(5 * 60)))
DOSSIER_STAGING_SWEEP_BATCH_SIZE = int(os.getenv("DOSSIER_STAGING_SWEEP_BATCH_SIZE", "50"))

ADMIN_NOTIFICATION_EMAIL = os.getenv("ADMIN_NOTIFICATION_EMAIL", "")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "")

# ============================================================================
# EXTERNAL API CONFIGURATION
# ============================================================================
PAPPERS_ENABLED = os.getenv("PAPPERS_ENABLED", "true").lower() == "true"
PAPPERS_TIMEOUT_SECONDS = int(os.getenv("PAPPERS_TIMEOUT_SECONDS", "8"))
PAPPERS_MAX_RETRIES = int(os.getenv("PAPPERS_MAX_RETRIES", "2"))

# Rate limiting
API_RATE_LIMIT_CALLS = int(os.getenv("API_RATE_LIMIT_CALLS", "100"))
API_RATE_LIMIT_PERIOD_SECONDS = int(os.getenv("API_RATE_LIMIT_PERIOD_SECONDS", "60"))

# ============================================================================
# B14-T1 — PER-ACTION RATE LIMITING (src/web/security/rate_limit.py)
# ============================================================================
# Each action passed to rate_limit.check_and_record(action, key, ...) reads
# its own (max_attempts, window_seconds) pair from here — additive-only,
# never a shared/global threshold, since different actions have different
# abuse profiles (a login brute-force script vs. an anonymous password-reset
# requester vs. a logged-in user re-clicking "regenerate document"). See
# docs/api/B14_T1_CSRF_RATE_LIMIT_CONTRACT.md for the full contract.
RATE_LIMIT_LOGIN_MAX_ATTEMPTS = int(os.getenv("RATE_LIMIT_LOGIN_MAX_ATTEMPTS", "10"))
RATE_LIMIT_LOGIN_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_LOGIN_WINDOW_SECONDS", "60"))

RATE_LIMIT_REGISTER_MAX_ATTEMPTS = int(os.getenv("RATE_LIMIT_REGISTER_MAX_ATTEMPTS", "5"))
RATE_LIMIT_REGISTER_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_REGISTER_WINDOW_SECONDS", "60"))

# Authenticated, per-user costly action (document regeneration re-renders a
# PDF/DOCX from a persisted analysis snapshot — real per-request cost, no
# LLM call but real CPU/IO). Keyed by user id, not IP, in the route.
RATE_LIMIT_REGENERATE_DOCUMENT_MAX_ATTEMPTS = int(os.getenv("RATE_LIMIT_REGENERATE_DOCUMENT_MAX_ATTEMPTS", "20"))
RATE_LIMIT_REGENERATE_DOCUMENT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_REGENERATE_DOCUMENT_WINDOW_SECONDS", "60"))

# B21-T1 (Agent C, password reset) — provisioned here since this module owns
# the shared rate-limiting primitive; neither constant is read by any route
# in this file's ownership. reset_request is anonymous/pre-auth (keyed by
# IP); reset_consume similarly has no authenticated identity to key on until
# the token itself is validated, so it is also keyed by IP by convention —
# see docs/api/B14_T1_CSRF_RATE_LIMIT_CONTRACT.md.
RATE_LIMIT_RESET_REQUEST_MAX_ATTEMPTS = int(os.getenv("RATE_LIMIT_RESET_REQUEST_MAX_ATTEMPTS", "5"))
RATE_LIMIT_RESET_REQUEST_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_RESET_REQUEST_WINDOW_SECONDS", "3600"))

RATE_LIMIT_RESET_CONSUME_MAX_ATTEMPTS = int(os.getenv("RATE_LIMIT_RESET_CONSUME_MAX_ATTEMPTS", "10"))
RATE_LIMIT_RESET_CONSUME_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_RESET_CONSUME_WINDOW_SECONDS", "3600"))

# ============================================================================
# FEATURE FLAGS
# ============================================================================
FEATURE_DOCUMENT_GENERATION = os.getenv("FEATURE_DOCUMENT_GENERATION", "true").lower() == "true"
FEATURE_AUTO_SCORING = os.getenv("FEATURE_AUTO_SCORING", "true").lower() == "true"
FEATURE_CAPACITY_ANALYSIS = os.getenv("FEATURE_CAPACITY_ANALYSIS", "true").lower() == "true"
FEATURE_COMPANY_ENRICHMENT = os.getenv("FEATURE_COMPANY_ENRICHMENT", "true").lower() == "true"

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()  # DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_FORMAT_JSON = os.getenv("LOG_FORMAT_JSON", "false").lower() == "true"
LOG_TO_FILE = os.getenv("LOG_TO_FILE", "true").lower() == "true"
LOG_TO_CONSOLE = os.getenv("LOG_TO_CONSOLE", "true").lower() == "true"

# ============================================================================
# VALIDATION & CONSTRAINTS
# ============================================================================
MAX_DOCUMENT_SIZE_MB = int(os.getenv("MAX_DOCUMENT_SIZE_MB", "50"))
MAX_AO_TEXT_LENGTH = int(os.getenv("MAX_AO_TEXT_LENGTH", "50000"))  # Max chars for AO extraction
MIN_EXTRACTION_CONFIDENCE = float(os.getenv("MIN_EXTRACTION_CONFIDENCE", "0.5"))

# B12-T1: bounded background analysis job executor (src/web/job_executor.py)
# — replaces the previous unbounded `threading.Thread(...).start()` per
# analysis. JOB_EXECUTOR_MAX_CONCURRENCY is the fixed number of long-lived
# worker threads consuming the queue; JOB_QUEUE_MAX_DEPTH is the bounded
# queue.Queue's maxsize (submissions beyond this raise
# job_executor.JobQueueSaturatedError instead of growing unbounded).
# JOB_HEARTBEAT_INTERVAL_SECONDS is how often a worker refreshes
# last_heartbeat_at while a claimed job actually runs; a job restart-
# reconciliation scan (job_executor.reconcile_on_startup) treats a
# 'running' row whose heartbeat is older than JOB_HEARTBEAT_STALE_SECONDS
# as abandoned. Stale threshold is a multiple of the tick interval, never
# equal to it, so one merely-slow tick is never mistaken for abandonment.
JOB_EXECUTOR_MAX_CONCURRENCY = int(os.getenv("JOB_EXECUTOR_MAX_CONCURRENCY", "4"))
JOB_QUEUE_MAX_DEPTH = int(os.getenv("JOB_QUEUE_MAX_DEPTH", "20"))
JOB_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("JOB_HEARTBEAT_INTERVAL_SECONDS", "10"))
JOB_HEARTBEAT_STALE_SECONDS = int(os.getenv("JOB_HEARTBEAT_STALE_SECONDS", "60"))

# ============================================================================
# BUSINESS LOGIC CONFIGURATION
# ============================================================================
# Lot 43: no scoring thresholds and no capacity thresholds live here any
# more. Both are the ACCOUNT's own private configuration (ScoringPolicy
# threshold_go / threshold_sous_reserve, PrivateCapacityPlan
# disponibilite_minimum_pct); a global environment default used to stand in
# for them and is removed.

# Lot 44: the unused CERTIFICATIONS_LIBRARY / TECHNOLOGIES_LIBRARY (an IT
# vocabulary no code read — checked across src, tests, scripts, migrations)
# are removed: what a criterion compares now comes from the account's own
# policy and declared profile.

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================
def get_config_dict() -> Dict[str, Any]:
    """Return all configuration as dictionary."""
    return {
        "paths": {
            "root": str(ROOT_DIR),
            "data": str(DATA_DIR),
            "outputs": str(OUTPUT_DIR),
            "prompts": str(PROMPTS_DIR),
            "logs": str(LOGS_DIR),
        },
        "llm": {
            "enabled": LLM_ENABLED,
            "timeout_seconds": LLM_TIMEOUT_SECONDS,
            "max_tokens": LLM_MAX_TOKENS,
            "temperature": LLM_TEMPERATURE,
            "temperature_factual": LLM_TEMPERATURE_FACTUAL,
            "temperature_generation": LLM_TEMPERATURE_GENERATION,
            "provider_priority": LLM_PROVIDER_PRIORITY,
            "max_retries": LLM_MAX_RETRIES,
        },
        "rag": {
            "enabled": RAG_ENABLED,
            "min_score_threshold": RAG_MIN_SCORE_THRESHOLD,
            "top_k_results": RAG_TOP_K_RESULTS,
            "cache_ttl_seconds": RAG_CACHE_TTL_SECONDS,
        },
        "external_apis": {
            "pappers_enabled": PAPPERS_ENABLED,
            "pappers_timeout_seconds": PAPPERS_TIMEOUT_SECONDS,
            "pappers_max_retries": PAPPERS_MAX_RETRIES,
        },
        "features": {
            "document_generation": FEATURE_DOCUMENT_GENERATION,
            "auto_scoring": FEATURE_AUTO_SCORING,
            "capacity_analysis": FEATURE_CAPACITY_ANALYSIS,
            "company_enrichment": FEATURE_COMPANY_ENRICHMENT,
        },
        "logging": {
            "level": LOG_LEVEL,
            "json_format": LOG_FORMAT_JSON,
            "to_file": LOG_TO_FILE,
            "to_console": LOG_TO_CONSOLE,
        },
    }


def validate_config() -> None:
    """Validate configuration settings — called explicitly at real
    application startup (main.py's lifespan), never automatically at
    import time (see the module-level comment above this function's
    former call site for the confirmed defect this fixes: a bare
    `import src.core.config` — done transitively by nearly everything,
    including migrations/env.py and the whole test suite's collection —
    used to require a real LLM/Pappers credential to even succeed).

    Two severities, deliberately NOT both hard errors:
    - Missing external credentials (no LLM provider key, no Pappers token)
      only ever DEGRADE optional functionality to its already-supported
      local/algorithmic fallback (CLAUDE.md: "le fallback sans API doit
      toujours rester fonctionnel") — logged as a warning, never raised,
      so a legitimate no-key deployment (or a test/CI run) can still start.
    - Genuine internal coherence bugs (a threshold misconfigured against
      another, a negative/zero limit, a budget smaller than what it must
      contain) indicate a broken deployment regardless of any external
      credential and stay hard errors, raised exactly as before.
    """
    errors = []
    warnings = []

    available_llm_keys = {"anthropic": ANTHROPIC_API_KEY, "openai": OPENAI_API_KEY, "mistral": MISTRAL_API_KEY}
    if LLM_ENABLED and not any(available_llm_keys.get(p) for p in LLM_PROVIDER_PRIORITY):
        warnings.append(
            "No API key configured for any provider in LLM_PROVIDER_PRIORITY — "
            "every LLM-dependent step will use its local/algorithmic fallback."
        )

    if not PAPPERS_API_TOKEN and PAPPERS_ENABLED:
        warnings.append(
            "PAPPERS_API_TOKEN not set but PAPPERS_ENABLED=true — company enrichment "
            "will use its local fallback instead of the real Pappers lookup."
        )

    # B18-T6: self-contained numeric sanity only (positive integers, block
    # can hold at least one excerpt) — the cross-check against
    # src.rag.passage_location.MAX_PASSAGE_CHARS lives in
    # src/rag/context_budget.py itself, not here, so this core config
    # module never depends on a specific RAG submodule.
    if ANALYZE_DOCX_MAX_ZIP_ENTRIES <= 0:
        errors.append("ANALYZE_DOCX_MAX_ZIP_ENTRIES must be a positive integer")
    if MAX_REQUEST_BODY_MB <= 0:
        errors.append("MAX_REQUEST_BODY_MB must be a positive integer")
    elif MAX_REQUEST_BODY_MB * 1024 * 1024 < max(ANALYZE_MAX_UPLOAD_MB, KNOWLEDGE_MAX_FILE_SIZE_MB) * 1024 * 1024:
        errors.append(
            "MAX_REQUEST_BODY_MB must be >= the largest configured single-file upload bound "
            "(ANALYZE_MAX_UPLOAD_MB / KNOWLEDGE_MAX_FILE_SIZE_MB), or a legitimate upload would "
            "never reach the route-level check that produces a proper error"
        )

    for _dossier_name in ("DOSSIER_MAX_TOTAL_BYTES", "DOSSIER_MULTIPART_MARGIN_BYTES", "DOSSIER_MAX_EXTRACTED_CHARS"):
        if globals()[_dossier_name] <= 0:
            errors.append(f"{_dossier_name} must be a positive integer")

    for _rl_name in (
        "RATE_LIMIT_LOGIN_MAX_ATTEMPTS", "RATE_LIMIT_LOGIN_WINDOW_SECONDS",
        "RATE_LIMIT_REGISTER_MAX_ATTEMPTS", "RATE_LIMIT_REGISTER_WINDOW_SECONDS",
        "RATE_LIMIT_REGENERATE_DOCUMENT_MAX_ATTEMPTS", "RATE_LIMIT_REGENERATE_DOCUMENT_WINDOW_SECONDS",
        "RATE_LIMIT_RESET_REQUEST_MAX_ATTEMPTS", "RATE_LIMIT_RESET_REQUEST_WINDOW_SECONDS",
        "RATE_LIMIT_RESET_CONSUME_MAX_ATTEMPTS", "RATE_LIMIT_RESET_CONSUME_WINDOW_SECONDS",
    ):
        if globals()[_rl_name] <= 0:
            errors.append(f"{_rl_name} must be a positive integer")

    if RAG_SELECTION_MAX_CANDIDATES <= 0:
        errors.append("RAG_SELECTION_MAX_CANDIDATES must be a positive integer")
    if RAG_SELECTION_MAX_EXCERPT_CHARS <= 0:
        errors.append("RAG_SELECTION_MAX_EXCERPT_CHARS must be a positive integer")
    if RAG_SELECTION_MAX_DOCUMENT_BLOCK_CHARS <= 0:
        errors.append("RAG_SELECTION_MAX_DOCUMENT_BLOCK_CHARS must be a positive integer")
    elif RAG_SELECTION_MAX_DOCUMENT_BLOCK_CHARS < RAG_SELECTION_MAX_EXCERPT_CHARS:
        errors.append(
            "RAG_SELECTION_MAX_DOCUMENT_BLOCK_CHARS must be >= RAG_SELECTION_MAX_EXCERPT_CHARS "
            "(a single candidate must be able to fit)"
        )

    if warnings:
        from src.core.logger import get_agent_logger
        logger = get_agent_logger("config")
        for w in warnings:
            logger.warning("Configuration warning: %s", w)

    if errors:
        raise ValueError("Configuration errors:\n" + "\n".join(f"  - {e}" for e in errors))


# B24-T1 (DEFECT confirmed): this module used to call validate_config()
# unconditionally at the bottom, the moment ANYTHING imported it — which is
# nearly everything (models.py, every route, every script, migrations/env.py
# via `from src.core.config import DATABASE_URL`). On a genuinely fresh
# checkout with no .env and no real provider key (LLM_ENABLED defaults
# true), `import src.core.config` — and therefore `alembic upgrade head`,
# any admin script, and the whole test suite's own collection — raised
# ValueError before a single test or migration ever ran, never having
# attempted to construct a provider or read a real secret. Reproduced
# directly: a subprocess with load_dotenv() short-circuited and every
# credential env var cleared fails this exact import with "No API key
# configured for any provider" + "PAPPERS_API_TOKEN not set". This app's own
# CLAUDE.md states the fallback-without-API-key path must always stay
# functional — requiring a key just to IMPORT the config module contradicts
# that even before any LLM call is attempted.
#
# Fixed by moving the call to the application's own startup instead of bare
# import: `main.py`'s lifespan (the FastAPI/uvicorn entrypoint) calls
# validate_config() explicitly before serving traffic — a real deployment
# still gets the same loud, explicit failure if genuinely misconfigured,
# just at actual startup rather than at every import. Every LLM call site
# tolerates a disabled LLM (CLAUDE.md: "le fallback sans API doit toujours
# rester fonctionnel"). Tests/scripts/migrations that only ever import this
# module transitively no longer pay this cost at all.
#
# `validate_config()` itself is UNCHANGED — same checks, same errors, same
# ValueError shape — only WHEN it runs has moved.
