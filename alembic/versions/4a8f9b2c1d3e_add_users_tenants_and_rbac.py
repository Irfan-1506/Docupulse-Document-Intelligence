"""add_users_tenants_and_rbac

Revision ID: 4a8f9b2c1d3e
Revises: 319357f8a3e9
Create Date: 2026-10-31 15:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4a8f9b2c1d3e'
down_revision: Union[str, Sequence[str], None] = '319357f8a3e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema to add multi-tenancy, enterprise users, RBAC, and hash-chained audit logs."""
    # 1. Create tenants table
    op.create_table(
        'tenants',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('slug', sa.String(length=100), nullable=False),
        sa.Column('status', sa.String(length=50), server_default='ACTIVE', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_tenants_slug'), 'tenants', ['slug'], unique=True)

    # 2. Create users table
    op.create_table(
        'users',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('full_name', sa.String(length=255), server_default='', nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('is_verified', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('failed_login_attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)

    # 3. Create tenant_memberships table
    op.create_table(
        'tenant_memberships',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('role', sa.String(length=50), server_default='VIEWER', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'user_id', name='uq_tenant_user_membership'),
    )
    op.create_index(op.f('ix_tenant_memberships_tenant_id'), 'tenant_memberships', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_tenant_memberships_user_id'), 'tenant_memberships', ['user_id'], unique=False)

    # 4. Create refresh_tokens table
    op.create_table(
        'refresh_tokens',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_refresh_tokens_token_hash'), 'refresh_tokens', ['token_hash'], unique=True)
    op.create_index(op.f('ix_refresh_tokens_user_id'), 'refresh_tokens', ['user_id'], unique=False)

    # 5. Insert default legacy tenant for safe backward-compatible data migration
    op.execute(
        "INSERT INTO tenants (id, name, slug, status) "
        "VALUES ('default-enterprise-tenant', 'Default Enterprise Tenant', 'default-tenant', 'ACTIVE')"
    )

    # 6. Add tenant_id and hash chaining columns to existing tables
    with op.batch_alter_table('documents') as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.String(length=36), server_default='default-enterprise-tenant', nullable=False))
        batch_op.create_index('ix_documents_tenant_id', ['tenant_id'])

    with op.batch_alter_table('processing_jobs') as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.String(length=36), server_default='default-enterprise-tenant', nullable=False))
        batch_op.create_index('ix_processing_jobs_tenant_id', ['tenant_id'])

    with op.batch_alter_table('extracted_records') as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.String(length=36), server_default='default-enterprise-tenant', nullable=False))
        batch_op.create_index('ix_extracted_records_tenant_id', ['tenant_id'])

    with op.batch_alter_table('validation_results') as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.String(length=36), server_default='default-enterprise-tenant', nullable=False))
        batch_op.create_index('ix_validation_results_tenant_id', ['tenant_id'])

    with op.batch_alter_table('document_embeddings') as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.String(length=36), server_default='default-enterprise-tenant', nullable=False))
        batch_op.create_index('ix_document_embeddings_tenant_id', ['tenant_id'])

    with op.batch_alter_table('audit_logs') as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.String(length=36), server_default='default-enterprise-tenant', nullable=False))
        batch_op.add_column(sa.Column('prev_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('record_hash', sa.String(length=64), nullable=True))
        batch_op.create_index('ix_audit_logs_tenant_id', ['tenant_id'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('audit_logs') as batch_op:
        batch_op.drop_index('ix_audit_logs_tenant_id')
        batch_op.drop_column('record_hash')
        batch_op.drop_column('prev_hash')
        batch_op.drop_column('tenant_id')

    with op.batch_alter_table('document_embeddings') as batch_op:
        batch_op.drop_index('ix_document_embeddings_tenant_id')
        batch_op.drop_column('tenant_id')

    with op.batch_alter_table('validation_results') as batch_op:
        batch_op.drop_index('ix_validation_results_tenant_id')
        batch_op.drop_column('tenant_id')

    with op.batch_alter_table('extracted_records') as batch_op:
        batch_op.drop_index('ix_extracted_records_tenant_id')
        batch_op.drop_column('tenant_id')

    with op.batch_alter_table('processing_jobs') as batch_op:
        batch_op.drop_index('ix_processing_jobs_tenant_id')
        batch_op.drop_column('tenant_id')

    with op.batch_alter_table('documents') as batch_op:
        batch_op.drop_index('ix_documents_tenant_id')
        batch_op.drop_column('tenant_id')

    op.drop_table('refresh_tokens')
    op.drop_table('tenant_memberships')
    op.drop_table('users')
    op.drop_table('tenants')
