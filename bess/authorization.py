"""Central authorization policy for organization and project operations."""
from __future__ import annotations

from enum import Enum


class OrganizationRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MANAGER = "manager"
    MEMBER = "member"


class ProjectRole(str, Enum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class Operation(str, Enum):
    CREATE_PROJECT = "create_project"
    MANAGE_ORGANIZATION = "manage_organization"
    MANAGE_ADMIN_SETTINGS = "manage_admin_settings"
    MANAGE_MEMBERS = "manage_members"
    GRANT_MANAGER_ACCESS = "grant_manager_access"
    READ_PROJECT = "read_project"
    READ_PROJECT_CONTENT = "read_project_content"
    RUN_SIZING = "run_sizing"
    EDIT_PROJECT = "edit_project"
    SAVE_VERSION = "save_version"
    RESTORE_VERSION = "restore_version"
    SHARE_PROJECT = "share_project"
    TRANSFER_PROJECT_OWNERSHIP = "transfer_project_ownership"
    ARCHIVE_PROJECT = "archive_project"


ORGANIZATION_PERMISSIONS: dict[OrganizationRole, frozenset[Operation]] = {
    OrganizationRole.OWNER: frozenset(
        {
            Operation.CREATE_PROJECT,
            Operation.MANAGE_ORGANIZATION,
            Operation.MANAGE_ADMIN_SETTINGS,
            Operation.MANAGE_MEMBERS,
            Operation.GRANT_MANAGER_ACCESS,
        }
    ),
    OrganizationRole.ADMIN: frozenset(
        {
            Operation.MANAGE_ADMIN_SETTINGS,
            Operation.MANAGE_MEMBERS,
            Operation.GRANT_MANAGER_ACCESS,
        }
    ),
    OrganizationRole.MANAGER: frozenset(),
    OrganizationRole.MEMBER: frozenset({Operation.CREATE_PROJECT}),
}


PROJECT_PERMISSIONS: dict[ProjectRole, frozenset[Operation]] = {
    ProjectRole.OWNER: frozenset(
        {
            Operation.READ_PROJECT,
            Operation.READ_PROJECT_CONTENT,
            Operation.RUN_SIZING,
            Operation.EDIT_PROJECT,
            Operation.SAVE_VERSION,
            Operation.RESTORE_VERSION,
            Operation.SHARE_PROJECT,
            Operation.TRANSFER_PROJECT_OWNERSHIP,
            Operation.ARCHIVE_PROJECT,
        }
    ),
    ProjectRole.EDITOR: frozenset(
        {
            Operation.READ_PROJECT,
            Operation.READ_PROJECT_CONTENT,
            Operation.RUN_SIZING,
            Operation.EDIT_PROJECT,
            Operation.SAVE_VERSION,
            Operation.RESTORE_VERSION,
        }
    ),
    ProjectRole.VIEWER: frozenset(
        {
            Operation.READ_PROJECT,
            Operation.READ_PROJECT_CONTENT,
        }
    ),
}


def organization_role_allows(role: str | None, operation: Operation) -> bool:
    try:
        normalized = OrganizationRole(role)
    except (TypeError, ValueError):
        return False
    return operation in ORGANIZATION_PERMISSIONS[normalized]


def project_role_allows(role: str | None, operation: Operation) -> bool:
    try:
        normalized = ProjectRole(role)
    except (TypeError, ValueError):
        return False
    return operation in PROJECT_PERMISSIONS[normalized]


def organization_role_can_assign(actor_role: str | None, target_role: str) -> bool:
    if target_role not in {"admin", "manager", "member"}:
        return False
    if actor_role == OrganizationRole.OWNER:
        return True
    return actor_role == OrganizationRole.ADMIN and target_role in {"manager", "member"}


def organization_role_can_manage_member(
    actor_role: str | None, target_role: str
) -> bool:
    return organization_role_can_assign(actor_role, target_role)
