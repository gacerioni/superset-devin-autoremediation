#!/usr/bin/env bash
# reset_for_demo.sh — restore the demo to its "pre-recording" state.
#
# What this gives you after running:
#   - Redis has EXACTLY 1 session row: the Flask CVE-2026-27205 (completed, PR #2 linked)
#   - All seed/smoke/placeholder sessions wiped
#   - Issue #1 closed with the orchestrator's auto-comment
#   - Any extra remediation issues (from previous demo runs) closed
#   - PR #2 stays open (don't touch it — it's the artifact)
#
# Run from the repo root:  bash scripts/reset_for_demo.sh
# Idempotent — safe to run multiple times.

set -e

cd "$(dirname "$0")/.."

echo "▶ 1/5  Wiping Redis state (all sessions, events, indexes)..."
docker compose exec -T orchestrator python -c "
from app.storage import get_redis
r = get_redis()
deleted = 0
for pattern in ['session:*', 'issue:*', 'counter:*']:
    keys = list(r.scan_iter(match=pattern, count=500))
    if keys:
        deleted += r.delete(*keys)
for key in ['sessions:index', 'events']:
    if r.exists(key):
        r.delete(key)
        deleted += 1
print(f'   wiped {deleted} keys')
"

echo "▶ 2/5  Re-importing the Flask session from Devin (real state, no extra ACU)..."
docker compose exec -T orchestrator python -c "
import asyncio
from app.devin_client import (
    get_devin_client, derive_terminal_from_output, extract_acu,
    extract_devin_url, extract_outcome, extract_pr_url, map_devin_status,
)
from app.models import RemediationSession, SessionEvent, SessionOutcome, SessionStatus
from app.storage import save_session, emit_event, record_session_started, record_session_completed, record_acu_spent

async def main():
    client = get_devin_client()
    sid = '90cb2c05dbed47e3b4ff9acb0424b103'  # the Flask session
    remote = await client.get_session(sid)

    raw_status = map_devin_status(remote.get('status'))
    terminal_override = derive_terminal_from_output(remote)
    final_status = terminal_override or raw_status

    pr_url = extract_pr_url(remote)
    outcome_str = extract_outcome(remote, final_status)
    acu = extract_acu(remote)
    notes = (remote.get('structured_output') or {}).get('notes', '')[:1000]

    s = RemediationSession(
        session_id=sid,
        issue_number=1,
        cve_id='CVE-2026-27205',
        package='flask',
        severity='high',
        title='Fix CVE-2026-27205 in flask',
        status=SessionStatus(final_status),
        outcome=SessionOutcome(outcome_str),
        pr_url=pr_url,
        acu_used=acu,
        devin_session_url=extract_devin_url(remote) or f'https://app.devin.ai/sessions/{sid}',
        notes=notes,
        owner='gabriel.cerioni',
        pollable=False,
        extra={'kind': 'cve', 'issue_commented': True},
        ended_at='2026-05-27T15:39:56+00:00',
    )
    save_session(s)
    emit_event(SessionEvent(session_id=sid, event='demo_imported', payload={'pr': pr_url}))
    record_session_started()
    record_session_completed(success=True)
    if acu > 0:
        record_acu_spent(acu)
    print(f'   imported: status={s.status.value} outcome={s.outcome.value} pr={s.pr_url}')

asyncio.run(main())
"

echo "▶ 3/5  Closing any extra open remediation issues (keeping #1 closed)..."
# Close everything labeled devin-remediate except #1 (which should already be closed)
gh api '/repos/gacerioni/superset/issues?labels=devin-remediate&state=open&per_page=20' \
  | python3 -c "
import json, subprocess, sys
issues = json.load(sys.stdin)
for issue in issues:
    if issue['number'] == 1:
        continue
    print(f'   closing extra issue #{issue[\"number\"]}: {issue[\"title\"][:60]}')
    subprocess.run(['gh', 'issue', 'close', str(issue['number']),
                    '--repo', 'gacerioni/superset',
                    '--reason', 'not planned',
                    '--comment', 'Closing pre-demo. PR #2 covers the canonical Flask fix.'],
                   check=False)
"

echo "▶ 4/5  Ensuring issue #1 is closed (it should already be)..."
state=$(gh issue view 1 --repo gacerioni/superset --json state -q .state)
if [ "$state" != "CLOSED" ]; then
  echo "   issue #1 was OPEN — closing..."
  gh issue close 1 --repo gacerioni/superset --reason completed
else
  echo "   ✓ issue #1 already closed"
fi

echo "▶ 5/5  Sanity check — what the dashboard will show:"
curl -s "http://localhost:8080/api/sessions?limit=10" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(f'   total sessions in dashboard: {data[\"total\"]}')
for s in data['sessions']:
    print(f'      • {s[\"cve_id\"] or \"(no cve)\":24s} status={s[\"status\"]:10s} pr={s.get(\"pr_url\") or \"-\"}')
"

echo
echo "✅ Demo reset complete."
echo "   Dashboard:  http://localhost:8501"
echo "   PR #2:      https://github.com/gacerioni/superset/pull/2"
echo "   Issue #1:   https://github.com/gacerioni/superset/issues/1 (CLOSED — money-shot evidence)"
