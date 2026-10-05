"""Synthetic manual-access lifecycle; no external services or payment."""
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import select, func
from src.web.auth import manual_access as access
from src.web.auth.dependencies import evaluate_subscription_access
from src.web.database.models import AccessAudit, Membership, Organization, Subscription, User
from src.web.database.repositories import analysis_jobs
from tests.test_saas_auth import _register


def pending(client, db):
    _register(client, 'manual@example.test')
    user = db.execute(select(User).where(User.email == 'manual@example.test')).scalar_one()
    member = db.execute(select(Membership).where(Membership.user_id == user.id)).scalar_one()
    _, token = access.bootstrap_operator(db, actor='synthetic-server-operator')
    db.commit()
    return dict(credential=token, user_id=user.id, organization_id=member.organization_id,
                reason='Synthetic explicit grant', activate=True,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2), max_analyses=1)


def test_cookie_activation_revocation_and_audit(client, db):
    args = pending(client, db)
    assert client.get('/api/capacity').status_code == 403
    assert access.change_access(db, **args)
    db.commit()
    assert not access.change_access(db, **args)
    db.commit()
    assert client.get('/api/capacity').status_code == 200
    assert client.post('/api/activate', json={}).status_code == 404
    assert access.change_access(db, **{**args, 'activate': False, 'reason': 'End of synthetic access'})
    db.commit()
    assert client.get('/api/capacity').status_code == 403
    assert not access.change_access(db, **{**args, 'activate': False})
    db.commit()
    events = db.execute(select(AccessAudit).order_by(AccessAudit.created_at)).scalars().all()
    assert [e.action for e in events] == ['bootstrap_operator', 'activate', 'revoke']
    assert all(e.operator_id and e.reason and e.created_at for e in events)
    assert db.get(User, args['user_id']).status == 'active'


def test_expiry_quota_and_scope(client, db):
    args = pending(client, db)
    access.change_access(db, **args)
    db.commit()
    analysis_jobs.create_queued(db, job_id='synthetic-one', user_id=args['user_id'], organization_id=args['organization_id'])
    db.commit()
    with pytest.raises(access.ManualAccessDenied) as error:
        analysis_jobs.create_queued(db, job_id='synthetic-two', user_id=args['user_id'], organization_id=args['organization_id'])
    assert error.value.status_code == 429
    sub = db.execute(select(Subscription).where(Subscription.user_id == args['user_id'])).scalar_one()
    sub.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert not evaluate_subscription_access(db, db.get(User, args['user_id'])).granted
    assert client.get('/api/capacity').status_code == 403


@pytest.mark.parametrize('field,value', [('credential','organization_admin'), ('max_analyses',None), ('expires_at',None), ('reason','')])
def test_no_implicit_operator_or_unbounded_grant(client, db, field, value):
    args = pending(client, db)
    with pytest.raises((ValueError, PermissionError)):
        access.change_access(db, **{**args, field:value})
    assert db.execute(select(func.count()).select_from(AccessAudit).where(AccessAudit.action == 'activate')).scalar_one() == 0
    assert client.get('/api/capacity').status_code == 403


@pytest.mark.parametrize('entity', ['organization','membership'])
def test_activation_cannot_restore_suspended_membership_or_org(client, db, entity):
    args = pending(client, db)
    row = db.get(Organization,args['organization_id']) if entity == 'organization' else db.execute(select(Membership).where(Membership.user_id == args['user_id'])).scalar_one()
    row.status = 'suspended' if entity == 'organization' else 'revoked'
    db.commit()
    with pytest.raises(ValueError):
        access.change_access(db, **args)
    assert row.status != 'active'

def test_manual_grant_does_not_follow_user_into_another_membership(client, db):
    from src.web.database.repositories import organizations, memberships
    args = pending(client, db)
    access.change_access(db, **args)
    other = organizations.create_organization(db,name='Synthetic second space')
    memberships.create_membership(db,user_id=args['user_id'],organization_id=other.id,role='organization_admin',status='active')
    db.commit()
    assert client.get('/api/capacity',params={'organization_id':str(other.id)}).status_code == 403
    assert client.get('/api/capacity',params={'organization_id':str(args['organization_id'])}).status_code == 200


def test_new_account_has_no_demo_configuration_and_active_cookie_respects_membership(client, db):
    from sqlalchemy import text
    args = pending(client, db)
    for table in ('provider_profiles','scoring_policies','private_capacity_plans','knowledge_documents'):
        assert db.execute(text('SELECT count(*) FROM '+table)).scalar_one() == 0
    access.change_access(db, **args)
    db.commit()
    member = db.execute(select(Membership).where(Membership.user_id == args['user_id'])).scalar_one()
    member.status='revoked'
    db.commit()
    assert client.get('/api/capacity').status_code==403
    with pytest.raises(ValueError):
        access.change_access(db, **args)
