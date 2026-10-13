"""HTTP API for BESS sizing.

Run locally with ``uvicorn api:app --host 127.0.0.1 --port 8000``.
"""
from __future__ import annotations

import hashlib
import logging
import math
import platform
from dataclasses import asdict
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.requests import Request
from starlette.responses import JSONResponse

from bess.database import Container, load_database
from bess.engine import Inputs, size_container
from bess.authentication import AuthenticatedUser, get_current_user
from bess.project_repository import (
    ProjectAccessError,
    ProjectConflict,
    ProjectPersistenceError,
    ProjectRepository,
)
from bess.simulation_repository import (
    IdempotencyConflict,
    SimulationInProgress,
    SimulationPersistenceError,
    SimulationPreviouslyFailed,
    SimulationRepository,
)

logger = logging.getLogger(__name__)
DB_PATH = Path(__file__).parent / "data" / "BESS_DATABASE.xlsx"
INPUT_SCHEMA_VERSION = "sizing-input-v1"
APPLICATION_VERSION = "1.0.0"
CATALOG_MANIFEST_SCHEMA_VERSION = "catalog-manifest-v1"


DatabaseUnavailable = SimulationPersistenceError


class RunRepository(Protocol):
    def ping(self) -> None: ...

    def start_run(
        self,
        request_id: UUID,
        user_id: UUID,
        project_id: UUID | None,
        input_snapshot: dict[str, Any],
        source_container_key: str,
        source_catalog_sha256: str,
        engine_version: str,
        application_version: str,
        idempotency_key: str | None,
    ) -> dict[str, Any] | None: ...

    def complete_run(
        self,
        request_id: UUID,
        response: dict[str, Any],
        warnings: list[str],
        summary: dict[str, Any],
    ) -> None: ...

    def fail_run(self, request_id: UUID, error_code: str, message: str) -> None: ...

    def get_run(self, request_id: UUID, user_id: UUID) -> dict | None: ...


def get_repository() -> RunRepository:
    return SimulationRepository()


def get_project_repository() -> ProjectRepository:
    return ProjectRepository()


def get_containers() -> dict[str, Container]:
    return _catalog_snapshot()[0]


def _source_catalog_sha256() -> str:
    return hashlib.sha256(DB_PATH.read_bytes()).hexdigest()


@lru_cache(maxsize=2)
def _parse_catalog(workbook_bytes: bytes) -> dict[str, Container]:
    return {
        container.id: container
        for container in load_database(BytesIO(workbook_bytes))
    }


def _catalog_snapshot() -> tuple[dict[str, Container], str]:
    workbook_bytes = DB_PATH.read_bytes()
    return (
        _parse_catalog(workbook_bytes),
        hashlib.sha256(workbook_bytes).hexdigest(),
    )


