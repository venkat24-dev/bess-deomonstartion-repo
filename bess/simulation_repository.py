"""SQLAlchemy persistence for API sizing requests and simulation runs."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from math import isfinite
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from bess.db_connection import DatabaseConfigurationError, get_engine, get_session_factory
from bess.models import SimulationRun, SizingRequest as SizingRequestRecord
from bess.project_repository import ProjectAccessError, _project_role, _require_project_role

logger = logging.getLogger(__name__)


class SimulationPersistenceError(RuntimeError):
    """A non-secret, client-safe persistence error."""


class IdempotencyConflict(SimulationPersistenceError):
    """An idempotency key was reused with different input."""


class SimulationInProgress(SimulationPersistenceError):
    """A matching request is already being processed."""


class SimulationPreviouslyFailed(SimulationPersistenceError):
    """An idempotent retry references a prior failed request."""

    def __init__(self, request_id: UUID) -> None:
        self.request_id = request_id
        super().__init__(
            f"The matching sizing request previously failed; retrieve request {request_id} for details."
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _input_for(request: SizingRequestRecord) -> dict[str, Any]:
    snapshot = request.input_snapshot
    if snapshot is None:
        return {}
    if not isinstance(snapshot, dict):
        raise SimulationPersistenceError("The stored sizing input snapshot is invalid.")
    submitted_request = snapshot.get("submitted_request", snapshot)
    if not isinstance(submitted_request, dict):
        raise SimulationPersistenceError("The stored sizing request is invalid.")
    return submitted_request


def _idempotency_identity(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        key: snapshot.get(key)
        for key in (
            "schema_version",
            "engine_inputs",
            "container",
            "source",
            "owner_user_id",
            "project_id",
        )
    }


def _validated_result_snapshot(value: Any) -> dict[str, Any]:
    required_fields = {
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
    }
    if not isinstance(value, dict) or not required_fields.issubset(value):
        raise SimulationPersistenceError("The stored sizing result is invalid.")
    if not isinstance(value["summary"], dict) or any(
        not isinstance(value[field], list)
        for field in ("discharging", "charging", "performance", "verification")
    ):
        raise SimulationPersistenceError("The stored sizing result is invalid.")
    if (
        not isinstance(value["request_id"], str)
        or not isinstance(value["container_id"], str)
        or not isinstance(value["accepted"], bool)
        or not isinstance(value["in_range"], bool)
        or type(value["margin_applied_pct"]) not in (int, float)
        or (
            isinstance(value["margin_applied_pct"], float)
            and not isfinite(value["margin_applied_pct"])
        )
        or not isinstance(value["note"], str)
        or any(
            not all(isinstance(row, dict) for row in value[field])
            for field in ("discharging", "charging", "performance", "verification")
        )
    ):
        raise SimulationPersistenceError("The stored sizing result is invalid.")
    try:
        UUID(value["request_id"])
    except (TypeError, ValueError):
        raise SimulationPersistenceError("The stored sizing result is invalid.") from None
    return value


def _completed_response(
    request: SizingRequestRecord,
    run: SimulationRun,
) -> dict[str, Any] | None:
    if run.result_snapshot is None:
        if run.status == "SUCCEEDED" or request.status == "COMPLETED":
            raise SimulationPersistenceError("The completed sizing result is missing.")
        if run.status == "FAILED" or request.status == "FAILED":
            if run.status != "FAILED" or request.status != "FAILED":
                raise SimulationPersistenceError("The sizing failure has an inconsistent status.")
        return None
    if run.status != "SUCCEEDED" or request.status != "COMPLETED":
        raise SimulationPersistenceError("The stored sizing result has an inconsistent status.")
    if run.warnings is not None and (
        not isinstance(run.warnings, list)
        or not all(isinstance(warning, str) for warning in run.warnings)
    ):
        raise SimulationPersistenceError("The stored sizing warnings are invalid.")
    result = _validated_result_snapshot(run.result_snapshot)
    if UUID(result["request_id"]) != request.id:
        raise SimulationPersistenceError("The stored sizing result has an inconsistent request identifier.")
    return result


class SimulationRepository:
    """Own database transactions for the lifecycle of one simulation."""

    def ping(self) -> None:
        try:
            with get_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("PostgreSQL health check failed (%s)", type(exc).__name__)
            raise SimulationPersistenceError("PostgreSQL is unavailable.") from exc

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
    ) -> dict[str, Any] | None:
        """Commit the request/run before calculation; return a duplicate result if complete."""
        try:
            with get_session_factory()() as session, session.begin():
                if project_id is not None:
                    _require_project_role(
                        session, project_id, user_id, {"owner", "editor"}
                    )
                if idempotency_key:
                    existing = session.scalar(
                        select(SizingRequestRecord)
                        .where(SizingRequestRecord.idempotency_key == idempotency_key)
                        .options(selectinload(SizingRequestRecord.simulation_runs))
                        .with_for_update()
                    )
                    if existing is not None:
                        return self._resolve_duplicate(existing, input_snapshot)

                record = SizingRequestRecord(
                    id=request_id,
                    project_id=project_id,
                    created_by_user_id=user_id,
                    source="api",
                    status="SUBMITTED",
                    input_snapshot=input_snapshot,
                    input_schema_version="sizing-input-v1",
                    application_version=application_version,
                    idempotency_key=idempotency_key,
                )
                record.simulation_runs.append(
                    SimulationRun(
                        request_id=request_id,
                        container_model_id=None,
                        source_container_key=source_container_key,
                        source_catalog_sha256=source_catalog_sha256,
                        status="RUNNING",
                        engine_version=engine_version,
                        started_at=_now(),
                    )
                )
                session.add(record)
            return None
        except IdempotencyConflict:
            raise
        except SimulationInProgress:
            raise
        except IntegrityError as exc:
            if idempotency_key:
                duplicate = self._find_duplicate(idempotency_key, input_snapshot)
                if duplicate is not None or self._duplicate_exists(idempotency_key):
                    return duplicate
            logger.error("Could not start sizing simulation (%s)", type(exc).__name__)
            raise SimulationPersistenceError("Could not create the sizing request.") from exc
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not start sizing simulation (%s)", type(exc).__name__)
            raise SimulationPersistenceError("Could not create the sizing request.") from exc

    @staticmethod
    def _resolve_duplicate(
        existing: SizingRequestRecord,
        requested_snapshot: dict[str, Any],
    ) -> dict[str, Any] | None:
        existing_snapshot = existing.input_snapshot or {}
        if not isinstance(existing_snapshot, dict):
            raise SimulationPersistenceError("The stored sizing input snapshot is invalid.")
        if _idempotency_identity(existing_snapshot) != _idempotency_identity(requested_snapshot):
            raise IdempotencyConflict(
                "Idempotency-Key was already used for a different sizing request."
            )
        runs = existing.simulation_runs
        if not runs:
            raise SimulationPersistenceError("The sizing request has no simulation run.")
        completed_response = _completed_response(existing, runs[0])
        if completed_response is not None:
            return completed_response
        if existing.status == "FAILED":
            raise SimulationPreviouslyFailed(existing.id)
        raise SimulationInProgress("A sizing request with this Idempotency-Key is in progress.")

    def _find_duplicate(
        self,
        idempotency_key: str,
        requested_snapshot: dict[str, Any],
    ) -> dict[str, Any] | None:
        try:
            with get_session_factory()() as session:
                existing = session.scalar(
                    select(SizingRequestRecord)
                    .where(SizingRequestRecord.idempotency_key == idempotency_key)
                    .options(selectinload(SizingRequestRecord.simulation_runs))
                )
                if existing is None:
                    return None
                return self._resolve_duplicate(existing, requested_snapshot)
        except (IdempotencyConflict, SimulationInProgress):
            raise
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not resolve sizing idempotency key (%s)", type(exc).__name__)
            raise SimulationPersistenceError("Could not resolve the sizing request.") from exc

    def _duplicate_exists(self, idempotency_key: str) -> bool:
        try:
            with get_session_factory()() as session:
                return (
                    session.scalar(
                        select(SizingRequestRecord.id).where(
                            SizingRequestRecord.idempotency_key == idempotency_key
                        )
                    )
                    is not None
                )
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not check sizing idempotency key (%s)", type(exc).__name__)
            raise SimulationPersistenceError("Could not resolve the sizing request.") from exc

    def complete_run(
        self,
        request_id: UUID,
        response: dict[str, Any],
        warnings: list[str],
        summary: dict[str, Any],
    ) -> None:
        try:
            with get_session_factory()() as session, session.begin():
                request = session.scalar(
                    select(SizingRequestRecord)
                    .where(SizingRequestRecord.id == request_id)
                    .with_for_update()
                )
                run = session.scalar(
                    select(SimulationRun)
                    .where(SimulationRun.request_id == request_id)
                    .with_for_update()
                )
                if request is None or run is None:
                    raise SimulationPersistenceError("The sizing request could not be completed.")
                if request.status != "SUBMITTED" or run.status != "RUNNING":
                    raise SimulationPersistenceError("The sizing request is not in a running state.")

                run.status = "SUCCEEDED"
                run.completed_at = _now()
                run.result_snapshot = response
                run.warnings = warnings
                run.initial_container_count = int(summary["n_init"])
                run.margin_applied_pct = Decimal(str(response["margin_applied_pct"]))
                run.in_range = bool(response["in_range"])
                run.accepted = bool(response["accepted"])
                run.note = str(response["note"])
                request.status = "COMPLETED"
                request.completed_at = run.completed_at
        except SimulationPersistenceError:
            raise
        except (DatabaseConfigurationError, SQLAlchemyError, KeyError, TypeError, ValueError) as exc:
            logger.error("Could not persist sizing results (%s)", type(exc).__name__)
            raise SimulationPersistenceError("Could not save the sizing results.") from exc

    def fail_run(self, request_id: UUID, error_code: str, message: str) -> None:
        try:
            with get_session_factory()() as session, session.begin():
                request = session.scalar(
                    select(SizingRequestRecord)
                    .where(SizingRequestRecord.id == request_id)
                    .with_for_update()
                )
                run = session.scalar(
                    select(SimulationRun)
                    .where(SimulationRun.request_id == request_id)
                    .with_for_update()
                )
                if request is None or run is None:
                    return
                if request.status != "SUBMITTED" or run.status != "RUNNING":
                    return
                request.status = "FAILED"
                request.completed_at = _now()
                run.status = "FAILED"
                run.completed_at = request.completed_at
                run.error_code = error_code[:100]
                run.error_message = message
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not persist sizing failure state (%s)", type(exc).__name__)
            raise SimulationPersistenceError("Could not record the sizing failure.") from exc

    def get_run(self, request_id: UUID, user_id: UUID) -> dict[str, Any] | None:
        try:
            with get_session_factory()() as session:
                request = session.scalar(
                    select(SizingRequestRecord)
                    .where(SizingRequestRecord.id == request_id)
                    .options(selectinload(SizingRequestRecord.simulation_runs))
                )
                if request is None:
                    return None
                if request.project_id is None:
                    if request.created_by_user_id != user_id:
                        return None
                elif _project_role(session, request.project_id, user_id) not in {
                    "owner",
                    "editor",
                    "viewer",
                }:
                    return None
                run = request.simulation_runs[0] if request.simulation_runs else None
                if run is None and request.status == "COMPLETED":
                    raise SimulationPersistenceError("The completed sizing result is missing.")
                response = _completed_response(request, run) if run is not None else None
                saved = {
                    "request_id": str(request.id),
                    "created_at": request.created_at.isoformat(),
                    "request": _input_for(request),
                    "response": response,
                }
                if response is None:
                    saved["status"] = run.status if run is not None else request.status
                    saved["error"] = (
                        {
                            "code": run.error_code,
                            "message": run.error_message,
                        }
                        if run is not None and run.error_code
                        else None
                    )
                return saved
        except SimulationPersistenceError:
            raise
        except ProjectAccessError:
            return None
        except (DatabaseConfigurationError, SQLAlchemyError) as exc:
            logger.error("Could not retrieve sizing simulation (%s)", type(exc).__name__)
            raise SimulationPersistenceError("Could not retrieve the sizing simulation.") from exc
