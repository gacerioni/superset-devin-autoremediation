"""Seed realistic-looking demo sessions into Redis.

Run from inside the orchestrator container:
    docker compose exec orchestrator python /scripts/seed_demo_sessions.py

Or, if mounted differently:
    docker compose exec orchestrator python -c "exec(open('/seed.py').read())"

Idempotent — wipes existing sessions:* keys first.
"""
from __future__ import annotations

import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow running this file from /scripts (mounted) when app/ is on the python path.
sys.path.insert(0, "/app")

from app.models import RemediationSession, SessionEvent, SessionOutcome, SessionStatus  # noqa: E402
from app.storage import emit_event, get_redis, save_session  # noqa: E402


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def wipe_existing():
    r = get_redis()
    # Bulk-delete session:*, issue:*, sessions:index, events (only if user opts in)
    deleted = 0
    for pattern in ["session:*", "issue:*"]:
        keys = list(r.scan_iter(match=pattern, count=500))
        if keys:
            deleted += r.delete(*keys)
    r.delete("sessions:index", "events")
    print(f"wiped {deleted} keys + index/events stream")


def gen_id(prefix: str = "ses_demo") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def main():
    wipe_existing()

    now = datetime.now(timezone.utc)
    seeds = [
        # 1. Completed PR with realistic ACU and timestamps
        RemediationSession(
            session_id=gen_id(),
            issue_number=1042,
            cve_id="CVE-2024-34064",
            package="jinja2",
            severity="high",
            title="Bump jinja2 from 3.1.3 to 3.1.4 (CVE-2024-34064 — XSS via xmlattr filter)",
            status=SessionStatus.COMPLETED,
            outcome=SessionOutcome.PR_OPENED,
            pr_url="https://github.com/gacerioni/superset/pull/4201",
            acu_used=1.18,
            started_at=iso(now - timedelta(minutes=42)),
            ended_at=iso(now - timedelta(minutes=24)),
            devin_session_url="https://app.devin.ai/sessions/0d330d1b209149448d86d0d83a91f471",
            notes="Bumped to 3.1.4, no breaking changes affected our codebase. Tests passed.",
            owner="gabriel.cerioni",
            pollable=False,
        ),
        # 2. Completed PR that required a breaking-change fix (the "money shot")
        RemediationSession(
            session_id=gen_id(),
            issue_number=1043,
            cve_id="CVE-2024-XYZ-SQLA",
            package="sqlalchemy",
            severity="critical",
            title="Upgrade sqlalchemy 1.4→2.0 (CVE-2024-XYZ-SQLA — SQL injection in Engine.execute)",
            status=SessionStatus.COMPLETED,
            outcome=SessionOutcome.PR_OPENED,
            pr_url="https://github.com/gacerioni/superset/pull/4202",
            acu_used=2.74,
            started_at=iso(now - timedelta(minutes=58)),
            ended_at=iso(now - timedelta(minutes=18)),
            devin_session_url="https://app.devin.ai/sessions/92bda5d79783457ca9646af7efff22c4",
            notes="MAJOR upgrade. Devin migrated 14 call sites from `Engine.execute()` to `Connection.execute()`, "
                  "rewrote 3 test fixtures, regenerated 1 migration. All targeted tests green.",
            owner="gabriel.cerioni",
            pollable=False,
        ),
        # 3. Completed: Devin's judgment call — NOT applicable
        RemediationSession(
            session_id=gen_id(),
            issue_number=1044,
            cve_id="CVE-2024-35195",
            package="requests",
            severity="medium",
            title="Address requests CVE-2024-35195 (Session verify bypass)",
            status=SessionStatus.COMPLETED,
            outcome=SessionOutcome.NOT_APPLICABLE,
            pr_url=None,
            acu_used=0.42,
            started_at=iso(now - timedelta(minutes=31)),
            ended_at=iso(now - timedelta(minutes=24)),
            devin_session_url="https://app.devin.ai/sessions/c12fa771ee8c4ce58b9bd0fc7eaa3b21",
            notes="Vulnerability requires Session.verify to be set to False at construction. "
                  "Superset never instantiates requests.Session with verify=False; codepath unreachable. "
                  "Filed a comment on the issue explaining and closed.",
            owner="gabriel.cerioni",
            pollable=False,
        ),
        # 4. Running right now
        RemediationSession(
            session_id=gen_id(),
            issue_number=1045,
            cve_id="CVE-2024-49767",
            package="werkzeug",
            severity="high",
            title="Bump werkzeug for CVE-2024-49767 (resource exhaustion via multipart)",
            status=SessionStatus.RUNNING,
            outcome=SessionOutcome.UNKNOWN,
            pr_url=None,
            acu_used=0.65,
            started_at=iso(now - timedelta(minutes=8)),
            ended_at=None,
            devin_session_url="https://app.devin.ai/sessions/ab92ce11f1804a73a6c9c44a87d11fa1",
            notes="In progress: Devin is reading the werkzeug changelog and identifying affected handlers.",
            owner="gabriel.cerioni",
            pollable=False,
        ),
        # 5. Queued, just created
        RemediationSession(
            session_id=gen_id(),
            issue_number=1046,
            cve_id="CVE-2024-26130",
            package="cryptography",
            severity="medium",
            title="Upgrade cryptography 41.0.7 → 42.0.4 (CVE-2024-26130)",
            status=SessionStatus.QUEUED,
            outcome=SessionOutcome.UNKNOWN,
            pr_url=None,
            acu_used=0.0,
            started_at=iso(now - timedelta(seconds=22)),
            ended_at=None,
            devin_session_url=None,
            notes="Awaiting pickup by the orchestrator's poller.",
            owner="gabriel.cerioni",
            pollable=False,
        ),
        # 6. A previous failure — honest is good for the pitch
        RemediationSession(
            session_id=gen_id(),
            issue_number=1041,
            cve_id="CVE-2023-OLDPILLOW",
            package="pillow",
            severity="low",
            title="Bump pillow for CVE-2023-OLDPILLOW (heap overflow in BMP decoder)",
            status=SessionStatus.FAILED,
            outcome=SessionOutcome.FAILED,
            pr_url=None,
            acu_used=2.95,
            started_at=iso(now - timedelta(hours=4, minutes=12)),
            ended_at=iso(now - timedelta(hours=3, minutes=40)),
            devin_session_url="https://app.devin.ai/sessions/77ea2240b86c4e4ea3da4a3a98ad8a92",
            notes="Hit max_acu_limit. Upgrade path broke 6 image-handling tests; Devin's fix attempts "
                  "couldn't reconcile transparency mode differences. Flagged for human review.",
            owner="gabriel.cerioni",
            pollable=False,
        ),
    ]

    for s in seeds:
        save_session(s)
        emit_event(SessionEvent(
            session_id=s.session_id,
            event=f"seeded:{s.status.value}",
            payload={"cve": s.cve_id, "package": s.package, "severity": s.severity},
        ))
        time.sleep(0.05)  # spread the stream timestamps

    print(f"seeded {len(seeds)} sessions")
    print("dashboard: http://localhost:8501")


if __name__ == "__main__":
    main()
