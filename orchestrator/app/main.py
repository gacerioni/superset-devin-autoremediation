"""FastAPI entrypoint for the orchestrator."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from app.config import settings
from app.devin_client import (
    DevinAPIError,
    extract_acu,
    extract_devin_url,
    extract_outcome,
    extract_pr_url,
    get_devin_client,
    map_devin_status,
)
from app.logging_config import configure_logging, get_logger
from app.mock_devin import router as mock_devin_router
from app.models import (
    RemediationSession,
    SessionEvent,
    SessionOutcome,
    SessionStatus,
    utcnow_iso,
)
from app.orchestrator import dispatch_cve_remediation
from app.poller import poll_once, run_poller
from app.scanner import run_scan_and_file_issues
from app.storage import (
    emit_event,
    get_redis,
    list_sessions,
    record_session_started,
    save_session,
    tail_events,
)

configure_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("orchestrator_starting", devin_mode=settings.devin_mode, app_env=settings.app_env)
    try:
        get_redis().ping()
        log.info("redis_connected", url=settings.redis_url)
    except Exception as exc:  # noqa: BLE001
        log.error("redis_connect_failed", error=str(exc))

    poller_task = asyncio.create_task(run_poller())
    log.info("poller_task_spawned")
    try:
        yield
    finally:
        log.info("orchestrator_stopping")
        poller_task.cancel()
        try:
            await poller_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Devin Auto-Remediation Orchestrator",
    description="Event-driven orchestrator that drives Devin sessions for CVE remediation in Apache Superset.",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(mock_devin_router)


# -----------------------------------------------------------------------------
# basic
# -----------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    return {
        "service": "devin-auto-remediation-orchestrator",
        "version": "0.2.0",
        "devin_mode": settings.devin_mode,
        "endpoints": {
            "health": "/health",
            "sessions": "/api/sessions",
            "events": "/api/events",
            "trigger_smoke": "POST /trigger/smoke",
            "trigger_placeholder": "POST /trigger/placeholder",
            "poll_now": "POST /poll/now",
            "openapi": "/docs",
        },
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    redis_ok = False
    try:
        redis_ok = bool(get_redis().ping())
    except Exception:  # noqa: BLE001
        redis_ok = False
    return {"status": "ok" if redis_ok else "degraded", "redis": redis_ok, "devin_mode": settings.devin_mode}


# -----------------------------------------------------------------------------
# read-only
# -----------------------------------------------------------------------------
@app.get("/api/sessions")
async def api_list_sessions(limit: int = 100) -> dict[str, Any]:
    items = [s.model_dump() for s in list_sessions(limit=limit)]
    return {"sessions": items, "total": len(items)}


@app.get("/api/events")
async def api_events(limit: int = 50) -> dict[str, Any]:
    return {"events": tail_events(count=limit)}


# -----------------------------------------------------------------------------
# triggers
# -----------------------------------------------------------------------------
@app.post("/trigger/placeholder")
async def trigger_placeholder() -> dict[str, Any]:
    """Insert a fake session into Redis (no Devin call). Useful for UI sanity."""
    import uuid
    sid = "ses_placeholder_" + uuid.uuid4().hex[:8]
    session = RemediationSession(
        session_id=sid,
        title="Placeholder session (no Devin call)",
        status=SessionStatus.QUEUED,
        cve_id="CVE-PLACEHOLDER",
        package="placeholder",
        pollable=False,
    )
    save_session(session)
    emit_event(SessionEvent(session_id=sid, event="created", payload={"kind": "placeholder"}))
    return {"created": session.model_dump()}


SMOKE_PROMPT = (
    "SMOKE TEST — connectivity validation only.\n\n"
    "Please:\n"
    "1. Print exactly the line: Hello from Devin. Smoke test OK.\n"
    "2. Do NOT clone any repository.\n"
    "3. Do NOT modify any files in your workspace.\n"
    "4. Do NOT open any pull request.\n"
    "5. Return structured output with `status` = `smoke_ok` and a short `notes` field.\n\n"
    "End the session immediately after step 5. This is a one-shot connectivity check."
)

SMOKE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["smoke_ok", "error"]},
        "notes": {"type": "string"},
    },
    "required": ["status"],
}


@app.post("/trigger/smoke")
async def trigger_smoke() -> dict[str, Any]:
    """Fire a single tiny Devin session to validate the full loop.

    Safe by construction: prompt forbids clone/PR/file changes.
    `max_acu_limit=1` caps spend below ~$2.25 worst case.
    Expected spend: ~0.05 ACU.
    """
    client = get_devin_client()
    try:
        resp = await client.create_session(
            prompt=SMOKE_PROMPT,
            title="Smoke test — connectivity",
            tags=["smoke", "connectivity"],
            max_acu_limit=1,
            bypass_approval=True,
            structured_output_required=True,
            structured_output_schema=SMOKE_OUTPUT_SCHEMA,
        )
    except DevinAPIError as e:
        log.error("smoke_devin_create_failed", status=e.status, body=e.body)
        raise HTTPException(status_code=502, detail=f"Devin API error: {e}")

    sid = resp.get("session_id") or resp.get("id")
    if not sid:
        raise HTTPException(status_code=500, detail=f"Devin response missing session_id: {resp}")

    devin_url = extract_devin_url(resp) or f"https://app.devin.ai/sessions/{sid}"

    session = RemediationSession(
        session_id=sid,
        title="Smoke test — connectivity (Devin says hello)",
        status=SessionStatus(map_devin_status(resp.get("status"))),
        outcome=SessionOutcome.UNKNOWN,
        cve_id=None,
        package=None,
        severity="",
        owner="gabriel.cerioni",
        acu_used=extract_acu(resp),
        devin_session_url=devin_url,
        notes="Connectivity-only test. No code touched.",
        extra={"kind": "smoke"},
    )
    save_session(session)
    record_session_started()
    emit_event(SessionEvent(
        session_id=sid,
        event="created:smoke",
        payload={"devin_url": devin_url, "mode": settings.devin_mode},
    ))

    log.info("smoke_session_created", session_id=sid, devin_url=devin_url, mode=settings.devin_mode)
    return {
        "session_id": sid,
        "devin_url": devin_url,
        "mode": settings.devin_mode,
        "max_acu_limit": 1,
        "watch": "Open the devin_url in your browser to watch the session live.",
        "session": session.model_dump(),
    }


@app.post("/poll/now")
async def poll_now() -> dict[str, Any]:
    """Trigger an immediate poll pass — handy for the Loom (don't wait 30s)."""
    client = get_devin_client()
    updated = await poll_once(client)
    return {"updated": updated, "mode": settings.devin_mode}


@app.post("/trigger/scan")
async def trigger_scan(dispatch: bool = True, max_issues: int = 5) -> dict[str, Any]:
    """Run pip-audit → file GH issues → (optionally) dispatch Devin sessions.

    Args:
        dispatch: if True, also create a Devin session per new issue (full flow).
                  if False, only file the issues — Devin sessions stay manual.
        max_issues: cap on how many NEW issues to file per scan (safety).
    """
    try:
        result = await asyncio.to_thread(
            run_scan_and_file_issues,
            settings.github_repo,
            max_issues=max_issues,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=f"requirements file missing: {e}")
    except Exception as e:  # noqa: BLE001
        log.error("scan_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"scan failed: {e}")

    dispatched: list[dict[str, Any]] = []
    if dispatch:
        # Map issue → finding by CVE id, then dispatch
        findings_by_cve = {f["cve_id"]: f for f in result.findings}
        for issue in result.new_issues:
            finding = findings_by_cve.get(issue["cve_id"])
            if not finding:
                continue
            session = await dispatch_cve_remediation(finding, issue_number=issue["number"])
            dispatched.append({
                "session_id": session.session_id,
                "devin_url": session.devin_session_url,
                "issue_number": issue["number"],
                "cve": session.cve_id,
            })

    return {
        "total_findings": result.total_findings,
        "new_issues_created": len(result.new_issues),
        "skipped_duplicates": len(result.skipped_duplicates),
        "skipped_cves": result.skipped_duplicates,
        "issues": result.new_issues,
        "dispatched_sessions": dispatched,
        "mode": settings.devin_mode,
    }


@app.delete("/api/sessions/{session_id}")
async def cancel_session(session_id: str) -> dict[str, Any]:
    """Cancel a running Devin session. Proxies DELETE to api.devin.ai."""
    client = get_devin_client()
    try:
        resp = await client.cancel_session(session_id)
    except DevinAPIError as e:
        raise HTTPException(status_code=e.status, detail=f"Devin cancel failed: {e}")
    emit_event(SessionEvent(session_id=session_id, event="cancelled:by_user", payload={}))
    return {"cancelled": True, "devin_response": resp}


@app.post("/webhook/github")
async def webhook_github(request: Request) -> dict[str, Any]:
    """Receive GitHub webhook events. Dispatches Devin on issue:opened with `devin-remediate` label.

    For the demo this endpoint can be exposed to GitHub via smee.io or ngrok. It validates the
    optional HMAC signature (X-Hub-Signature-256) when GITHUB_WEBHOOK_SECRET is set.
    """
    import hashlib
    import hmac

    raw = await request.body()
    sig_header = request.headers.get("X-Hub-Signature-256", "")
    event = request.headers.get("X-GitHub-Event", "")

    if settings.github_webhook_secret and settings.github_webhook_secret != "change-me-in-prod":
        expected = "sha256=" + hmac.new(
            settings.github_webhook_secret.encode(),
            raw,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig_header):
            log.warning("webhook_invalid_signature", event=event)
            raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid JSON payload")

    log.info("webhook_received", event=event, action=payload.get("action"))

    if event != "issues" or payload.get("action") != "opened":
        return {"ignored": True, "reason": "not issues:opened", "event": event, "action": payload.get("action")}

    issue = payload.get("issue") or {}
    labels = [l.get("name") for l in (issue.get("labels") or [])]
    if settings.github_remediate_label not in labels:
        return {"ignored": True, "reason": f"missing {settings.github_remediate_label} label", "labels": labels}

    # Title is "Fix CVE-XXXX-YYYY in <pkg>" — extract CVE id
    title = issue.get("title", "")
    cve_id = next((tok for tok in title.split() if tok.startswith("CVE-")), None)
    if not cve_id:
        return {"ignored": True, "reason": "no CVE id in title", "title": title}

    # Re-run the scanner to get the structured finding for this CVE
    from app.scanner import run_pip_audit
    findings = await asyncio.to_thread(run_pip_audit)
    finding = next((f for f in findings if f.cve_id == cve_id), None)
    if not finding:
        return {"ignored": True, "reason": f"CVE {cve_id} not in current scan", "cve_id": cve_id}

    session = await dispatch_cve_remediation(finding.to_dict(), issue_number=issue.get("number"))
    return {
        "dispatched": True,
        "cve_id": cve_id,
        "issue_number": issue.get("number"),
        "session_id": session.session_id,
        "devin_url": session.devin_session_url,
    }


@app.post("/trigger/dispatch")
async def trigger_dispatch(cve_id: str, issue_number: int | None = None) -> dict[str, Any]:
    """Manually dispatch a Devin session for a specific CVE.

    Use case: you ran /trigger/scan?dispatch=false earlier (created issues only).
    Now you want Devin to actually work one of them — call this with the CVE id.
    """
    from app.scanner import run_pip_audit  # local import to avoid hot-reload edge cases

    findings = await asyncio.to_thread(run_pip_audit)
    finding = next((f for f in findings if f.cve_id == cve_id), None)
    if not finding:
        raise HTTPException(
            status_code=404,
            detail=f"CVE {cve_id} not found in current scan. Available: {[f.cve_id for f in findings]}",
        )
    session = await dispatch_cve_remediation(finding.to_dict(), issue_number=issue_number)
    return {
        "session_id": session.session_id,
        "devin_url": session.devin_session_url,
        "cve": session.cve_id,
        "title": session.title,
        "watch": f"Open {session.devin_session_url} in your browser to watch Devin work.",
    }
