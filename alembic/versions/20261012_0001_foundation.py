"""Create the initial BESS application foundation schema.

Revision ID: 20261012_0001
Revises:
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateSchema

revision = "20261012_0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "bess"


def upgrade() -> None:
    op.execute(CreateSchema(SCHEMA, if_not_exists=True))

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("oidc_issuer", sa.String(length=500), nullable=False),
        sa.Column("oidc_subject", sa.String(length=500), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=300), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("oidc_issuer", "oidc_subject", name="uq_users_oidc_identity"),
        schema=SCHEMA,
    )
    op.create_index("ix_users_email", "users", ["email"], schema=SCHEMA)

    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], [f"{SCHEMA}.users.id"],
            name="fk_organizations_created_by_user_id_users", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_organizations"),
        schema=SCHEMA,
    )

    op.create_table(
        "organization_members",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name="valid_role"),
        sa.ForeignKeyConstraint(
            ["organization_id"], [f"{SCHEMA}.organizations.id"],
            name="fk_organization_members_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], [f"{SCHEMA}.users.id"],
            name="fk_organization_members_user_id_users", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("organization_id", "user_id", name="pk_organization_members"),
        schema=SCHEMA,
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version >= 1", name="positive_version"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], [f"{SCHEMA}.users.id"],
            name="fk_projects_created_by_user_id_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], [f"{SCHEMA}.organizations.id"],
            name="fk_projects_organization_id_organizations", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_projects_organization_updated", "projects", ["organization_id", "updated_at"], schema=SCHEMA
    )

    op.create_table(
        "project_members",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="valid_role"),
        sa.ForeignKeyConstraint(
            ["project_id"], [f"{SCHEMA}.projects.id"],
            name="fk_project_members_project_id_projects", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], [f"{SCHEMA}.users.id"],
            name="fk_project_members_user_id_users", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id", "user_id", name="pk_project_members"),
        schema=SCHEMA,
    )
    op.create_index("ix_project_members_user", "project_members", ["user_id"], schema=SCHEMA)

    op.create_table(
        "catalog_revisions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_filename", sa.String(length=500), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("imported_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint("length(source_sha256) = 64", name="sha256_length"),
        sa.ForeignKeyConstraint(
            ["imported_by_user_id"], [f"{SCHEMA}.users.id"],
            name="fk_catalog_revisions_imported_by_user_id_users", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_catalog_revisions"),
        sa.UniqueConstraint("source_sha256", name="uq_catalog_revisions_source_sha256"),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_catalog_revisions_active", "catalog_revisions", ["is_active"],
        unique=True, schema=SCHEMA, postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "container_models",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("catalog_revision_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(length=200), nullable=False),
        sa.Column("source_sheet", sa.String(length=200), nullable=False),
        sa.Column("energy_mwh", sa.Numeric(18, 6), nullable=False),
        sa.Column("c_rate", sa.Numeric(12, 8), nullable=False),
        sa.Column("cycles_per_day", sa.Numeric(12, 6), nullable=False),
        sa.Column("aux_discharge_mw", sa.Numeric(18, 8), nullable=True),
        sa.Column("aux_charge_mw", sa.Numeric(18, 8), nullable=True),
        sa.Column("aux_standby_mw", sa.Numeric(18, 8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("energy_mwh > 0", name="positive_energy"),
        sa.CheckConstraint("c_rate > 0", name="positive_c_rate"),
        sa.CheckConstraint("cycles_per_day > 0", name="positive_cycles_per_day"),
        sa.CheckConstraint(
            "aux_discharge_mw IS NULL OR aux_discharge_mw >= 0",
            name="nonnegative_aux_discharge",
        ),
        sa.CheckConstraint(
            "aux_charge_mw IS NULL OR aux_charge_mw >= 0",
            name="nonnegative_aux_charge",
        ),
        sa.CheckConstraint(
            "aux_standby_mw IS NULL OR aux_standby_mw >= 0",
            name="nonnegative_aux_standby",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_revision_id"], [f"{SCHEMA}.catalog_revisions.id"],
            name="fk_container_models_catalog_revision_id_catalog_revisions", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_container_models"),
        sa.UniqueConstraint(
            "catalog_revision_id", "source_key", name="uq_container_revision_source"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_container_models_energy_c_rate", "container_models", ["energy_mwh", "c_rate"], schema=SCHEMA
    )

    op.create_table(
        "sizing_requests",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("parent_request_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=30), server_default=sa.text("'api'"), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'DRAFT'"), nullable=False),
        sa.Column("scenario_name", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'SUBMITTED', 'COMPLETED', 'FAILED')",
            name="valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], [f"{SCHEMA}.users.id"],
            name="fk_sizing_requests_created_by_user_id_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_request_id"], [f"{SCHEMA}.sizing_requests.id"],
            name="fk_sizing_requests_parent_request_id_sizing_requests", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], [f"{SCHEMA}.projects.id"],
            name="fk_sizing_requests_project_id_projects", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sizing_requests"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_sizing_requests_project_created", "sizing_requests", ["project_id", "created_at"], schema=SCHEMA
    )
    op.create_index(
        "ix_sizing_requests_parent", "sizing_requests", ["parent_request_id"], schema=SCHEMA
    )

    op.create_table(
        "simulation_runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("container_model_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'QUEUED'"), nullable=False),
        sa.Column("engine_version", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("initial_container_count", sa.Integer(), nullable=True),
        sa.Column("margin_applied_pct", sa.Numeric(9, 4), nullable=True),
        sa.Column("in_range", sa.Boolean(), nullable=True),
        sa.Column("accepted", sa.Boolean(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="valid_status",
        ),
        sa.CheckConstraint(
            "initial_container_count IS NULL OR initial_container_count > 0",
            name="positive_container_count",
        ),
        sa.ForeignKeyConstraint(
            ["container_model_id"], [f"{SCHEMA}.container_models.id"],
            name="fk_simulation_runs_container_model_id_container_models", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"], [f"{SCHEMA}.sizing_requests.id"],
            name="fk_simulation_runs_request_id_sizing_requests", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_simulation_runs"),
        sa.UniqueConstraint("request_id", "container_model_id", name="uq_run_request_container"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_simulation_runs_status_created", "simulation_runs", ["status", "created_at"], schema=SCHEMA
    )
    op.create_index(
        "ix_simulation_runs_container_created",
        "simulation_runs",
        ["container_model_id", "created_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_simulation_runs_container_created", table_name="simulation_runs", schema=SCHEMA)
    op.drop_index("ix_simulation_runs_status_created", table_name="simulation_runs", schema=SCHEMA)
    op.drop_table("simulation_runs", schema=SCHEMA)
    op.drop_index("ix_sizing_requests_parent", table_name="sizing_requests", schema=SCHEMA)
    op.drop_index("ix_sizing_requests_project_created", table_name="sizing_requests", schema=SCHEMA)
    op.drop_table("sizing_requests", schema=SCHEMA)
    op.drop_index("ix_container_models_energy_c_rate", table_name="container_models", schema=SCHEMA)
    op.drop_table("container_models", schema=SCHEMA)
    op.drop_index("uq_catalog_revisions_active", table_name="catalog_revisions", schema=SCHEMA)
    op.drop_table("catalog_revisions", schema=SCHEMA)
    op.drop_index("ix_project_members_user", table_name="project_members", schema=SCHEMA)
    op.drop_table("project_members", schema=SCHEMA)
    op.drop_index("ix_projects_organization_updated", table_name="projects", schema=SCHEMA)
    op.drop_table("projects", schema=SCHEMA)
    op.drop_table("organization_members", schema=SCHEMA)
    op.drop_table("organizations", schema=SCHEMA)
    op.drop_index("ix_users_email", table_name="users", schema=SCHEMA)
    op.drop_table("users", schema=SCHEMA)
