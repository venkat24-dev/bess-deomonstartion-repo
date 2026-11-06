"""Add immutable project history, audit events and scoped access controls.

Revision ID: 20261028_0004
Revises: 20261012_0003
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261028_0004"
down_revision = "20261012_0003"
branch_labels = None
depends_on = None

SCHEMA = "bess"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM bess.organization_members
                WHERE role = 'owner'
                GROUP BY organization_id HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Multiple organization owners exist; reconcile ownership before revision 20261028_0004.';
            END IF;
            IF EXISTS (
                SELECT 1 FROM bess.project_members
                WHERE role = 'owner'
                GROUP BY project_id HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Multiple project owners exist; reconcile ownership before revision 20261028_0004.';
            END IF;
        END
        $$;
        """
    )

    op.add_column(
        "projects",
        sa.Column("current_version_number", sa.Integer(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "projects",
        sa.Column(
            "access_state",
            sa.String(length=30),
            server_default="ownership_review",
            nullable=False,
        ),
        schema=SCHEMA,
    )
    op.alter_column(
        "projects",
        "access_state",
        server_default="active",
        existing_type=sa.String(length=30),
        schema=SCHEMA,
    )
    op.add_column(
        "projects",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "projects",
        sa.Column("archived_by_user_id", sa.Uuid(), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "valid_access_state",
        "projects",
        "access_state IN ('active', 'ownership_review')",
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_projects_archived_by_user_id_users",
        "projects",
        "users",
        ["archived_by_user_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_projects_access_state",
        "projects",
        ["access_state"],
        schema=SCHEMA,
        postgresql_where=sa.text("access_state = 'ownership_review'"),
    )

    op.drop_constraint(
        "ck_organization_members_valid_role", "organization_members", schema=SCHEMA
    )
    op.create_check_constraint(
        "valid_role",
        "organization_members",
        "role IN ('owner', 'admin', 'manager', 'member')",
        schema=SCHEMA,
    )
    op.create_index(
        "uq_organization_members_one_owner",
        "organization_members",
        ["organization_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("role = 'owner'"),
    )
    op.create_index(
        "uq_project_members_one_owner",
        "project_members",
        ["project_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("role = 'owner'"),
    )

    op.create_table(
        "project_versions",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("input_schema_version", sa.String(length=32), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "selected_container_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("catalog_revision_id", sa.Uuid(), nullable=True),
        sa.Column("source_catalog_sha256", sa.String(length=64), nullable=False),
        sa.Column("engine_version", sa.String(length=100), nullable=False),
        sa.Column("source_version_number", sa.Integer(), nullable=True),
        sa.Column("change_summary", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("version_number > 0", name="positive_version_number"),
        sa.CheckConstraint(
            "input_schema_version <> '' AND length(input_schema_version) <= 32",
            name="valid_input_schema_version",
        ),
        sa.CheckConstraint(
            "length(source_catalog_sha256) = 64",
            name="source_catalog_sha256_length",
        ),
        sa.CheckConstraint(
            "length(engine_version) > 0",
            name="engine_version_nonempty",
        ),
        sa.CheckConstraint(
            "source_version_number IS NULL OR source_version_number < version_number",
            name="valid_source_version",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(input_snapshot) = 'object'",
            name="input_snapshot_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(result_snapshot) = 'object'",
            name="result_snapshot_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(selected_container_snapshot) = 'object'",
            name="selected_container_snapshot_object",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            [f"{SCHEMA}.projects.id"],
            name="fk_project_versions_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_project_versions_created_by_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_revision_id"],
            [f"{SCHEMA}.catalog_revisions.id"],
            name="fk_project_versions_catalog_revision_id_catalog_revisions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_version_number"],
            [
                f"{SCHEMA}.project_versions.project_id",
                f"{SCHEMA}.project_versions.version_number",
            ],
            name="fk_project_versions_source_version",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_project_versions"),
        sa.UniqueConstraint(
            "project_id", "version_number", name="uq_project_version_number"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_project_versions_created",
        "project_versions",
        ["project_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_projects_current_version",
        "projects",
        "project_versions",
        ["id", "current_version_number"],
        ["project_id", "version_number"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(
        """
        CREATE FUNCTION bess.enforce_project_owner()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            checked_project_id uuid;
            owner_count integer;
            valid_owner_count integer;
        BEGIN
            IF TG_TABLE_NAME = 'projects' THEN
                checked_project_id := COALESCE(NEW.id, OLD.id);
            ELSE
                checked_project_id := COALESCE(NEW.project_id, OLD.project_id);
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM bess.projects
                WHERE id = checked_project_id AND access_state = 'active'
            ) THEN
                RETURN NULL;
            END IF;
            SELECT count(*) INTO owner_count
            FROM bess.project_members
            WHERE project_id = checked_project_id AND role = 'owner';
            SELECT count(*) INTO valid_owner_count
            FROM bess.project_members pm
            JOIN bess.projects p ON p.id = pm.project_id
            JOIN bess.organization_members om
              ON om.organization_id = p.organization_id AND om.user_id = pm.user_id
            JOIN bess.users u ON u.id = pm.user_id AND u.is_active
            WHERE pm.project_id = checked_project_id AND pm.role = 'owner';
            IF owner_count <> 1 OR valid_owner_count <> 1 THEN
                RAISE EXCEPTION
                    'Active projects require exactly one owner who is an organization member';
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER projects_require_owner
        AFTER INSERT OR UPDATE ON bess.projects
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION bess.enforce_project_owner();
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER project_members_require_owner
        AFTER INSERT OR UPDATE OR DELETE ON bess.project_members
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION bess.enforce_project_owner();
        """
    )
    op.execute(
        """
        CREATE FUNCTION bess.enforce_project_owner_membership()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            checked_project_id uuid;
            owner_count integer;
            valid_owner_count integer;
        BEGIN
            FOR checked_project_id IN
                SELECT p.id
                FROM bess.projects p
                JOIN bess.project_members pm ON pm.project_id = p.id
                WHERE p.organization_id = COALESCE(NEW.organization_id, OLD.organization_id)
                  AND pm.user_id = COALESCE(NEW.user_id, OLD.user_id)
                  AND pm.role = 'owner'
                  AND p.access_state = 'active'
            LOOP
                SELECT count(*) INTO owner_count
                FROM bess.project_members
                WHERE project_id = checked_project_id AND role = 'owner';
                SELECT count(*) INTO valid_owner_count
                FROM bess.project_members pm
                JOIN bess.projects p ON p.id = pm.project_id
                JOIN bess.organization_members om
                  ON om.organization_id = p.organization_id AND om.user_id = pm.user_id
                JOIN bess.users u ON u.id = pm.user_id AND u.is_active
                WHERE pm.project_id = checked_project_id AND pm.role = 'owner';
                IF owner_count <> 1 OR valid_owner_count <> 1 THEN
                    RAISE EXCEPTION
                        'Active projects require exactly one active organization-member owner';
                END IF;
            END LOOP;
            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER organization_members_preserve_project_owner
        AFTER UPDATE OR DELETE ON bess.organization_members
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION bess.enforce_project_owner_membership();
        """
    )
    op.execute(
        """
        CREATE FUNCTION bess.enforce_active_project_owner()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.is_active OR NOT EXISTS (
                SELECT 1
                FROM bess.project_members pm
                JOIN bess.projects p ON p.id = pm.project_id
                WHERE pm.user_id = NEW.id
                  AND pm.role = 'owner'
                  AND p.access_state = 'active'
            ) THEN
                RETURN NULL;
            END IF;
            RAISE EXCEPTION
                'An active project owner cannot be deactivated before ownership is transferred';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER users_preserve_active_project_owner
        AFTER UPDATE ON bess.users
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION bess.enforce_active_project_owner();
        """
    )

    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=40), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("length(action) > 0", name="action_nonempty"),
        sa.CheckConstraint(
            "length(resource_type) > 0",
            name="resource_type_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_audit_events_actor_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_events_resource_created",
        "audit_events",
        ["resource_type", "resource_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_audit_events_actor_created",
        "audit_events",
        ["actor_user_id", "created_at"],
        schema=SCHEMA,
    )
    op.execute(
        """
        CREATE FUNCTION bess.reject_audit_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_row_mutation
        BEFORE UPDATE OR DELETE ON bess.audit_events
        FOR EACH ROW EXECUTE FUNCTION bess.reject_audit_event_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_truncate
        BEFORE TRUNCATE ON bess.audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION bess.reject_audit_event_mutation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION bess.reject_project_version_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'project_versions are immutable';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_versions_no_row_mutation
        BEFORE UPDATE OR DELETE ON bess.project_versions
        FOR EACH ROW EXECUTE FUNCTION bess.reject_project_version_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER project_versions_no_truncate
        BEFORE TRUNCATE ON bess.project_versions
        FOR EACH STATEMENT EXECUTE FUNCTION bess.reject_project_version_mutation();
        """
    )

    op.create_table(
        "project_access_grants",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("granted_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("access_scope", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "access_scope IN ('management', 'content_read')",
            name="valid_access_scope",
        ),
        sa.CheckConstraint(
            "length(reason) > 0",
            name="reason_nonempty",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name="valid_expiration",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            [f"{SCHEMA}.projects.id"],
            name="fk_project_access_grants_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_project_access_grants_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"],
            [f"{SCHEMA}.users.id"],
            name="fk_project_access_grants_granted_by_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_project_access_grants"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_project_access_grants_user",
        "project_access_grants",
        ["user_id", "project_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "uq_project_access_grants_active",
        "project_access_grants",
        ["project_id", "user_id", "access_scope"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.drop_index(
        "uq_sizing_requests_idempotency_key",
        table_name="sizing_requests",
        schema=SCHEMA,
    )
    op.create_index(
        "uq_sizing_requests_user_idempotency_key",
        "sizing_requests",
        ["created_by_user_id", "idempotency_key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text(
            "created_by_user_id IS NOT NULL AND idempotency_key IS NOT NULL"
        ),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Revision 20261028_0004 contains immutable project and audit history and "
        "is not safely reversible by dropping those records."
    )
