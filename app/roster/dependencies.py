"""Roster-local authorization and explicit, fail-closed data configuration."""

import logging
import os
from threading import Lock

from fastapi import Depends, HTTPException

from app.core.security import require_roles
from app.roster.dev_adapter import DevelopmentRosterAdapter
from app.roster.fixtures import make_fixtures
from app.roster.mysql_adapter import MySQLRosterAdapter
from app.roster.repository import RosterReportingRepository, RosterRepository

logger = logging.getLogger(__name__)
_adapter: RosterRepository | None = None
_initialization_failed = False
_initialization_lock = Lock()


def _require_reset_complete(user: dict) -> dict:
    if user.get("force_password_reset"):
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "PASSWORD_RESET_REQUIRED",
                "message": "Complete your password reset from your profile before accessing the roster.",
            },
        )
    return user


def require_roster_reader(
    user: dict = Depends(require_roles(["DISPATCHER", "SUPERADMIN", "LOGISTICS_MGR"])),
) -> dict:
    return _require_reset_complete(user)


def require_roster_writer(
    user: dict = Depends(require_roles(["DISPATCHER", "SUPERADMIN", "LOGISTICS_MGR"])),
) -> dict:
    return _require_reset_complete(user)


def get_roster_repository() -> RosterRepository:
    global _adapter, _initialization_failed
    if os.environ.get("ROSTER_DATA_MODE") == "mysql":
        # Stateless: each operation opens its own connection, including across workers.
        return MySQLRosterAdapter()
    if os.environ.get("APP_ENV") != "development" or os.environ.get("ROSTER_DATA_MODE") != "dev-memory":
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "ROSTER_DATA_DISABLED",
                "message": "Select ROSTER_DATA_MODE=mysql, or APP_ENV=development with ROSTER_DATA_MODE=dev-memory.",
            },
        )
    if os.environ.get("WEB_CONCURRENCY", "1") != "1":
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "ROSTER_UNSUPPORTED_CONFIGURATION",
                "message": "The development roster adapter requires a single backend worker.",
            },
        )
    with _initialization_lock:
        if _initialization_failed:
            raise HTTPException(
                status_code=503,
                detail={"error_code": "ROSTER_DATA_UNAVAILABLE", "message": "Roster initialization failed. Restart after resolving the cause."},
            )
        if _adapter is None:
            try:
                _adapter = DevelopmentRosterAdapter(*make_fixtures())
            except Exception as exc:
                _initialization_failed = True
                logger.exception("Roster development adapter initialization failed")
                raise HTTPException(
                    status_code=503,
                    detail={"error_code": "ROSTER_DATA_UNAVAILABLE", "message": "Roster initialization failed. Restart after resolving the cause."},
                ) from exc
        return _adapter


def get_roster_assignment_repository() -> MySQLRosterAdapter:
    if os.environ.get("ROSTER_DATA_MODE") == "mysql":
        return MySQLRosterAdapter()
    raise HTTPException(
        status_code=503,
        detail={
            "error_code": "ROSTER_ASSIGNMENT_NOT_IMPLEMENTED",
            "message": "Assignment creation requires ROSTER_DATA_MODE=mysql.",
        },
    )


def get_roster_reporting_repository() -> RosterReportingRepository:
    """Return the database-backed repository for roster reports."""
    if os.environ.get("ROSTER_DATA_MODE") == "mysql":
        return MySQLRosterAdapter()
    if (os.environ.get("APP_ENV") == "development"
            and os.environ.get("ROSTER_DATA_MODE") == "dev-memory"):
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "ROSTER_REPORTING_REQUIRES_MYSQL",
                "message": "Selected-week and persistent audit reporting require ROSTER_DATA_MODE=mysql.",
            },
        )
    raise HTTPException(
        status_code=503,
        detail={
            "error_code": "ROSTER_DATA_DISABLED",
            "message": "Select ROSTER_DATA_MODE=mysql for persistent roster reporting.",
        },
    )
