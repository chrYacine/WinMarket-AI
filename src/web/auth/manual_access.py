"""Server-operator grants share the existing subscription authority, never payment."""
from datetime import datetime, timezone
import hashlib
import secrets
from fastapi import HTTPException
from sqlalchemy import func, select
from src.web.database.models import AccessAudit, AnalysisJob, Membership, Organization, PlatformOperator, Subscription, User

ORIGIN = 'manual_without_payment'


class ManualAccessDenied(HTTPException):
    def __init__(self, message='Activation manuelle requise.', status_code=403):
        super().__init__(status_code, {'error_code':'MANUAL_ACCESS_DENIED','message':message})


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def bootstrap_operator(db, *, actor):
    actor = actor.strip()
    if not actor or db.execute(select(PlatformOperator).where(PlatformOperator.actor == actor)).scalar_one_or_none():
        raise ValueError('An explicit new operator identity is required')
    credential = secrets.token_urlsafe(48)
    row = PlatformOperator(actor=actor, credential_digest=hashlib.sha256(credential.encode()).hexdigest())
    db.add(row); db.flush()
    db.add(AccessAudit(operator_id=row.id, action='bootstrap_operator', target_user_id=None, organization_id=None, reason='Explicit local server bootstrap', details={}))
    return row, credential


def authenticate_operator(db, credential):
    digest = hashlib.sha256(credential.strip().encode()).hexdigest()
    row = db.execute(select(PlatformOperator).where(PlatformOperator.credential_digest == digest, PlatformOperator.active.is_(True))).scalar_one_or_none()
    if row is None:
        raise PermissionError('Server operator credential refused')
    return row


def change_access(db, *, credential, user_id, organization_id, reason, activate, expires_at=None, max_analyses=None):
    actor = authenticate_operator(db, credential)
    if not reason or not reason.strip():
        raise ValueError('A reason is required')
    user = db.execute(select(User).where(User.id == user_id).with_for_update()).scalar_one()
    if activate and user.status not in ('active','pending'):
        raise ValueError('Disabled or rejected account cannot be implicitly restored')
    membership = db.execute(select(Membership).where(Membership.user_id == user.id, Membership.organization_id == organization_id)).scalar_one_or_none()
    org = db.get(Organization, organization_id)
    if org is None or membership is None:
        raise ValueError('Explicit account/organization membership is required')
    if activate and (org.status != 'active' or membership.status != 'active'):
        raise ValueError('Suspended organization or revoked membership cannot be restored by activation')
    sub = db.execute(select(Subscription).where(Subscription.user_id == user.id).order_by(Subscription.created_at.desc()).limit(1).with_for_update()).scalar_one_or_none()
    if sub is None:
        raise ValueError('Account has no pending access record')
    now = datetime.now(timezone.utc)
    if activate:
        if expires_at is None or expires_at.tzinfo is None or expires_at <= now or not isinstance(max_analyses,int) or isinstance(max_analyses,bool) or max_analyses <= 0:
            raise ValueError('Explicit future expiry and positive analysis quota required')
        if sub.access_origin == ORIGIN and sub.status == 'active' and sub.organization_id == organization_id and utc(sub.expires_at) == expires_at and sub.max_analyses == max_analyses:
            return False
        sub.plan = 'starter'; sub.status = 'active'; sub.started_at = now; sub.expires_at = expires_at
        sub.access_origin = ORIGIN; sub.organization_id = organization_id; sub.max_analyses = max_analyses
        user.status = 'active'
    else:
        if sub.organization_id != organization_id or sub.access_origin != ORIGIN:
            raise ValueError('No manual grant for this exact account and organization')
        if sub.status == 'cancelled':
            return False
        sub.status = 'cancelled'
    db.add(AccessAudit(operator_id=actor.id, target_user_id=user.id, organization_id=organization_id, action='activate' if activate else 'revoke', reason=reason.strip(), details={'origin':ORIGIN,'expires_at':sub.expires_at.isoformat() if sub.expires_at else None,'max_analyses':sub.max_analyses}))
    db.flush()
    return True


def check_scope(db, *, user_id, organization_id):
    sub = db.execute(select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.created_at.desc()).limit(1)).scalar_one_or_none()
    if sub and sub.access_origin == ORIGIN and sub.organization_id != organization_id:
        raise ManualAccessDenied('Cet espace ne dispose pas de cette activation.')


def check_job_quota(db, *, user_id, organization_id):
    sub = db.execute(select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.created_at.desc()).limit(1).with_for_update()).scalar_one_or_none()
    if sub is None or sub.access_origin != ORIGIN:
        return  # Existing explicitly provisioned records retain their historical contract.
    from src.web.auth.dependencies import evaluate_subscription_access
    if not evaluate_subscription_access(db, db.get(User,user_id)).granted:
        raise ManualAccessDenied()
    check_scope(db, user_id=user_id, organization_id=organization_id)
    count = db.execute(select(func.count()).select_from(AnalysisJob).where(AnalysisJob.user_id == user_id, AnalysisJob.organization_id == organization_id, AnalysisJob.created_at >= sub.started_at)).scalar_one()
    if sub.max_analyses is None or count >= sub.max_analyses:
        raise ManualAccessDenied('Quota explicite d’analyses atteint pour cette activation.', 429)
