"""Transactional project persistence with server-side authorization."""
from __future__ import annotations

import logging
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.exc import StaleDataError

from bess.authorization import (
    Operation,
    organization_role_allows,
    organization_role_can_assign,
    organization_role_can_manage_member,
    project_role_allows,
)
from bess.db_connection import DatabaseConfigurationError, get_session_factory
from bess.models import (
    AuditEvent,
    ContainerModel,
    Organization,
    OrganizationMember,
    Project,
    ProjectAccessGrant,
    ProjectMember,
    ProjectVersion,
    SimulationRun,
    SizingRequest,
    User,
)

logger = logging.getLogger(__name__)


class ProjectAccessError(RuntimeError):
    """The requested project or organization is not available to the caller."""


class ProjectConflict(RuntimeError):
    """A project or membership changed concurrently or already exists."""


class ProjectPersistenceError(RuntimeError):
    """A non-secret project persistence failure."""


def _is_concurrency_error(exc: OperationalError) -> bool:
    sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    if sqlstate in {"40001", "40P01"}:
        return True
    sqlite_error_code = getattr(exc.orig, "sqlite_errorcode", None)
    return (
        isinstance(sqlite_error_code, int)
        and sqlite_error_code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    )


def _organization_role(
    session: Session,
    organization_id: UUID,
    user_id: UUID,
    *,
    lock: bool = False,
) -> str | None:
    statement = (
        select(OrganizationMember.role)
        .join(User, User.id == OrganizationMember.user_id)
        .where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
            User.is_active.is_(True),
        )
    )
    if lock:
        statement = statement.with_for_update(of=(OrganizationMember, User))
    return session.scalar(statement)


def _project_role(
    session: Session,
    project_id: UUID,
    user_id: UUID,
    *,
    lock: bool = False,
) -> str | None:
    statement = (
        select(ProjectMember.role)
        .join(Project, Project.id == ProjectMember.project_id)
        .join(
            OrganizationMember,
            (OrganizationMember.organization_id == Project.organization_id)
            & (OrganizationMember.user_id == ProjectMember.user_id),
        )
        .join(User, User.id == ProjectMember.user_id)
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
            User.is_active.is_(True),
        )
    )
    if lock:
        statement = statement.with_for_update(of=(ProjectMember, OrganizationMember, User))
    return session.scalar(statement)


def _active_grant_scopes(
    session: Session, project: Project, user_id: UUID, *, lock: bool = False
) -> set[str]:
    now = datetime.now(timezone.utc)
    statement = (
        select(ProjectAccessGrant.access_scope)
        .join(
            OrganizationMember,
            (OrganizationMember.organization_id == project.organization_id)
            & (OrganizationMember.user_id == ProjectAccessGrant.user_id),
        )
        .join(User, User.id == ProjectAccessGrant.user_id)
        .where(
            ProjectAccessGrant.project_id == project.id,
            ProjectAccessGrant.user_id == user_id,
            ProjectAccessGrant.revoked_at.is_(None),
            or_(ProjectAccessGrant.expires_at.is_(None), ProjectAccessGrant.expires_at > now),
            OrganizationMember.role == "manager",
            User.is_active.is_(True),
        )
    )
    if lock:
        statement = statement.with_for_update(
            of=(ProjectAccessGrant, OrganizationMember, User)
        )
    return set(session.scalars(statement).all())


def _require_project_permission(
    session: Session,
    project_id: UUID,
    user_id: UUID,
    operation: Operation,
    *,
    lock: bool = False,
) -> Project:
    statement = select(Project).where(Project.id == project_id)
    if lock:
        statement = statement.with_for_update()
    project = session.scalar(statement)
    role = _project_role(session, project_id, user_id, lock=lock)
    if project is None or project.access_state != "active":
        raise ProjectAccessError("Project not found or access denied.")
    if project.archived_at is not None and operation not in {
        Operation.READ_PROJECT,
        Operation.READ_PROJECT_CONTENT,
    }:
        raise ProjectAccessError("Archived projects are read-only.")
    if role is not None and project_role_allows(role, operation):
        return project
    grant_scopes = _active_grant_scopes(session, project, user_id, lock=lock)
    if operation == Operation.READ_PROJECT and grant_scopes:
        return project
    if operation == Operation.READ_PROJECT_CONTENT and "content_read" in grant_scopes:
        return project
    if operation == Operation.ARCHIVE_PROJECT and "management" in grant_scopes:
        return project
    raise ProjectAccessError("Project not found or access denied.")


def _require_project_role(
    session: Session,
    project_id: UUID,
    user_id: UUID,
    allowed_roles: set[str],
) -> Project:
    """Compatibility helper for existing simulation paths; policy remains centralized."""
    operation_by_roles = {
        frozenset({"owner", "editor"}): Operation.RUN_SIZING,
        frozenset({"owner", "editor", "viewer"}): Operation.READ_PROJECT_CONTENT,
    }
    operation = operation_by_roles.get(frozenset(allowed_roles))
    if operation is None:
        raise ValueError("Unsupported project role policy.")
    return _require_project_permission(session, project_id, user_id, operation)


