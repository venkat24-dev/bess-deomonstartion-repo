"""Identity, organization and project foundation models."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    JSON,
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


class User(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("oidc_issuer", "oidc_subject", name="uq_users_oidc_identity"),
        Index("ix_users_email", "email"),
    )

    oidc_issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    oidc_subject: Mapped[str] = mapped_column(String(500), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")

    organization_memberships: Mapped[list[OrganizationMember]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    project_memberships: Mapped[list[ProjectMember]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Organization(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bess.users.id", ondelete="SET NULL")
    )

    memberships: Mapped[list[OrganizationMember]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    projects: Mapped[list[Project]] = relationship(back_populates="organization")


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'admin', 'manager', 'member')", name="valid_role"),
        Index(
            "uq_organization_members_one_owner",
            "organization_id",
            unique=True,
            postgresql_where=text("role = 'owner'"),
            sqlite_where=text("role = 'owner'"),
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    organization: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="organization_memberships")


class Project(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("version >= 1", name="positive_version"),
        CheckConstraint(
            "access_state IN ('active', 'ownership_review')",
            name="valid_access_state",
        ),
        ForeignKeyConstraint(
            ["id", "current_version_number"],
            ["bess.project_versions.project_id", "bess.project_versions.version_number"],
            name="fk_projects_current_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        Index("ix_projects_organization_updated", "organization_id", "updated_at"),
        Index(
            "ix_projects_access_state",
            "access_state",
            postgresql_where=text("access_state = 'ownership_review'"),
            sqlite_where=text("access_state = 'ownership_review'"),
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.organizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bess.users.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(nullable=False, default=1, server_default="1")
    current_version_number: Mapped[int | None] = mapped_column()
    access_state: Mapped[str] = mapped_column(
        String(30), nullable=False, default="active", server_default="active"
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bess.users.id", ondelete="SET NULL")
    )
    __mapper_args__ = {"version_id_col": version}

    organization: Mapped[Organization] = relationship(back_populates="projects")
    created_by: Mapped[User | None] = relationship(foreign_keys=[created_by_user_id])
    memberships: Mapped[list[ProjectMember]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="valid_role"),
        Index("ix_project_members_user", "user_id"),
        Index(
            "uq_project_members_one_owner",
            "project_id",
            unique=True,
            postgresql_where=text("role = 'owner'"),
            sqlite_where=text("role = 'owner'"),
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.projects.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    project: Mapped[Project] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="project_memberships")


class ProjectVersion(UUIDPrimaryKey, Base):
    __tablename__ = "project_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "version_number", name="uq_project_version_number"),
        CheckConstraint("version_number > 0", name="positive_version_number"),
        CheckConstraint(
            "input_schema_version <> '' AND length(input_schema_version) <= 32",
            name="valid_input_schema_version",
        ),
        CheckConstraint(
            "length(source_catalog_sha256) = 64",
            name="source_catalog_sha256_length",
        ),
        CheckConstraint("length(engine_version) > 0", name="engine_version_nonempty"),
        CheckConstraint(
            "source_version_number IS NULL OR source_version_number < version_number",
            name="valid_source_version",
        ),
        CheckConstraint(
            "jsonb_typeof(input_snapshot) = 'object'",
            name="input_snapshot_object",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "jsonb_typeof(result_snapshot) = 'object'",
            name="result_snapshot_object",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "jsonb_typeof(selected_container_snapshot) = 'object'",
            name="selected_container_snapshot_object",
        ).ddl_if(dialect="postgresql"),
        ForeignKeyConstraint(
            ["project_id", "source_version_number"],
            ["bess.project_versions.project_id", "bess.project_versions.version_number"],
            name="fk_project_versions_source_version",
        ),
        Index("ix_project_versions_created", "project_id", "created_at"),
    )

    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.projects.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(nullable=False)
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bess.users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    input_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    result_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    selected_container_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    catalog_revision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bess.catalog_revisions.id", ondelete="RESTRICT")
    )
    source_catalog_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_version_number: Mapped[int | None] = mapped_column()
    change_summary: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditEvent(UUIDPrimaryKey, Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("length(action) > 0", name="action_nonempty"),
        CheckConstraint("length(resource_type) > 0", name="resource_type_nonempty"),
        Index("ix_audit_events_resource_created", "resource_type", "resource_id", "created_at"),
        Index("ix_audit_events_actor_created", "actor_user_id", "created_at"),
    )

    actor_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bess.users.id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    correlation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    change_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB().with_variant(JSON(), "sqlite")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProjectAccessGrant(UUIDPrimaryKey, Base):
    __tablename__ = "project_access_grants"
    __table_args__ = (
        CheckConstraint(
            "access_scope IN ('management', 'content_read')",
            name="valid_access_scope",
        ),
        CheckConstraint("length(reason) > 0", name="reason_nonempty"),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name="valid_expiration",
        ),
        Index("ix_project_access_grants_user", "user_id", "project_id"),
        Index(
            "uq_project_access_grants_active",
            "project_id",
            "user_id",
            "access_scope",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bess.projects.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bess.users.id", ondelete="RESTRICT"), nullable=False
    )
    granted_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("bess.users.id", ondelete="RESTRICT"), nullable=False
    )
    access_scope: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
