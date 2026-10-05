"""B22-T1 (DEFECT confirmed): src/web/database/repositories/analyses.py::
list_for_user hardcoded `limit: int = 200` with no offset/pagination at
all. Every consumer called it directly:
- src/web/services/history_service.py::sidebar_stats counted GO/RESERVE/
  NO-GO from that SAME capped list — an account with more than 200
  analyses got silently WRONG stats (undercounted).
- /app/historique's page and /api/history/export.csv both silently
  truncated at the 200 most recent analyses with no error, no truncation
  notice, and no way to reach anything older.

Fixed with real SQL pagination (analyses_repo.list_for_user_page,
deterministic order: created_at DESC, id DESC), a real COUNT(*)
(count_for_user) and a SQL GROUP BY aggregate (decision_counts_for_user)
that both reflect the FULL authorized set regardless of page/limit size,
and a batched, unbounded export generator (history_service.
iter_for_export) with CSV formula-injection protection applied only at
the exported cell, never to the stored row.
"""
from __future__ import annotations

import csv
import io
import re
import uuid

from src.web.database.repositories import analyses as analyses_repo
from src.web.services import history_service
from tests.conftest import default_org_id, make_active_starter_user

N_ANALYSES = 201


def _seed_analyses(db, user, org_id, count: int, *, title_prefix: str = "AO"):
    for i in range(count):
        analyses_repo.create_analysis(
            db, user_id=user.id, organization_id=org_id,
            job_id=f"{title_prefix.lower()}-job-{i:04d}",
            title=f"{title_prefix} synthétique {i:04d}",
            client_name="Client Synthétique", sector="Retail",
            score=50.0 + (i % 50), decision=("GO" if i % 3 == 0 else ("NO-GO" if i % 3 == 1 else "GO SOUS RESERVE")),
            budget="100000", technologies=["Python"],
            result_data={"ao": {}, "result": {}},
        )
    db.commit()


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client, email, password="Sup3rSecret!"):
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": email, "password": password, "next": "/app", "csrf_token": csrf})
    return csrf


def test_pagination_covers_all_201_with_no_duplicate_and_no_foreign_row(client, db):
    user = make_active_starter_user(db, "history201@example.com", scoring=False)
    org_id = default_org_id(db, user)
    _seed_analyses(db, user, org_id, N_ANALYSES, title_prefix="Mine")

    other_user = make_active_starter_user(db, "historyother@example.com", scoring=False)
    other_org = default_org_id(db, other_user)
    _seed_analyses(db, other_user, other_org, 5, title_prefix="Other")

    _login(client, "history201@example.com")

    seen_job_ids: set[str] = set()
    page = 1
    total_reported = None
    while True:
        r = client.get(f"/api/history?page={page}&page_size=50")
        assert r.status_code == 200
        body = r.json()
        if total_reported is None:
            total_reported = body["total"]
        assert body["total"] == N_ANALYSES, "total must reflect the FULL authorized set, not a page/limit slice"
        items = body["items"]
        if not items:
            break
        for item in items:
            job_id = item["job_id"]
            assert job_id not in seen_job_ids, f"duplicate row across pages: {job_id}"
            assert "other-job-" not in (job_id or ""), "another account's row leaked into this listing"
            seen_job_ids.add(job_id)
        if page >= body["total_pages"]:
            break
        page += 1

    assert len(seen_job_ids) == N_ANALYSES, f"expected exactly {N_ANALYSES} distinct rows, got {len(seen_job_ids)}"


def test_history_page_size_is_capped_never_an_implicit_full_export(client, db):
    user = make_active_starter_user(db, "historycap@example.com", scoring=False)
    org_id = default_org_id(db, user)
    _seed_analyses(db, user, org_id, N_ANALYSES, title_prefix="Cap")
    _login(client, "historycap@example.com")

    r = client.get("/api/history?page=1&page_size=999999")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) <= 100, "page_size must be capped, not an implicit bulk-export vector"
    assert body["total"] == N_ANALYSES


def test_sidebar_stats_reflect_the_full_set_not_a_200_row_cap(db):
    user = make_active_starter_user(db, "historystats@example.com", scoring=False)
    org_id = default_org_id(db, user)
    _seed_analyses(db, user, org_id, N_ANALYSES, title_prefix="Stat")

    stats = history_service.sidebar_stats(db, user.id, org_id)
    assert stats["total"] == N_ANALYSES, "the confirmed defect: stats used to be silently capped at 200"
    assert stats["go"] + stats["nogo"] + stats["reserve"] == N_ANALYSES


def test_export_csv_contains_exactly_201_rows_and_no_foreign_row(client, db):
    user = make_active_starter_user(db, "historyexport@example.com", scoring=False)
    org_id = default_org_id(db, user)
    _seed_analyses(db, user, org_id, N_ANALYSES, title_prefix="Exp")

    other_user = make_active_starter_user(db, "historyexportother@example.com", scoring=False)
    other_org = default_org_id(db, other_user)
    _seed_analyses(db, other_user, other_org, 3, title_prefix="ExpOther")

    _login(client, "historyexport@example.com")
    r = client.get("/api/history/export.csv")
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == N_ANALYSES, f"export must be complete — expected {N_ANALYSES} rows, got {len(rows)}"
    assert all("ExpOther" not in row["titre"] for row in rows), "another account's row leaked into the export"


def test_export_csv_neutralizes_a_formula_looking_cell_without_touching_the_stored_row(client, db):
    user = make_active_starter_user(db, "historyformula@example.com", scoring=False)
    org_id = default_org_id(db, user)
    dangerous_title = "=cmd|'/c calc'!A1"
    analyses_repo.create_analysis(
        db, user_id=user.id, organization_id=org_id, job_id="formula-job-1",
        title=dangerous_title, client_name="=HYPERLINK(\"http://evil\",\"click\")",
        sector="Retail", score=80.0, decision="GO", budget="100000", technologies=["Python"],
        result_data={"ao": {}, "result": {}},
    )
    db.commit()

    _login(client, "historyformula@example.com")
    r = client.get("/api/history/export.csv")
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 1
    assert rows[0]["titre"].startswith("'="), "a formula-looking cell must be neutralized in the export"
    assert rows[0]["client"].startswith("'="), "a formula-looking cell must be neutralized in the export"

    # The STORED row is untouched by the export — re-read it fresh.
    stored = analyses_repo.get_by_job_id_for_user(db, "formula-job-1", user.id)
    assert stored.title == dangerous_title, "export-time neutralization must never modify the stored value"
    assert stored.client_name == "=HYPERLINK(\"http://evil\",\"click\")"


def test_a_safe_cell_is_never_altered():
    assert history_service.neutralize_csv_cell("Portail client Acme") == "Portail client Acme"
    assert history_service.neutralize_csv_cell("") == ""


def test_pagination_is_deterministic_across_two_identical_reads(db):
    user = make_active_starter_user(db, "historydeterm@example.com", scoring=False)
    org_id = default_org_id(db, user)
    _seed_analyses(db, user, org_id, 30, title_prefix="Det")

    page_a, total_a = history_service.list_for_user_page(db, user.id, org_id, page=2, page_size=10)
    page_b, total_b = history_service.list_for_user_page(db, user.id, org_id, page=2, page_size=10)
    assert total_a == total_b == 30
    assert [r["job_id"] for r in page_a] == [r["job_id"] for r in page_b]