def _project_dict(project: Project) -> dict[str, Any]:
    return {
        "id": str(project.id),
        "organization_id": str(project.organization_id),
        "created_by_user_id": str(project.created_by_user_id) if project.created_by_user_id else None,
        "name": project.name,
        "description": project.description,
        "version": project.version,
        "current_version_number": project.current_version_number,
        "access_state": project.access_state,
        "archived_at": project.archived_at.isoformat() if project.archived_at else None,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


def _member_dict(project_id: UUID, user_id: UUID, role: str, added_at: datetime) -> dict[str, str]:
    return {
        "project_id": str(project_id),
        "user_id": str(user_id),
        "role": role,
        "added_at": added_at.isoformat(),
    }


def _audit(
    session: Session,
    actor_id: UUID,
    action: str,
    resource_type: str,
    resource_id: UUID,
    correlation_id: UUID | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            actor_user_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            correlation_id=correlation_id,
            change_metadata=metadata,
        )
    )


def _version_summary(version: ProjectVersion) -> dict[str, Any]:
    return {
        "project_id": str(version.project_id),
        "version_number": version.version_number,
        "created_by_user_id": str(version.created_by_user_id),
        "created_at": version.created_at.isoformat(),
        "input_schema_version": version.input_schema_version,
        "catalog_revision_id": (
            str(version.catalog_revision_id) if version.catalog_revision_id else None
        ),
        "source_catalog_sha256": version.source_catalog_sha256,
        "engine_version": version.engine_version,
        "source_version_number": version.source_version_number,
        "change_summary": version.change_summary,
    }


def _version_dict(version: ProjectVersion) -> dict[str, Any]:
    return {
        **_version_summary(version),
        "input_snapshot": version.input_snapshot,
        "result_snapshot": version.result_snapshot,
        "selected_container_snapshot": version.selected_container_snapshot,
    }


def _lock_user_project_rows(
    session: Session, organization_id: UUID, user_id: UUID
) -> None:
    project_ids = session.scalars(
        select(Project.id)
        .outerjoin(
            ProjectMember,
            (ProjectMember.project_id == Project.id)
            & (ProjectMember.user_id == user_id),
        )
        .outerjoin(
            ProjectAccessGrant,
            (ProjectAccessGrant.project_id == Project.id)
            & (ProjectAccessGrant.user_id == user_id),
        )
        .where(
            Project.organization_id == organization_id,
            or_(
                ProjectMember.user_id.is_not(None),
                ProjectAccessGrant.id.is_not(None),
            ),
        )
        .distinct()
        .order_by(Project.id)
    ).all()
    for project_id in project_ids:
        session.scalar(
            select(Project.id).where(Project.id == project_id).with_for_update()
        )


