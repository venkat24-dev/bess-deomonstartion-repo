"""Project authorization tests using an isolated in-memory SQLite database."""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from bess import project_repository
from bess.models import (
    Base,
    Organization,
    OrganizationMember,
    Project,
    ProjectMember,
    User,
)


@pytest.fixture
def project_context(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def attach_schema(connection, _record):
        cursor = connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS bess")
        cursor.close()

    tables = [
        User.__table__,
        Organization.__table__,
        OrganizationMember.__table__,
        Project.__table__,
        ProjectMember.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = lambda: Session(engine, expire_on_commit=False)
    monkeypatch.setattr(project_repository, "get_session_factory", lambda: factory)

    users = {name: uuid4() for name in ("owner", "editor", "viewer", "outsider", "target")}
    organization_id = uuid4()
    with factory() as session, session.begin():
        for name, user_id in users.items():
            session.add(
                User(
                    id=user_id,
                    oidc_issuer="https://issuer.example.test/",
                    oidc_subject=name,
                    is_active=True,
                )
            )
        session.add(Organization(id=organization_id, name="Test organization"))
        for name, role in (
            ("owner", "owner"),
            ("editor", "member"),
            ("viewer", "member"),
            ("target", "member"),
        ):
            session.add(
                OrganizationMember(
                    organization_id=organization_id,
                    user_id=users[name],
                    role=role,
                )
            )
    yield project_repository.ProjectRepository(), factory, users, organization_id
    engine.dispose()


def test_create_list_retrieve_and_update_projects_by_membership(project_context):
    repository, _factory, users, organization_id = project_context
    project = repository.create_project(
        users["owner"], organization_id, "Sizing study", "Initial scenario"
    )
    project_id = uuid4()
    assert project["name"] == "Sizing study"
    assert repository.list_projects(users["outsider"]) == []
    assert repository.get_project(users["outsider"], project_id) is None
    assert repository.get_project(users["owner"], UUID(project["id"])) == project

    repository.add_project_member(users["owner"], UUID(project["id"]), users["editor"], "editor")
    repository.add_project_member(users["owner"], UUID(project["id"]), users["viewer"], "viewer")
    assert [item["id"] for item in repository.list_projects(users["editor"])] == [project["id"]]
    updated = repository.update_project(users["editor"], UUID(project["id"]), {"name": "Revised"})
    assert updated["name"] == "Revised"


def test_project_creation_requires_organization_owner_or_admin(project_context):
    repository, _factory, users, organization_id = project_context
    with pytest.raises(project_repository.ProjectAccessError):
        repository.create_project(users["editor"], organization_id, "Denied", None)


def test_project_writers_and_member_addition_follow_existing_roles(project_context):
    repository, _factory, users, organization_id = project_context
    project = repository.create_project(users["owner"], organization_id, "Project", None)
    project_id = UUID(project["id"])
    repository.add_project_member(users["owner"], project_id, users["editor"], "editor")
    repository.add_project_member(users["owner"], project_id, users["viewer"], "viewer")

    with pytest.raises(project_repository.ProjectAccessError):
        repository.update_project(users["viewer"], project_id, {"name": "Denied"})
    with pytest.raises(project_repository.ProjectAccessError):
        repository.add_project_member(users["editor"], project_id, users["target"], "viewer")

    member = repository.add_project_member(
        users["owner"], project_id, users["target"], "viewer"
    )
    assert member["role"] == "viewer"
    with pytest.raises(project_repository.ProjectConflict):
        repository.add_project_member(users["owner"], project_id, users["target"], "editor")


def test_project_member_must_be_active_and_in_the_same_organization(project_context):
    repository, factory, users, organization_id = project_context
    project = repository.create_project(users["owner"], organization_id, "Project", None)
    project_id = UUID(project["id"])
    with factory() as session, session.begin():
        target = session.get(User, users["target"])
        target.is_active = False
    with pytest.raises(project_repository.ProjectAccessError):
        repository.add_project_member(users["owner"], project_id, users["target"], "viewer")
    with pytest.raises(project_repository.ProjectAccessError):
        repository.add_project_member(users["owner"], project_id, users["outsider"], "viewer")
