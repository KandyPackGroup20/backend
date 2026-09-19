"""Authenticated roster reads and durable MySQL assignment creation."""

import logging
import re
from datetime import date, datetime, timedelta
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.roster.dependencies import (
    get_roster_assignment_repository, get_roster_reporting_repository, get_roster_repository,
    require_roster_reader, require_roster_writer,
)
from app.roster.mysql_adapter import MySQLRosterAdapter
from app.roster.policy import AssignmentProposal
from app.roster.repository import (
    RosterBusinessRejection, RosterDataError, RosterReportingRepository, RosterRepository,
    RosterWindowError,
)
from app.roster.schemas import (
    AssignmentCreatedResponse, AssignmentRequest, AssignmentsResponse, AuditResponse,
    CandidatesResponse, HoursResponse, parse_instant,
)

logger = logging.getLogger(__name__)


class RosterRoute(APIRoute):
    """Keep both roster data and authentication/validation errors out of caches."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def no_store_handler(request: Request):
            try:
                response = await handler(request)
            except StarletteHTTPException as exc:
                response = await http_exception_handler(request, exc)
            except RequestValidationError as exc:
                response = await request_validation_exception_handler(request, exc)
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store_handler


router = APIRouter(
    prefix="/roster", tags=["Fleet Roster & Driver Assignment"], route_class=RosterRoute
)


def assignment_window(
    from_time: Annotated[str, Query(alias="from")],
    to_time: Annotated[str, Query(alias="to")],
) -> tuple[datetime, datetime]:
    try:
        start, end = parse_instant(from_time), parse_instant(to_time)
        if end <= start:
            raise ValueError("to must be later than from.")
        return start, end
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "INVALID_ROSTER_WINDOW", "message": str(exc)},
        ) from exc


def selected_week(week_start: Annotated[str, Query()]) -> date:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", week_start) is None:
            raise ValueError("week_start must use YYYY-MM-DD format.")
        result = date.fromisoformat(week_start)
        if result.year < 1000:
            raise ValueError("week_start is outside the supported MySQL date range.")
        if result.weekday() != 0:
            raise ValueError("week_start must be a Monday in Asia/Colombo.")
        result + timedelta(days=7)
        return result
    except (OverflowError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "INVALID_ROSTER_WEEK", "message": str(exc)},
        ) from exc


@router.get("/candidates", response_model=CandidatesResponse)
def get_roster_candidates(
    current_user: dict = Depends(require_roster_reader),
    repository: RosterRepository = Depends(get_roster_repository),
):
    try:
        return repository.candidates()
    except RosterDataError as exc:
        logger.error("Roster catalog read failed: %s", exc.error_code)
        raise HTTPException(status_code=503, detail={"error_code": exc.error_code, "message": exc.message}) from exc
    except Exception as exc:
        logger.exception("Roster catalog read failed")
        raise HTTPException(
            status_code=503,
            detail={"error_code": "ROSTER_DATA_UNAVAILABLE", "message": "Roster data is unavailable. Retry later."},
        ) from exc


@router.get("/assignments", response_model=AssignmentsResponse)
def get_roster_assignments(
    current_user: dict = Depends(require_roster_reader),
    window: tuple[datetime, datetime] = Depends(assignment_window),
    repository: RosterRepository = Depends(get_roster_repository),
):
    try:
        return repository.assignments(*window)
    except RosterWindowError as exc:
        raise HTTPException(status_code=422, detail={"error_code": "INVALID_ROSTER_WINDOW", "message": str(exc)}) from exc
    except RosterDataError as exc:
        logger.error("Roster assignment read failed: %s", exc.error_code)
        raise HTTPException(status_code=503, detail={"error_code": exc.error_code, "message": exc.message}) from exc
    except Exception as exc:
        logger.exception("Roster assignment read failed")
        raise HTTPException(
            status_code=503,
            detail={"error_code": "ROSTER_DATA_UNAVAILABLE", "message": "Roster data is unavailable. Retry later."},
        ) from exc


@router.get("/hours", response_model=HoursResponse)
def get_roster_hours(
    current_user: dict = Depends(require_roster_reader),
    week_start: date = Depends(selected_week),
    repository: RosterReportingRepository = Depends(get_roster_reporting_repository),
):
    try:
        return repository.hours(week_start)
    except RosterWindowError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "INVALID_ROSTER_WEEK", "message": str(exc)},
        ) from exc
    except RosterDataError as exc:
        logger.error("Roster hours read failed: %s", exc.error_code)
        raise HTTPException(
            status_code=503, detail={"error_code": exc.error_code, "message": exc.message},
        ) from exc
    except Exception as exc:
        logger.exception("Roster hours read failed")
        raise HTTPException(
            status_code=503,
            detail={"error_code": "ROSTER_DATA_UNAVAILABLE", "message": "Roster data is unavailable. Retry later."},
        ) from exc


@router.get("/audit", response_model=AuditResponse)
def get_roster_audit(
    current_user: dict = Depends(require_roster_reader),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    repository: RosterReportingRepository = Depends(get_roster_reporting_repository),
):
    try:
        return repository.audit(limit)
    except RosterWindowError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error_code": "INVALID_ROSTER_AUDIT_LIMIT", "message": str(exc)},
        ) from exc
    except RosterDataError as exc:
        logger.error("Roster audit read failed: %s", exc.error_code)
        raise HTTPException(
            status_code=503, detail={"error_code": exc.error_code, "message": exc.message},
        ) from exc
    except Exception as exc:
        logger.exception("Roster audit read failed")
        raise HTTPException(
            status_code=503,
            detail={"error_code": "ROSTER_DATA_UNAVAILABLE", "message": "Roster data is unavailable. Retry later."},
        ) from exc


@router.post("/assign", status_code=status.HTTP_201_CREATED, response_model=AssignmentCreatedResponse)
def assign_roster(
    request: AssignmentRequest,
    current_user: dict = Depends(require_roster_writer),
    repository: MySQLRosterAdapter = Depends(get_roster_assignment_repository),
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)
    ] = None,
):
    proposal = AssignmentProposal(**request.model_dump())
    try:
        result = repository.assign(
            proposal,
            actor_id=current_user["user_id"],
            request_key=idempotency_key or str(uuid4()),
        )
        return AssignmentCreatedResponse(
            result_code="ROSTER_ASSIGNMENT_REPLAYED" if result.replayed else "ROSTER_ASSIGNED",
            message="Roster assignment already exists." if result.replayed else "Roster assignment created.",
            assignment=result.assignment,
        )
    except RosterBusinessRejection as exc:
        missing = {"ROUTE_NOT_FOUND", "TRUCK_NOT_FOUND", "DRIVER_NOT_FOUND", "ASSISTANT_NOT_FOUND"}
        invalid = {
            "DRIVER_STAFF_TYPE_INVALID", "ASSISTANT_STAFF_TYPE_INVALID",
            "DRIVER_ASSISTANT_SAME_PERSON", "INVALID_ASSIGNMENT_INTERVAL", "TRUCK_INACTIVE",
            "DRIVER_INACTIVE", "ASSISTANT_INACTIVE",
        }
        rejection_status = 404 if exc.error_code in missing else 422 if exc.error_code in invalid else 409
        raise HTTPException(
            status_code=rejection_status,
            detail={"error_code": exc.error_code, "message": exc.message},
        ) from exc
    except RosterDataError as exc:
        logger.error("Roster assignment failed: %s", exc.error_code)
        raise HTTPException(
            status_code=503,
            detail={"error_code": exc.error_code, "message": exc.message},
        ) from exc
    except Exception as exc:
        logger.exception("Roster assignment failed")
        raise HTTPException(
            status_code=503,
            detail={"error_code": "ROSTER_ASSIGNMENT_FAILED", "message": "Roster assignment could not be stored."},
        ) from exc
