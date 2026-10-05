"""B04-T2 — closing the gap B04-T0 left open: module-level path constants
bound ONCE at import time from the ORIGINAL config.DATA_DIR/config.
OUTPUT_DIR (src/web/jobs.py::ANALYSES_DIR/ANALYSIS_FILES_DIR; lot 43 removed
the second one, historique_service.HIST_FILE) do not get redirected by
per-test monkeypatching of config.* — because pytest imports every test
module (and everything it transitively imports) during COLLECTION, which
happens before ANY fixture body ever runs for the whole session. Two
pre-existing test files (tests/test_job_error_handling.py, tests/
test_qa_llm_capture.py) proved this for real: running a genuine
/api/analyze job through them, before this ticket's fix, wrote real PDF/
DOCX/JSON files straight into this project's own data/outputs/ and
data/historique/ directories.

The fix lives entirely in tests/conftest.py's autouse
_b04_isolated_filesystem_roots fixture (now also monkeypatching
jobs.ANALYSES_DIR/jobs.ANALYSIS_FILES_DIR) plus a new sibling guard
fixture, _b04_historique_jobs_write_guard. This
file is the regression suite for that fix — see docs/qa/B04_T2_NOTES.md
for the full inventory, the exact commands run, and their real output.

No real network, no real API key, no real .env value, no destructive
action on anything outside a temp directory this file creates itself.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from src.core.models import AOContext, CriterionScore, ScoringResult
from src.web import jobs
from tests.conftest import make_active_starter_user

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_DATA_DIR = REPO_ROOT / "data"
REAL_HIST_FILE = REAL_DATA_DIR / "historique" / "historique_ao.json"
REAL_ANALYSES_DIR = REAL_DATA_DIR / "historique" / "analyses"
REAL_OUTPUTS_DIR = REAL_DATA_DIR / "outputs"

VALID_WEIGHTS = {
    "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
    "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
    "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
    "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
}
COMPLETE_BUSINESS_RULES = {
    "budget_minimum_eur": 0, "max_charge_pct": 100,
    "max_unmastered_technologies": 999, "certification_penalty_score": 20,
}
VALID_AO_TEXT = (
    "Appel d'offres - Portail client\n"
    "Acheteur : Collectivite Exemple\n"
    "Budget : 250 000 euros. Date limite : 30/11/2026.\n"
    "Exigences : Python et Django.\n"
)


# ---------------------------------------------------------------------------
# Shared helpers (this codebase's own convention: test files do not import
# these from one another, only from tests/conftest.py).
# ---------------------------------------------------------------------------

def _csrf_from(html: str) -> str:
    import re
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


def _configure_capacity(client, charge: int = 40, *, csrf: str | None = None):
    return client.post(
        "/api/capacity",
        json={
            "charge_globale_pct": charge, "nombre_projets_en_cours": 1,
            "projets_en_cours": ["Projet test"], "capacites_par_pole": {"Software Engineering": 40},
        },
        headers={"X-CSRF-Token": csrf} if csrf else None,
    )


def _save_profile(client, *, csrf: str | None = None):
    return client.put(
        "/api/scoring-config/profile",
        json={
            "raison_sociale": "ESN de test", "effectif": "10-50",
            "competences": ["python", "django"], "certifications": [],
        },
        headers={"X-CSRF-Token": csrf} if csrf else None,
    )


def _save_draft(client, *, csrf: str | None = None):
    return client.put(
        "/api/scoring-config/policy",
        json={
            "weights": dict(VALID_WEIGHTS), "threshold_go": 88, "threshold_sous_reserve": 60,
            "business_rules": COMPLETE_BUSINESS_RULES,
        },
        headers={"X-CSRF-Token": csrf} if csrf else None,
    )


def _activate(client, *, csrf: str | None = None):
    return client.post(
        "/api/scoring-config/policy/activate",
        json={"expected_active_version": None},
        headers={"X-CSRF-Token": csrf} if csrf else None,
    )


def _configure_account(client, db, email):
    make_active_starter_user(db, email, scoring=False)
    csrf = _login(client, email)
    _configure_capacity(client, csrf=csrf)
    _save_profile(client, csrf=csrf)
    _save_draft(client, csrf=csrf)
    _activate(client, csrf=csrf)
    return csrf


def _install_no_op_search(monkeypatch):
    from src.rag import private_rag_manager
    monkeypatch.setattr(private_rag_manager, "search", lambda db, **kw: [])


def _run_analysis_to_terminal(client, text: str = VALID_AO_TEXT, *, csrf: str | None = None):
    r = client.post("/api/analyze", data={"mode": "paste", "text": text}, headers={"X-CSRF-Token": csrf} if csrf else None)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    for _ in range(50):
        job = jobs.get_job(job_id)
        if job.status != "running":
            break
        time.sleep(0.1)
    assert job.status != "running", "job never reached a terminal state"
    return job


def _inventory(root: Path) -> dict[str, tuple[int, int]]:
    """Metadata/fingerprint inventory (mtime_ns, size) of every file under
    `root` — deliberately never reads file CONTENTS, so this never exposes
    what is in this project's real data/ directory."""
    inventory: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return inventory
    for path in root.rglob("*"):
        if path.is_file():
            try:
                st = path.stat()
            except OSError:
                continue
            inventory[str(path.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    return inventory


def _assert_real_project_dirs_untouched(before: dict, after: dict) -> None:
    assert before == after, (
        "importing/running application code must never create, modify or delete "
        "any file under this project's real data/ directory"
    )


# ---------------------------------------------------------------------------
# 1. Collection alone never writes anything to the real data/ directory.
# ---------------------------------------------------------------------------

def test_importing_application_modules_never_touches_real_data_dir():
    """Simulates pure COLLECTION: a fresh subprocess that only IMPORTS the
    modules confirmed (by this ticket's own investigation) to hold leaky
    module-level path constants, plus `main` (which transitively imports
    the whole routing/pipeline graph) — no fixture runs, no test executes,
    exactly like the moment pytest collects test_*.py files today, before
    any fixture body has ever run. A before/after metadata inventory of the
    real project's data/ directory (mtime + size only, contents never
    read) must be byte-for-byte identical."""
    before = _inventory(REAL_DATA_DIR)
    script = (
        "import src.web.jobs\n"
        "import src.web.examples_service\n"
        "import src.core.capacity_plan\n"
        "import src.rag.semantic_rerank\n"
        "import src.rag.private_rag_manager\n"
        "import src.web.knowledge.extraction\n"
        "import src.web.analysis_services\n"
        "import main\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    after = _inventory(REAL_DATA_DIR)
    _assert_real_project_dirs_untouched(before, after)


# ---------------------------------------------------------------------------
# 2. The old leaky path, proven to have written for real, now doesn't —
#    success, handled failure, and document-rendering failure, all relying
#    ONLY on the now-fixed autouse fixture (no manual per-test redirection).
# ---------------------------------------------------------------------------

def test_successful_analyze_job_never_writes_under_the_real_project_dirs(client, db, monkeypatch):
    before_hist = _inventory(REAL_DATA_DIR / "historique")
    before_out = _inventory(REAL_OUTPUTS_DIR)

    _install_no_op_search(monkeypatch)
    csrf = _configure_account(client, db, "b04t2-success@example.com")
    job = _run_analysis_to_terminal(client, csrf=csrf)

    assert job.status == "done", job.error
    assert job.files, "a successful job must actually have generated documents"
    for file_path_str in job.files.values():
        resolved = Path(file_path_str).resolve()
        assert REAL_OUTPUTS_DIR.resolve() not in resolved.parents, (
            f"generated document landed under the REAL project outputs dir: {resolved}"
        )
        assert resolved.exists(), "the document must exist somewhere — under the isolated test root"

    # The isolated redirection actually received the writes (not just "no
    # writes happened at all").
    assert list(Path(jobs.ANALYSES_DIR).glob(f"{job.id}.json")), "the JSON snapshot must exist under the isolated root"
    # Lot 43: the legacy global JSON history is no longer written anywhere.
    assert not (Path(jobs.ANALYSES_DIR).parent / "historique_ao.json").exists()

    after_hist = _inventory(REAL_DATA_DIR / "historique")
    after_out = _inventory(REAL_OUTPUTS_DIR)
    _assert_real_project_dirs_untouched(before_hist, after_hist)
    _assert_real_project_dirs_untouched(before_out, after_out)


def test_handled_extraction_failure_never_writes_under_the_real_project_dirs(client, db, monkeypatch):
    """A handled/controlled failure (extraction raises, exactly the
    pattern tests/test_job_error_handling.py already uses) must confine
    itself just as strictly as a success — this job never reaches the
    persistence step, so the assertion is that nothing appears anywhere
    real, not that something appears in the isolated root."""
    import src.agents.ao_extractor as ao_extractor_module

    before_hist = _inventory(REAL_DATA_DIR / "historique")
    before_out = _inventory(REAL_OUTPUTS_DIR)

    def _raise_extraction(self, text):
        raise RuntimeError("B04-T2 synthetic extraction failure")
    monkeypatch.setattr(ao_extractor_module.AOExtractor, "extract", _raise_extraction)

    csrf = _configure_account(client, db, "b04t2-extractionfail@example.com")
    job = _run_analysis_to_terminal(client, csrf=csrf)

    assert job.status == "error"
    assert job.error_code == "extraction_failed"

    after_hist = _inventory(REAL_DATA_DIR / "historique")
    after_out = _inventory(REAL_OUTPUTS_DIR)
    _assert_real_project_dirs_untouched(before_hist, after_hist)
    _assert_real_project_dirs_untouched(before_out, after_out)


def test_document_rendering_failure_never_writes_under_the_real_project_dirs(client, db, monkeypatch):
    """A rendering failure is the interesting case: per src/web/jobs.py's
    own documented ordering (B10-T1/B11-T1), the computed result is still
    persisted (jobs._persist still runs) even though no document was
    produced — this is exactly the ANALYSES_DIR write path that leaked for
    real before this fix, now exercised deliberately under a rendering
    failure."""
    before_hist = _inventory(REAL_DATA_DIR / "historique")
    before_out = _inventory(REAL_OUTPUTS_DIR)

    _install_no_op_search(monkeypatch)
    monkeypatch.setattr(
        jobs, "_generate_documents",
        lambda job, ao, result: (_ for _ in ()).throw(RuntimeError("B04-T2 synthetic render failure")),
    )

    csrf = _configure_account(client, db, "b04t2-renderfail@example.com")
    job = _run_analysis_to_terminal(client, csrf=csrf)

    assert job.status == "error"
    assert job.error_code == jobs.DOCUMENT_GENERATION_FAILED_ERROR_CODE
    assert job.files == {}
    # The result WAS computed and the durable JSON persistence path still ran
    # despite the rendering failure — proving jobs.ANALYSES_DIR was actually
    # exercised by this test, isolated.
    assert list(Path(jobs.ANALYSES_DIR).glob(f"{job.id}.json"))
    assert not (Path(jobs.ANALYSES_DIR).parent / "historique_ao.json").exists()

    after_hist = _inventory(REAL_DATA_DIR / "historique")
    after_out = _inventory(REAL_OUTPUTS_DIR)
    _assert_real_project_dirs_untouched(before_hist, after_hist)
    _assert_real_project_dirs_untouched(before_out, after_out)


# ---------------------------------------------------------------------------
# 3. The guard actually detects a forbidden attempt.
# ---------------------------------------------------------------------------

def _make_minimal_job() -> jobs.Job:
    job = jobs.create_job(source_label="b04-t2-guard-test")
    job.ao = AOContext(titre="AO guard", client="Client guard")
    job.result = ScoringResult(
        decision="INCOMPLET", score_global=1.0,
        criteres=[CriterionScore(nom="Adequation expertise", score=1.0, poids=20.0, justification="x")],
        scoring_completeness="incomplete", scoring_missing=["budget_minimum_eur"],
    )
    job.status = "done"
    return job


def test_guard_refuses_persist_redirected_outside_allowed_roots(monkeypatch):
    """`sentinel` is created with tempfile.mkdtemp(), deliberately NOT via
    the tmp_path/tmp_path_factory fixtures — tmp_path lives INSIDE pytest's
    own --basetemp tree, which is exactly what _B04_ALLOWED_ROOTS trusts.
    A genuinely forbidden target has to live outside that entire tree."""
    sentinel = Path(tempfile.mkdtemp(prefix="b04_t2_sentinel_"))
    try:
        monkeypatch.setattr(jobs, "ANALYSES_DIR", sentinel / "historique" / "analyses")
        job = _make_minimal_job()

        with pytest.raises(RuntimeError, match="B04-T0 guard"):
            jobs._persist(job)

        assert not any(sentinel.rglob("*")), "the guard must refuse BEFORE a single byte is written"
    finally:
        shutil.rmtree(sentinel, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. No real dotenv/API key ever reaches config.*/os.environ — proven even
#    right after a fresh import (i.e. what "collection alone" looks like),
#    not merely inside a fixture body.
# ---------------------------------------------------------------------------

def test_credential_blanking_happens_at_module_import_not_inside_a_fixture():
    """Regression guard for the exact historical bug tests/conftest.py's
    own docstring documents (the "IMPORTANT CORRECTION" section): an
    earlier version blanked credentials inside a session-scoped autouse
    FIXTURE — but a fixture body only runs at the first test's SETUP,
    which happens AFTER collection has already imported every test module
    with the real key still live. This spawns a fresh subprocess that
    ONLY imports tests.conftest (mimicking collection: nothing here ever
    runs a fixture) with a synthetic, obviously-fake credential pre-seeded
    into the subprocess's own environment — never the real .env file, never
    a real key — and asserts it is already gone by the time the import
    finishes. If the blanking were ever moved back into a fixture, this
    would fail, because a bare `import` never runs one."""
    import os as os_module

    env = dict(os_module.environ)
    env["ANTHROPIC_API_KEY"] = "sk-ant-FAKE-not-real-B04-T2-regression-probe"
    env["PAPPERS_API_TOKEN"] = "FAKE-not-real-B04-T2-regression-probe"
    env["OPENAI_API_KEY"] = "FAKE-not-real-B04-T2-regression-probe"
    env["MISTRAL_API_KEY"] = "FAKE-not-real-B04-T2-regression-probe"

    script = (
        "import tests.conftest\n"
        "import os\n"
        "from src.core import config\n"
        "leaked = []\n"
        "for name in ('ANTHROPIC_API_KEY', 'PAPPERS_API_TOKEN', 'OPENAI_API_KEY', 'MISTRAL_API_KEY'):\n"
        "    if os.environ.get(name):\n"
        "        leaked.append(('env', name))\n"
        "    if getattr(config, name, None):\n"
        "        leaked.append(('config', name))\n"
        "assert not leaked, leaked\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
