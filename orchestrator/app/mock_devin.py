"""In-process mock of the Devin API for local development.

Mimics the v3 endpoints we actually use:
  POST   /v3/organizations/{org_id}/sessions
  GET    /v3/organizations/{org_id}/sessions/{session_id}
  GET    /v3/organizations/{org_id}/sessions
  DELETE /v3/organizations/{org_id}/sessions/{session_id}

A mock session moves queued -> running -> completed over ~30s.
About 80% land as `pr_opened`, 15% `not_applicable`, 5% `failed` — tunable.
"""
from __future__ import annotations

import asyncio
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Path

router = APIRouter(prefix="/mock-devin/v3", tags=["mock-devin"])


@dataclass
class MockSession:
    session_id: str
    org_id: str
    prompt: str
    title: str
    tags: list[str]
    max_acu_limit: float
    created_at: float = field(default_factory=time.time)
    status: str = "queued"
    structured_output: dict[str, Any] | None = None
    acu_used: float = 0.0
    devin_session_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "title": self.title,
            "tags": self.tags,
            "structured_output": self.structured_output,
            "acu_used": self.acu_used,
            "url": self.devin_session_url,
            "created_at": self.created_at,
            "max_acu_limit": self.max_acu_limit,
        }


_sessions: dict[str, MockSession] = {}


def _pick_outcome() -> tuple[str, dict[str, Any]]:
    roll = random.random()
    sid = uuid.uuid4().hex[:8]
    if roll < 0.80:
        pr = f"https://github.com/gacerioni/superset/pull/{random.randint(1000, 9999)}"
        return "completed", {
            "pr_url": pr,
            "status": "pr_opened",
            "notes": f"Bumped package, fixed {random.randint(1, 5)} call sites, updated tests. [mock-{sid}]",
        }
    if roll < 0.95:
        return "completed", {
            "pr_url": None,
            "status": "not_applicable",
            "notes": f"CVE doesn't affect Superset's usage of this dep. [mock-{sid}]",
        }
    return "failed", {
        "pr_url": None,
        "status": "failed",
        "notes": f"Tests still red after upgrade; backed off. [mock-{sid}]",
    }


async def _drive_lifecycle(session_id: str) -> None:
    """Advance the session through its states with realistic-feeling delays."""
    await asyncio.sleep(random.uniform(2, 4))
    s = _sessions.get(session_id)
    if not s:
        return
    s.status = "running"
    s.devin_session_url = f"https://app.devin.ai/sessions/{session_id}"

    await asyncio.sleep(random.uniform(15, 25))
    s = _sessions.get(session_id)
    if not s or s.status == "cancelled":
        return
    final_status, output = _pick_outcome()
    s.status = final_status
    s.structured_output = output
    s.acu_used = round(random.uniform(0.5, min(2.5, s.max_acu_limit)), 2)


@router.post("/organizations/{org_id}/sessions", status_code=201)
async def create_session(org_id: str, body: dict[str, Any]) -> dict[str, Any]:
    if not body.get("prompt"):
        raise HTTPException(400, detail="prompt is required")
    session_id = "ses_" + uuid.uuid4().hex[:16]
    s = MockSession(
        session_id=session_id,
        org_id=org_id,
        prompt=body["prompt"],
        title=body.get("title", "untitled"),
        tags=body.get("tags", []),
        max_acu_limit=float(body.get("max_acu_limit", 3)),
    )
    _sessions[session_id] = s
    asyncio.create_task(_drive_lifecycle(session_id))
    return s.to_dict()


@router.get("/organizations/{org_id}/sessions/{session_id}")
async def get_session(org_id: str, session_id: str = Path(...)) -> dict[str, Any]:
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(404, detail="session not found")
    return s.to_dict()


@router.get("/organizations/{org_id}/sessions")
async def list_sessions(org_id: str) -> dict[str, Any]:
    items = [s.to_dict() for s in _sessions.values()]
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return {"sessions": items, "total": len(items)}


@router.delete("/organizations/{org_id}/sessions/{session_id}")
async def cancel_session(org_id: str, session_id: str) -> dict[str, Any]:
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(404, detail="session not found")
    s.status = "cancelled"
    return s.to_dict()
