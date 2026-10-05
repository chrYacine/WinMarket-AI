"""Lot 58 — scripts/operator_access.py used to REQUIRE a local --env-file, reading DATABASE_URL only
from it. A server-managed deployment (Render) configures the process environment directly (Dashboard/
secrets), never a mounted .env file — this proves the new env-var-sourcing path works, with the exact
same validation and operator rules, without requiring a real database connection (which is out of
scope for a targeted regression on the CLI's argument/config plumbing).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "operator_access.py"
REPO_ROOT = SCRIPT.parents[1]


def _run(env, args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
    )


def test_env_file_omitted_and_no_database_url_in_environment_is_a_clean_refusal(tmp_path):
    credential_file = tmp_path / "operator.token"
    env = {**os.environ, "WM_DB_TEST_MODE": "", "PYTEST_CURRENT_TEST": ""}
    env.pop("DATABASE_URL", None)
    result = _run(env, ["--credential-file", str(credential_file), "list"])
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "database url" in (result.stderr + result.stdout).lower()
    assert not credential_file.exists()


def test_env_file_omitted_reads_database_url_from_the_real_process_environment(tmp_path):
    """Proves the CLI reaches the SAME validate_environment/DATABASE_URL check it always did, sourced
    from os.environ instead of a file — a syntactically valid, deployment-shaped but UNREACHABLE
    target must fail at the actual connection attempt, never at argument parsing or validation, and
    never with a leaked credential in the error output."""
    credential_file = tmp_path / "operator.token"
    env = {**os.environ}
    for key in ("WM_DB_TEST_MODE", "PYTEST_CURRENT_TEST"):
        env.pop(key, None)
    env.update({
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql://wm58_role:s3cr3t-token-value@dpg-nonexistent-host.example.invalid/wm58_db",
        "BASE_URL": "https://winmarket-ai-58.example.invalid",
        "SESSION_SECRET": "a" * 32,
    })
    result = _run(env, ["--credential-file", str(credential_file), "list"])
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "s3cr3t-token-value" not in combined, "a credential must never appear in the CLI's own output"
    # It must have passed argument parsing AND validate_environment/DATABASE_URL presence — the failure
    # is a real (expected) connection/DNS error, not our own guard refusing the shape of the URL.
    assert "Explicit isolated database URL required" not in combined
    assert "ProtectedTargetError" not in combined
    assert not credential_file.exists()


def test_explicit_env_file_mode_still_works_unchanged(tmp_path):
    """The pre-existing local mode (--env-file) must behave exactly as before this lot."""
    env_file = tmp_path / "runtime.env"
    env_file.write_text("", encoding="utf-8")  # no DATABASE_URL inside
    credential_file = tmp_path / "operator.token"
    env = {**os.environ}
    for key in ("WM_DB_TEST_MODE", "PYTEST_CURRENT_TEST", "DATABASE_URL", "APP_ENV", "BASE_URL"):
        env.pop(key, None)
    result = _run(env, ["--env-file", str(env_file), "--credential-file", str(credential_file), "list"])
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "database url" in (result.stderr + result.stdout).lower()
