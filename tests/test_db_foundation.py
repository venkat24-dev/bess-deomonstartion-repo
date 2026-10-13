"""Unit tests for database configuration, ORM metadata, and Alembic safety."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import configure_mappers

from bess import db_connection
from bess.db_connection import DatabaseConfigurationError
from bess.models import (
    Base,
    CatalogRevision,
    ContainerModel,
    Organization,
    OrganizationMember,
    Project,
    ProjectMember,
    SimulationRun,
    SizingRequest,
    User,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_all_foundation_models_register_in_bess_schema():
    configure_mappers()
    expected = {
        "users",
        "organizations",
        "organization_members",
        "projects",
        "project_members",
        "catalog_revisions",
        "container_models",
        "container_performance_years",
        "container_temperature_bands",
        "container_calendar_degradation",
        "container_ac_parameters",
        "container_raw_ac_parameters",
        "container_model_notes",
        "sizing_requests",
        "simulation_runs",
    }
    assert set(Base.metadata.tables) == {f"bess.{name}" for name in expected}
    assert len(Base.metadata.tables) == 15


def test_model_imports_do_not_open_a_database_connection(monkeypatch):
    def fail_if_engine_requested():
        raise AssertionError("model import must not request a database engine")

    monkeypatch.setattr(db_connection, "get_engine", fail_if_engine_requested)
    assert User.__table__.schema == "bess"
    assert SimulationRun.__table__.schema == "bess"


def test_models_have_expected_relationships_and_foreign_keys():
    configure_mappers()
    assert {fk.target_fullname for fk in Project.__table__.foreign_keys} == {
        "bess.organizations.id",
        "bess.users.id",
    }
    assert {fk.target_fullname for fk in SimulationRun.__table__.foreign_keys} == {
        "bess.sizing_requests.id",
        "bess.container_models.id",
    }
    assert "memberships" in inspect(Project).relationships
    assert "simulation_runs" in inspect(SizingRequest).relationships
    assert "container_models" in inspect(CatalogRevision).relationships
    assert "organization_memberships" in inspect(User).relationships
    assert "catalog_revision" in inspect(ContainerModel).relationships
    assert "organization" in inspect(OrganizationMember).relationships
    assert "user" in inspect(ProjectMember).relationships
    assert "container_model" in inspect(SimulationRun).relationships


def test_audit_timestamps_are_timezone_aware():
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if column.name.endswith("_at"):
                assert column.type.timezone, f"{table.fullname}.{column.name} must be timezone-aware"


def test_missing_database_url_has_clear_error(monkeypatch):
    monkeypatch.setattr(db_connection, "_dotenv_loaded", True)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(DatabaseConfigurationError, match="DATABASE_URL is not configured"):
        db_connection.database_url()


def test_database_url_is_parsed_without_exposing_password(monkeypatch):
    monkeypatch.setattr(db_connection, "_dotenv_loaded", True)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://bess_user:sensitive%40password@localhost:5432/bess_db",
    )
    url = db_connection.database_url()
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "localhost"
    assert url.database == "bess_db"
    assert "sensitive" not in url.render_as_string(hide_password=True)


def test_engine_and_session_factory_are_created_lazily(monkeypatch):
    monkeypatch.setattr(db_connection, "_dotenv_loaded", True)
    monkeypatch.setattr(db_connection, "_engine", None)
    monkeypatch.setattr(db_connection, "_session_factory", None)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://bess_user:local-only@localhost:5432/bess_db",
    )

    engine = db_connection.get_engine()
    factory = db_connection.get_session_factory()
    assert factory.kw["bind"] is engine
    assert engine.pool.size() == 5
    assert engine.pool._max_overflow == 5
    engine.dispose()


def test_session_dependency_rolls_back_on_error_and_closes(monkeypatch):
    tracked = SimpleNamespace(rolled_back=False, closed=False)

    class FakeSession:
        def rollback(self):
            tracked.rolled_back = True

        def close(self):
            tracked.closed = True

    monkeypatch.setattr(db_connection, "get_session_factory", lambda: lambda: FakeSession())
    dependency = db_connection.get_db_session()
    next(dependency)
    with pytest.raises(RuntimeError, match="request failure"):
        dependency.throw(RuntimeError("request failure"))
    assert tracked.rolled_back
    assert tracked.closed


def test_session_dependency_closes_after_success(monkeypatch):
    tracked = SimpleNamespace(closed=False)

    class FakeSession:
        def rollback(self):
            raise AssertionError("successful request should not roll back")

        def close(self):
            tracked.closed = True

    monkeypatch.setattr(db_connection, "get_session_factory", lambda: lambda: FakeSession())
    dependency = db_connection.get_db_session()
    next(dependency)
    with pytest.raises(StopIteration):
        next(dependency)
    assert tracked.closed


def test_health_check_runs_select_one_and_closes_connection():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    statements = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, *_args: statements.append(statement),
    )
    assert db_connection.check_database_health(engine)
    assert any("SELECT 1" in statement for statement in statements)
    engine.dispose()


def test_alembic_config_and_migration_are_create_only_for_bess():
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_current_head() == "20261012_0003"
    migration = Path(scripts.get_revision("20261012_0001").path)
    source = migration.read_text(encoding="utf-8")
    assert 'SCHEMA = "bess"' in source
    assert source.count("op.create_table(") == 9
    assert "op.drop_table(" not in source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert "public." not in source
    assert "op.execute(CreateSchema(SCHEMA, if_not_exists=True))" in source


def test_catalog_detail_migration_is_additive_and_bess_only():
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    migration = Path(scripts.get_revision("20261012_0002").path)
    source = migration.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert source.count("op.create_table(") == 6
    assert "op.drop_table(" not in upgrade
    assert "op.alter_column(" not in upgrade
    assert "public." not in source


def test_simulation_persistence_migration_is_additive_and_bess_only():
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    migration = Path(scripts.get_revision("20261012_0003").path)
    source = migration.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert "op.add_column(" in upgrade
    assert "op.alter_column(" in upgrade
    assert "op.create_index(" in upgrade
    assert "op.create_check_constraint(" in upgrade
    assert "op.drop_" not in upgrade
    assert "op.execute(" not in upgrade
    assert "public." not in source
    assert 'nullable=True' in upgrade
    assert "input_snapshot" in upgrade
    assert "result_snapshot" in upgrade


def test_alembic_environment_targets_only_bess():
    env_source = (PROJECT_ROOT / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "version_table_schema=TARGET_SCHEMA" in env_source
    assert "return name == TARGET_SCHEMA" in env_source
    assert "include_schemas=True" in env_source


def test_api_contract_remains_registered():
    from api import app

    routes = {(route.path, tuple(sorted(route.methods or []))) for route in app.routes}
    assert ("/health", ("GET",)) in routes
    assert ("/api/v1/containers", ("GET",)) in routes
    assert ("/api/v1/sizing", ("POST",)) in routes
    assert ("/api/v1/sizing/{request_id}", ("GET",)) in routes
