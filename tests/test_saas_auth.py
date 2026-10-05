"""V3 SaaS — registration, pending/active status, login, logout.

Covers checklist items 1-8 from the V3 spec:
inscription Starter, compte pending, pending refusé sur /app,
activation manuelle, active autorisé, connexion, mauvais mot de passe,
déconnexion.
"""
import re

from tests.conftest import make_active_starter_user


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf token not found in rendered page"
    return match.group(1)


def _register(client, email, password="Sup3rSecret!", **overrides):
    r = client.get("/register")
    payload = {
        "first_name": "Ada", "last_name": "Lovelace", "email": email,
        "company": "", "job_title": "", "phone": "",
        "password": password, "password_confirm": password,
        "accept_terms": "1", "csrf_token": _csrf_from(r.text),
    }
    payload.update(overrides)
    return client.post("/register", data=payload)


def test_register_creates_pending_user_and_starter_subscription(client, db):
    from src.web.database.repositories import subscriptions as subscriptions_repo
    from src.web.database.repositories import users as users_repo

    r = _register(client, "ada@example.com")
    assert r.status_code == 200
    assert "en attente" in r.text.lower()

    user = users_repo.get_by_email(db, "ada@example.com")
    assert user is not None
    assert user.status == "pending"

    subscription = subscriptions_repo.get_latest_for_user(db, user.id)
    assert subscription.plan == "starter"
    assert subscription.status == "pending"


def test_register_duplicate_email_is_rejected(client):
    _register(client, "dup@example.com")
    r = _register(client, "dup@example.com")
    assert "Impossible de créer ce compte" in r.text


def test_register_password_mismatch_is_rejected(client):
    r = client.get("/register")
    payload = {
        "first_name": "A", "last_name": "B", "email": "mismatch@example.com",
        "company": "", "job_title": "", "phone": "",
        "password": "Sup3rSecret!", "password_confirm": "Different!",
        "accept_terms": "1", "csrf_token": _csrf_from(r.text),
    }
    r2 = client.post("/register", data=payload)
    assert "ne correspondent pas" in r2.text


def test_pending_user_is_blocked_from_app(client):
    _register(client, "pending@example.com")
    r = client.get("/app/analyser")
    # resolve_app_access redirects pending users to /account/pending
    assert "en attente" in r.text.lower()
    assert "Nouvelle analyse" not in r.text


def test_active_user_can_access_app(client, db):
    make_active_starter_user(db, "active@example.com")
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": "active@example.com", "password": "Sup3rSecret!", "next": "/app/analyser", "csrf_token": csrf})

    r2 = client.get("/app/analyser")
    assert r2.status_code == 200
    assert "Nouvelle analyse" in r2.text


def test_login_wrong_password_shows_generic_error(client, db):
    make_active_starter_user(db, "wrongpw@example.com")
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    r2 = client.post("/login", data={"email": "wrongpw@example.com", "password": "not-the-password", "next": "/app", "csrf_token": csrf})
    assert "Email ou mot de passe incorrect." in r2.text


def test_rejected_user_gets_same_generic_error_as_wrong_password(client, db):
    from src.web.database.repositories import users as users_repo
    user = make_active_starter_user(db, "rejected@example.com")
    users_repo.set_status(db, user, "rejected")
    db.commit()

    r = client.get("/login")
    csrf = _csrf_from(r.text)
    r2 = client.post("/login", data={"email": "rejected@example.com", "password": "Sup3rSecret!", "next": "/app", "csrf_token": csrf})
    assert "Email ou mot de passe incorrect." in r2.text


def test_activation_script_logic_sets_user_and_subscription_active(client, db):
    """Exercises the same repository calls scripts/activate_user.py runs,
    in one atomic transaction."""
    from src.web.database.repositories import subscriptions as subscriptions_repo
    from src.web.database.repositories import users as users_repo

    _register(client, "toactivate@example.com")
    user = users_repo.get_by_email(db, "toactivate@example.com")
    subscription = subscriptions_repo.get_latest_for_user(db, user.id)

    users_repo.set_status(db, user, "active")
    subscriptions_repo.activate(db, subscription)
    db.commit()

    assert user.status == "active"
    assert subscription.status == "active"
    assert subscription.started_at is not None


def test_logout_clears_session(client, db):
    make_active_starter_user(db, "logout@example.com")
    r = client.get("/login")
    csrf = _csrf_from(r.text)
    client.post("/login", data={"email": "logout@example.com", "password": "Sup3rSecret!", "next": "/account", "csrf_token": csrf})

    r_account = client.get("/account")
    logout_csrf = _csrf_from(r_account.text)
    client.post("/logout", data={"csrf_token": logout_csrf})

    r2 = client.get("/account")
    assert "Connexion" in r2.text  # redirected to the login page
