"""Project persistence and membership authorization."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from bess.db_connection import DatabaseConfigurationError, get_session_factory
from bess.models import (
    Organization,
    OrganizationMember,
    Project,
    ProjectMember,
    User,
)

logger = logging.getLogger(__name__)


class ProjectAccessError(RuntimeError):
    """The requested project or organization is not available to the caller."""


class ProjectConflict(RuntimeError):
    """A project or membership changed concurrently or already exists."""


class ProjectPersistenceError(RuntimeError):
    """A non-secret project persistence failure."""


def _project_role(session: Session, project_id: UUID, user_id: UUID) -> str | None:
    return session.scalar(
        select(ProjectMember.role)
        .join(Project, Project.id == ProjectMember.project_id)
        .join(
            OrganizationMember,
            (OrganizationMember.organization_id == Project.organization_id)
            & (OrganizationMember.user_id == ProjectMember.user_id),
        )
        .where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
    )


def _require_project_role(
    session: Session,
    project_id: UUID,
    user_id: UUID,
    allowed_roles: set[str],
) -> Project:
    project = session.get(Project, project_id)
    role = _project_role(session, project_id, user_id)
    if project is None or role not in allowed_roles:
        raise ProjectAccessError("Project not found or access denied.")
    return project


def _project_dict(project: Project) -> dict[str, Any]:
    return {
        "id": str(project.id),
        "organization_id": str(project.organization_id),
        "created_by_user_id": str(project.created_by_user_id) if project.created_by_user_id else None,
        "name": project.name,
        "description": project.description,
        "version": project.version,
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


class ProjectRepository:
    """Own transactional project operations and enforce membership in the data layer."""

    def create_project(
        self,
        user_id: UUID,
        organization_id: UUID,
        name: str,
        description: str | None,
    ) -> dict[str, Any]:
        try:
            with get_session_factory()() as session, session.begin():
                organization = session.get(Organization, organization_id)
                organization_role = session.scalar(
                    select(OrganizationMember.role).where(
                        OrganizationMember.organization_id == organization_id,
                        OrganizationMember.user_id == user_id,
                    )
                )
                if organization is None or organization_role not in {"owner", "admin"}:
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
                return _project_dict(project)
        except ProjectAccessError:
            raise
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not create project (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not create the project.") from exc

    def list_projects(self, user_id: UUID) -> list[dict[str, Any]]:
        try:
            with get_session_factory()() as session:
                projects = session.scalars(
                    select(Project)
                    .join(ProjectMember, ProjectMember.project_id == Project.id)
                    .join(
                        OrganizationMember,
                        (OrganizationMember.organization_id == Project.organization_id)
                        & (OrganizationMember.user_id == ProjectMember.user_id),
                    )
                    .where(ProjectMember.user_id == user_id)
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
                    project = _require_project_role(
                        session, project_id, user_id, {"owner", "editor", "viewer"}
                    )
                except ProjectAccessError:
                    return None
                return _project_dict(project)
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not retrieve project (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not retrieve the project.") from exc

    def update_project(
        self,
        user_id: UUID,
        project_id: UUID,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            with get_session_factory()() as session, session.begin():
                project = _require_project_role(
                    session, project_id, user_id, {"owner", "editor"}
                )
                for field, value in changes.items():
                    setattr(project, field, value)
                session.flush()
                return _project_dict(project)
        except ProjectAccessError:
            raise
        except StaleDataError as exc:
            raise ProjectConflict("The project was updated by another request.") from exc
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not update project (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not update the project.") from exc

    def add_project_member(
        self,
        caller_id: UUID,
        project_id: UUID,
        member_user_id: UUID,
        role: str,
    ) -> dict[str, str]:
        try:
            with get_session_factory()() as session, session.begin():
                project = _require_project_role(session, project_id, caller_id, {"owner"})
                target = session.get(User, member_user_id)
                organization_member = session.get(
                    OrganizationMember, (project.organization_id, member_user_id)
                )
                if target is None or not target.is_active or organization_member is None:
                    raise ProjectAccessError("Active organization member not found.")
                if _project_role(session, project_id, member_user_id) is not None:
                    raise ProjectConflict("User is already a project member.")
                member = ProjectMember(project_id=project_id, user_id=member_user_id, role=role)
                session.add(member)
                session.flush()
                return _member_dict(project_id, member_user_id, role, member.added_at)
        except (ProjectAccessError, ProjectConflict):
            raise
        except IntegrityError as exc:
            raise ProjectConflict("Project membership already exists.") from exc
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not add project member (%s)", type(exc).__name__)
            raise ProjectPersistenceError("Could not add the project member.") from exc
