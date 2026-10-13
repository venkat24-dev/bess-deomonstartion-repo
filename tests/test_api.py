"""Tests for the HTTP API, using an in-memory repository for deterministic runs."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from io import BytesIO
from dataclasses import asdict
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from api import (
    DatabaseUnavailable,
    _yearly_records,
    app,
    get_project_repository,
    get_repository,
)
from bess.authentication import AuthenticatedUser, get_current_user
from bess.database import load_database
from bess.engine import Inputs, size_container
from bess.simulation_repository import _idempotency_identity


TEST_USER = AuthenticatedUser(
    id=UUID("11111111-1111-1111-1111-111111111111"),
    oidc_issuer="https://issuer.example.test/",
    oidc_subject="test-user",
)


class MemoryRepository:
    def __init__(self) -> None:
        self.runs: dict[UUID, dict] = {}
        self.idempotency_keys: dict[str, UUID] = {}

    def ping(self) -> None:
        return None

    def start_run(
        self,
        request_id: UUID,
        user_id: UUID,
        project_id: UUID | None,
        input_snapshot: dict,
        source_container_key: str,
        source_catalog_sha256: str,
        engine_version: str,
        application_version: str,
        idempotency_key: str | None,
    ) -> dict | None:
        if idempotency_key and idempotency_key in self.idempotency_keys:
            prior_id = self.idempotency_keys[idempotency_key]
            prior = self.runs[prior_id]
            if _idempotency_identity(prior["input_snapshot"]) != _idempotency_identity(input_snapshot):
                from bess.simulation_repository import IdempotencyConflict

                raise IdempotencyConflict("Idempotency-Key was already used for a different sizing request.")
            if prior["response"] is None:
                from bess.simulation_repository import SimulationInProgress

                raise SimulationInProgress("A sizing request with this Idempotency-Key is in progress.")
            return prior["response"]
        self.runs[request_id] = {
            "request_id": str(request_id),
            "owner_user_id": user_id,
            "project_id": project_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "request": input_snapshot["submitted_request"],
            "input_snapshot": input_snapshot,
            "response": None,
            "status": "RUNNING",
        }
        if idempotency_key:
            self.idempotency_keys[idempotency_key] = request_id
        return None

    def complete_run(self, request_id: UUID, response: dict, warnings: list[str], summary: dict) -> None:
        self.runs[request_id]["response"] = response
        self.runs[request_id]["warnings"] = warnings
        self.runs[request_id]["summary"] = summary
        self.runs[request_id]["status"] = "SUCCEEDED"

    def fail_run(self, request_id: UUID, error_code: str, message: str) -> None:
        self.runs[request_id]["status"] = "FAILED"
        self.runs[request_id]["error"] = {"code": error_code, "message": message}

    def get_run(self, request_id: UUID, user_id: UUID) -> dict | None:
        record = self.runs.get(request_id)
        if record is None or (
            record["project_id"] is None and record["owner_user_id"] != user_id
        ):
            return None
        result = {
            "request_id": record["request_id"],
            "created_at": record["created_at"],
            "request": record["request"],
            "response": record["response"],
        }
        if record["response"] is None:
            result["status"] = record["status"]
            result["error"] = record.get("error")
        return result


@pytest.fixture
def api_client():
    repository = MemoryRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    with TestClient(app) as client:
        yield client, repository
    app.dependency_overrides.clear()


def _post_sizing(client, request, headers=None):
    manifest = client.get("/api/v1/catalog/manifest")
    assert manifest.status_code == 200
    return client.post(
        "/api/v1/sizing",
        json={**request, "catalog_sha256": manifest.json()["catalog_sha256"]},
        headers=headers,
    )


def test_health_reports_database_ready(api_client):
    client, _ = api_client
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}


def test_list_containers(api_client):
    client, _ = api_client
    response = client.get("/api/v1/containers")
    assert response.status_code == 200
    containers = response.json()
    assert len(containers) == 6
    assert any(item["id"] == "5.015MWh_0.25C" for item in containers)


def test_catalog_manifest_hash_and_metadata_match_catalog_endpoint(api_client, monkeypatch):
    import api as sizing_api

    client, _ = api_client
    original_snapshot = sizing_api._catalog_snapshot
    snapshot_calls = []

    def counted_snapshot():
        snapshot_calls.append(True)
        return original_snapshot()

    monkeypatch.setattr(sizing_api, "_catalog_snapshot", counted_snapshot)
    manifest_response = client.get("/api/v1/catalog/manifest")
    assert len(snapshot_calls) == 1
    containers_response = client.get("/api/v1/containers")
    assert len(snapshot_calls) == 2
    assert manifest_response.status_code == 200
    assert containers_response.status_code == 200

    manifest = manifest_response.json()
    workbook_bytes = sizing_api.DB_PATH.read_bytes()
    assert manifest["schema_version"] == "catalog-manifest-v1"
    assert manifest["catalog_sha256"] == hashlib.sha256(workbook_bytes).hexdigest()
    assert len(manifest["catalog_sha256"]) == 64
    assert manifest["container_ids"] == [item["id"] for item in manifest["containers"]]
    assert manifest["containers"] == containers_response.json()


@pytest.mark.parametrize(
    "request_data",
    [
        {"container_id": "5.015MWh_0.25C"},
        {
            "container_id": "6.626MWh_0.33C",
            "required_energy_mwh": 90,
            "required_power_mw": 20,
            "project_years": 7,
            "bol_energy_mwh": 95,
            "eol_energy_mwh": 82,
            "cycles_required": 2100,
            "operating_temp_c": 35,
            "availability_pct": 97.5,
            "dod_pct": 92,
            "margin_min_pct": 15,
            "margin_max_pct": 24,
            "allow_extended_margin": True,
            "mismatch_constant_pct": 1.5,
        },
        {
            "container_id": "7.044MWh_0.25C",
            "required_energy_mwh": 120,
            "required_power_mw": 28,
            "project_years": 4,
            "manual_containers": 23,
            "operating_temp_c": 20,
            "availability_pct": 98,
            "dod_pct": 95,
            "margin_min_pct": 14,
            "margin_max_pct": 20,
            "allow_extended_margin": False,
        },
    ],
)
def test_sizing_route_matches_local_engine_for_supported_inputs(
    request_data,
    api_client,
    tmp_path,
    monkeypatch,
):
    import api as sizing_api

    client, repository = api_client
    workbook_bytes = sizing_api.DB_PATH.read_bytes()
    workbook_path = tmp_path / "catalog.xlsx"
    workbook_path.write_bytes(workbook_bytes)
    monkeypatch.setattr(sizing_api, "DB_PATH", workbook_path)

    manifest = client.get("/api/v1/catalog/manifest").json()
    assert manifest["catalog_sha256"] == hashlib.sha256(workbook_bytes).hexdigest()
    request = {**request_data, "catalog_sha256": manifest["catalog_sha256"]}
    response = client.post("/api/v1/sizing", json=request)
    assert response.status_code == 201, response.text
    actual = response.json()

    containers = {
        container.id: container
        for container in load_database(BytesIO(workbook_bytes))
    }
    energy = request_data.get("required_energy_mwh", 100.0)
    project_years = request_data.get("project_years", 10)
    expected_inputs = Inputs(
        required_energy_mwh=energy,
        required_power_mw=request_data.get("required_power_mw", 25.0),
        c_rate=request_data.get("c_rate", 0.25),
        project_years=project_years,
        operating_temp_c=request_data.get("operating_temp_c", 25.0),
        availability_pct=request_data.get("availability_pct", 99.0),
        bol_energy_mwh=request_data.get("bol_energy_mwh") or energy,
        eol_energy_mwh=request_data.get("eol_energy_mwh") or energy,
        min_rte_eol_pct=request_data.get("min_rte_eol_pct", 80.0),
        cycles_required=request_data.get("cycles_required") or 365 * project_years,
        dod_pct=request_data.get("dod_pct", 100.0),
        aux_required=request_data.get("aux_required", True),
        mismatch_constant_pct=request_data.get("mismatch_constant_pct", 1.0),
        manual_containers=request_data.get("manual_containers", 0),
        margin_min_pct=request_data.get("margin_min_pct", 14.0),
        margin_max_pct=request_data.get("margin_max_pct", 20.0),
        allow_extended_margin=request_data.get("allow_extended_margin", True),
    )
    expected = size_container(containers[request_data["container_id"]], expected_inputs)
    expected_result = expected.result

    assert actual["container_id"] == expected_result.container.id
    assert actual["summary"]["n_init"] == expected_result.n_init
    assert actual["accepted"] == expected_result.summary["accepted"]
    assert actual["margin_applied_pct"] == expected.margin_applied_pct
    assert actual["in_range"] == expected.in_range
    assert actual["summary"] == sizing_api._json_safe(expected_result.summary)
    assert actual["warnings"] == sizing_api._json_safe(expected_result.warnings)
    assert actual["effective_inputs"] == sizing_api._json_safe(asdict(expected_inputs))
    assert actual["catalog_sha256"] == hashlib.sha256(workbook_bytes).hexdigest()
    for field, frame in (
        ("discharging", expected_result.dis),
        ("charging", expected_result.chg),
        ("performance", expected_result.perf),
        ("verification", expected_result.verify),
    ):
        assert actual[field] == sizing_api._yearly_records(frame)
        assert [record["Year"] for record in actual[field]] == list(range(project_years + 1))
    assert len(repository.runs) == 1


def test_sizing_requires_catalog_hash(api_client):
    client, repository = api_client
    response = client.post(
        "/api/v1/sizing",
        json={"container_id": "5.015MWh_0.25C"},
    )
    assert response.status_code == 422
    assert repository.runs == {}


def test_sizing_rejects_malformed_catalog_hash(api_client):
    client, repository = api_client
    response = client.post(
        "/api/v1/sizing",
        json={
            "container_id": "5.015MWh_0.25C",
            "catalog_sha256": "not-a-sha256-digest",
        },
    )
    assert response.status_code == 422
    assert repository.runs == {}


def test_stale_catalog_hash_conflicts_before_calculation_or_persistence(
    api_client,
    monkeypatch,
):
    import api as sizing_api

    client, repository = api_client
    calculation_calls = []
    monkeypatch.setattr(
        sizing_api,
        "size_container",
        lambda *_args, **_kwargs: calculation_calls.append(True),
    )
    response = client.post(
        "/api/v1/sizing",
        json={
            "container_id": "5.015MWh_0.25C",
            "catalog_sha256": "0" * 64,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "catalog_mismatch"
    assert calculation_calls == []
    assert repository.runs == {}


def test_catalog_change_after_manifest_conflicts_before_calculation_or_persistence(
    api_client,
    monkeypatch,
    tmp_path,
):
    from io import BytesIO
    from openpyxl import load_workbook
    import api as sizing_api

    client, repository = api_client
    workbook_path = tmp_path / "catalog.xlsx"
    workbook_path.write_bytes(sizing_api.DB_PATH.read_bytes())
    monkeypatch.setattr(sizing_api, "DB_PATH", workbook_path)

    manifest_response = client.get("/api/v1/catalog/manifest")
    assert manifest_response.status_code == 200
    verified_hash = manifest_response.json()["catalog_sha256"]

    workbook = load_workbook(BytesIO(workbook_path.read_bytes()))
    workbook.properties.title = "Updated catalog revision"
    changed_workbook = BytesIO()
    workbook.save(changed_workbook)
    workbook_path.write_bytes(changed_workbook.getvalue())

    calculation_calls = []
    monkeypatch.setattr(
        sizing_api,
        "size_container",
        lambda *_args, **_kwargs: calculation_calls.append(True),
    )
    response = client.post(
        "/api/v1/sizing",
        json={
            "container_id": "5.015MWh_0.25C",
            "catalog_sha256": verified_hash,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "catalog_mismatch"
    assert calculation_calls == []
    assert repository.runs == {}


def test_reproducibility_hashes_track_the_source_files(tmp_path, monkeypatch):
    import api as sizing_api

    workbook = tmp_path / "catalog.xlsx"
    engine_file = tmp_path / "bess" / "engine.py"
    engine_file.parent.mkdir()
    workbook.write_bytes(b"catalog version one")
    engine_file.write_bytes(b"engine version one")
    monkeypatch.setattr(sizing_api, "DB_PATH", workbook)
    monkeypatch.setattr(sizing_api, "__file__", str(tmp_path / "api.py"))

    first_catalog_hash = sizing_api._source_catalog_sha256()
    first_engine_hash = sizing_api._engine_version()
    assert first_catalog_hash == hashlib.sha256(workbook.read_bytes()).hexdigest()
    assert first_engine_hash == f"sha256:{hashlib.sha256(engine_file.read_bytes()).hexdigest()}"
    assert sizing_api._source_catalog_sha256() == first_catalog_hash
    assert sizing_api._engine_version() == first_engine_hash

    workbook.write_bytes(b"catalog version two")
    engine_file.write_bytes(b"engine version two")
    assert sizing_api._source_catalog_sha256() != first_catalog_hash
    assert sizing_api._engine_version() != first_engine_hash


def test_catalog_hash_is_bound_to_the_parsed_workbook_bytes(tmp_path, monkeypatch):
    import api as sizing_api

    workbook = tmp_path / "catalog.xlsx"
    workbook.write_bytes(b"catalog source one")
    loaded_bytes = []

    def parse(source):
        loaded_bytes.append(source.read())
        return [type("ContainerFixture", (), {"id": "container"})()]

    monkeypatch.setattr(sizing_api, "DB_PATH", workbook)
    monkeypatch.setattr(sizing_api, "load_database", parse)
    containers, first_hash = sizing_api._catalog_snapshot()
    assert list(containers) == ["container"]
    assert loaded_bytes[-1] == workbook.read_bytes()
    assert first_hash == hashlib.sha256(loaded_bytes[-1]).hexdigest()

    workbook.write_bytes(b"catalog source two")
    _, second_hash = sizing_api._catalog_snapshot()
    assert len(loaded_bytes) == 2
    assert second_hash != first_hash
    assert second_hash == hashlib.sha256(loaded_bytes[-1]).hexdigest()


def test_sizing_request_persists_and_can_be_retrieved(api_client):
    import api as sizing_api

    client, repository = api_client
    response = _post_sizing(
        client,
        {
            "container_id": "5.015MWh_0.25C",
            "required_energy_mwh": 100,
            "required_power_mw": 15,
            "project_years": 5,
        },
    )
    assert response.status_code == 201, response.text
    result = response.json()
    request_id = UUID(result["request_id"])
    assert result["container_id"] == "5.015MWh_0.25C"
    assert isinstance(result["accepted"], bool)
    assert len(result["verification"]) == 6
    assert result["catalog_sha256"] == repository.runs[request_id]["input_snapshot"]["source"]["workbook_sha256"]
    assert result["effective_inputs"] == repository.runs[request_id]["input_snapshot"]["engine_inputs"]
    assert result["warnings"] == repository.runs[request_id]["warnings"]
    for field in ("discharging", "charging", "performance", "verification"):
        assert [record["Year"] for record in result[field]] == list(range(6))

    container = sizing_api.get_containers()[result["container_id"]]
    expected = size_container(
        container,
        Inputs(**repository.runs[request_id]["input_snapshot"]["engine_inputs"]),
    ).result
    assert result["discharging"] == _yearly_records(expected.dis)
    assert result["charging"] == _yearly_records(expected.chg)
    assert result["performance"] == _yearly_records(expected.perf)
    assert result["verification"] == _yearly_records(expected.verify)

    assert request_id in repository.runs

    stored = client.get(f"/api/v1/sizing/{request_id}")
    assert stored.status_code == 200
    assert stored.json()["response"] == result
    assert stored.json()["request"]["project_years"] == 5
    assert repository.runs[request_id]["status"] == "SUCCEEDED"
    assert repository.runs[request_id]["input_snapshot"]["source"]["workbook_sha256"]
    assert repository.runs[request_id]["input_snapshot"]["engine_inputs"]["cycles_required"] == 1825
    snapshot_container = repository.runs[request_id]["input_snapshot"]["container"]
    assert snapshot_container["soh"] == sizing_api.get_containers()["5.015MWh_0.25C"].soh
    assert snapshot_container["cal_rows"] == sizing_api.get_containers()["5.015MWh_0.25C"].cal_rows
    assert isinstance(repository.runs[request_id]["warnings"], list)


def test_sizing_request_rejects_unsupported_inputs(api_client):
    client, repository = api_client
    response = _post_sizing(
        client,
        {
            "container_id": "5.015MWh_0.25C",
            "augmentation_enabled": True,
        },
    )
    assert response.status_code == 422
    assert repository.runs == {}


def test_yearly_record_serialization_preserves_values_order_and_nulls():
    import numpy as np
    import pandas as pd

    frame = pd.DataFrame(
        {
            "energy": [np.float64(12.5), pd.NA],
            "power": [np.nan, np.float64(2.0)],
        },
        index=pd.Index([0, 1], name="Year"),
    )

    assert _yearly_records(frame) == [
        {"Year": 0, "energy": 12.5, "power": None},
        {"Year": 1, "energy": None, "power": 2.0},
    ]


def test_idempotency_key_reuses_completed_response(api_client):
    client, repository = api_client
    request = {
        "container_id": "5.015MWh_0.25C",
        "required_energy_mwh": 100,
        "required_power_mw": 15,
        "project_years": 5,
    }
    first = _post_sizing(client, request, headers={"Idempotency-Key": "same-request"})
    second = _post_sizing(client, request, headers={"Idempotency-Key": "same-request"})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert len(repository.runs) == 1


def test_idempotency_key_accepts_equivalent_resolved_inputs(api_client):
    client, repository = api_client
    headers = {"Idempotency-Key": "normalized-input"}
    request = {
        "container_id": "5.015MWh_0.25C",
        "required_energy_mwh": 100,
        "required_power_mw": 15,
        "project_years": 5,
    }
    first = _post_sizing(client, request, headers=headers)
    equivalent = _post_sizing(
        client,
        {
            **request,
            "bol_energy_mwh": 100,
            "eol_energy_mwh": 100,
            "cycles_required": 1825,
        },
        headers=headers,
    )
    assert first.status_code == equivalent.status_code == 201
    assert first.json() == equivalent.json()
    assert len(repository.runs) == 1


def test_idempotency_key_rejects_different_input(api_client):
    client, _ = api_client
    headers = {"Idempotency-Key": "same-key"}
    original = {
        "container_id": "5.015MWh_0.25C",
        "required_energy_mwh": 100,
        "required_power_mw": 15,
    }
    assert _post_sizing(client, original, headers=headers).status_code == 201
    changed = {**original, "required_energy_mwh": 101}
    conflict = _post_sizing(client, changed, headers=headers)
    assert conflict.status_code == 409


def test_failed_calculation_is_persisted(api_client, monkeypatch):
    import api as sizing_api

    client, repository = api_client

    def fail_calculation(*_args, **_kwargs):
        raise ValueError("unsupported calculation input")

    monkeypatch.setattr(sizing_api, "size_container", fail_calculation)
    response = _post_sizing(
        client,
        {"container_id": "5.015MWh_0.25C", "required_energy_mwh": 100},
    )
    assert response.status_code == 422
    run = next(iter(repository.runs.values()))
    assert run["status"] == "FAILED"
    assert run["error"]["code"] == "calculation_error"
    assert response.json()["detail"] == "unsupported calculation input"
    assert response.headers["X-Sizing-Request-ID"] == run["request_id"]
    retrieved = client.get(f"/api/v1/sizing/{run['request_id']}")
    assert retrieved.status_code == 200
    assert retrieved.json()["status"] == "FAILED"
    assert retrieved.json()["response"] is None


def test_failed_persistence_is_recorded_and_returns_retrieval_id(api_client):
    class FailingPersistenceRepository(MemoryRepository):
        def complete_run(self, request_id: UUID, response: dict, warnings: list[str], summary: dict) -> None:
            from bess.simulation_repository import SimulationPersistenceError

            raise SimulationPersistenceError("Could not save the sizing results.")

    repository = FailingPersistenceRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        with TestClient(app) as client:
            response = _post_sizing(
                client,
                {"container_id": "5.015MWh_0.25C", "required_energy_mwh": 100},
            )
            assert response.status_code == 503
            request_id = response.headers["X-Sizing-Request-ID"]
            saved = client.get(f"/api/v1/sizing/{request_id}")
        assert response.status_code == 503
        assert saved.status_code == 200
        assert saved.json()["status"] == "FAILED"
        assert saved.json()["error"]["code"] == "persistence_error"
    finally:
        app.dependency_overrides.clear()


def test_unknown_container_returns_not_found(api_client):
    client, repository = api_client
    response = _post_sizing(client, {"container_id": "missing"})
    assert response.status_code == 404
    assert repository.runs == {}


def test_invalid_request_is_rejected(api_client):
    client, _ = api_client
    response = _post_sizing(
        client,
        {
            "container_id": "5.015MWh_0.25C",
            "project_years": 99,
        },
    )
    assert response.status_code == 422


def test_missing_run_returns_not_found(api_client):
    client, _ = api_client
    response = client.get("/api/v1/sizing/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_database_failure_is_reported_as_service_unavailable():
    class UnavailableRepository(MemoryRepository):
        def ping(self) -> None:
            raise DatabaseUnavailable("PostgreSQL is unavailable.")

    app.dependency_overrides[get_repository] = UnavailableRepository
    try:
        with TestClient(app) as client:
            response = client.get("/health")
        assert response.status_code == 503
        assert response.json() == {"detail": "PostgreSQL is unavailable."}
    finally:
        app.dependency_overrides.clear()


def test_sizing_and_project_routes_require_bearer_authentication(api_client):
    app.dependency_overrides.pop(get_current_user)
    client, _ = api_client
    catalog_sha256 = client.get("/api/v1/catalog/manifest").json()["catalog_sha256"]
    response = client.post(
        "/api/v1/sizing",
        json={
            "container_id": "5.015MWh_0.25C",
            "catalog_sha256": catalog_sha256,
        },
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert client.get("/api/v1/projects").status_code == 401


def test_sizing_result_is_not_visible_to_another_authenticated_user(api_client):
    client, _ = api_client
    created = _post_sizing(
        client,
        {"container_id": "5.015MWh_0.25C", "required_energy_mwh": 100},
    )
    request_id = created.json()["request_id"]
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        id=UUID("22222222-2222-2222-2222-222222222222"),
        oidc_issuer=TEST_USER.oidc_issuer,
        oidc_subject="different-user",
    )
    assert client.get(f"/api/v1/sizing/{request_id}").status_code == 404


def test_optional_project_id_preserves_sizing_response_contract(api_client):
    client, repository = api_client
    response = _post_sizing(
        client,
        {
            "container_id": "5.015MWh_0.25C",
            "project_id": "33333333-3333-3333-3333-333333333333",
        },
    )
    assert response.status_code == 201
    assert set(response.json()) == {
        "request_id",
        "container_id",
        "accepted",
        "margin_applied_pct",
        "in_range",
        "note",
        "summary",
        "discharging",
        "charging",
        "performance",
        "verification",
        "effective_inputs",
        "catalog_sha256",
        "warnings",
    }
    stored = next(iter(repository.runs.values()))
    assert stored["project_id"] == UUID("33333333-3333-3333-3333-333333333333")
    assert stored["input_snapshot"]["project_id"] == str(stored["project_id"])


def test_project_routes_pass_authenticated_identity_to_repository(api_client):
    class ProjectRepository:
        def create_project(self, user_id, organization_id, name, description):
            return {
                "created_by_user_id": str(user_id),
                "organization_id": str(organization_id),
                "name": name,
                "description": description,
            }

        def list_projects(self, user_id):
            return [{"created_by_user_id": str(user_id)}]

        def get_project(self, user_id, project_id):
            return {"id": str(project_id), "created_by_user_id": str(user_id)}

        def update_project(self, user_id, project_id, changes):
            return {"id": str(project_id), "updated_by_user_id": str(user_id), **changes}

        def add_project_member(self, caller_id, project_id, member_user_id, role):
            return {
                "project_id": str(project_id),
                "user_id": str(member_user_id),
                "role": role,
                "caller_id": str(caller_id),
            }

    app.dependency_overrides[get_project_repository] = ProjectRepository
    client, _ = api_client
    organization_id = "44444444-4444-4444-4444-444444444444"
    project_id = "55555555-5555-5555-5555-555555555555"
    member_id = "66666666-6666-6666-6666-666666666666"
    created = client.post(
        "/api/v1/projects",
        json={"organization_id": organization_id, "name": "Scenario"},
    )
    assert created.status_code == 201
    assert created.json()["created_by_user_id"] == str(TEST_USER.id)
    assert client.get("/api/v1/projects").json() == [
        {"created_by_user_id": str(TEST_USER.id)}
    ]
    assert client.get(f"/api/v1/projects/{project_id}").status_code == 200
    assert client.patch(
        f"/api/v1/projects/{project_id}", json={"name": "Updated"}
    ).json()["updated_by_user_id"] == str(TEST_USER.id)
    added = client.post(
        f"/api/v1/projects/{project_id}/members",
        json={"user_id": member_id, "role": "viewer"},
    )
    assert added.status_code == 201
    assert added.json()["role"] == "viewer"
