from __future__ import annotations

import pytest

from bess.authorization import (
    Operation,
    organization_role_allows,
    organization_role_can_assign,
    project_role_allows,
)


@pytest.mark.parametrize(
    ("role", "operation", "allowed"),
    [
        ("owner", Operation.MANAGE_ORGANIZATION, True),
        ("owner", Operation.MANAGE_MEMBERS, True),
        ("admin", Operation.MANAGE_MEMBERS, True),
        ("admin", Operation.MANAGE_ORGANIZATION, False),
        ("admin", Operation.MANAGE_ADMIN_SETTINGS, True),
        ("admin", Operation.READ_PROJECT_CONTENT, False),
        ("manager", Operation.CREATE_PROJECT, False),
        ("manager", Operation.GRANT_MANAGER_ACCESS, False),
        ("member", Operation.CREATE_PROJECT, True),
        ("member", Operation.MANAGE_MEMBERS, False),
        ("invalid", Operation.MANAGE_MEMBERS, False),
        (None, Operation.MANAGE_MEMBERS, False),
    ],
)
def test_organization_role_permission_matrix(role, operation, allowed):
    assert organization_role_allows(role, operation) is allowed


@pytest.mark.parametrize(
    ("role", "operation", "allowed"),
    [
        ("owner", Operation.TRANSFER_PROJECT_OWNERSHIP, True),
        ("owner", Operation.SHARE_PROJECT, True),
        ("editor", Operation.SAVE_VERSION, True),
        ("editor", Operation.SHARE_PROJECT, False),
        ("editor", Operation.TRANSFER_PROJECT_OWNERSHIP, False),
        ("viewer", Operation.READ_PROJECT_CONTENT, True),
        ("viewer", Operation.SAVE_VERSION, False),
        ("viewer", Operation.ARCHIVE_PROJECT, False),
        ("invalid", Operation.READ_PROJECT, False),
        (None, Operation.READ_PROJECT, False),
    ],
)
def test_project_role_permission_matrix(role, operation, allowed):
    assert project_role_allows(role, operation) is allowed


@pytest.mark.parametrize(
    ("actor_role", "target_role", "allowed"),
    [
        ("owner", "admin", True),
        ("owner", "manager", True),
        ("owner", "member", True),
        ("owner", "owner", False),
        ("admin", "manager", True),
        ("admin", "member", True),
        ("admin", "admin", False),
        ("admin", "owner", False),
        ("manager", "member", False),
        ("member", "admin", False),
    ],
)
def test_organization_role_assignment_matrix(actor_role, target_role, allowed):
    assert organization_role_can_assign(actor_role, target_role) is allowed
