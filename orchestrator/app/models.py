from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}


class SessionOutcome(str, Enum):
    PR_OPENED = "pr_opened"
    NOT_APPLICABLE = "not_applicable"
    FAILED = "failed"
    SMOKE_OK = "smoke_ok"
    UNKNOWN = "unknown"


class CVEFinding(BaseModel):
    cve_id: str
    package: str
    affected_versions: str = ""
    fixed_version: str = ""
    severity: str = "unknown"
    summary: str = ""
    source_file: str = ""  # which requirements file it came from


class RemediationSession(BaseModel):
    session_id: str
    issue_number: int | None = None
    cve_id: str | None = None
    package: str | None = None
    title: str = ""
    status: SessionStatus = SessionStatus.QUEUED
    outcome: SessionOutcome = SessionOutcome.UNKNOWN
    pr_url: str | None = None
    notes: str = ""
    acu_used: float = 0.0
    severity: str = ""  # critical | high | medium | low — surfaced in the dashboard
    owner: str = "gabriel.cerioni"  # who triggered the session (for attribution in the UI)
    pollable: bool = True  # set False for seed/placeholder rows so the poller doesn't hit Devin
    started_at: str = Field(default_factory=utcnow_iso)
    ended_at: str | None = None
    devin_session_url: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class SessionEvent(BaseModel):
    """Append-only state-transition event for the dashboard's live log."""

    session_id: str
    event: str  # e.g., "created", "running", "completed", "pr_opened"
    timestamp: str = Field(default_factory=utcnow_iso)
    payload: dict[str, Any] = Field(default_factory=dict)
