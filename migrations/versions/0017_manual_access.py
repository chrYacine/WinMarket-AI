"""Add explicit manual grants and server-operator audit without payment.

Revision ID: 0017
Revises: 0016
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0017'
down_revision = '0016'
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table('subscriptions') as batch:
        batch.add_column(sa.Column('access_origin',sa.String(40),nullable=False,server_default='legacy'))
        batch.add_column(sa.Column('organization_id',sa.Uuid(),nullable=True))
        batch.add_column(sa.Column('max_analyses',sa.Integer(),nullable=True))
        batch.create_foreign_key('fk_subscriptions_manual_org','organizations',['organization_id'],['id'],ondelete='RESTRICT')
    op.create_table('platform_operators',
        sa.Column('id',sa.Uuid(),primary_key=True),sa.Column('actor',sa.String(200),nullable=False,unique=True),
        sa.Column('credential_digest',sa.String(64),nullable=False,unique=True),sa.Column('active',sa.Boolean(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_table('access_audit',
        sa.Column('id',sa.Uuid(),primary_key=True),sa.Column('operator_id',sa.Uuid(),sa.ForeignKey('platform_operators.id',ondelete='RESTRICT'),nullable=False),
        sa.Column('target_user_id',sa.Uuid(),sa.ForeignKey('users.id',ondelete='RESTRICT')),
        sa.Column('organization_id',sa.Uuid(),sa.ForeignKey('organizations.id',ondelete='RESTRICT')),
        sa.Column('action',sa.String(40),nullable=False),sa.Column('reason',sa.Text(),nullable=False),
        sa.Column('details',sa.JSON().with_variant(JSONB(),'postgresql'),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))


def downgrade():
    connection = op.get_bind()
    occupied = any(connection.execute(sa.text(query)).scalar() for query in (
        "SELECT count(*) FROM access_audit", "SELECT count(*) FROM platform_operators",
        "SELECT count(*) FROM subscriptions WHERE access_origin <> 'legacy'"))
    if occupied:
        raise RuntimeError('Downgrade of 0017 refused: manual access or audit exists; restore a verified backup')
    op.drop_table('access_audit')
    op.drop_table('platform_operators')
    with op.batch_alter_table('subscriptions') as batch:
        batch.drop_constraint('fk_subscriptions_manual_org',type_='foreignkey')
        batch.drop_column('max_analyses')
        batch.drop_column('organization_id')
        batch.drop_column('access_origin')
