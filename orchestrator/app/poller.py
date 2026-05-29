"""Background poller: hydrate Redis state from Devin sessions.

Devin doesn't push state via webhooks (yet), so we poll. Every N seconds:
  - load active (non-terminal) sessions from Redis
  - GET /sessions/{id} from Devin
  - update Redis if anything changed
  - emit events on state transitions
"""
from __future__ import annotations

import asyncio

from app.config import settings
from app.devin_client import (
    DevinAPIError,
    DevinClient,
    derive_terminal_from_output,
    extract_acu,
    extract_devin_url,
    extract_outcome,
    extract_pr_url,
    get_devin_client,
    map_devin_status,
)
from app.logging_config import get_logger
from app.models import (
    RemediationSession,
    SessionEvent,
    SessionOutcome,
    SessionStatus,
    utcnow_iso,
)
from app.storage import (
    active_session_ids,
    emit_event,
    get_session,
    record_acu_spent,
    record_session_completed,
    save_session,
)
from app.scanner import comment_on_issue, close_issue  # noqa: E402

log = get_logger(__name__)


async def poll_once(client: DevinClient) -> int:
    """One pass over active sessions. Returns the number of sessions updated."""
    ids = active_session_ids()
    if not ids:
        return 0

    updated = 0
    for sid in ids:
        local = get_session(sid)
        if not local:
            continue
        try:
            remote = await client.get_session(sid)
        except DevinAPIError as e:
            if e.status in (401, 403, 404):
                # Devin doesn't know / doesn't authorize this session — likely a seed/mock leftover.
                log.debug("poll_skip", session_id=sid, status=e.status)
                continue
            log.warning("poll_error", session_id=sid, status=e.status)
            continue
        except Exception as e:  # noqa: BLE001
            log.warning("poll_unexpected", session_id=sid, error=str(e))
            continue

        new_status_raw = remote.get("status")
        new_status = SessionStatus(map_devin_status(new_status_raw))
        # Devin can sit in "running"/"waiting_for_user" after the task is done.
        # If structured_output gave us a definitive verdict, force a terminal state.
        terminal_override = derive_terminal_from_output(remote)
        if terminal_override:
            new_status = SessionStatus(terminal_override)
        new_acu = extract_acu(remote)
        new_pr = extract_pr_url(remote)
        new_devin_url = extract_devin_url(remote)
        try:
            new_outcome = SessionOutcome(extract_outcome(remote, new_status.value))
        except ValueError:
            new_outcome = SessionOutcome.UNKNOWN

        prev_status = local.status

        changed = (
            new_status != prev_status
            or (new_pr and new_pr != local.pr_url)
            or abs(new_acu - local.acu_used) > 0.01
            or (new_devin_url and not local.devin_session_url)
            or new_outcome != local.outcome
        )
        if not changed:
            continue

        local.status = new_status
        local.acu_used = new_acu
        if new_pr:
            local.pr_url = new_pr
        if new_devin_url:
            local.devin_session_url = new_devin_url
        local.outcome = new_outcome

        # Extract Devin's structured_output.notes if present
        so = remote.get("structured_output") or {}
        if isinstance(so, dict) and so.get("notes"):
            local.notes = str(so["notes"])[:1000]

        if new_status.is_terminal and not local.ended_at:
            local.ended_at = utcnow_iso()

        save_session(local)
        updated += 1

        emit_event(SessionEvent(
            session_id=sid,
            event=f"poll:{new_status.value}",
            payload={
                "prev": prev_status.value,
                "next": new_status.value,
                "acu": new_acu,
                "pr_url": new_pr,
                "outcome": new_outcome.value,
            },
        ))

        if new_status.is_terminal:
            record_session_completed(success=(new_status == SessionStatus.COMPLETED))
            if new_acu > 0:
                record_acu_spent(new_acu)
            # Close the loop visually — comment back on the source GitHub issue.
            if local.issue_number is not None and local.extra.get("kind") == "cve" \
               and not local.extra.get("issue_commented"):
                try:
                    _post_completion_comment(local, remote)
                    local.extra["issue_commented"] = True
                    save_session(local)
                except Exception as exc:  # noqa: BLE001
                    log.warning("issue_comment_failed", issue=local.issue_number, error=str(exc))

        log.info(
            "poll_state_change",
            session_id=sid,
            prev=prev_status.value,
            next=new_status.value,
            acu=new_acu,
            pr=new_pr,
        )

    return updated


def _post_completion_comment(local: RemediationSession, remote: dict) -> None:
    """Comment on the source GitHub issue with the result of a terminal Devin session.

    Optionally close the issue if Devin opened a PR or determined the CVE doesn't apply.
    """
    from app.config import settings

    outcome = local.outcome.value
    devin_url = local.devin_session_url or "(no link)"
    acu = local.acu_used

    if outcome == "pr_opened" and local.pr_url:
        body = (
            f"### 🤖 Devin completed the remediation.\n\n"
            f"- **Outcome**: `pr_opened`\n"
            f"- **PR**: {local.pr_url}\n"
            f"- **ACUs consumed**: `{acu:.2f}` (≈ ${acu * 2.25:.2f})\n"
            f"- **Session log**: {devin_url}\n\n"
            f"> {local.notes[:600] if local.notes else ''}\n\n"
            f"_This comment was posted automatically by the Devin auto-remediation orchestrator._"
        )
        comment_on_issue(settings.github_repo, local.issue_number, body)
        close_issue(settings.github_repo, local.issue_number, reason="completed")
    elif outcome == "not_applicable":
        body = (
            f"### 🧠 Devin reviewed and concluded this CVE does NOT apply.\n\n"
            f"- **Outcome**: `not_applicable`\n"
            f"- **ACUs consumed**: `{acu:.2f}` (≈ ${acu * 2.25:.2f})\n"
            f"- **Session log**: {devin_url}\n\n"
            f"> {local.notes[:600] if local.notes else ''}\n\n"
            f"_Closing — no action needed. This judgment was made by Devin after reviewing the codebase._"
        )
        comment_on_issue(settings.github_repo, local.issue_number, body)
        close_issue(settings.github_repo, local.issue_number, reason="not_planned")
    else:
        # Failed or other terminal — comment but don't close
        body = (
            f"### ⚠️ Devin's remediation attempt ended without success.\n\n"
            f"- **Outcome**: `{outcome}`\n"
            f"- **Status**: `{local.status.value}`\n"
            f"- **ACUs consumed**: `{acu:.2f}` (≈ ${acu * 2.25:.2f})\n"
            f"- **Session log**: {devin_url}\n\n"
            f"> {local.notes[:600] if local.notes else ''}\n\n"
            f"_Leaving the issue open for human review._"
        )
        comment_on_issue(settings.github_repo, local.issue_number, body)


async def run_poller():
    """Long-running background task — call from FastAPI lifespan."""
    log.info("poller_starting", interval_seconds=settings.poll_interval_seconds, mode=settings.devin_mode)
    try:
        while True:
            try:
                n = await poll_once(get_devin_client())
                if n:
                    log.info("poll_pass", updated=n)
            except Exception as e:  # noqa: BLE001
                log.error("poll_loop_error", error=str(e))
            await asyncio.sleep(settings.poll_interval_seconds)
    except asyncio.CancelledError:
        log.info("poller_cancelled")
        raise
