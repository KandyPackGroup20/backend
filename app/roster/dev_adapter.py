"""Single-process, volatile development data. Never a database error fallback."""

from datetime import datetime
from threading import RLock

from app.roster.schemas import Assignment, AssignmentsResponse, CandidatesResponse


class DevelopmentRosterAdapter:
    def __init__(self, catalog: CandidatesResponse, assignments: tuple[Assignment, ...]):
        self._catalog = catalog
        self._assignments = tuple(sorted(assignments, key=lambda row: (row.start_time, row.roster_id)))
        self._lock = RLock()

    def candidates(self) -> CandidatesResponse:
        # Models and all nested values are immutable, so readers cannot mutate data.
        with self._lock:
            return self._catalog

    def assignments(self, start: datetime, end: datetime) -> AssignmentsResponse:
        with self._lock:
            return AssignmentsResponse(
                assignments=tuple(
                    row for row in self._assignments
                    if row.start_time < end and row.end_time > start
                ),
                meta=self._catalog.meta,
            )
