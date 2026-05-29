"""Core orchestration: turn a CVE finding (or a GitHub issue) into a Devin session.

This is the glue between scanner / webhook → DevinClient → RemediationSession.
"""
from __future__ import annotations

import json
from typing import Any

from app.config import settings
from app.devin_client import (
    DevinAPIError,
    extract_acu,
    extract_devin_url,
    get_devin_client,
    map_devin_status,
)
from app.logging_config import get_logger
from app.models import (
    RemediationSession,
    SessionEvent,
    SessionOutcome,
    SessionStatus,
)
from app.prompts import CVE_REMEDIATION_PROMPT
from app.storage import emit_event, record_session_started, save_session

log = get_logger(__name__)

# Schema Devin must return after working a CVE
CVE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["pr_opened", "not_applicable", "failed"],
        },
        "pr_url": {"type": ["string", "null"]},
        "notes": {"type": "string"},
    },
    "required": ["status", "notes"],
}


def _build_prompt(finding: dict) -> str:
    return CVE_REMEDIATION_PROMPT.format(
        cve_id=finding["cve_id"],
        advisory_id=finding.get("advisory_id", finding["cve_id"]),
        package=finding["package"],
        current_version=finding["current_version"],
        fix_versions_str=finding.get("fix_versions_str") or ", ".join(finding.get("fix_versions") or []) or "(no fix available)",
        source_file=finding.get("source_file", "base.txt"),
        summary=(finding.get("summary") or "").strip()[:1200],
        repo=settings.github_repo,
    )


async def dispatch_cve_remediation(finding: dict, issue_number: int | None = None) -> RemediationSession:
    """Create a Devin session for a single CVE and persist a RemediationSession in Redis.

    Returns the persisted session record.
    """
    client = get_devin_client()
    prompt = _build_prompt(finding)
    title = f"Fix {finding['cve_id']} in {finding['package']}"
    tags = [
        "cve-remediation",
        f"pkg:{finding['package']}",
        finding["cve_id"],
    ]
    if issue_number is not None:
        tags.append(f"issue-{issue_number}")

    log.info("dispatch_cve", cve=finding["cve_id"], package=finding["package"], issue=issue_number)

    try:
        resp = await client.create_session(
            prompt=prompt,
            title=title,
            tags=tags,
            max_acu_limit=settings.devin_max_acu_per_session,
            bypass_approval=True,
            # Devin's v3 API expects repos as a list of strings (repo URLs), not objects.
            repos=[f"https://github.com/{settings.github_repo}"],
            structured_output_required=True,
            structured_output_schema=CVE_OUTPUT_SCHEMA,
        )
    except DevinAPIError as e:
        log.error("devin_dispatch_failed", cve=finding["cve_id"], status=e.status)
        # Persist a "failed-on-dispatch" session so the dashboard reflects the attempt.
        sid = f"dispatch_failed_{finding['cve_id']}"
        session = RemediationSession(
            session_id=sid,
            issue_number=issue_number,
            cve_id=finding["cve_id"],
            package=finding["package"],
            severity=finding.get("severity", "medium"),
            title=title,
            status=SessionStatus.FAILED,
            outcome=SessionOutcome.FAILED,
            notes=f"Devin API error on dispatch (status={e.status}): {str(e.body)[:300]}",
            owner="gabriel.cerioni",
            pollable=False,
            extra={"kind": "cve", "dispatch_error": True},
        )
        save_session(session)
        emit_event(SessionEvent(session_id=sid, event="dispatch_failed",
                                payload={"cve": finding["cve_id"], "status": e.status}))
        return session

    sid = resp.get("session_id") or resp.get("id")
    if not sid:
        raise RuntimeError(f"Devin response missing session_id: {json.dumps(resp)[:200]}")

    devin_url = extract_devin_url(resp) or f"https://app.devin.ai/sessions/{sid}"
    status = SessionStatus(map_devin_status(resp.get("status")))

    session = RemediationSession(
        session_id=sid,
        issue_number=issue_number,
        cve_id=finding["cve_id"],
        package=finding["package"],
        severity=finding.get("severity", "medium"),
        title=title,
        status=status,
        outcome=SessionOutcome.UNKNOWN,
        acu_used=extract_acu(resp),
        devin_session_url=devin_url,
        owner="gabriel.cerioni",
        pollable=True,
        notes=f"Dispatched to Devin. Watch live: {devin_url}",
        extra={"kind": "cve", "finding": finding},
    )
    save_session(session)
    record_session_started()
    emit_event(SessionEvent(
        session_id=sid,
        event="dispatched:cve",
        payload={
            "cve": finding["cve_id"],
            "package": finding["package"],
            "devin_url": devin_url,
            "issue": issue_number,
        },
    ))
    log.info("devin_session_dispatched", session_id=sid, devin_url=devin_url, cve=finding["cve_id"])
    return session
