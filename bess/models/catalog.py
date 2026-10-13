"""Versioned catalog and container model foundation."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, Timestamped, UUIDPrimaryKey


class CatalogRevision(UUIDPrimaryKey, Base):
    __tablename__ = "catalog_revisions"
    __table_args__ = (
        CheckConstraint("length(source_sha256) = 64", name="sha256_length"),
        Index(
            "uq_catalog_revisions_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    source_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    imported_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bess.users.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    metadata_json: Mapped[dict | None] = mapped_column(
        "metadata", JSONB().with_variant(JSON(), "sqlite")
    )

    container_models: Mapped[list[ContainerModel]] = relationship(back_populates="catalog_revision")


class ContainerModel(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "container_models"
    __table_args__ = (
        UniqueConstraint("catalog_revision_id", "source_key", name="uq_container_revision_source"),
        CheckConstraint("energy_mwh > 0", name="positive_energy"),
        CheckConstraint("c_rate > 0", name="positive_c_rate"),
        CheckConstraint("cycles_per_day > 0", name="positive_cycles_per_day"),
        CheckConstraint(
            "aux_discharge_mw IS NULL OR aux_discharge_mw >= 0",
            name="nonnegative_aux_discharge",
        ),
        CheckConstraint(
            "aux_charge_mw IS NULL OR aux_charge_mw >= 0",
            name="nonnegative_aux_charge",
        ),
        CheckConstraint(
            "aux_standby_mw IS NULL OR aux_standby_mw >= 0",
            name="nonnegative_aux_standby",
        ),
        Index("ix_container_models_energy_c_rate", "energy_mwh", "c_rate"),
    )

    catalog_revision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.catalog_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    source_sheet: Mapped[str] = mapped_column(String(200), nullable=False)
    energy_mwh: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    c_rate: Mapped[Decimal] = mapped_column(Numeric(12, 8), nullable=False)
    cycles_per_day: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    aux_discharge_mw: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    aux_charge_mw: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    aux_standby_mw: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))

    catalog_revision: Mapped[CatalogRevision] = relationship(back_populates="container_models")
    performance_years: Mapped[list[ContainerPerformanceYear]] = relationship(
        back_populates="container_model", cascade="all, delete-orphan"
    )
    temperature_bands: Mapped[list[ContainerTemperatureBand]] = relationship(
        back_populates="container_model", cascade="all, delete-orphan"
    )
    calendar_degradation: Mapped[list[ContainerCalendarDegradation]] = relationship(
        back_populates="container_model", cascade="all, delete-orphan"
    )
    ac_parameters: Mapped[list[ContainerACParameter]] = relationship(
        back_populates="container_model", cascade="all, delete-orphan"
    )
    raw_ac_parameters: Mapped[list[ContainerRawACParameter]] = relationship(
        back_populates="container_model", cascade="all, delete-orphan"
    )
    data_notes: Mapped[list[ContainerModelNote]] = relationship(
        back_populates="container_model", cascade="all, delete-orphan"
    )


class ContainerPerformanceYear(Base):
    __tablename__ = "container_performance_years"
    __table_args__ = (
        CheckConstraint("year_index >= 0", name="nonnegative_year"),
        CheckConstraint("soh > 0 AND soh <= 1", name="valid_soh"),
        CheckConstraint("dc_rte > 0 AND dc_rte <= 1", name="valid_dc_rte"),
    )

    container_model_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.container_models.id", ondelete="CASCADE", name="fk_perf_year_model"),
        primary_key=True,
    )
    year_index: Mapped[int] = mapped_column(primary_key=True)
    soh: Mapped[Decimal] = mapped_column(Numeric(24, 16), nullable=False)
    dc_rte: Mapped[Decimal] = mapped_column(Numeric(24, 16), nullable=False)

    container_model: Mapped[ContainerModel] = relationship(back_populates="performance_years")


class ContainerTemperatureBand(Base):
    __tablename__ = "container_temperature_bands"
    __table_args__ = (
        CheckConstraint("band_index >= 0", name="nonnegative_band_index"),
        CheckConstraint("length(label) > 0", name="nonempty_label"),
    )

    container_model_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.container_models.id", ondelete="CASCADE", name="fk_temp_band_model"),
        primary_key=True,
    )
    band_index: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    upper_c: Mapped[Decimal] = mapped_column(Numeric(9, 4), nullable=False)

    container_model: Mapped[ContainerModel] = relationship(back_populates="temperature_bands")


class ContainerCalendarDegradation(Base):
    __tablename__ = "container_calendar_degradation"
    __table_args__ = (
        CheckConstraint("month_index >= 1", name="positive_month_index"),
        CheckConstraint("degradation >= 0 AND degradation < 1", name="valid_degradation"),
        ForeignKeyConstraint(
            ["container_model_id", "band_index"],
            [
                "bess.container_temperature_bands.container_model_id",
                "bess.container_temperature_bands.band_index",
            ],
            name="fk_calendar_degradation_band",
            ondelete="CASCADE",
        ),
    )

    container_model_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.container_models.id", ondelete="CASCADE", name="fk_calendar_degradation_model"),
        primary_key=True,
    )
    month_index: Mapped[int] = mapped_column(primary_key=True)
    band_index: Mapped[int] = mapped_column(primary_key=True)
    degradation: Mapped[Decimal] = mapped_column(Numeric(24, 16), nullable=False)

    container_model: Mapped[ContainerModel] = relationship(back_populates="calendar_degradation")


class ContainerACParameter(Base):
    __tablename__ = "container_ac_parameters"
    __table_args__ = (
        CheckConstraint(
            "parameter_key IN ('lv_cable', 'pcs', 'pcs_idt_cable', 'idt', 'dc_db', "
            "'mv_cable_switchgear', 'main_transformer', 'cable_11kv', 'measurement', "
            "'hv_transformer')",
            name="valid_parameter_key",
        ),
        CheckConstraint(
            "source_kind IN ('database', 'derived', 'assumed', 'missing')",
            name="valid_source_kind",
        ),
        CheckConstraint(
            "(parameter_key = 'hv_transformer' AND "
            "((efficiency IS NULL AND source_kind = 'missing') OR "
            "(efficiency > 0 AND efficiency <= 1 AND source_kind = 'database'))) OR "
            "(parameter_key <> 'hv_transformer' AND efficiency > 0 AND efficiency <= 1 "
            "AND source_kind IN ('database', 'derived', 'assumed'))",
            name="valid_efficiency_provenance",
        ),
    )

    container_model_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.container_models.id", ondelete="CASCADE", name="fk_ac_parameter_model"),
        primary_key=True,
    )
    parameter_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    efficiency: Mapped[Decimal | None] = mapped_column(Numeric(24, 16))
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_label: Mapped[str | None] = mapped_column(Text)
    source_text: Mapped[str | None] = mapped_column(Text)

    container_model: Mapped[ContainerModel] = relationship(back_populates="ac_parameters")


class ContainerRawACParameter(Base):
    __tablename__ = "container_raw_ac_parameters"

    container_model_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.container_models.id", ondelete="CASCADE", name="fk_raw_ac_parameter_model"),
        primary_key=True,
    )
    source_label: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Decimal] = mapped_column(Numeric(24, 16), nullable=False)

    container_model: Mapped[ContainerModel] = relationship(back_populates="raw_ac_parameters")


class ContainerModelNote(Base):
    __tablename__ = "container_model_notes"
    __table_args__ = (
        CheckConstraint("note_index >= 0", name="nonnegative_note_index"),
        CheckConstraint("length(note) > 0", name="nonempty_note"),
    )

    container_model_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.container_models.id", ondelete="CASCADE", name="fk_container_note_model"),
        primary_key=True,
    )
    note_index: Mapped[int] = mapped_column(primary_key=True)
    note: Mapped[str] = mapped_column(Text, nullable=False)

    container_model: Mapped[ContainerModel] = relationship(back_populates="data_notes")


class SizingRequest(UUIDPrimaryKey, Base):
    __tablename__ = "sizing_requests"
    __table_args__ = (
        CheckConstraint(
            "input_snapshot IS NULL OR jsonb_typeof(input_snapshot) = 'object'",
            name="input_snapshot_object",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "idempotency_key IS NULL OR length(idempotency_key) > 0",
            name="idempotency_key_nonempty",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'SUBMITTED', 'COMPLETED', 'FAILED')",
            name="valid_status",
        ),
        Index(
            "uq_sizing_requests_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_sizing_requests_project_created", "project_id", "created_at"),
        Index("ix_sizing_requests_parent", "parent_request_id"),
    )

    project_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bess.projects.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bess.users.id", ondelete="SET NULL")
    )
    parent_request_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bess.sizing_requests.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="api", server_default="api")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT", server_default="DRAFT")
    scenario_name: Mapped[str | None] = mapped_column(String(300))
    input_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite")
    )
    input_schema_version: Mapped[str | None] = mapped_column(String(32))
    application_version: Mapped[str | None] = mapped_column(String(100))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    parent_request: Mapped[SizingRequest | None] = relationship(
        remote_side="SizingRequest.id", back_populates="child_requests"
    )
    child_requests: Mapped[list[SizingRequest]] = relationship(back_populates="parent_request")
    simulation_runs: Mapped[list[SimulationRun]] = relationship(back_populates="sizing_request")


class SimulationRun(UUIDPrimaryKey, Base):
    __tablename__ = "simulation_runs"
    __table_args__ = (
        UniqueConstraint("request_id", "container_model_id", name="uq_run_request_container"),
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="valid_status",
        ),
        CheckConstraint(
            "initial_container_count IS NULL OR initial_container_count > 0",
            name="positive_container_count",
        ),
        CheckConstraint(
            "result_snapshot IS NULL OR jsonb_typeof(result_snapshot) = 'object'",
            name="result_snapshot_object",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "warnings IS NULL OR jsonb_typeof(warnings) = 'array'",
            name="warnings_array",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "source_catalog_sha256 IS NULL OR length(source_catalog_sha256) = 64",
            name="source_catalog_sha256_length",
        ),
        CheckConstraint(
            "source_container_key IS NULL OR length(source_container_key) > 0",
            name="source_container_key_nonempty",
        ),
        Index("ix_simulation_runs_status_created", "status", "created_at"),
        Index("ix_simulation_runs_container_created", "container_model_id", "created_at"),
        Index(
            "ix_simulation_runs_source_catalog",
            "source_catalog_sha256",
            "source_container_key",
        ),
    )

    request_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.sizing_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    container_model_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.container_models.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_container_key: Mapped[str | None] = mapped_column(String(200))
    source_catalog_sha256: Mapped[str | None] = mapped_column(String(64))
    result_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite")
    )
    warnings: Mapped[list[str] | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="QUEUED", server_default="QUEUED")
    engine_version: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    initial_container_count: Mapped[int | None] = mapped_column()
    margin_applied_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 4))
    in_range: Mapped[bool | None] = mapped_column(Boolean)
    accepted: Mapped[bool | None] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)

    sizing_request: Mapped[SizingRequest] = relationship(back_populates="simulation_runs")
    container_model: Mapped[ContainerModel] = relationship()
