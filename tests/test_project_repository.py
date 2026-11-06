"""Project authorization tests using an isolated in-memory SQLite database."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bess import project_repository
from bess.models import (
    Base,
    AuditEvent,
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
        ProjectVersion.__table__,
        AuditEvent.__table__,
        ProjectAccessGrant.__table__,
        SizingRequest.__table__,
        SimulationRun.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """CREATE TRIGGER bess.audit_events_immutable_update
            BEFORE UPDATE ON audit_events BEGIN
            SELECT RAISE(ABORT, 'audit_events is append-only');
            END"""
        )
        connection.exec_driver_sql(
            """CREATE TRIGGER bess.audit_events_immutable_delete
            BEFORE DELETE ON audit_events BEGIN
            SELECT RAISE(ABORT, 'audit_events is append-only');
            END"""
        )
        connection.exec_driver_sql(
            """CREATE TRIGGER bess.project_versions_immutable_update
            BEFORE UPDATE ON project_versions BEGIN
            SELECT RAISE(ABORT, 'project_versions are immutable');
            END"""
        )
        connection.exec_driver_sql(
            """CREATE TRIGGER bess.project_versions_immutable_delete
            BEFORE DELETE ON project_versions BEGIN
            SELECT RAISE(ABORT, 'project_versions are immutable');
            END"""
        )
    factory = lambda: Session(engine, expire_on_commit=False)
    monkeypatch.setattr(project_repository, "get_session_factory", lambda: factory)

    users = {
        name: uuid4()
        for name in ("owner", "editor", "viewer", "outsider", "target", "manager", "admin")
    }
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
            ("manager", "manager"),
            ("admin", "admin"),
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
    assert repository.get_project(users["owner"], UUID(project["id"])) == {
        **project,
        "current_user_role": "owner",
    }

    repository.add_project_member(users["owner"], UUID(project["id"]), users["editor"], "editor")
    repository.add_project_member(users["owner"], UUID(project["id"]), users["viewer"], "viewer")
    listed_project = repository.list_projects(users["editor"])[0]
    assert listed_project["id"] == project["id"]
    assert listed_project["current_user_role"] == "editor"
    assert listed_project["owner_user_id"] == str(users["owner"])
    assert listed_project["owner_display_name"] is None
    updated = repository.update_project(
        users["editor"], UUID(project["id"]), 1, {"name": "Revised"}
    )
    assert updated["name"] == "Revised"


def test_project_creation_requires_organization_membership(project_context):
    repository, _factory, users, organization_id = project_context
    project = repository.create_project(users["editor"], organization_id, "Member project", None)
    assert project["created_by_user_id"] == str(users["editor"])
    with pytest.raises(project_repository.ProjectAccessError):
        repository.create_project(users["outsider"], organization_id, "Denied", None)


def test_project_owner_can_archive_with_version_check_and_other_roles_cannot(project_context):
    repository, _factory, users, organization_id = project_context
    project = repository.create_project(
        users["owner"], organization_id, "Archive study", None
    )
    project_id = UUID(project["id"])

    with pytest.raises(project_repository.ProjectAccessError):
        repository.archive_project(users["editor"], project_id, 1)

    archived = repository.archive_project(users["owner"], project_id, 1)
    assert archived["archived_at"] is not None
    assert repository.get_project(users["owner"], project_id)["archived_at"] is not None


def test_project_writers_and_member_addition_follow_existing_roles(project_context):
    repository, _factory, users, organization_id = project_context
    project = repository.create_project(users["owner"], organization_id, "Project", None)
    project_id = UUID(project["id"])
    repository.add_project_member(users["owner"], project_id, users["editor"], "editor")
    repository.add_project_member(users["owner"], project_id, users["viewer"], "viewer")

    with pytest.raises(project_repository.ProjectAccessError):
        repository.update_project(users["viewer"], project_id, 1, {"name": "Denied"})
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


def _insert_completed_sizing(factory, user_id, project_id, source_catalog_sha256="a" * 64):
    request_id = uuid4()
    input_snapshot = {
        "schema_version": "sizing-input-v1",
        "submitted_request": {"required_energy_mwh": 90},
        "engine_inputs": {"required_energy_mwh": 90},
        "container": {"id": "test-container", "energy_mwh": 7.044},
        "source": {
            "workbook_sha256": source_catalog_sha256,
            "container_key": "test-container",
        },
        "application": {"engine_source_sha256": "test-engine"},
    }
    result_snapshot = {
        "request_id": str(request_id),
        "accepted": True,
        "verification": [],
    }
    with factory() as session, session.begin():
        request = SizingRequest(
            id=request_id,
            project_id=project_id,
            created_by_user_id=user_id,
            status="COMPLETED",
            input_snapshot=input_snapshot,
            input_schema_version="sizing-input-v1",
            application_version="1.0.0",
        )
        request.simulation_runs.append(
            SimulationRun(
                request_id=request_id,
                status="SUCCEEDED",
                source_container_key="test-container",
                source_catalog_sha256=source_catalog_sha256,
                result_snapshot=result_snapshot,
                engine_version="sha256:test-engine",
            )
        )
        session.add(request)
    return request_id, input_snapshot, result_snapshot


def test_project_version_save_and_restore_are_immutable(project_context):
    repository, factory, users, organization_id = project_context
    project = repository.create_project(users["owner"], organization_id, "Versioned", None)
    project_id = UUID(project["id"])
    repository.add_project_member(users["owner"], project_id, users["viewer"], "viewer")
    request_id, original_inputs, original_result = _insert_completed_sizing(
        factory, users["owner"], project_id
    )

    first = repository.create_version(
        users["owner"], project_id, request_id, 0, "Initial sizing"
    )
    assert first["version_number"] == 1
    with pytest.raises(project_repository.ProjectConflict, match="stale"):
        repository.create_version(users["owner"], project_id, request_id, 0, "Stale retry")

    second_request_id, changed_inputs, changed_result = _insert_completed_sizing(
        factory, users["owner"], project_id, "b" * 64
    )
    second = repository.create_version(
        users["owner"], project_id, second_request_id, 1, "Changed sizing"
    )
    restored = repository.restore_version(users["owner"], project_id, 1, 2, "Restore first")
    assert second["version_number"] == 2
    assert restored["version_number"] == 3
    assert restored["source_version_number"] == 1

    history = repository.list_versions(users["viewer"], project_id)
    assert [item["version_number"] for item in history] == [1, 2, 3]
    original = repository.get_version(users["viewer"], project_id, 1)
    restored_snapshot = repository.get_version(users["owner"], project_id, 3)
    assert original["input_snapshot"] == original_inputs
    assert original["result_snapshot"] == original_result
    assert restored_snapshot["input_snapshot"] == original_inputs
    assert restored_snapshot["result_snapshot"] == original_result
    assert changed_inputs != original_inputs
    assert changed_result != original_result
    with factory() as session:
        project_record = session.get(Project, project_id)
        assert project_record.current_version_number == 3
        assert session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action.in_(("project.version_saved", "project.version_restored"))
            )
        ) == 3


def test_version_and_audit_write_roll_back_together(project_context, monkeypatch):
    repository, factory, users, organization_id = project_context
    project = repository.create_project(users["owner"], organization_id, "Rollback", None)
    project_id = UUID(project["id"])
    request_id, _, _ = _insert_completed_sizing(factory, users["owner"], project_id)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(project_repository, "_audit", fail_audit)
    with pytest.raises(RuntimeError, match="simulated"):
        repository.create_version(users["owner"], project_id, request_id, 0, None)

    with factory() as session:
        assert session.get(Project, project_id).current_version_number is None
        assert session.scalar(
            select(func.count()).select_from(ProjectVersion).where(
                ProjectVersion.project_id == project_id
            )
        ) == 0


def test_manager_needs_audited_project_scope_and_admin_cannot_read_content(project_context):
    repository, factory, users, organization_id = project_context
    project = repository.create_project(users["owner"], organization_id, "Restricted", None)
    project_id = UUID(project["id"])
    repository.add_project_member(users["owner"], project_id, users["viewer"], "viewer")
    request_id, _, _ = _insert_completed_sizing(factory, users["owner"], project_id)
    repository.create_version(users["owner"], project_id, request_id, 0, "Restricted results")

    assert repository.get_project(users["manager"], project_id) is None
    assert repository.get_project(users["admin"], project_id) is None
    grant = repository.grant_manager_access(
        users["admin"],
        project_id,
        users["manager"],
        "management",
        "Team oversight",
        None,
    )
    assert repository.get_project(users["manager"], project_id) is not None
    assert repository.get_project(users["admin"], project_id) is None
    with pytest.raises(project_repository.ProjectAccessError):
        repository.get_version(users["manager"], project_id, 1)
    assert any(item["id"] == project["id"] for item in repository.list_projects(users["manager"]))
    content_grant = repository.grant_manager_access(
        users["admin"],
        project_id,
        users["manager"],
        "content_read",
        "Approved result review",
        None,
    )
    assert repository.get_version(users["manager"], project_id, 1)["version_number"] == 1
    repository.revoke_manager_access(
        users["admin"], project_id, UUID(content_grant["id"])
    )
    repository.revoke_manager_access(users["admin"], project_id, UUID(grant["id"]))
    assert repository.get_project(users["manager"], project_id) is None


def test_project_and_audit_records_reject_mutation(project_context):
    repository, factory, users, organization_id = project_context
    project = repository.create_project(users["owner"], organization_id, "Append-only", None)
    project_id = UUID(project["id"])
    request_id, _, _ = _insert_completed_sizing(factory, users["owner"], project_id)
    repository.create_version(users["owner"], project_id, request_id, 0, None)

    with factory() as session:
        with pytest.raises(IntegrityError, match="project_versions are immutable"):
            session.execute(
                update(ProjectVersion)
                .where(ProjectVersion.project_id == project_id)
                .values(change_summary="tampered")
            )
    with factory() as session:
        with pytest.raises(IntegrityError, match="audit_events is append-only"):
            session.execute(
                update(AuditEvent)
                .where(AuditEvent.resource_id == project_id)
                .values(action="tampered")
            )


def test_stale_project_metadata_update_is_rejected(project_context):
    repository, _factory, users, organization_id = project_context
    project = repository.create_project(users["owner"], organization_id, "CAS", None)
    project_id = UUID(project["id"])

    changed = repository.update_project(users["owner"], project_id, 1, {"name": "First"})
    assert changed["version"] == 2
    with pytest.raises(project_repository.ProjectConflict, match="stale"):
        repository.update_project(users["owner"], project_id, 1, {"name": "Lost update"})


def test_two_simultaneous_updates_using_same_expected_version_have_one_winner(
    tmp_path: Path, monkeypatch
):
    database_path = tmp_path / "concurrent-main.sqlite"
    schema_path = tmp_path / "concurrent-bess.sqlite"
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(database_path)),
        connect_args={"timeout": 2},
    )

    @event.listens_for(engine, "connect")
    def attach_schema(connection, _record):
        cursor = connection.cursor()
        cursor.execute("ATTACH DATABASE ? AS bess", (str(schema_path),))
        cursor.close()

    tables = [
        User.__table__,
        Organization.__table__,
        OrganizationMember.__table__,
        Project.__table__,
        ProjectMember.__table__,
        ProjectVersion.__table__,
        AuditEvent.__table__,
        ProjectAccessGrant.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = lambda: Session(engine, expire_on_commit=False)
    monkeypatch.setattr(project_repository, "get_session_factory", lambda: factory)

    owner_id = uuid4()
    organization_id = uuid4()
    with factory() as session, session.begin():
        session.add(
            User(
                id=owner_id,
                oidc_issuer="https://issuer.example.test/",
                oidc_subject="concurrent-owner",
                is_active=True,
            )
        )
        session.add(Organization(id=organization_id, name="Concurrent tests"))
        session.add(
            OrganizationMember(
                organization_id=organization_id, user_id=owner_id, role="owner"
            )
        )
    repository = project_repository.ProjectRepository()
    project_id = UUID(
        repository.create_project(owner_id, organization_id, "Concurrent", None)["id"]
    )

    original_require = project_repository._require_project_permission
    synchronize_reads = Barrier(2)

    def synchronized_permission(session, *args, **kwargs):
        project = original_require(session, *args, **kwargs)
        synchronize_reads.wait(timeout=10)
        return project

    monkeypatch.setattr(
        project_repository, "_require_project_permission", synchronized_permission
    )

    def update(name):
        try:
            repository.update_project(owner_id, project_id, 1, {"name": name})
            return "saved"
        except project_repository.ProjectConflict:
            return "conflict"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(update, ("Concurrent A", "Concurrent B")))
        assert sorted(results) == ["conflict", "saved"]
        with factory() as session:
            project = session.get(Project, project_id)
            assert project.version == 2
            assert project.name in {"Concurrent A", "Concurrent B"}
            assert session.scalar(
                select(func.count()).select_from(AuditEvent).where(
                    AuditEvent.action == "project.updated"
                )
            ) == 1
    finally:
        engine.dispose()


def test_member_creation_and_audit_are_atomic(project_context, monkeypatch):
    repository, factory, users, organization_id = project_context

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(project_repository, "_audit", fail_audit)
    with pytest.raises(RuntimeError, match="simulated"):
        repository.create_project(users["editor"], organization_id, "Atomic", None)

    with factory() as session:
        assert session.scalar(select(Project.id).where(Project.name == "Atomic")) is None
        assert session.scalar(
            select(ProjectMember.project_id).where(ProjectMember.user_id == users["editor"])
        ) is None


def test_ownership_transfer_requires_existing_same_organization_member(project_context):
    repository, factory, users, organization_id = project_context
    project = repository.create_project(users["owner"], organization_id, "Transfer", None)
    project_id = UUID(project["id"])
    repository.add_project_member(users["owner"], project_id, users["target"], "viewer")

    transferred = repository.transfer_project_ownership(
        users["owner"], project_id, users["target"], 1
    )
    assert transferred["version"] == 2
    with factory() as session:
        assert session.get(ProjectMember, (project_id, users["owner"])).role == "editor"
        assert session.get(ProjectMember, (project_id, users["target"])).role == "owner"
    with pytest.raises(project_repository.ProjectAccessError):
        repository.transfer_project_ownership(
            users["owner"], project_id, users["target"], 2
        )


def test_cross_organization_project_access_is_denied(project_context):
    repository, factory, users, organization_id = project_context
    own_project = repository.create_project(users["owner"], organization_id, "Private A", None)
    other_organization_id = uuid4()
    with factory() as session, session.begin():
        session.add(Organization(id=other_organization_id, name="Organization B"))
        session.add(
            OrganizationMember(
                organization_id=other_organization_id,
                user_id=users["outsider"],
                role="owner",
            )
        )
    other_project = repository.create_project(
        users["outsider"], other_organization_id, "Private B", None
    )

    assert repository.get_project(users["outsider"], UUID(own_project["id"])) is None
    assert repository.get_project(users["owner"], UUID(other_project["id"])) is None
    with pytest.raises(project_repository.ProjectAccessError):
        repository.add_project_member(
            users["owner"], UUID(own_project["id"]), users["outsider"], "viewer"
        )


def test_admin_cannot_escalate_to_admin_or_transfer_organization_ownership(project_context):
    repository, _factory, users, organization_id = project_context
    with pytest.raises(project_repository.ProjectAccessError):
        repository.change_organization_member_role(
            users["admin"], organization_id, users["editor"], "admin"
        )
    with pytest.raises(project_repository.ProjectAccessError):
        repository.transfer_organization_ownership(
            users["admin"], organization_id, users["editor"]
        )
    changed = repository.change_organization_member_role(
        users["admin"], organization_id, users["editor"], "manager"
    )
    assert changed["role"] == "manager"


def test_manager_role_removal_revokes_scoped_project_grants(project_context):
    repository, factory, users, organization_id = project_context
    project = repository.create_project(users["owner"], organization_id, "Manager grant", None)
    project_id = UUID(project["id"])
    repository.grant_manager_access(
        users["admin"],
        project_id,
        users["manager"],
        "content_read",
        "Temporary review",
        None,
    )
    assert repository.get_project(users["manager"], project_id) is not None

    repository.change_organization_member_role(
        users["owner"], organization_id, users["manager"], "member"
    )
    assert repository.get_project(users["manager"], project_id) is None
    with factory() as session:
        grant = session.scalar(
            select(ProjectAccessGrant).where(
                ProjectAccessGrant.project_id == project_id,
                ProjectAccessGrant.user_id == users["manager"],
            )
        )
        assert grant.revoked_at is not None
        assert session.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == "project.manager_access_revoked"
            )
        ) == 1


def test_project_owner_can_update_and_remove_non_owner_shares(project_context):
    repository, factory, users, organization_id = project_context
    project = repository.create_project(users["owner"], organization_id, "Share changes", None)
    project_id = UUID(project["id"])
    repository.add_project_member(users["owner"], project_id, users["target"], "viewer")

    updated = repository.update_project_member_role(
        users["owner"], project_id, users["target"], "editor"
    )
    assert updated["role"] == "editor"
    assert repository.get_project(users["target"], project_id) is not None
    with pytest.raises(project_repository.ProjectAccessError):
        repository.update_project_member_role(
            users["owner"], project_id, users["owner"], "viewer"
        )

    repository.remove_project_member(users["owner"], project_id, users["target"])
    assert repository.get_project(users["target"], project_id) is None
    with factory() as session:
        assert session.get(ProjectMember, (project_id, users["target"])) is None
        actions = set(
            session.scalars(
                select(AuditEvent.action).where(AuditEvent.resource_id == project_id)
            )
        )
        assert "project.member_role_changed" in actions
        assert "project.member_removed" in actions


def test_organization_member_add_remove_enforces_roles_and_revokes_project_access(
    project_context,
):
    repository, factory, users, organization_id = project_context
    member = repository.add_organization_member(
        users["owner"], organization_id, users["outsider"], "manager"
    )
    assert member["role"] == "manager"
    with pytest.raises(project_repository.ProjectAccessError):
        repository.add_organization_member(
            users["admin"], organization_id, users["outsider"], "admin"
        )
    project = repository.create_project(users["owner"], organization_id, "Member removal", None)
    project_id = UUID(project["id"])
    repository.grant_manager_access(
        users["admin"],
        project_id,
        users["outsider"],
        "content_read",
        "Temporary project access",
        None,
    )
    repository.add_project_member(users["owner"], project_id, users["outsider"], "viewer")

    repository.remove_organization_member(
        users["admin"], organization_id, users["outsider"]
    )
    assert repository.get_project(users["outsider"], project_id) is None
    with factory() as session:
        assert session.get(OrganizationMember, (organization_id, users["outsider"])) is None
        assert session.get(ProjectMember, (project_id, users["outsider"])) is None
        grant = session.scalar(
            select(ProjectAccessGrant).where(
                ProjectAccessGrant.project_id == project_id,
                ProjectAccessGrant.user_id == users["outsider"],
            )
        )
        assert grant.revoked_at is not None


def test_active_project_owner_must_transfer_before_membership_removal(project_context):
    repository, factory, users, organization_id = project_context
    project = repository.create_project(users["editor"], organization_id, "Owner removal", None)
    project_id = UUID(project["id"])

    with pytest.raises(project_repository.ProjectConflict, match="Transfer ownership"):
        repository.remove_organization_member(
            users["owner"], organization_id, users["editor"]
        )
    with factory() as session:
        assert session.get(OrganizationMember, (organization_id, users["editor"])) is not None
        assert session.get(ProjectMember, (project_id, users["editor"])).role == "owner"
