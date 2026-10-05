"""V3 SaaS — CLI validation scripts (list/activate/reject/disable/create_demo_user)."""
import io
import sys

from tests.conftest import make_active_starter_user


def _register_pending(db, email):
    from src.web.auth import service as auth_service

    user = auth_service.register_starter_user(
        db, email=email, password="Sup3rSecret!", password_confirm="Sup3rSecret!",
        first_name="Test", last_name="User",
    )
    db.commit()
    return user


def test_list_pending_users_lists_only_pending(db, test_db, capsys, monkeypatch):
    import scripts.list_pending_users as list_pending

    _register_pending(db, "pending1@example.com")
    make_active_starter_user(db, "active1@example.com")  # must NOT show up

    monkeypatch.setattr(sys, "argv", ["list_pending_users.py"])
    list_pending.main()
    out = capsys.readouterr().out
    assert "pending1@example.com" in out
    assert "active1@example.com" not in out


def test_activate_user_script(db, test_db, monkeypatch):
    import scripts.activate_user as activate

    _register_pending(db, "toactivate@example.com")
    monkeypatch.setattr(sys, "argv", ["activate_user.py", "toactivate@example.com"])
    import pytest
    with pytest.raises(SystemExit, match="operator_access.py"):
        activate.main()
    from src.web.database.repositories import subscriptions as subscriptions_repo
    from src.web.database.repositories import users as users_repo
    user = users_repo.get_by_email(db, "toactivate@example.com")
    subscription = subscriptions_repo.get_latest_for_user(db, user.id)
    assert user.status == "pending"
    assert subscription.status == "pending"
    assert subscription.started_at is None


def test_activate_unknown_email_exits_with_error(test_db, monkeypatch):
    import scripts.activate_user as activate

    monkeypatch.setattr(sys, "argv", ["activate_user.py", "nobody@example.com"])
    try:
        activate.main()
        raised = False
    except SystemExit:
        raised = True
    assert raised


def test_reject_user_script(db, test_db, monkeypatch):
    import scripts.reject_user as reject

    _register_pending(db, "torejeect@example.com")
    monkeypatch.setattr(sys, "argv", ["reject_user.py", "torejeect@example.com"])
    reject.main()

    from src.web.database.repositories import users as users_repo
    user = users_repo.get_by_email(db, "torejeect@example.com")
    assert user.status == "rejected"


def test_disable_user_script(db, test_db, monkeypatch):
    import scripts.disable_user as disable

    make_active_starter_user(db, "todisable@example.com")
    monkeypatch.setattr(sys, "argv", ["disable_user.py", "todisable@example.com"])
    disable.main()

    from src.web.database.repositories import users as users_repo
    user = users_repo.get_by_email(db, "todisable@example.com")
    assert user.status == "disabled"


def test_disabled_user_blocked_from_login(client, db):
    from src.web.database.repositories import users as users_repo
    import re

    user = make_active_starter_user(db, "willbedisabled@example.com")
    users_repo.set_status(db, user, "disabled")
    db.commit()

    r = client.get("/login")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
    r2 = client.post("/login", data={"email": "willbedisabled@example.com", "password": "Sup3rSecret!", "next": "/app", "csrf_token": csrf})
    assert "Email ou mot de passe incorrect." in r2.text


def test_create_demo_user_pure_logic(db, test_db):
    from scripts.create_demo_user import DemoUserError, create_active_starter_account
    from src.web.database.repositories import subscriptions as subscriptions_repo

    user = create_active_starter_account(
        db, email="demo@winmarket.local", password="DemoPassw0rd!", first_name="Synthetic", last_name="Operator",
    )
    db.commit()
    assert user.status == "active"
    subscription = subscriptions_repo.get_latest_for_user(db, user.id)
    assert subscription.plan == "starter" and subscription.status == "active"

    raised = False
    try:
        create_active_starter_account(db, email="demo@winmarket.local", password="DemoPassw0rd!", first_name="X", last_name="Y")
    except DemoUserError:
        raised = True
    assert raised