class ProjectRepository:
    """Own transactional project operations and enforce access in the data layer."""

    def create_project(
        self,
        user_id: UUID,
        organization_id: UUID,
        name: str,
        description: str | None,
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        try:
            with get_session_factory()() as session, session.begin():
                organization = session.get(Organization, organization_id)
                organization_role = _organization_role(
                    session, organization_id, user_id, lock=True
                )
                if (
                    organization is None
                    or not organization_role_allows(organization_role, Operation.CREATE_PROJECT)
                ):
                    raise ProjectAccessError("Organization not found or project creation is not allowed.")
                project = Project(
                    organization_id=organization_id,
                    created_by_user_id=user_id,
                    name=name,
                    description=description,
                )
                project.memberships.append(ProjectMember(user_id=user_id, role="owner"))
                session.add(project)
                session.flush()
                _audit(
                    session,
                    user_id,
                    "project.created",
                    "project",
                    project.id,
                    correlation_id,
                )
                return _project_dict(project)
        except ProjectAccessError:
            raise
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not create project (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not create the project.") from exc

    def list_projects(self, user_id: UUID) -> list[dict[str, Any]]:
        try:
            with get_session_factory()() as session:
                now = datetime.now(timezone.utc)
                projects = session.scalars(
                    select(Project)
                    .join(
                        User,
                        and_(User.id == user_id, User.is_active.is_(True)),
                    )
                    .outerjoin(
                        ProjectAccessGrant,
                        (ProjectAccessGrant.project_id == Project.id)
                        & (ProjectAccessGrant.user_id == user_id)
                        & ProjectAccessGrant.revoked_at.is_(None)
                        & or_(
                            ProjectAccessGrant.expires_at.is_(None),
                            ProjectAccessGrant.expires_at > now,
                        ),
                    )
                    .join(
                        OrganizationMember,
                        (OrganizationMember.organization_id == Project.organization_id)
                        & (OrganizationMember.user_id == user_id),
                    )
                    .outerjoin(
                        ProjectMember,
                        (ProjectMember.project_id == Project.id)
                        & (ProjectMember.user_id == user_id),
                    )
                    .where(
                        Project.access_state == "active",
                        or_(
                            ProjectMember.user_id.is_not(None),
                            and_(
                                ProjectAccessGrant.id.is_not(None),
                                OrganizationMember.role == "manager",
                            ),
                        ),
                    )
                    .distinct()
                    .order_by(Project.updated_at.desc(), Project.id)
                ).all()
                return [_project_dict(project) for project in projects]
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not list projects (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not list projects.") from exc

    def get_project(self, user_id: UUID, project_id: UUID) -> dict[str, Any] | None:
        try:
            with get_session_factory()() as session:
                try:
                    project = _require_project_permission(
                        session, project_id, user_id, Operation.READ_PROJECT
                    )
                except ProjectAccessError:
                    return None
                result = _project_dict(project)
                result["current_user_role"] = _project_role(
                    session, project_id, user_id
                )
                return result
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not retrieve project (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not retrieve the project.") from exc

    def update_project(
        self,
        user_id: UUID,
        project_id: UUID,
        expected_version: int,
        changes: dict[str, Any],
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        try:
            with get_session_factory()() as session, session.begin():
                project = _require_project_permission(
                    session, project_id, user_id, Operation.EDIT_PROJECT, lock=True
                )
                if project.version != expected_version:
                    raise ProjectConflict("The project version is stale; retrieve it and retry.")
                for field, value in changes.items():
                    if field not in {"name", "description"}:
                        raise ProjectConflict("Unsupported project field.")
                    setattr(project, field, value)
                session.flush()
                _audit(
                    session,
                    user_id,
                    "project.updated",
                    "project",
                    project.id,
                    correlation_id,
                    {"project_version": project.version},
                )
                return _project_dict(project)
        except (ProjectAccessError, ProjectConflict):
            raise
        except StaleDataError as exc:
            raise ProjectConflict("The project was updated by another request.") from exc
        except OperationalError as exc:
            if _is_concurrency_error(exc):
                raise ProjectConflict("The project was updated by another request.") from exc
            logger.error("Could not update project (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not update the project.") from exc
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not update project (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not update the project.") from exc

    def add_project_member(
        self,
        caller_id: UUID,
        project_id: UUID,
        member_user_id: UUID,
        role: str,
        correlation_id: UUID | None = None,
    ) -> dict[str, str]:
        try:
            with get_session_factory()() as session, session.begin():
                project = _require_project_permission(
                    session,
                    project_id,
                    caller_id,
                    Operation.SHARE_PROJECT,
                    lock=True,
                )
                if role not in {"editor", "viewer"}:
                    raise ProjectAccessError("Project owners must use ownership transfer to grant ownership.")
                target = session.scalar(
                    select(User)
                    .where(User.id == member_user_id)
                    .with_for_update()
                )

                organization_member = session.scalar(
                    select(OrganizationMember)
                    .where(
                        OrganizationMember.organization_id == project.organization_id,
                        OrganizationMember.user_id == member_user_id,
                    )
                    .with_for_update()
                )
                if target is None or not target.is_active or organization_member is None:
                    raise ProjectAccessError("Active organization member not found.")
                if session.get(ProjectMember, (project_id, member_user_id)) is not None:
                    raise ProjectConflict("User is already a project member.")
                member = ProjectMember(project_id=project_id, user_id=member_user_id, role=role)
                session.add(member)
                session.flush()
                _audit(
                    session,
                    caller_id,
                    "project.member_added",
                    "project",
                    project_id,
                    correlation_id,
                    {"member_user_id": str(member_user_id), "role": role},
                )
                return _member_dict(project_id, member_user_id, role, member.added_at)
        except (ProjectAccessError, ProjectConflict):
            raise
        except IntegrityError as exc:
            raise ProjectConflict("Project membership already exists.") from exc
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not add project member (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not add the project member.") from exc

    def update_project_member_role(
        self,
        caller_id: UUID,
        project_id: UUID,
        member_user_id: UUID,
        role: str,
        correlation_id: UUID | None = None,
    ) -> dict[str, str]:
        try:
            with get_session_factory()() as session, session.begin():
                _require_project_permission(
                    session,
                    project_id,
                    caller_id,
                    Operation.SHARE_PROJECT,
                    lock=True,
                )
                if role not in {"editor", "viewer"}:
                    raise ProjectAccessError(
                        "Project ownership must use the ownership-transfer operation."
                    )
                member = session.get(ProjectMember, (project_id, member_user_id))
                if member is None or member.role == "owner":
                    raise ProjectAccessError("Project member not found.")
                previous_role = member.role
                member.role = role
                session.flush()
                _audit(
                    session,
                    caller_id,
                    "project.member_role_changed",
                    "project",
                    project_id,
                    correlation_id,
                    {
                        "member_user_id": str(member_user_id),
                        "previous_role": previous_role,
                        "role": role,
                    },
                )
                return {
                    "project_id": str(project_id),
                    "user_id": str(member_user_id),
                    "role": role,
                }
        except ProjectAccessError:
            raise
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not change project member role (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not change the project member role.") from exc

    def remove_project_member(
        self,
        caller_id: UUID,
        project_id: UUID,
        member_user_id: UUID,
        correlation_id: UUID | None = None,
    ) -> None:
        try:
            with get_session_factory()() as session, session.begin():
                _require_project_permission(
                    session,
                    project_id,
                    caller_id,
                    Operation.SHARE_PROJECT,
                    lock=True,
                )
                member = session.get(ProjectMember, (project_id, member_user_id))
                if member is None or member.role == "owner":
                    raise ProjectAccessError(
                        "Project owner not found; transfer ownership before removal."
                    )
                role = member.role
                session.delete(member)
                session.flush()
                _audit(
                    session,
                    caller_id,
                    "project.member_removed",
                    "project",
                    project_id,
                    correlation_id,
                    {"member_user_id": str(member_user_id), "role": role},
                )
        except ProjectAccessError:
            raise
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not remove project member (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not remove the project member.") from exc

    def transfer_project_ownership(
        self,
        actor_id: UUID,
        project_id: UUID,
        target_user_id: UUID,
        expected_project_version: int,
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        try:
            with get_session_factory()() as session, session.begin():
                project = _require_project_permission(
                    session,
                    project_id,
                    actor_id,
                    Operation.TRANSFER_PROJECT_OWNERSHIP,
                    lock=True,
                )
                if project.version != expected_project_version:
                    raise ProjectConflict("The project version is stale; retrieve it and retry.")
                current_owner = session.scalar(
                    select(ProjectMember)
                    .where(
                        ProjectMember.project_id == project_id,
                        ProjectMember.user_id == actor_id,
                    )
                    .with_for_update()
                )
                target = session.scalar(
                    select(ProjectMember)
                    .where(
                        ProjectMember.project_id == project_id,
                        ProjectMember.user_id == target_user_id,
                    )
                    .with_for_update()
                )
                target_org_member = session.scalar(
                    select(OrganizationMember)
                    .where(
                        OrganizationMember.organization_id == project.organization_id,
                        OrganizationMember.user_id == target_user_id,
                    )
                    .with_for_update()
                )
                target_user = session.scalar(
                    select(User).where(User.id == target_user_id).with_for_update()
                )
                if (
                    current_owner is None
                    or current_owner.role != "owner"
                    or target is None
                    or target_org_member is None
                    or target_user is None
                    or not target_user.is_active
                ):
                    raise ProjectAccessError("Ownership transfer requires an existing project member.")
                if target_user_id == actor_id:
                    raise ProjectConflict("The selected user already owns the project.")
                current_owner.role = "editor"
                session.flush()
                target.role = "owner"
                session.flush()
                project.version += 1
                _audit(
                    session,
                    actor_id,
                    "project.ownership_transferred",
                    "project",
                    project_id,
                    correlation_id,
                    {"new_owner_user_id": str(target_user_id)},
                )
                session.flush()
                return _project_dict(project)
        except (ProjectAccessError, ProjectConflict):
            raise
        except StaleDataError as exc:
            raise ProjectConflict("The project was updated by another request.") from exc
        except OperationalError as exc:
            if _is_concurrency_error(exc):
                raise ProjectConflict("The project was updated by another request.") from exc
            logger.error("Could not transfer project ownership (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not transfer project ownership.") from exc
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not transfer project ownership (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not transfer project ownership.") from exc

    def create_version(
        self,
        actor_id: UUID,
        project_id: UUID,
        simulation_request_id: UUID,
        expected_current_version: int,
        change_summary: str | None,
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        try:
            with get_session_factory()() as session, session.begin():
                project = _require_project_permission(
                    session, project_id, actor_id, Operation.SAVE_VERSION, lock=True
                )
                if (project.current_version_number or 0) != expected_current_version:
                    raise ProjectConflict("The current project version is stale; retrieve history and retry.")
                request = session.scalar(
                    select(SizingRequest)
                    .where(
                        SizingRequest.id == simulation_request_id,
                        SizingRequest.project_id == project_id,
                    )
                    .options(selectinload(SizingRequest.simulation_runs))
                )
                if request is None or request.status != "COMPLETED" or not request.input_snapshot:
                    raise ProjectAccessError("A completed sizing run for this project was not found.")
                run = next(
                    (item for item in request.simulation_runs if item.status == "SUCCEEDED"),
                    None,
                )
                if (
                    run is None
                    or not isinstance(run.result_snapshot, dict)
                    or not isinstance(request.input_snapshot, dict)
                    or not isinstance(request.input_snapshot.get("container"), dict)
                    or not run.source_catalog_sha256
                    or not run.engine_version
                ):
                    raise ProjectConflict("The sizing run is missing required reproducibility snapshots.")
                schema_version = request.input_schema_version or request.input_snapshot.get("schema_version")
                source_snapshot = request.input_snapshot.get("source")
                application_snapshot = request.input_snapshot.get("application")
                if not isinstance(schema_version, str) or not schema_version:
                    raise ProjectConflict("The sizing input schema version is missing.")
                if (
                    not isinstance(source_snapshot, dict)
                    or source_snapshot.get("workbook_sha256") != run.source_catalog_sha256
                    or source_snapshot.get("container_key") != run.source_container_key
                    or request.input_snapshot["container"].get("id") != run.source_container_key
                    or not isinstance(application_snapshot, dict)
                    or application_snapshot.get("engine_source_sha256")
                    != run.engine_version.removeprefix("sha256:")
                    or request.input_schema_version != schema_version
                    or request.input_snapshot.get("schema_version") != schema_version
                    or run.result_snapshot.get("request_id") != str(request.id)
                    or not isinstance(run.result_snapshot.get("verification"), list)
                ):
                    raise ProjectConflict("The sizing run snapshots are inconsistent.")
                catalog_revision_id = None
                if run.container_model_id is not None:
                    catalog_revision_id = session.scalar(
                        select(ContainerModel.catalog_revision_id).where(
                            ContainerModel.id == run.container_model_id
                        )
                    )
                version = ProjectVersion(
                    project_id=project_id,
                    version_number=(project.current_version_number or 0) + 1,
                    created_by_user_id=actor_id,
                    input_schema_version=schema_version,
                    input_snapshot=deepcopy(request.input_snapshot),
                    result_snapshot=deepcopy(run.result_snapshot),
                    selected_container_snapshot=deepcopy(request.input_snapshot["container"]),
                    catalog_revision_id=catalog_revision_id,
                    source_catalog_sha256=run.source_catalog_sha256,
                    engine_version=run.engine_version,
                    change_summary=change_summary,
                )
                session.add(version)
                project.current_version_number = version.version_number
                session.flush()
                _audit(
                    session,
                    actor_id,
                    "project.version_saved",
                    "project",
                    project_id,
                    correlation_id,
                    {
                        "version_number": version.version_number,
                        "simulation_request_id": str(simulation_request_id),
                    },
                )
                return _version_summary(version)
        except (ProjectAccessError, ProjectConflict):
            raise
        except IntegrityError as exc:
            raise ProjectConflict("A concurrent save created this project version; reload and retry.") from exc
        except StaleDataError as exc:
            raise ProjectConflict("The project was updated by another request.") from exc
        except OperationalError as exc:
            if _is_concurrency_error(exc):
                raise ProjectConflict("The project was updated by another request.") from exc
            logger.error("Could not save project version (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not save the project version.") from exc
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not save project version (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not save the project version.") from exc

    def restore_version(
        self,
        actor_id: UUID,
        project_id: UUID,
        source_version_number: int,
        expected_current_version: int,
        change_summary: str | None,
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        try:
            with get_session_factory()() as session, session.begin():
                project = _require_project_permission(
                    session, project_id, actor_id, Operation.RESTORE_VERSION, lock=True
                )
                if (project.current_version_number or 0) != expected_current_version:
                    raise ProjectConflict("The current project version is stale; retrieve history and retry.")
                source = session.scalar(
                    select(ProjectVersion).where(
                        ProjectVersion.project_id == project_id,
                        ProjectVersion.version_number == source_version_number,
                    )
                )
                if source is None:
                    raise ProjectAccessError("Project version not found.")
                version = ProjectVersion(
                    project_id=project_id,
                    version_number=(project.current_version_number or 0) + 1,
                    created_by_user_id=actor_id,
                    input_schema_version=source.input_schema_version,
                    input_snapshot=deepcopy(source.input_snapshot),
                    result_snapshot=deepcopy(source.result_snapshot),
                    selected_container_snapshot=deepcopy(source.selected_container_snapshot),
                    catalog_revision_id=source.catalog_revision_id,
                    source_catalog_sha256=source.source_catalog_sha256,
                    engine_version=source.engine_version,
                    source_version_number=source.version_number,
                    change_summary=change_summary,
                )
                session.add(version)
                project.current_version_number = version.version_number
                session.flush()
                _audit(
                    session,
                    actor_id,
                    "project.version_restored",
                    "project",
                    project_id,
                    correlation_id,
                    {
                        "version_number": version.version_number,
                        "source_version_number": source.version_number,
                    },
                )
                return _version_summary(version)
        except (ProjectAccessError, ProjectConflict):
            raise
        except IntegrityError as exc:
            raise ProjectConflict("A concurrent save created this project version; reload and retry.") from exc
        except StaleDataError as exc:
            raise ProjectConflict("The project was updated by another request.") from exc
        except OperationalError as exc:
            if _is_concurrency_error(exc):
                raise ProjectConflict("The project was updated by another request.") from exc
            logger.error("Could not restore project version (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not restore the project version.") from exc
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not restore project version (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not restore the project version.") from exc

    def list_versions(self, user_id: UUID, project_id: UUID) -> list[dict[str, Any]]:
        try:
            with get_session_factory()() as session:
                _require_project_permission(
                    session, project_id, user_id, Operation.READ_PROJECT
                )
                versions = session.scalars(
                    select(ProjectVersion)
                    .where(ProjectVersion.project_id == project_id)
                    .order_by(ProjectVersion.version_number)
                ).all()
                return [_version_summary(version) for version in versions]
        except ProjectAccessError:
            raise
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not list project versions (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not list project versions.") from exc

    def get_version(
        self, user_id: UUID, project_id: UUID, version_number: int
    ) -> dict[str, Any] | None:
        try:
            with get_session_factory()() as session:
                _require_project_permission(
                    session, project_id, user_id, Operation.READ_PROJECT_CONTENT
                )
                version = session.scalar(
                    select(ProjectVersion).where(
                        ProjectVersion.project_id == project_id,
                        ProjectVersion.version_number == version_number,
                    )
                )
                return _version_dict(version) if version else None
        except ProjectAccessError:
            raise
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not retrieve project version (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not retrieve the project version.") from exc

    def grant_manager_access(
        self,
        actor_id: UUID,
        project_id: UUID,
        manager_user_id: UUID,
        access_scope: str,
        reason: str,
        expires_at: datetime | None,
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        try:
            with get_session_factory()() as session, session.begin():
                project = session.scalar(
                    select(Project)
                    .where(Project.id == project_id)
                    .with_for_update()
                )
                actor_role = (
                    _organization_role(session, project.organization_id, actor_id, lock=True)
                    if project is not None
                    else None
                )
                if (
                    project is None
                    or project.access_state != "active"
                    or not organization_role_allows(actor_role, Operation.GRANT_MANAGER_ACCESS)
                ):
                    raise ProjectAccessError("Organization administration is required to grant access.")
                if access_scope not in {"management", "content_read"}:
                    raise ProjectConflict("Unsupported manager access scope.")
                target_role = _organization_role(
                    session, project.organization_id, manager_user_id, lock=True
                )
                target = session.scalar(
                    select(User).where(User.id == manager_user_id).with_for_update()
                )
                if target_role != "manager" or target is None:
                    raise ProjectAccessError("An active organization manager must be selected.")
                if not reason.strip():
                    raise ProjectConflict("An access-grant reason is required.")
                if expires_at is not None and expires_at <= datetime.now(timezone.utc):
                    raise ProjectConflict("The access grant expiration must be in the future.")
                active_grant = session.scalar(
                    select(ProjectAccessGrant).where(
                        ProjectAccessGrant.project_id == project_id,
                        ProjectAccessGrant.user_id == manager_user_id,
                        ProjectAccessGrant.access_scope == access_scope,
                        ProjectAccessGrant.revoked_at.is_(None),
                        or_(
                            ProjectAccessGrant.expires_at.is_(None),
                            ProjectAccessGrant.expires_at > func.now(),
                        ),
                    )
                    .with_for_update()
                )
                if active_grant is not None:
                    raise ProjectConflict("An active access grant already exists.")
                previous = session.scalar(
                    select(ProjectAccessGrant).where(
                        ProjectAccessGrant.project_id == project_id,
                        ProjectAccessGrant.user_id == manager_user_id,
                        ProjectAccessGrant.access_scope == access_scope,
                        ProjectAccessGrant.revoked_at.is_(None),
                    )
                )
                if previous is not None:
                    previous.revoked_at = datetime.now(timezone.utc)
                    _audit(
                        session,
                        actor_id,
                        "project.manager_access_revoked",
                        "project",
                        project_id,
                        correlation_id,
                        {
                            "grant_id": str(previous.id),
                            "reason": "expired_grant_replaced",
                        },
                    )
                grant = ProjectAccessGrant(
                    project_id=project_id,
                    user_id=manager_user_id,
                    granted_by_user_id=actor_id,
                    access_scope=access_scope,
                    reason=reason.strip(),
                    expires_at=expires_at,
                )
                session.add(grant)
                session.flush()
                _audit(
                    session,
                    actor_id,
                    "project.manager_access_granted",
                    "project",
                    project_id,
                    correlation_id,
                    {
                        "grant_id": str(grant.id),
                        "manager_user_id": str(manager_user_id),
                        "access_scope": access_scope,
                    },
                )
                return {
                    "id": str(grant.id),
                    "project_id": str(project_id),
                    "user_id": str(manager_user_id),
                    "access_scope": access_scope,
                    "created_at": grant.created_at.isoformat(),
                    "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
                }
        except (ProjectAccessError, ProjectConflict):
            raise
        except IntegrityError as exc:
            raise ProjectConflict("An active access grant already exists.") from exc
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not grant manager project access (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not grant manager project access.") from exc

    def revoke_manager_access(
        self,
        actor_id: UUID,
        project_id: UUID,
        grant_id: UUID,
        correlation_id: UUID | None = None,
    ) -> None:
        try:
            with get_session_factory()() as session, session.begin():
                project = session.scalar(
                    select(Project)
                    .where(Project.id == project_id)
                    .with_for_update()
                )
                actor_role = (
                    _organization_role(session, project.organization_id, actor_id, lock=True)
                    if project is not None
                    else None
                )
                if (
                    project is None
                    or not organization_role_allows(actor_role, Operation.GRANT_MANAGER_ACCESS)
                ):
                    raise ProjectAccessError("Organization administration is required to revoke access.")
                grant = session.scalar(
                    select(ProjectAccessGrant).where(
                        ProjectAccessGrant.id == grant_id,
                        ProjectAccessGrant.project_id == project_id,
                        ProjectAccessGrant.revoked_at.is_(None),
                    )
                    .with_for_update()
                )
                if grant is None:
                    raise ProjectAccessError("Active access grant not found.")
                grant.revoked_at = datetime.now(timezone.utc)
                _audit(
                    session,
                    actor_id,
                    "project.manager_access_revoked",
                    "project",
                    project_id,
                    correlation_id,
                    {"grant_id": str(grant_id)},
                )
        except ProjectAccessError:
            raise
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not revoke manager project access (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not revoke manager project access.") from exc

    def add_organization_member(
        self,
        actor_id: UUID,
        organization_id: UUID,
        member_user_id: UUID,
        role: str,
        correlation_id: UUID | None = None,
    ) -> dict[str, str]:
        try:
            with get_session_factory()() as session, session.begin():
                actor_role = _organization_role(
                    session, organization_id, actor_id, lock=True
                )
                target = session.scalar(
                    select(User).where(User.id == member_user_id).with_for_update()
                )
                if (
                    not organization_role_can_assign(actor_role, role)
                    or target is None
                    or not target.is_active
                ):
                    raise ProjectAccessError("Organization membership assignment is not allowed.")
                if session.scalar(
                    select(OrganizationMember)
                    .where(
                        OrganizationMember.organization_id == organization_id,
                        OrganizationMember.user_id == member_user_id,
                    )
                    .with_for_update()
                ) is not None:
                    raise ProjectConflict("User is already an organization member.")
                member = OrganizationMember(
                    organization_id=organization_id,
                    user_id=member_user_id,
                    role=role,
                )
                session.add(member)
                session.flush()
                _audit(
                    session,
                    actor_id,
                    "organization.member_added",
                    "organization",
                    organization_id,
                    correlation_id,
                    {"member_user_id": str(member_user_id), "role": role},
                )
                return {
                    "organization_id": str(organization_id),
                    "user_id": str(member_user_id),
                    "role": role,
                }
        except (ProjectAccessError, ProjectConflict):
            raise
        except IntegrityError as exc:
            raise ProjectConflict("Organization membership already exists.") from exc
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not add organization member (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not add the organization member.") from exc

    def remove_organization_member(
        self,
        actor_id: UUID,
        organization_id: UUID,
        member_user_id: UUID,
        correlation_id: UUID | None = None,
    ) -> None:
        try:
            with get_session_factory()() as session, session.begin():
                actor_role = _organization_role(session, organization_id, actor_id)
                member = session.get(OrganizationMember, (organization_id, member_user_id))
                if (
                    member is None
                    or member_user_id == actor_id
                    or not organization_role_can_manage_member(actor_role, member.role)
                ):
                    raise ProjectAccessError("Organization membership removal is not allowed.")
                _lock_user_project_rows(session, organization_id, member_user_id)
                actor_role = _organization_role(
                    session, organization_id, actor_id, lock=True
                )
                member = session.scalar(
                    select(OrganizationMember)
                    .where(
                        OrganizationMember.organization_id == organization_id,
                        OrganizationMember.user_id == member_user_id,
                    )
                    .with_for_update()
                )
                if (
                    member is None
                    or not organization_role_can_manage_member(actor_role, member.role)
                ):
                    raise ProjectAccessError("Organization membership removal is not allowed.")
                owned_project = session.scalar(
                    select(ProjectMember.project_id)
                    .join(Project, Project.id == ProjectMember.project_id)
                    .where(
                        Project.organization_id == organization_id,
                        ProjectMember.user_id == member_user_id,
                        ProjectMember.role == "owner",
                        Project.access_state == "active",
                    )
                )
                if owned_project is not None:
                    raise ProjectConflict(
                        "Transfer ownership of active projects before removing this member."
                    )
                member_role = member.role
                grants = session.scalars(
                    select(ProjectAccessGrant)
                    .join(Project, Project.id == ProjectAccessGrant.project_id)
                    .where(
                        Project.organization_id == organization_id,
                        ProjectAccessGrant.user_id == member_user_id,
                        ProjectAccessGrant.revoked_at.is_(None),
                    )
                ).all()
                revoked_at = datetime.now(timezone.utc)
                for grant in grants:
                    grant.revoked_at = revoked_at
                    _audit(
                        session,
                        actor_id,
                        "project.manager_access_revoked",
                        "project",
                        grant.project_id,
                        correlation_id,
                        {
                            "grant_id": str(grant.id),
                            "reason": "organization_membership_removed",
                        },
                    )
                project_memberships = session.scalars(
                    select(ProjectMember)
                    .join(Project, Project.id == ProjectMember.project_id)
                    .where(
                        Project.organization_id == organization_id,
                        ProjectMember.user_id == member_user_id,
                    )
                ).all()
                for project_member in project_memberships:
                    project_id = project_member.project_id
                    role = project_member.role
                    session.delete(project_member)
                    _audit(
                        session,
                        actor_id,
                        "project.member_removed",
                        "project",
                        project_id,
                        correlation_id,
                        {"member_user_id": str(member_user_id), "role": role},
                    )
                session.delete(member)
                session.flush()
                _audit(
                    session,
                    actor_id,
                    "organization.member_removed",
                    "organization",
                    organization_id,
                    correlation_id,
                    {"member_user_id": str(member_user_id), "role": member_role},
                )
        except (ProjectAccessError, ProjectConflict):
            raise
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not remove organization member (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not remove the organization member.") from exc

    def change_organization_member_role(
        self,
        actor_id: UUID,
        organization_id: UUID,
        target_user_id: UUID,
        role: str,
        correlation_id: UUID | None = None,
    ) -> dict[str, str]:
        try:
            with get_session_factory()() as session, session.begin():
                actor_role = _organization_role(session, organization_id, actor_id)
                target = session.get(OrganizationMember, (organization_id, target_user_id))
                if (
                    target is None
                    or not organization_role_can_assign(actor_role, role)
                    or target.role == "owner"
                    or target_user_id == actor_id
                ):
                    raise ProjectAccessError("Organization role assignment is not allowed.")
                _lock_user_project_rows(session, organization_id, target_user_id)
                actor_role = _organization_role(
                    session, organization_id, actor_id, lock=True
                )
                target = session.scalar(
                    select(OrganizationMember)
                    .where(
                        OrganizationMember.organization_id == organization_id,
                        OrganizationMember.user_id == target_user_id,
                    )
                    .with_for_update()
                )
                if (
                    target is None
                    or not organization_role_can_assign(actor_role, role)
                    or target.role == "owner"
                    or target_user_id == actor_id
                ):
                    raise ProjectAccessError("Organization role assignment is not allowed.")
                previous_role = target.role
                target.role = role
                session.flush()
                _audit(
                    session,
                    actor_id,
                    "organization.member_role_changed",
                    "organization",
                    organization_id,
                    correlation_id,
                    {
                        "member_user_id": str(target_user_id),
                        "previous_role": previous_role,
                        "role": role,
                    },
                )
                if previous_role == "manager" and role != "manager":
                    grants = session.scalars(
                        select(ProjectAccessGrant).where(
                            ProjectAccessGrant.user_id == target_user_id,
                            ProjectAccessGrant.revoked_at.is_(None),
                        )
                    ).all()
                    revoked_at = datetime.now(timezone.utc)
                    for grant in grants:
                        grant.revoked_at = revoked_at
                        _audit(
                            session,
                            actor_id,
                            "project.manager_access_revoked",
                            "project",
                            grant.project_id,
                            correlation_id,
                            {"grant_id": str(grant.id), "reason": "organization_role_changed"},
                        )
                return {
                    "organization_id": str(organization_id),
                    "user_id": str(target_user_id),
                    "role": role,
                }
        except ProjectAccessError:
            raise
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not change organization role (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not change the organization role.") from exc

    def transfer_organization_ownership(
        self,
        actor_id: UUID,
        organization_id: UUID,
        target_user_id: UUID,
        correlation_id: UUID | None = None,
    ) -> None:
        try:
            with get_session_factory()() as session, session.begin():
                organization = session.scalar(
                    select(Organization)
                    .where(Organization.id == organization_id)
                    .with_for_update()
                )
                owner = session.get(OrganizationMember, (organization_id, actor_id))
                target = session.get(OrganizationMember, (organization_id, target_user_id))
                owner_count = session.scalar(
                    select(func.count())
                    .select_from(OrganizationMember)
                    .where(
                        OrganizationMember.organization_id == organization_id,
                        OrganizationMember.role == "owner",
                    )
                )
                if (
                    organization is None
                    or owner is None
                    or owner.role != "owner"
                    or target is None
                    or target_user_id == actor_id
                    or owner_count != 1
                ):
                    raise ProjectAccessError("Organization ownership transfer is not available.")
                owner.role = "admin"
                target.role = "owner"
                session.flush()
                _audit(
                    session,
                    actor_id,
                    "organization.ownership_transferred",
                    "organization",
                    organization_id,
                    correlation_id,
                    {"new_owner_user_id": str(target_user_id)},
                )
        except ProjectAccessError:
            raise
        except IntegrityError as exc:
            raise ProjectConflict("Organization ownership changed concurrently.") from exc
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not transfer organization ownership (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not transfer organization ownership.") from exc

    def archive_project(
        self,
        actor_id: UUID,
        project_id: UUID,
        expected_project_version: int,
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        try:
            with get_session_factory()() as session, session.begin():
                project = _require_project_permission(
                    session, project_id, actor_id, Operation.ARCHIVE_PROJECT, lock=True
                )
                if project.version != expected_project_version:
                    raise ProjectConflict("The project version is stale; retrieve it and retry.")
                if project.archived_at is not None:
                    raise ProjectConflict("Project is already archived.")
                project.archived_at = datetime.now(timezone.utc)
                project.archived_by_user_id = actor_id
                session.flush()
                _audit(
                    session,
                    actor_id,
                    "project.archived",
                    "project",
                    project_id,
                    correlation_id,
                    {"project_version": project.version},
                )
                return _project_dict(project)
        except (ProjectAccessError, ProjectConflict):
            raise
        except StaleDataError as exc:
            raise ProjectConflict("The project was updated by another request.") from exc
        except OperationalError as exc:
            if _is_concurrency_error(exc):
                raise ProjectConflict("The project was updated by another request.") from exc
            logger.error("Could not archive project (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not archive the project.") from exc
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not archive project (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not archive the project.") from exc
