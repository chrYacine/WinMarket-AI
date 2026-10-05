"""B24-T1 (config startup) + B16-T1 (remaining externalized prompts).

DEFECT confirmed for B24-T1: `src.core.config` used to call
`validate_config()` unconditionally at the bottom of the module, the
moment ANYTHING imported it — including `migrations/env.py` (via
`from src.core.config import DATABASE_URL`) and this whole test suite's
own collection. On a genuinely fresh checkout with no `.env` and no real
provider key (`LLM_ENABLED` defaults `true`), that import raised
`ValueError` before a single test/migration ever ran. Fixed by moving the
call to `main.py`'s own startup (lifespan) and splitting missing-external-
credential conditions (warn, app's own no-key fallback stays supported)
from genuine internal coherence bugs (still a hard error).

B16-T1: the last two prompts this codebase kept inline as Python f-strings
(scoring_engine.py's enrichment prompt/system-prompt tail, the RAG
selection system prompt — now in semantic_rerank.py) are now externalized via the same shared
`src.core.prompt_loader.load_prompt` already used for
`src/agents/prompts/ao_extraction_*.txt` (B05-T2),
`src/rag/prompts/reference_selection.txt` (B18-T3) and
`src/livrables/prompts/document_*.txt` (B19-T1).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_isolated_script(script: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Runs `script` in a real, separate Python process with dotenv
    short-circuited and every credential env var cleared FIRST — the only
    way to prove "no real .env/key is required" without risking this
    session's own real .env ever being read (see tests/conftest.py's own
    subprocess pattern in test_b12_t1_job_executor.py for the identical
    idiom)."""
    header = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, r"{PROJECT_ROOT}")
        for _v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY", "PAPPERS_API_TOKEN"):
            os.environ.pop(_v, None)
        import dotenv
        dotenv.load_dotenv = lambda *a, **k: False
    """)
    return subprocess.run(
        [sys.executable, "-c", header + script],
        cwd=str(cwd) if cwd else str(PROJECT_ROOT),
        capture_output=True, text=True, timeout=30,
    )


# ---------------------------------------------------------------------------
# B24-T1 proof 1 — import (and, by extension, migrations/env.py which only
# ever does `from src.core.config import DATABASE_URL`) never requires a
# real provider key or .env.
# ---------------------------------------------------------------------------

def test_config_import_never_requires_a_provider_key_or_real_env_file():
    proc = _run_isolated_script("import src.core.config; print('IMPORT_OK')")
    assert proc.returncode == 0, proc.stderr
    assert "IMPORT_OK" in proc.stdout


def test_validate_config_warns_but_does_not_raise_for_missing_credentials_alone():
    proc = _run_isolated_script(
        "from src.core import config\n"
        "config.validate_config()\n"
        "print('VALIDATE_OK')\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert "VALIDATE_OK" in proc.stdout


def test_validate_config_still_raises_for_a_genuine_coherence_bug():
    proc = _run_isolated_script(
        "from src.core import config\n"
        # Lot 43: the global SCORING_THRESHOLD_GO/SOUS_RESERVE (and their
        # coherence check) were removed — thresholds are per-account now,
        # validated by scoring_policy_validation. Another genuine internal
        # coherence bug still makes validate_config raise:
        "config.ANALYZE_DOCX_MAX_ZIP_ENTRIES = 0\n"
        "try:\n"
        "    config.validate_config()\n"
        "    print('DID_NOT_RAISE')\n"
        "except ValueError as e:\n"
        "    print('RAISED_AS_EXPECTED')\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert "RAISED_AS_EXPECTED" in proc.stdout
    assert "DID_NOT_RAISE" not in proc.stdout


# ---------------------------------------------------------------------------
# B24-T1 proof 2 — every resolved path is anchored to __file__, not CWD.
# ---------------------------------------------------------------------------

def test_resolved_paths_are_stable_from_a_completely_different_working_directory(tmp_path):
    elsewhere = tmp_path / "far_from_the_project"
    elsewhere.mkdir()
    proc = _run_isolated_script(
        "from src.core import config\n"
        "print('ROOT=' + str(config.ROOT_DIR))\n"
        "print('DATA=' + str(config.DATA_DIR))\n"
        "print('PROMPTS=' + str(config.PROMPTS_DIR))\n",
        cwd=elsewhere,
    )
    assert proc.returncode == 0, proc.stderr
    lines = dict(line.split("=", 1) for line in proc.stdout.strip().splitlines() if "=" in line)
    assert Path(lines["ROOT"]).resolve() == PROJECT_ROOT.resolve()
    assert Path(lines["DATA"]).resolve() == (PROJECT_ROOT / "data").resolve()
    assert Path(lines["PROMPTS"]).resolve() == (PROJECT_ROOT / "prompts").resolve()


# ---------------------------------------------------------------------------
# B16-T1 proof — a prompt renders real private data, and a missing
# file/invalid variable is a safe error, never a fictional fallback.
# ---------------------------------------------------------------------------

class _FakeProviderProfile:
    def __init__(self, raison_sociale, competences):
        self.raison_sociale = raison_sociale
        self.competences = competences


def test_scoring_system_prompt_renders_real_private_profile_data_not_a_placeholder():
    from src.agents.scoring_engine import ScoringEngine

    profile = _FakeProviderProfile("Cabinet Synthétique SARL", ["Python", "Kubernetes"])
    prompt = ScoringEngine()._build_scoring_system_prompt(profile)
    assert "Cabinet Synthétique SARL" in prompt
    assert "Python" in prompt and "Kubernetes" in prompt
    assert "{{IDENTITY}}" not in prompt, "the placeholder must be substituted, never leak into the real prompt"
    # No fictional headcount/tenure/track-record ever reintroduced (B19-T1).
    for forbidden in ("effectif", "ans d'existence", "marchés remportés"):
        assert forbidden not in prompt.lower()


def test_scoring_system_prompt_generic_when_no_profile_invents_nothing():
    from src.agents.scoring_engine import ScoringEngine

    prompt = ScoringEngine()._build_scoring_system_prompt(None)
    assert "Cabinet" not in prompt
    # Lot 44: the generic identity no longer assumes a trade ("ESN spécialisée en …").
    assert "ESN" not in prompt and "ne te sont pas communiqués" in prompt


def test_missing_prompt_file_is_a_safe_error_never_a_fictional_fallback(monkeypatch):
    """B16-T2: a missing prompt file now raises the centralized
    PromptLoadError (never a bare FileNotFoundError/OSError leaking a raw
    filesystem exception type to callers) — see src/core/prompt_loader.py.
    ScoringEngine.enrich_with_llm is the one caller that further translates
    this into enrichment_reason="prompt_missing" without losing the
    already-computed score; _build_scoring_system_prompt itself still just
    propagates it, which is what this test verifies."""
    from src.agents import scoring_engine
    from src.core.prompt_loader import PromptLoadError

    monkeypatch.setattr(scoring_engine, "_SCORING_SYSTEM_PATH", Path("/nonexistent/does_not_exist.txt"))
    with pytest.raises(PromptLoadError):
        scoring_engine.ScoringEngine()._build_scoring_system_prompt(None)


def test_missing_variable_value_is_a_safe_error_not_a_silent_corruption():
    from src.core.prompt_loader import load_prompt

    path = PROJECT_ROOT / "src" / "agents" / "prompts" / "scoring_system.txt"
    with pytest.raises(TypeError):
        load_prompt(path, identity=None)  # None is not a valid substitution value


def test_rag_selection_system_prompt_loads_and_is_the_real_content_not_a_stub():
    from src.rag.semantic_rerank import SemanticReranker

    prompt = SemanticReranker()._rag_system_prompt()
    assert "quatre dimensions" in prompt
    assert "proximité sectorielle" in prompt
