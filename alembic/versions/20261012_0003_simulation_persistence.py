"""Add snapshots and results to the existing simulation records.

Revision ID: 20261012_0003
Revises: 20261012_0002
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261012_0003"
down_revision = "20261012_0002"
branch_labels = None
depends_on = None

SCHEMA = "bess"


def upgrade() -> None:
    op.add_column(
        "sizing_requests",
        sa.Column("input_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "sizing_requests",
        sa.Column("input_schema_version", sa.String(length=32), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "sizing_requests",
        sa.Column("application_version", sa.String(length=100), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "sizing_requests",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_sizing_requests_input_snapshot_object",
        "sizing_requests",
        "input_snapshot IS NULL OR jsonb_typeof(input_snapshot) = 'object'",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_sizing_requests_idempotency_key_nonempty",
        "sizing_requests",
        "idempotency_key IS NULL OR length(idempotency_key) > 0",
        schema=SCHEMA,
    )
    op.create_index(
        "uq_sizing_requests_idempotency_key",
        "sizing_requests",
        ["idempotency_key"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.alter_column(
        "simulation_runs",
        "container_model_id",
        existing_type=sa.Uuid(),
        nullable=True,
        schema=SCHEMA,
    )
    op.add_column(
        "simulation_runs",
        sa.Column("source_container_key", sa.String(length=200), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "simulation_runs",
        sa.Column("source_catalog_sha256", sa.String(length=64), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "simulation_runs",
        sa.Column("result_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "simulation_runs",
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_simulation_runs_result_snapshot_object",
        "simulation_runs",
        "result_snapshot IS NULL OR jsonb_typeof(result_snapshot) = 'object'",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_simulation_runs_warnings_array",
        "simulation_runs",
        "warnings IS NULL OR jsonb_typeof(warnings) = 'array'",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_simulation_runs_source_catalog_sha256_length",
        "simulation_runs",
        "source_catalog_sha256 IS NULL OR length(source_catalog_sha256) = 64",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_simulation_runs_source_container_key_nonempty",
        "simulation_runs",
        "source_container_key IS NULL OR length(source_container_key) > 0",
        schema=SCHEMA,
    )
    op.create_index(
        "ix_simulation_runs_source_catalog",
        "simulation_runs",
        ["source_catalog_sha256", "source_container_key"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    raise RuntimeError(
        "Revision 20261012_0003 stores historical input/result data and is not "
        "safely reversible by dropping those records."
    )