def _engine_version() -> str:
    digest = hashlib.sha256(Path(__file__).parent.joinpath("bess", "engine.py").read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _yearly_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return _json_safe(frame.reset_index().to_dict(orient="records"))


class SizingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    container_id: str
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: UUID | None = None
    required_energy_mwh: float = Field(default=100.0, gt=0, le=1_000_000)
    required_power_mw: float = Field(default=25.0, gt=0, le=100_000)
    c_rate: float = Field(default=0.25, gt=0, le=2)
    project_years: int = Field(default=10, ge=1, le=15)
    operating_temp_c: float = Field(default=25.0, ge=-20, le=60)
    availability_pct: float = Field(default=99.0, ge=50, le=100)
    bol_energy_mwh: float | None = Field(default=None, gt=0, le=1_000_000)
    eol_energy_mwh: float | None = Field(default=None, gt=0, le=1_000_000)
    min_rte_eol_pct: float = Field(default=80.0, ge=10, le=100)
    cycles_required: int | None = Field(default=None, ge=1, le=100_000)
    dod_pct: float = Field(default=100.0, ge=10, le=100)
    aux_required: bool = True
    mismatch_constant_pct: float = Field(default=1.0, ge=0, le=50)
    manual_containers: int = Field(default=0, ge=0, le=100_000)
    margin_min_pct: float = Field(default=14.0, ge=0, le=100)
    margin_max_pct: float = Field(default=20.0, ge=0, le=100)
    allow_extended_margin: bool = True

    @model_validator(mode="after")
    def validate_margin_range(self) -> SizingRequest:
        if self.margin_max_pct < self.margin_min_pct:
            raise ValueError("margin_max_pct must be greater than or equal to margin_min_pct")
        return self


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organization_id: UUID
    name: str = Field(min_length=1, max_length=300)
    description: str | None = None


class ProjectUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None


class ProjectMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role: Literal["owner", "editor", "viewer"]


class SizingResponse(BaseModel):
    request_id: UUID
    container_id: str
    accepted: bool
    margin_applied_pct: float
    in_range: bool
    note: str
    summary: dict[str, Any]
    discharging: list[dict[str, Any]]
    charging: list[dict[str, Any]]
    performance: list[dict[str, Any]]
    verification: list[dict[str, Any]]
    effective_inputs: dict[str, Any]
    catalog_sha256: str
    warnings: list[str]


class ContainerMetadata(BaseModel):
    id: str
    label: str
    energy_mwh: float
    c_rate: float
    max_years: int


class CatalogManifest(BaseModel):
    schema_version: str
    catalog_sha256: str
    container_ids: list[str]
    containers: list[ContainerMetadata]


app = FastAPI(title="BESS Sizing API", version="1.0.0")


@app.exception_handler(DatabaseUnavailable)
async def database_unavailable_handler(
    request: Request,
    exc: DatabaseUnavailable,
) -> JSONResponse:
    _ = request
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(IdempotencyConflict)
async def idempotency_conflict_handler(
    request: Request,
    exc: IdempotencyConflict,
) -> JSONResponse:
    _ = request
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(SimulationInProgress)
async def simulation_in_progress_handler(
    request: Request,
    exc: SimulationInProgress,
) -> JSONResponse:
    _ = request
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(SimulationPreviouslyFailed)
async def previous_simulation_failed_handler(
    request: Request,
    exc: SimulationPreviouslyFailed,
) -> JSONResponse:
    _ = request
    return JSONResponse(
        status_code=409,
        content={"detail": {"message": str(exc), "request_id": str(exc.request_id)}},
    )


@app.exception_handler(ProjectPersistenceError)
async def project_persistence_error_handler(
    request: Request,
    exc: ProjectPersistenceError,
) -> JSONResponse:
    _ = request
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(ProjectAccessError)
async def project_access_error_handler(
    request: Request,
    exc: ProjectAccessError,
) -> JSONResponse:
    _ = request
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.get("/health")
def health(repository: RunRepository = Depends(get_repository)) -> dict[str, str]:
    repository.ping()
    return {"status": "ok", "database": "connected"}


@app.get("/api/v1/containers")
def list_containers() -> list[dict[str, Any]]:
    return [
        {
            "id": container.id,
            "label": container.label,
            "energy_mwh": container.energy_mwh,
            "c_rate": container.c_rate,
            "max_years": container.max_years,
        }
        for container in get_containers().values()
    ]


@app.get("/api/v1/catalog/manifest", response_model=CatalogManifest)
def read_catalog_manifest() -> dict[str, Any]:
    containers, source_sha256 = _catalog_snapshot()
    metadata = [
        {
            "id": container.id,
            "label": container.label,
            "energy_mwh": container.energy_mwh,
            "c_rate": container.c_rate,
            "max_years": container.max_years,
        }
        for container in containers.values()
    ]
    return {
        "schema_version": CATALOG_MANIFEST_SCHEMA_VERSION,
        "catalog_sha256": source_sha256,
        "container_ids": [item["id"] for item in metadata],
        "containers": metadata,
    }


@app.post("/api/v1/projects", status_code=201)
def create_project(
    request: ProjectCreateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: ProjectRepository = Depends(get_project_repository),
) -> dict[str, Any]:
    try:
        return repository.create_project(
            user.id, request.organization_id, request.name, request.description
        )
    except ProjectAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/v1/projects")
def list_projects(
    user: AuthenticatedUser = Depends(get_current_user),
    repository: ProjectRepository = Depends(get_project_repository),
) -> list[dict[str, Any]]:
    return repository.list_projects(user.id)


@app.get("/api/v1/projects/{project_id}")
def read_project(
    project_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: ProjectRepository = Depends(get_project_repository),
) -> dict[str, Any]:
    project = repository.get_project(user.id, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


@app.patch("/api/v1/projects/{project_id}")
def update_project(
    project_id: UUID,
    request: ProjectUpdateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: ProjectRepository = Depends(get_project_repository),
) -> dict[str, Any]:
    changes = request.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="At least one project field must be provided.")
    if "name" in changes and changes["name"] is None:
        raise HTTPException(status_code=422, detail="Project name cannot be null.")
    try:
        return repository.update_project(user.id, project_id, changes)
    except ProjectAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProjectConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/projects/{project_id}/members", status_code=201)
def add_project_member(
    project_id: UUID,
    request: ProjectMemberRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    repository: ProjectRepository = Depends(get_project_repository),
) -> dict[str, str]:
    try:
        return repository.add_project_member(
            user.id, project_id, request.user_id, request.role
        )
    except ProjectAccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProjectConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/sizing", response_model=SizingResponse, status_code=201)
def create_sizing(
    request: SizingRequest,
    repository: RunRepository = Depends(get_repository),
    user: AuthenticatedUser = Depends(get_current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
) -> dict[str, Any]:
    containers, source_sha256 = _catalog_snapshot()
    if request.catalog_sha256 != source_sha256:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "catalog_mismatch",
                "message": "Catalog hash mismatch. Retrieve the current catalog manifest and retry.",
            },
        )
    container = containers.get(request.container_id)
    if container is None:
        raise HTTPException(status_code=404, detail="Unknown container_id.")

    values = request.model_dump(
        exclude={"container_id", "catalog_sha256", "project_id"}
    )
    values["bol_energy_mwh"] = values["bol_energy_mwh"] or request.required_energy_mwh
    values["eol_energy_mwh"] = values["eol_energy_mwh"] or request.required_energy_mwh
    values["cycles_required"] = values["cycles_required"] or 365 * request.project_years
    inputs = Inputs(**values)
    request_id = uuid4()
    engine_version = _engine_version()
    request_payload = _json_safe(request.model_dump(mode="json", exclude={"project_id"}))
    if request.project_id is not None:
        request_payload["project_id"] = str(request.project_id)
    input_snapshot = _json_safe(
        {
            "schema_version": INPUT_SCHEMA_VERSION,
            "submitted_request": request_payload,
            "engine_inputs": asdict(inputs),
            "container": asdict(container),
            "source": {
                "kind": "excel",
                "container_key": container.id,
                "workbook_sha256": source_sha256,
            },
            "application": {
                "version": APPLICATION_VERSION,
                "engine_source_sha256": engine_version.removeprefix("sha256:"),
            },
            "runtime": {
                "python": platform.python_version(),
                "numpy": np.__version__,
                "pandas": pd.__version__,
            },
            "owner_user_id": str(user.id),
            "project_id": str(request.project_id) if request.project_id else None,
        }
    )
    existing_response = repository.start_run(
        request_id,
        user.id,
        request.project_id,
        input_snapshot,
        container.id,
        source_sha256,
        engine_version,
        APPLICATION_VERSION,
        idempotency_key,
    )
    if existing_response is not None:
        return existing_response

    try:
        outcome = size_container(container, inputs)
    except ValueError as exc:
        repository.fail_run(request_id, "calculation_error", str(exc))
        raise HTTPException(
            status_code=422,
            detail=str(exc),
            headers={"X-Sizing-Request-ID": str(request_id)},
        ) from exc
    except Exception as exc:
        logger.error("BESS sizing calculation failed (%s)", type(exc).__name__)
        repository.fail_run(request_id, "calculation_error", "Sizing calculation failed.")
        raise HTTPException(
            status_code=500,
            detail="Sizing calculation failed.",
            headers={"X-Sizing-Request-ID": str(request_id)},
        ) from exc

    result = outcome.result
    response = _json_safe(
        {
            "request_id": str(request_id),
            "container_id": container.id,
            "accepted": result.summary["accepted"],
            "margin_applied_pct": outcome.margin_applied_pct,
            "in_range": outcome.in_range,
            "note": outcome.note,
            "summary": result.summary,
            "discharging": _yearly_records(result.dis),
            "charging": _yearly_records(result.chg),
            "performance": _yearly_records(result.perf),
            "verification": _yearly_records(result.verify),
            "effective_inputs": asdict(inputs),
            "catalog_sha256": source_sha256,
            "warnings": result.warnings,
        }
    )
    try:
        repository.complete_run(request_id, response, result.warnings, result.summary)
    except SimulationPersistenceError:
        try:
            repository.fail_run(request_id, "persistence_error", "Sizing results could not be saved.")
        except SimulationPersistenceError as failure_exc:
            logger.error("Could not record failed sizing persistence (%s)", type(failure_exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="Sizing results could not be saved.",
            headers={"X-Sizing-Request-ID": str(request_id)},
        ) from None
    return response


@app.get("/api/v1/sizing/{request_id}")
def read_sizing(
    request_id: UUID,
    repository: RunRepository = Depends(get_repository),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    run = repository.get_run(request_id, user.id)
    if run is None:
        raise HTTPException(status_code=404, detail="Sizing run not found.")
    return run
