"""Migration 0017 preserves a populated predecessor and protects audit on rollback."""
import uuid
import pytest
from sqlalchemy import text
from tests import pg_support
from tests.pg_support import pg_engine, pg_url  # noqa: F401


def test_populated_0016_is_additive_and_idempotent(pg_engine, pg_url):
    pg_support.upgrade(pg_url,'0016')
    uid=str(uuid.uuid4());sid=str(uuid.uuid4())
    with pg_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id,email,password_hash,first_name,last_name,status,created_at,updated_at) VALUES (:id,'legacy-synthetic@example.test','unused','Synthetic','Legacy','active',now(),now())"),{'id':uid})
        conn.execute(text("INSERT INTO subscriptions (id,user_id,plan,status,created_at,updated_at) VALUES (:id,:uid,'starter','active',now(),now())"),{'id':sid,'uid':uid})
    pg_support.upgrade(pg_url,'head')
    pg_support.upgrade(pg_url,'head')
    with pg_engine.connect() as conn:
        row=conn.execute(text('SELECT access_origin,organization_id,max_analyses FROM subscriptions WHERE id=:id'),{'id':sid}).one()
        assert tuple(row)==('legacy',None,None)
        assert conn.execute(text('SELECT count(*) FROM platform_operators')).scalar_one()==0
    # Empty additive fields can be rolled back; there is no audit to discard.
    pg_support.downgrade(pg_url,'0016')
    pg_support.upgrade(pg_url,'head')


def test_operator_audit_forbids_downgrade(pg_engine, pg_url):
    from sqlalchemy.orm import Session
    from src.web.auth.manual_access import bootstrap_operator
    pg_support.upgrade(pg_url,'head')
    with Session(pg_engine) as db:
        bootstrap_operator(db,actor='synthetic-migration-operator')
        db.commit()
    with pytest.raises(RuntimeError,match='refused'):
        pg_support.downgrade(pg_url,'0016')
    with pg_engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM access_audit')).scalar_one()==1
