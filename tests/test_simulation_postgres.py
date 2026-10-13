"""PostgreSQL integration tests for managed simulation persistence."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from api import app, get_repository
from bess.authentication import AuthenticatedUser, get_current_user
from bess import simulation_repository
from bess.models import SimulationRun, SizingRequest, User


EXPECTED_REVISION = "20261012_0003"
REQUIRED_TABLES = {"sizing_requests", "simulation_runs"}


@dataclass
class PostgreSQLContext:
    engine: object
    session_factory: sessionmaker[Session]
    request_ids: list[UUID] = field(default_factory=list)
    idempotency_keys: list[str] = field(default_factory=list)
    user_id: UUID | None = None


@pytest.fixture
def postgres_context(monkeypatch) -> PostgreSQLContext:
    raw_url = os.environ.get("BESS_SIMULATION_TEST_DATABASE_URL")
    if not raw_url:
        if os.environ.get("BESS_REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail(
                "BESS_REQUIRE_POSTGRES_TESTS=1 but BESS_SIMULATION_TEST_DATABASE_URL is not configured."
            )
        pytest.skip(
            "BLOCKED: configure BESS_SIMULATION_TEST_DATABASE_URL to a dedicated migrated PostgreSQL test database."
        )

    try:
        url = make_url(raw_url)
    except Exception:
        pytest.fail("BESS_SIMULATION_TEST_DATABASE_URL is not a valid SQLAlchemy URL.")
    database_name = (url.database or "").casefold()
    if not url.drivername.startswith("postgresql"):
        pytest.fail("BESS_SIMULATION_TEST_DATABASE_URL must use PostgreSQL.")
    if database_name == "bess_db" or not re.search(r"(^|[_-])tests?($|[_-])", database_name):
        pytest.fail(
            "Refusing PostgreSQL integration writes unless the explicit database name is test-only and not bess_db."
        )
    application_url = os.environ.get("DATABASE_URL") or dotenv_values(
        Path(__file__).resolve().parents[1] / ".env"
    ).get("DATABASE_URL")
    if application_url:
        try:
            application_database = make_url(application_url).database
        except Exception:
            pytest.fail("Could not safely compare the simulation test and application database targets.")
        if application_database == url.database:
            pytest.fail("Simulation test and application database targets must be different.")

    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            actual_name = connection.scalar(text("SELECT current_database()"))
            revision = connection.scalar(text("SELECT version_num FROM bess.alembic_version"))
            tables = set(
                connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'bess' AND table_type = 'BASE TABLE'"
                    )
                ).scalars()
            )
    except Exception as exc:
        engine.dispose()
        pytest.fail(f"Could not verify the isolated PostgreSQL test target ({type(exc).__name__}).")
    if actual_name != url.database:
        engine.dispose()
        pytest.fail("Connected PostgreSQL database does not match the explicitly configured test target.")
    if revision != EXPECTED_REVISION:
        engine.dispose()
        pytest.fail(
            f"PostgreSQL test target is at revision {revision!r}; expected {EXPECTED_REVISION!r}. "
            "No migration was run automatically."
        )
    if not REQUIRED_TABLES.issubset(tables):
        engine.dispose()
        pytest.fail("PostgreSQL test target is missing the migrated simulation tables.")

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    user_id = uuid4()
    with factory() as session, session.begin():
        session.add(
            User(
                id=user_id,
                oidc_issuer="https://integration-test.example.test/",
                oidc_subject=str(user_id),
                is_active=True,
            )
        )
    context = PostgreSQLContext(engine=engine, session_factory=factory, user_id=user_id)
    monkeypatch.setattr(simulation_repository, "get_engine", lambda: engine)
    monkeypatch.setattr(simulation_repository, "get_session_factory", lambda: factory)
    try:
        yield context
    finally:
        if context.request_ids or context.idempotency_keys:
            with factory() as session, session.begin():
                tracked_ids = set(context.request_ids)
                if context.idempotency_keys:
                    tracked_ids.update(
                        session.scalars(
                            select(SizingRequest.id).where(
                                SizingRequest.idempotency_key.in_(context.idempotency_keys)
                            )
                        )
                    )
                session.execute(
                    delete(SizingRequest).where(SizingRequest.id.in_(tracked_ids))
                )
        with factory() as session, session.begin():
            session.execute(delete(User).where(User.id == user_id))
        engine.dispose()


@pytest.mark.postgres
def test_postgresql_api_round_trip_uses_managed_schema(postgres_context):
    repository = simulation_repository.SimulationRepository()
    previous_override = app.dependency_overrides.get(get_repository)
    previous_user_override = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        id=postgres_context.user_id,
        oidc_issuer="https://integration-test.example.test/",
        oidc_subject=str(postgres_context.user_id),
    )
    try:
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            manifest = client.get("/api/v1/catalog/manifest")
            assert manifest.status_code == 200
            request = {
                "container_id": "5.015MWh_0.25C",
                "catalog_sha256": manifest.json()["catalog_sha256"],
                "required_energy_mwh": 100,
                "required_power_mw": 15,
                "project_years": 5,
            }
            headers = {"Idempotency-Key": f"pg-roundtrip-{uuid4()}"}
            postgres_context.idempotency_keys.append(headers["Idempotency-Key"])
            created = client.post("/api/v1/sizing", json=request, headers=headers)
            assert created.status_code == 201, created.text
            result = created.json()
            request_id = UUID(result["request_id"])
            postgres_context.request_ids.append(request_id)

            repeated = client.post("/api/v1/sizing", json=request, headers=headers)
            assert repeated.status_code == 201
            assert repeated.json() == result

            retrieved = client.get(f"/api/v1/sizing/{request_id}")
            assert retrieved.status_code == 200
            assert retrieved.json()["response"] == result
            assert retrieved.json()["request"] == request | {
                "c_rate": 0.25,
                "operating_temp_c": 25.0,
                "availability_pct": 99.0,
                "bol_energy_mwh": None,
                "eol_energy_mwh": None,
                "min_rte_eol_pct": 80.0,
                "cycles_required": None,
                "dod_pct": 100.0,
                "aux_required": True,
                "mismatch_constant_pct": 1.0,
                "manual_containers": 0,
                "margin_min_pct": 14.0,
                "margin_max_pct": 20.0,
                "allow_extended_margin": True,
            }

        with postgres_context.session_factory() as session:
            sizing_request = session.get(SizingRequest, request_id)
            run = session.scalar(
                select(SimulationRun).where(SimulationRun.request_id == request_id)
            )
            assert sizing_request.status == "COMPLETED"
            assert sizing_request.completed_at is not None
            assert sizing_request.input_schema_version == "sizing-input-v1"
            assert sizing_request.input_snapshot["source"]["workbook_sha256"]
            assert sizing_request.input_snapshot["application"]["engine_source_sha256"]
            assert sizing_request.input_snapshot["container"]["soh"]
            assert run.status == "SUCCEEDED"
            assert run.started_at is not None and run.completed_at is not None
            assert run.source_container_key == "5.015MWh_0.25C"
            assert len(run.source_catalog_sha256) == 64
            assert run.container_model_id is None
            assert run.result_snapshot == result
            assert isinstance(run.warnings, list)
            assert all(
                isinstance(run.result_snapshot[field], list)
                for field in ("discharging", "charging", "performance", "verification")
            )
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_repository, None)
        else:
            app.dependency_overrides[get_repository] = previous_override
        if previous_user_override is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous_user_override


@pytest.mark.postgres
def test_postgresql_concurrent_idempotency_and_independent_keys(postgres_context):
    repository = simulation_repository.SimulationRepository()
    same_key = f"pg-concurrent-{uuid4()}"
    other_key = f"pg-independent-{uuid4()}"
    snapshot = {
        "schema_version": "sizing-input-v1",
        "submitted_request": {"container_id": "integration-test"},
        "engine_inputs": {"required_energy_mwh": 10},
        "container": {"id": "integration-test"},
        "source": {"container_key": "integration-test", "workbook_sha256": "a" * 64},
    }
    ids = [uuid4(), uuid4(), uuid4()]
    postgres_context.request_ids.extend(ids)
    postgres_context.idempotency_keys.extend((same_key, other_key))
    barrier = Barrier(3)

    def start(index: int):
        barrier.wait(timeout=10)
        return repository.start_run(
            ids[index],
            postgres_context.user_id,
            None,
            snapshot,
            "integration-test",
            "a" * 64,
            "sha256:test",
            "test",
            same_key if index < 2 else other_key,
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(start, index) for index in range(3)]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=30))
            except simulation_repository.SimulationInProgress:
                outcomes.append("in-progress")

    assert outcomes.count(None) == 2
    assert outcomes.count("in-progress") == 1
    with postgres_context.session_factory() as session:
        created_ids = set(
            session.scalars(select(SizingRequest.id).where(SizingRequest.id.in_(ids)))
        )
        assert len(created_ids) == 2
        assert session.scalar(
            select(SizingRequest.id).where(SizingRequest.idempotency_key == same_key)
        ) in ids
        assert session.scalar(
            select(SizingRequest.id).where(SizingRequest.idempotency_key == other_key)
        ) in ids


@pytest.mark.postgres
def test_postgresql_result_rollback_and_failed_state_persistence(postgres_context):
    repository = simulation_repository.SimulationRepository()
    request_id = uuid4()
    postgres_context.request_ids.append(request_id)
    repository.start_run(
        request_id,
        postgres_context.user_id,
        None,
        {
            "schema_version": "sizing-input-v1",
            "submitted_request": {"container_id": "integration-test"},
            "engine_inputs": {"required_energy_mwh": 10},
            "container": {"id": "integration-test"},
            "source": {"container_key": "integration-test", "workbook_sha256": "b" * 64},
        },
        "integration-test",
        "b" * 64,
        "sha256:test",
        "test",
        None,
    )
    response = {
        "request_id": str(request_id),
        "container_id": "integration-test",
        "accepted": True,
        "margin_applied_pct": 14.0,
        "in_range": True,
        "note": "test",
        "summary": {"n_init": 1},
        "discharging": [],
        "charging": [],
        "performance": [],
        "verification": [],
    }
    with pytest.raises(simulation_repository.SimulationPersistenceError):
        repository.complete_run(request_id, response, ["test warning"], {})

    with postgres_context.session_factory() as session:
        request = session.get(SizingRequest, request_id)
        run = session.scalar(
            select(SimulationRun).where(SimulationRun.request_id == request_id)
        )
        assert request.status == "SUBMITTED"
        assert run.status == "RUNNING"
        assert run.result_snapshot is None
        assert run.warnings is None

    repository.fail_run(request_id, "calculation_error", "test failure")
    with postgres_context.session_factory() as session:
        request = session.get(SizingRequest, request_id)
        run = session.scalar(
            select(SimulationRun).where(SimulationRun.request_id == request_id)
        )
        assert request.status == "FAILED"
        assert request.completed_at is not None
        assert run.status == "FAILED"
        assert run.error_code == "calculation_error"
        assert run.error_message == "test failure"
    assert postgres_context.engine.pool.checkedout() == 0
