"""add inspector_profiles and payout_requests tables

Revision ID: 84a2214a46f3
Revises: 400c8f2fa42d
Create Date: 2026-08-14 08:11:20.910907

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '84a2214a46f3'
down_revision: Union[str, Sequence[str], None] = '400c8f2fa42d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — only the two new inspector tables."""
    op.create_table('inspector_profiles',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.String(length=50), nullable=False),
    sa.Column('sex', sa.String(length=10), nullable=True),
    sa.Column('bvn', sa.String(length=11), nullable=True),
    sa.Column('nin', sa.String(length=11), nullable=True),
    sa.Column('kyc_verified', sa.Boolean(), nullable=True),
    sa.Column('bank_account_number', sa.String(length=10), nullable=True),
    sa.Column('bank_name', sa.String(length=100), nullable=True),
    sa.Column('bank_account_name', sa.String(length=150), nullable=True),
    sa.Column('liveness_verified', sa.Boolean(), nullable=True),
    sa.Column('liveness_photo_url', sa.String(length=500), nullable=True),
    sa.Column('registration_stage', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_inspector_profile_stage', 'inspector_profiles', ['registration_stage'], unique=False)
    op.create_index(op.f('ix_inspector_profiles_user_id'), 'inspector_profiles', ['user_id'], unique=True)

    op.create_table('payout_requests',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('payout_id', sa.String(length=50), nullable=False),
    sa.Column('inspector_id', sa.String(length=50), nullable=False),
    sa.Column('amount_ngn', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('dcp_count', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('processed_by', sa.String(length=50), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['inspector_id'], ['users.user_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_payout_inspector', 'payout_requests', ['inspector_id'], unique=False)
    op.create_index('idx_payout_status', 'payout_requests', ['status'], unique=False)
    op.create_index(op.f('ix_payout_requests_inspector_id'), 'payout_requests', ['inspector_id'], unique=False)
    op.create_index(op.f('ix_payout_requests_payout_id'), 'payout_requests', ['payout_id'], unique=True)


def downgrade() -> None:
    """Downgrade schema — drop only the two new inspector tables."""
    op.drop_index(op.f('ix_payout_requests_payout_id'), table_name='payout_requests')
    op.drop_index(op.f('ix_payout_requests_inspector_id'), table_name='payout_requests')
    op.drop_index('idx_payout_status', table_name='payout_requests')
    op.drop_index('idx_payout_inspector', table_name='payout_requests')
    op.drop_table('payout_requests')

    op.drop_index(op.f('ix_inspector_profiles_user_id'), table_name='inspector_profiles')
    op.drop_index('idx_inspector_profile_stage', table_name='inspector_profiles')
    op.drop_table('inspector_profiles')