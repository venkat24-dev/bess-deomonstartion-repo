"""Preserve complete Excel container engineering data in the catalog.

Revision ID: 20261012_0002
Revises: 20261012_0001
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20261012_0002"
down_revision = "20261012_0001"
branch_labels = None
depends_on = None

SCHEMA = "bess"


def upgrade() -> None:
    op.create_table(
        "container_performance_years",
        sa.Column("container_model_id", sa.Uuid(), nullable=False),
        sa.Column("year_index", sa.Integer(), nullable=False),
        sa.Column("soh", sa.Numeric(24, 16), nullable=False),
        sa.Column("dc_rte", sa.Numeric(24, 16), nullable=False),
        sa.CheckConstraint("year_index >= 0", name="nonnegative_year"),
        sa.CheckConstraint("soh > 0 AND soh <= 1", name="valid_soh"),
        sa.CheckConstraint("dc_rte > 0 AND dc_rte <= 1", name="valid_dc_rte"),
        sa.ForeignKeyConstraint(
            ["container_model_id"],
            [f"{SCHEMA}.container_models.id"],
            name="fk_perf_year_model",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "container_model_id", "year_index",
            name="pk_container_performance_years",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "container_temperature_bands",
        sa.Column("container_model_id", sa.Uuid(), nullable=False),
        sa.Column("band_index", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("upper_c", sa.Numeric(9, 4), nullable=False),
        sa.CheckConstraint(
            "band_index >= 0",
            name="nonnegative_band_index",
        ),
        sa.CheckConstraint(
            "length(label) > 0",
            name="nonempty_label",
        ),
        sa.ForeignKeyConstraint(
            ["container_model_id"],
            [f"{SCHEMA}.container_models.id"],
            name="fk_temp_band_model",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "container_model_id", "band_index",
            name="pk_container_temperature_bands",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "container_calendar_degradation",
        sa.Column("container_model_id", sa.Uuid(), nullable=False),
        sa.Column("month_index", sa.Integer(), nullable=False),
        sa.Column("band_index", sa.Integer(), nullable=False),
        sa.Column("degradation", sa.Numeric(24, 16), nullable=False),
        sa.CheckConstraint(
            "month_index >= 1",
            name="positive_month_index",
        ),
        sa.CheckConstraint(
            "degradation >= 0 AND degradation < 1",
            name="valid_degradation",
        ),
        sa.ForeignKeyConstraint(
            ["container_model_id"],
            [f"{SCHEMA}.container_models.id"],
            name="fk_calendar_degradation_container_model",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["container_model_id", "band_index"],
            [
                f"{SCHEMA}.container_temperature_bands.container_model_id",
                f"{SCHEMA}.container_temperature_bands.band_index",
            ],
            name="fk_calendar_degradation_temperature_band",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "container_model_id", "month_index", "band_index",
            name="pk_container_calendar_degradation",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "container_ac_parameters",
        sa.Column("container_model_id", sa.Uuid(), nullable=False),
        sa.Column("parameter_key", sa.String(length=40), nullable=False),
        sa.Column("efficiency", sa.Numeric(24, 16), nullable=True),
        sa.Column("source_kind", sa.String(length=20), nullable=False),
        sa.Column("source_label", sa.Text(), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "parameter_key IN ('lv_cable', 'pcs', 'pcs_idt_cable', 'idt', 'dc_db', "
            "'mv_cable_switchgear', 'main_transformer', 'cable_11kv', 'measurement', "
            "'hv_transformer')",
            name="valid_parameter_key",
        ),
        sa.CheckConstraint(
            "source_kind IN ('database', 'derived', 'assumed', 'missing')",
            name="valid_source_kind",
        ),
        sa.CheckConstraint(
            "(parameter_key = 'hv_transformer' AND "
            "((efficiency IS NULL AND source_kind = 'missing') OR "
            "(efficiency > 0 AND efficiency <= 1 AND source_kind = 'database'))) OR "
            "(parameter_key <> 'hv_transformer' AND efficiency > 0 AND efficiency <= 1 "
            "AND source_kind IN ('database', 'derived', 'assumed'))",
            name="valid_efficiency_provenance",
        ),
        sa.ForeignKeyConstraint(
            ["container_model_id"],
            [f"{SCHEMA}.container_models.id"],
            name="fk_ac_parameter_model",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "container_model_id", "parameter_key",
            name="pk_container_ac_parameters",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "container_raw_ac_parameters",
        sa.Column("container_model_id", sa.Uuid(), nullable=False),
        sa.Column("source_label", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(24, 16), nullable=False),
        sa.ForeignKeyConstraint(
            ["container_model_id"],
            [f"{SCHEMA}.container_models.id"],
            name="fk_raw_ac_parameter_model",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "container_model_id", "source_label",
            name="pk_container_raw_ac_parameters",
        ),
        schema=SCHEMA,
    )
    op.create_table(
        "container_model_notes",
        sa.Column("container_model_id", sa.Uuid(), nullable=False),
        sa.Column("note_index", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "note_index >= 0",
            name="nonnegative_note_index",
        ),
        sa.CheckConstraint(
            "length(note) > 0",
            name="nonempty_note",
        ),
        sa.ForeignKeyConstraint(
            ["container_model_id"],
            [f"{SCHEMA}.container_models.id"],
            name="fk_container_note_model",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "container_model_id", "note_index",
            name="pk_container_model_notes",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("container_model_notes", schema=SCHEMA)
    op.drop_table("container_raw_ac_parameters", schema=SCHEMA)
    op.drop_table("container_ac_parameters", schema=SCHEMA)
    op.drop_table("container_calendar_degradation", schema=SCHEMA)
    op.drop_table("container_temperature_bands", schema=SCHEMA)
    op.drop_table("container_performance_years", schema=SCHEMA)
