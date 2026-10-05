"""B04-T0 — proof that the automatic isolation infrastructure in
tests/conftest.py actually protects a test that never asks for it, and
actually refuses a misconfigured write BEFORE it touches disk.

This file deliberately requests NO isolation fixture of its own — every
protection it relies on comes purely from the autouse fixtures in
tests/conftest.py (_b04_isolated_filesystem_roots, _b04_storage_write_guard,
_b04_block_real_network). If someone weakens or removes those fixtures,
this file is designed to start failing.
"""
from __future__ import annotations

import io
import re
import tempfile
from pathlib import Path

import pytest

from tests.conftest import make_active_starter_user

REAL_PROJECT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


def test_new_module_with_no_explicit_fixture_writes_only_under_tmp_path(client, db):
    """No isolation fixture requested here — proves the conftest.py autouse
    fixtures alone are enough. Uses the real upload route, real auth, real
    repository/DB writes; only the destination filesystem root is checked."""
    from src.core import config

    before_snapshot = set(REAL_PROJECT_DATA_DIR.rglob("*")) if REAL_PROJECT_DATA_DIR.exists() else set()

    make_active_starter_user(db, "b04proof@example.com")
    csrf = _login(client, "b04proof@example.com")
    r = client.post(
        "/api/knowledge/documents",
        files={"file": ("proof.md", io.BytesIO(b"# Proof\n\nPREUVE_B04_T0_ISOLATION"), "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201

    # The effective LOCAL_STORAGE_PATH at the time of the call must never be
    # (or be under) the real project data/ directory — only that it landed
    # somewhere else matters here; the second test below proves the guard
    # actively refuses an out-of-bounds root rather than just happening not
    # to be pointed at one.
    effective_root = Path(config.LOCAL_STORAGE_PATH).resolve()
    assert effective_root != REAL_PROJECT_DATA_DIR.resolve()
    assert REAL_PROJECT_DATA_DIR.resolve() not in effective_root.parents

    after_snapshot = set(REAL_PROJECT_DATA_DIR.rglob("*")) if REAL_PROJECT_DATA_DIR.exists() else set()
    new_files = after_snapshot - before_snapshot
    assert not new_files, f"upload leaked into the real project data/ directory: {new_files}"


def test_forbidden_storage_root_is_refused_before_any_write(client, db, monkeypatch):
    """Simulates a misconfigured LOCAL_STORAGE_PATH pointing somewhere
    outside every temp root this test session controls — a synthetic
    stand-in directory (never real project data), created and destroyed by
    this test alone, so nothing pre-existing is at risk. The guard must
    refuse before a single byte lands there."""
    from src.core import config

    forbidden_root = Path(tempfile.mkdtemp(prefix="wm_b04_forbidden_"))
    try:
        monkeypatch.setattr(config, "LOCAL_STORAGE_PATH", forbidden_root)

        make_active_starter_user(db, "b04forbidden@example.com")
        csrf = _login(client, "b04forbidden@example.com")

        with pytest.raises(RuntimeError, match="B04-T0 guard"):
            client.post(
                "/api/knowledge/documents",
                files={"file": ("should_not_land.md", io.BytesIO(b"CONTENU_NE_DOIT_JAMAIS_ETRE_ECRIT"), "text/plain")},
                headers={"X-CSRF-Token": csrf},
            )

        # Refused BEFORE any write: the forbidden root must still be empty.
        assert list(forbidden_root.rglob("*")) == []
    finally:
        import shutil
        shutil.rmtree(forbidden_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Network boundary — proves the block is actually ACTIVE (not just "nothing
# happened to trigger it"), without ever letting a real socket open. Both
# calls below are stopped by tests/conftest.py's module-level patch before
# `requests`/`anthropic` can do anything — no DNS resolution, no connection
# attempt, real or fake target notwithstanding.
# ---------------------------------------------------------------------------

def test_requests_get_is_actually_blocked():
    import requests
    with pytest.raises(RuntimeError, match="B04-T0 guard"):
        requests.get("http://127.0.0.1:1/this-must-never-be-attempted")


def test_requests_post_is_actually_blocked():
    import requests
    with pytest.raises(RuntimeError, match="B04-T0 guard"):
        requests.post("http://127.0.0.1:1/this-must-never-be-attempted")


def test_requests_session_request_is_actually_blocked():
    import requests
    session = requests.Session()
    with pytest.raises(RuntimeError, match="B04-T0 guard"):
        session.request("GET", "http://127.0.0.1:1/this-must-never-be-attempted")


def test_anthropic_client_constructor_is_actually_blocked():
    import anthropic
    with pytest.raises(RuntimeError, match="B04-T0 guard"):
        anthropic.Anthropic(api_key="sk-ant-not-a-real-key")


def test_credentials_are_blanked_in_both_environ_and_config():
    """Belt-and-suspenders: even if the transport patches above were ever
    bypassed by a code path this suite doesn't know about, every provider's
    own `enabled` check (src/agents/llm_providers.py) reads these two
    sources — both must be empty."""
    import os
    from src.core import config

    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MISTRAL_API_KEY", "PAPPERS_API_TOKEN"):
        assert os.environ.get(name, "") == ""
        assert getattr(config, name) == ""
