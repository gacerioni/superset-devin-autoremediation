"""Thin async client for the Devin API (v3).

Same surface for mock + real — the difference is just the base URL.
- Real: https://api.devin.ai/v3 with Bearer cog_… auth
- Mock: http://localhost:8080/mock-devin/v3 (the orchestrator's own sub-app)
"""
from __future__ import annotations

from typing import Any

import httpx

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)


class DevinAPIError(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"Devin API {status}: {message}")
        self.status = status
        self.body = body


class DevinClient:
    def __init__(self, base_url: str, api_key: str, org_id: str, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.org_id = org_id
        self._timeout = timeout

    # ----------------- low-level -----------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _org_path(self, *parts: str) -> str:
        return "/".join([self.base_url, "organizations", self.org_id, *parts])

    async def _request(self, method: str, url: str, **kwargs) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.request(method, url, headers=self._headers(), **kwargs)
        if r.status_code == 404:
            raise DevinAPIError(404, "not found", body=_safe_body(r))
        if r.status_code >= 400:
            log.error("devin_api_error",
                      method=method, url=url, status=r.status_code, body=_safe_body(r))
            raise DevinAPIError(r.status_code, r.text[:400], body=_safe_body(r))
        if not r.content:
            return None
        return r.json()

    # ----------------- session ops -----------------
    async def create_session(
        self,
        *,
        prompt: str,
        title: str | None = None,
        tags: list[str] | None = None,
        max_acu_limit: int | None = None,
        bypass_approval: bool = True,
        repos: list[dict] | None = None,
        knowledge_ids: list[str] | None = None,
        playbook_id: str | None = None,
        structured_output_required: bool = False,
        structured_output_schema: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {"prompt": prompt}
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if max_acu_limit is not None:
            body["max_acu_limit"] = max_acu_limit
        if bypass_approval:
            body["bypass_approval"] = True
        if repos:
            body["repos"] = repos
        if knowledge_ids:
            body["knowledge_ids"] = knowledge_ids
        if playbook_id:
            body["playbook_id"] = playbook_id
        if structured_output_required:
            body["structured_output_required"] = True
        if structured_output_schema:
            body["structured_output_schema"] = structured_output_schema

        url = self._org_path("sessions")
        log.info("devin_create_session", title=title, max_acu_limit=max_acu_limit, tags=tags)
        return await self._request("POST", url, json=body)

    async def get_session(self, session_id: str) -> dict:
        url = self._org_path("sessions", session_id)
        return await self._request("GET", url)

    async def list_sessions(self, *, limit: int = 20, tags: list[str] | None = None) -> dict:
        params: dict[str, Any] = {"limit": limit}
        if tags:
            params["tags"] = ",".join(tags)
        url = self._org_path("sessions")
        return await self._request("GET", url, params=params)

    async def send_message(self, session_id: str, message: str) -> dict:
        url = self._org_path("sessions", session_id, "messages")
        return await self._request("POST", url, json={"message": message})

    async def cancel_session(self, session_id: str) -> dict:
        url = self._org_path("sessions", session_id)
        return await self._request("DELETE", url)


def _safe_body(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return r.text[:400]


# ----------------- factory -----------------
def get_devin_client() -> DevinClient:
    """Build a client based on settings.devin_mode.

    `mock` -> talks to the orchestrator's own /mock-devin/v3 sub-app
    `real` -> talks to https://api.devin.ai/v3 with the real API key
    """
    if settings.devin_mode.lower() == "real":
        return DevinClient(
            base_url=settings.devin_api_base,
            api_key=settings.devin_api_key,
            org_id=settings.devin_org_id,
        )
    # Mock: call ourselves on localhost. Inside the orchestrator container,
    # the orchestrator listens on 0.0.0.0:8080.
    return DevinClient(
        base_url="http://localhost:8080/mock-devin/v3",
        api_key="mock-key",
        org_id="org-mock",
    )


# ----------------- response → internal model mapper -----------------
def map_devin_status(devin_status: str | None) -> str:
    """Map Devin's status strings to our SessionStatus values."""
    if not devin_status:
        return "queued"
    s = devin_status.lower()
    mapping = {
        "new": "queued",
        "queued": "queued",
        "pending": "queued",
        "starting": "queued",
        "scheduled": "queued",
        "running": "running",
        "in_progress": "running",
        "working": "running",
        "blocked": "blocked",
        "waiting_for_user": "blocked",
        "needs_input": "blocked",
        "completed": "completed",
        "finished": "completed",
        "succeeded": "completed",
        "done": "completed",
        "failed": "failed",
        "error": "failed",
        "errored": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "stopped": "cancelled",
        "expired": "cancelled",
        "timed_out": "failed",
    }
    return mapping.get(s, "queued")  # default to queued on unknown status to avoid 500s


def extract_pr_url(devin_payload: dict) -> str | None:
    """Devin returns PRs in `pull_requests` (real API: each entry has `pr_url`).

    Falls back to structured_output.pr_url (our mock + Devin's own field).
    """
    prs = devin_payload.get("pull_requests") or []
    if prs:
        pr = prs[0]
        if isinstance(pr, dict):
            # Devin v3 uses `pr_url`. Mocks / other variants might use `url` / `html_url`.
            url = pr.get("pr_url") or pr.get("url") or pr.get("html_url")
            if url:
                return url
        elif isinstance(pr, str):
            return pr
    so = devin_payload.get("structured_output") or {}
    if isinstance(so, dict) and so.get("pr_url"):
        return so["pr_url"]
    return None


def extract_acu(devin_payload: dict) -> float:
    return float(
        devin_payload.get("acus_consumed")
        or devin_payload.get("acu_used")
        or 0.0
    )


def extract_devin_url(devin_payload: dict) -> str | None:
    return devin_payload.get("url") or devin_payload.get("devin_session_url")


_KNOWN_OUTCOMES = {"pr_opened", "not_applicable", "failed", "smoke_ok", "unknown"}


def extract_outcome(devin_payload: dict, mapped_status: str) -> str:
    """Determine the business outcome of a session. Always returns a value in SessionOutcome.

    Mirrors the strictness of `derive_terminal_from_output`: don't report `pr_opened`
    while Devin's structured_output is still a placeholder (no real pr_url) — that's
    misleading in the dashboard.
    """
    so = devin_payload.get("structured_output") or {}
    pr_url = extract_pr_url(devin_payload)
    if isinstance(so, dict) and so.get("status"):
        v = str(so["status"]).lower()
        if v == "pr_opened" and not pr_url:
            # Placeholder while Devin still works — don't trust it yet
            return "unknown"
        if v in _KNOWN_OUTCOMES:
            return v
    if pr_url:
        return "pr_opened"
    if mapped_status in {"failed", "cancelled"}:
        return "failed"
    return "unknown"


def derive_terminal_from_output(devin_payload: dict) -> str | None:
    """If structured_output says Devin's task is done, force a terminal status on our side.

    IMPORTANT: Devin fills structured_output PROGRESSIVELY while still working — we cannot
    trust `status` alone. We require:
      - the raw Devin status to NOT be actively working ("running"/"working"/"in_progress")
        UNLESS the raw status is specifically "waiting_for_user" (Devin finished but is
        idling for ack — that's our "done" signal for automated flows)
      - if outcome is `pr_opened`, a non-empty `pr_url` MUST be present (otherwise it's
        just a placeholder Devin filled in early)
    """
    so = devin_payload.get("structured_output") or {}
    if not isinstance(so, dict):
        return None
    v = str(so.get("status", "")).lower()
    raw_status = str(devin_payload.get("status", "")).lower()
    raw_detail = str(devin_payload.get("status_detail", "")).lower()

    actively_working = raw_status in {"running", "in_progress", "working", "new", "queued"} \
        and raw_detail not in {"waiting_for_user", "needs_input", "blocked"}
    if actively_working:
        # Devin is still moving — structured_output is a placeholder; don't force terminal.
        return None

    if v == "pr_opened":
        # Require an actual PR URL to consider this real.
        pr_url = so.get("pr_url")
        if not pr_url:
            return None
        return "completed"
    if v in {"smoke_ok", "not_applicable", "completed"}:
        return "completed"
    if v in {"failed", "error"}:
        return "failed"
    return None
