"""Isolated repository tests using an in-memory SQLite schema."""
from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from bess import simulation_repository
from bess import project_repository
from bess.models import (
    Base,
    Organization,
    OrganizationMember,
    Project,
    ProjectMember,
    SimulationRun,
    SizingRequest,
    User,
)


@pytest.fixture
def repository(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def attach_schema(connection, _record):
        cursor = connection.cursor()
        cursor.execute("ATTACH DATABASE ':memory:' AS bess")
        connection.create_function(
            "jsonb_typeof",
            1,
            lambda value: (
                {"dict": "object", "list": "array"}.get(type(json.loads(value)).__name__)
                if value is not None
                else None
            ),
        )
        cursor.close()

    tables = [
        User.__table__,
        Organization.__table__,
        OrganizationMember.__table__,
        Project.__table__,
        ProjectMember.__table__,
        SizingRequest.__table__,
        SimulationRun.__table__,
    ]
    SizingRequest.metadata.create_all(engine, tables=tables)
    repository_user_id = uuid4()
    with Session(engine) as session:
        session.add(
            User(
                id=repository_user_id,
                oidc_issuer="https://issuer.example.test/",
                oidc_subject="repository-test-user",
                is_active=True,
            )
        )
        session.commit()
    monkeypatch.setattr(
        simulation_repository,
        "get_session_factory",
        lambda: lambda: Session(engine, expire_on_commit=False),
    )
    monkeypatch.setattr(
        project_repository,
        "get_session_factory",
        lambda: lambda: Session(engine, expire_on_commit=False),
    )
    repository = simulation_repository.SimulationRepository()
    repository.test_engine = engine
    repository.test_user_id = repository_user_id
    yield repository
    engine.dispose()


def _snapshot(value: int = 100) -> dict:
    return {
        "schema_version": "sizing-input-v1",
        "submitted_request": {"container_id": "container-a", "required_energy_mwh": value},
        "engine_inputs": {"required_energy_mwh": value},
    }


def _start(
    repository,
    snapshot: dict | None = None,
    key: str | None = None,
    user_id=None,
    project_id=None,
):
    request_id = uuid4()
    result = repository.start_run(
        request_id,
        user_id or repository.test_user_id,
        project_id,
        snapshot or _snapshot(),
        "container-a",
        "a" * 64,
        "sha256:engine",
        "1.0.0",
        key,
    )
    return request_id, result


def test_persists_and_retrieves_completed_run(repository):
    request_id, duplicate = _start(repository, key="stable-key")
    assert duplicate is None
    before = repository.get_run(request_id, repository.test_user_id)
    assert before["status"] == "RUNNING"
    assert before["request"]["required_energy_mwh"] == 100

    response = {
        "request_id": str(request_id),
        "container_id": "container-a",
        "accepted": True,
        "margin_applied_pct": 14.0,
        "in_range": True,
        "note": "OK",
        "summary": {"n_init": 21},
        "discharging": [],
        "charging": [],
        "performance": [],
        "verification": [],
    }
    repository.complete_run(request_id, response, ["warning"], {"n_init": 21})

    retrieved = repository.get_run(request_id, repository.test_user_id)
    assert retrieved["response"] == response
    assert "status" not in retrieved
    assert retrieved["request"]["required_energy_mwh"] == 100
    with Session(repository.test_engine) as session:
        run = session.scalar(select(SimulationRun).where(SimulationRun.request_id == request_id))
        request = session.get(SizingRequest, request_id)
        assert run.source_container_key == "container-a"
        assert run.source_catalog_sha256 == "a" * 64
        assert run.engine_version == "sha256:engine"
        assert run.warnings == ["warning"]
        assert run.initial_container_count == 21
        assert request.input_snapshot["engine_inputs"]["required_energy_mwh"] == 100


def test_idempotency_returns_result_or_rejects_changed_input(repository):
    request_id, _ = _start(repository, key="same-key")
    response = {
        "request_id": str(request_id),
        "container_id": "container-a",
        "accepted": True,
        "margin_applied_pct": 14.0,
        "in_range": True,
        "note": "OK",
        "summary": {"n_init": 21},
        "discharging": [],
        "charging": [],
        "performance": [],
        "verification": [],
    }
    repository.complete_run(request_id, response, [], {"n_init": 21})

    _, duplicate = _start(repository, key="same-key")
    assert duplicate == response
    with pytest.raises(simulation_repository.IdempotencyConflict):
        _start(repository, _snapshot(101), key="same-key")


def test_idempotency_ignores_execution_environment_changes(repository):
    request_id, _ = _start(repository, key="deployment-retry")
    response = {
        "request_id": str(request_id),
        "container_id": "container-a",
        "accepted": True,
        "margin_applied_pct": 14.0,
        "in_range": True,
        "note": "OK",
        "summary": {"n_init": 21},
        "discharging": [],
        "charging": [],
        "performance": [],
        "verification": [],
    }
    repository.complete_run(request_id, response, [], {"n_init": 21})
    changed_runtime = {
        **_snapshot(),
        "application": {"engine_source_sha256": "new-engine"},
        "runtime": {"python": "different"},
    }
    _, duplicate = _start(repository, changed_runtime, key="deployment-retry")
    assert duplicate == response


def test_idempotency_uses_resolved_input_not_original_submission(repository):
    normalized = _snapshot()
    normalized["submitted_request"] = {
        "container_id": "container-a",
        "required_energy_mwh": 100,
        "bol_energy_mwh": None,
    }
    request_id, _ = _start(repository, normalized, key="normalized")
    response = {
        "request_id": str(request_id),
        "container_id": "container-a",
        "accepted": True,
        "margin_applied_pct": 14.0,
        "in_range": True,
        "note": "OK",
        "summary": {"n_init": 21},
        "discharging": [],
        "charging": [],
        "performance": [],
        "verification": [],
    }
    repository.complete_run(request_id, response, [], {"n_init": 21})
    equivalent = {
        **normalized,
        "submitted_request": {
            "container_id": "container-a",
            "required_energy_mwh": 100,
            "bol_energy_mwh": 100,
        },
    }
    _, duplicate = _start(repository, equivalent, key="normalized")
    assert duplicate == response


def test_in_progress_duplicate_and_failure_state(repository):
    request_id, _ = _start(repository, key="running-key")
    with pytest.raises(simulation_repository.SimulationInProgress):
        _start(repository, key="running-key")

    repository.fail_run(request_id, "calculation_error", "calculation failed")
    retrieved = repository.get_run(request_id, repository.test_user_id)
    assert retrieved["status"] == "FAILED"
    assert retrieved["error"] == {
        "code": "calculation_error",
        "message": "calculation failed",
    }
    repository.fail_run(request_id, "later_error", "must not replace original failure")
    assert repository.get_run(request_id, repository.test_user_id)["error"]["message"] == "calculation failed"
    with pytest.raises(simulation_repository.SimulationPreviouslyFailed) as error:
        _start(repository, key="running-key")
    assert error.value.request_id == request_id


def test_failure_does_not_overwrite_a_completed_run(repository):
    request_id, _ = _start(repository)
    response = {
        "request_id": str(request_id),
        "container_id": "container-a",
        "accepted": True,
        "margin_applied_pct": 14.0,
        "in_range": True,
        "note": "OK",
        "summary": {"n_init": 21},
        "discharging": [],
        "charging": [],
        "performance": [],
        "verification": [],
    }
    repository.complete_run(request_id, response, [], {"n_init": 21})

    repository.fail_run(request_id, "persistence_error", "late acknowledgement failure")

    retrieved = repository.get_run(request_id, repository.test_user_id)
    assert retrieved["response"] == response
    assert "status" not in retrieved


def test_failed_result_write_rolls_back_all_result_fields(repository):
    request_id, _ = _start(repository)
    def fail_commit(_session):
        raise SQLAlchemyError("injected commit failure")

    event.listen(Session, "before_commit", fail_commit)
    try:
        with pytest.raises(simulation_repository.SimulationPersistenceError):
            repository.complete_run(
                request_id,
                {
                    "margin_applied_pct": 14.0,
                    "in_range": True,
                    "accepted": True,
                    "note": "OK",
                },
                [],
                {"n_init": 21},
            )
    finally:
        event.remove(Session, "before_commit", fail_commit)

    with Session(repository.test_engine) as session:
        run = session.scalar(select(SimulationRun).where(SimulationRun.request_id == request_id))
        assert run.status == "RUNNING"
        assert run.result_snapshot is None


def test_independent_simulations_create_distinct_request_and_run_rows(repository):
    first_id, _ = _start(repository)
    second_id, _ = _start(repository, _snapshot(200))
    assert first_id != second_id

    with Session(repository.test_engine) as session:
        assert session.scalar(select(SizingRequest.id).where(SizingRequest.id == first_id))
        assert session.scalar(select(SizingRequest.id).where(SizingRequest.id == second_id))
        assert session.scalar(select(SimulationRun.id).where(SimulationRun.request_id == first_id))
        assert session.scalar(select(SimulationRun.id).where(SimulationRun.request_id == second_id))


def test_simulation_access_is_owned_or_project_member_authorized(repository):
    owner_id = repository.test_user_id
    viewer_id = uuid4()
    outsider_id = uuid4()
    organization_id = uuid4()
    with Session(repository.test_engine) as session, session.begin():
        session.add_all(
            [
                User(
                    id=viewer_id,
                    oidc_issuer="https://issuer.example.test/",
                    oidc_subject="viewer",
                    is_active=True,
                ),
                User(
                    id=outsider_id,
                    oidc_issuer="https://issuer.example.test/",
                    oidc_subject="outsider",
                    is_active=True,
                ),
                Organization(id=organization_id, name="Simulation test"),
                OrganizationMember(
                    organization_id=organization_id,
                    user_id=owner_id,
                    role="owner",
                ),
                OrganizationMember(
                    organization_id=organization_id,
                    user_id=viewer_id,
                    role="member",
                ),
            ]
        )
    projects = project_repository.ProjectRepository()
    project = projects.create_project(owner_id, organization_id, "Scenario", None)
    project_id = UUID(project["id"])
    projects.add_project_member(owner_id, project_id, viewer_id, "viewer")

    request_id, _ = _start(repository, user_id=owner_id, project_id=project_id)
    assert repository.get_run(request_id, viewer_id)["status"] == "RUNNING"
    assert repository.get_run(request_id, outsider_id) is None
    with pytest.raises(project_repository.ProjectAccessError):
        _start(repository, user_id=viewer_id, project_id=project_id)

    private_request, _ = _start(repository, user_id=owner_id)
    assert repository.get_run(private_request, outsider_id) is None


@pytest.mark.parametrize("corrupt_field", ["input_snapshot", "result_snapshot"])
def test_corrupt_persisted_json_is_reported_as_persistence_error(repository, corrupt_field):
    request_id, _ = _start(repository)
    with Session(repository.test_engine) as session:
        record = session.get(SizingRequest, request_id)
        run = session.scalar(select(SimulationRun).where(SimulationRun.request_id == request_id))
        if corrupt_field == "input_snapshot":
            record.input_snapshot = ["not", "an", "object"]
        else:
            run.result_snapshot = {"incomplete": True}
            run.status = "SUCCEEDED"
        session.commit()

    with pytest.raises(simulation_repository.SimulationPersistenceError, match="stored sizing"):
        repository.get_run(request_id, repository.test_user_id)


@pytest.mark.parametrize(
    ("run_status", "request_status", "result"),
    [
        ("RUNNING", "SUBMITTED", {"incomplete": True}),
        ("SUCCEEDED", "COMPLETED", None),
    ],
)
def test_inconsistent_completed_result_is_not_exposed(repository, run_status, request_status, result):
    request_id, _ = _start(repository)
    with Session(repository.test_engine) as session:
        record = session.get(SizingRequest, request_id)
        run = session.scalar(select(SimulationRun).where(SimulationRun.request_id == request_id))
        record.status = request_status
        run.status = run_status
        run.result_snapshot = result
        session.commit()

    with pytest.raises(simulation_repository.SimulationPersistenceError, match="sizing result"):
        repository.get_run(request_id, repository.test_user_id)


def test_runtime_api_has_no_table_creation():
    from pathlib import Path
    import api

    source = Path(api.__file__).read_text(encoding="utf-8")
    assert "CREATE TABLE" not in source.upper()
