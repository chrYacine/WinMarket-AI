"""V3 SaaS — Business/Enterprise contact requests.

Covers checklist items 13-15: Business vers contact, Enterprise vers
contact, contact enregistré (in PostgreSQL, with CSRF protection).
"""
import re


def _csrf_from(html: str) -> str:
    match = re.search(r'id="contact-csrf" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_pricing_page_links_to_register_and_contact():
    # Static content check — no DB needed, but keep it alongside the other
    # contact/plan tests for discoverability.
    with open("templates/pricing.html", encoding="utf-8") as f:
        content = f.read()
    assert 'href="/register"' in content
    assert 'href="/contact?plan=Business"' in content
    assert 'href="/contact?plan=Enterprise"' in content


def test_contact_prefills_plan_from_query_string(client):
    r = client.get("/contact?plan=Business")
    assert 'value="Business" selected' in r.text


def test_business_contact_request_is_saved(client, db):
    from src.web.database.models import ContactRequest

    r = client.get("/contact?plan=Business")
    csrf = _csrf_from(r.text)
    payload = {
        "first_name": "Jean", "last_name": "Dupont", "email": "jean@example.com",
        "company": "Acme", "job_title": "CTO", "employee_count": 25,
        "plan": "Business", "message": "Nous voulons une démo.", "csrf_token": csrf,
    }
    r2 = client.post("/api/contact", json=payload)
    assert r2.status_code == 200

    saved = db.query(ContactRequest).filter_by(email="jean@example.com").one()
    assert saved.plan == "Business"
    assert saved.status == "new"
    assert saved.employee_count == 25


def test_enterprise_contact_request_is_saved(client, db):
    from src.web.database.models import ContactRequest

    r = client.get("/contact?plan=Enterprise")
    csrf = _csrf_from(r.text)
    payload = {
        "first_name": "Marie", "last_name": "Curie", "email": "marie@example.com",
        "company": "Institut", "job_title": "Directrice", "employee_count": 500,
        "plan": "Enterprise", "message": "Besoin d'un déploiement dédié.", "csrf_token": csrf,
    }
    r2 = client.post("/api/contact", json=payload)
    assert r2.status_code == 200

    saved = db.query(ContactRequest).filter_by(email="marie@example.com").one()
    assert saved.plan == "Enterprise"


def test_contact_without_csrf_token_is_rejected(client):
    payload = {
        "first_name": "X", "last_name": "Y", "email": "x@example.com",
        "message": "hello", "plan": "",
    }
    r = client.post("/api/contact", json=payload)
    assert r.status_code == 403
