# Devin Take-Home — Full Plan

**Author**: Gabriel Cerioni
**Deadline**: Monday 2026-06-01 (Loom + repos delivered)
**Audience for pitch**: VP of Engineering + senior ICs evaluating Devin
**Working directory**: `/Users/gabriel.cerioni/DEVIN_COGNITION`

---

## 0. TL;DR

Build an event-driven automation that **scans `apache/superset` for dependency CVEs → opens GitHub issues → triggers Devin to fix each one → ships a PR → reports outcomes to a live dashboard**. The orchestrator is thin (FastAPI, ~300 LOC). Devin is the engineering primitive doing the actual work. Redis backs the state/eventing layer (authentic to your day job and a stealth secondary pitch). Everything runs via `docker compose up`.

**The Loom story**: "Renovate bumps versions; Devin fixes the breakage. Here's a self-healing pipeline for security debt."

---

## 1. What is Devin, really? (the knowledge primer)

### 1.1 The mental model

Devin is **a remote engineer with a Linux box**. Not an IDE plugin. Not autocomplete. When you start a session, Devin gets:

- A sandboxed cloud VM with shell, editor, and Chromium browser
- Network access (it can `pip install`, `npm i`, hit external docs, etc.)
- A persistent plan that it executes step-by-step, narrating in a chat-style log
- Optionally, a cloned git repo and a GitHub identity with which to push branches + open PRs

The unit of work is a **session**. You hand it a prompt with a clear goal and completion criteria; it plans, executes, runs tests, iterates, and either (a) reaches "done" and reports back, (b) asks a clarifying question and blocks, or (c) hits something it can't solve and reports failure.

**Operational heuristic Cognition publishes**: *"If a competent engineer could do it in three hours, Devin can probably do it."* That's the ceiling. Multi-week feature work is out of scope; bounded, well-specified tasks are in scope.

### 1.2 ACUs (Agent Compute Units)

- **1 ACU ≈ 15 minutes of active autonomous work** (VM time + model inference + bandwidth)
- Billed per session, only while the agent is actively running
- Core plan: $2.25/ACU; Team plan: $2.00/ACU; both have an `max_acu_limit` you can pass per session to cap blast radius
- For our use case, a dep-bump session is typically **0.5–2 ACU**. A breaking-change fix could be 3–5 ACU.

**Always set `max_acu_limit`** in your session creation calls. Otherwise a session can run away if Devin gets stuck on a flaky test.

### 1.3 Knowledge, Playbooks, and Notes

These three primitives are how you avoid pasting the same context into every prompt:

| Primitive | What it is | When to use |
|---|---|---|
| **Knowledge** | Persistent facts about a codebase (architecture, conventions, gotchas). Auto-applied based on session context. | "Superset uses Celery for async tasks; do not introduce sync calls in `tasks/`". |
| **Playbook** | Reusable, parameterized prompt template — a "recipe" Devin can be invoked with. | "Upgrade-a-Python-dep" playbook: takes a CVE id + package + target version, produces a PR. |
| **Notes** | Org-level wiki entries you want Devin to read. | "Our release process for security PRs is X." |

**For the demo, we should create one playbook** ("remediate-dependency-cve") and 2–3 knowledge entries about Superset. This makes prompts terser and shows you understand Devin as a platform, not just an API.

### 1.4 Integrations & permissions

- **GitHub**: install the Devin GitHub App on your fork; Devin opens PRs as itself. Repo must be **indexed** via the Repositories API before Devin can work on it efficiently (this takes a few minutes on first run).
- **Slack/Linear/Jira**: Devin can be triggered from these and post status back. Out of scope for our build.
- **Secrets API**: store API keys, npm tokens, etc. that Devin needs inside a session. Never put secrets in prompts.

### 1.5 What Devin is *bad* at (so we don't pitch into it)

- **Greenfield ambiguity**: "build us a notification service" — too open-ended
- **Tasks needing real-time human collaboration mid-stream** (vs. async review)
- **Anything requiring physical infra access** Devin doesn't have credentials for
- **Long multi-week features** — sessions are time-bounded
- **Tests that require flaky human setup** — Devin times out

Our use case (dep CVE → PR) sits in Devin's sweet spot: bounded, well-specified, has a clear "done" signal (PR opened + CI green).

---

## 2. Devin vs Windsurf vs Claude Code (clarification)

| Tool | What it is | Where it runs | When you use it |
|---|---|---|---|
| **Devin** | Autonomous agent | Cloud VM, async | You hand off a task and walk away |
| **Windsurf** | AI-native IDE (ex-Codeium, Cognition acquired Jul 2025) | Your laptop | You code with AI-assist in real-time |
| **Claude Code** (this tool) | CLI coding agent | Your laptop | Interactive pair programming in your shell |

For this challenge: **only Devin matters**. Windsurf is not used. Claude Code (me) is just your build assistant — I'll write the orchestrator code with you.

---

## 3. The Devin API surface (what we'll actually call)

**Base URL**: `https://api.devin.ai/v3`
**Auth**: `Authorization: Bearer cog_xxxxx`

| Endpoint | Method | Use |
|---|---|---|
| `/organizations/{org_id}/sessions` | POST | Start a Devin session |
| `/organizations/{org_id}/sessions` | GET | List sessions (filter by tags) |
| `/organizations/{org_id}/sessions/{session_id}` | GET | Poll status |
| `/organizations/{org_id}/sessions/{session_id}/messages` | POST | Send a follow-up message |
| `/organizations/{org_id}/sessions/{session_id}/insights` | GET | Get structured session insights |
| `/organizations/{org_id}/sessions/{session_id}` | DELETE | Terminate a runaway session |
| `/organizations/{org_id}/playbooks` | POST | Create our reusable playbook |
| `/organizations/{org_id}/knowledge-notes` | POST | Create Superset knowledge entries |
| `/organizations/{org_id}/repositories/index` | PUT | Index the Superset fork |
| `/organizations/{org_id}/metrics/sessions` | GET | Native session metrics (we'll augment in our dashboard) |
| `/organizations/{org_id}/metrics/prs` | GET | Native PR metrics |
| `/organizations/{org_id}/consumption/daily` | GET | ACU usage |

**Key payload fields for session creation** (the ones we'll actually use):

```python
{
  "prompt": "...",                    # required, the task
  "title": "Fix CVE-2024-XXXX in package Y",  # for dashboard readability
  "tags": ["cve-remediation", "issue-42"],     # for filtering
  "max_acu_limit": 3,                 # hard cap
  "playbook_id": "pb_xxx",            # our reusable recipe
  "knowledge_ids": ["kn_xxx"],        # Superset-specific context
  "repos": [{"url": "https://github.com/gabsi-redis/superset", "branch": "main"}],
  "structured_output_required": true, # forces Devin to return JSON
  "structured_output_schema": { ... } # we define {pr_url, status, notes}
}
```

**`structured_output_required` is the killer feature** for our orchestrator — it forces Devin to return machine-readable output we can store and display, vs. having to parse a chat log.

**Important quirk**: Devin does **not** expose outbound webhooks for session status changes. The orchestrator must **poll** `GET /sessions/{id}` every ~30s. This is fine; we model it as a background worker.

---

## 4. Use case & VP pitch narrative

### 4.1 The problem framing (for the Loom intro)

> Apache Superset has 80+ Python deps and 200+ npm deps. `pip-audit` flags 6–12 CVEs at any given time. Each one is a 1–4 hour engineering task: read the changelog, bump the version, fix breaking calls, update tests, run CI, get review. Multiply across 50 repos and you have a permanent backlog.
>
> Dependabot/Renovate raise the PR. **Humans still have to fix the breakage.** That's where the queue stalls.

### 4.2 What we're building

A self-healing security pipeline:

1. **Scanner** runs `pip-audit` on the Superset fork on a schedule (cron)
2. For each new CVE → **create a GitHub issue** with structured CVE data, label `devin-remediate`
3. **Webhook** fires on issue-created → orchestrator wakes up
4. Orchestrator **creates a Devin session** with the CVE context + playbook
5. Devin clones, upgrades, fixes breakage, runs tests, **opens a PR**
6. Orchestrator **polls** until terminal state, **comments on the issue** with the PR link, updates dashboard
7. **Dashboard** shows live status, success rate, ACU burn, throughput

### 4.3 The "why Devin" beat (the moment that sells)

Find one CVE where the upgrade requires code changes — e.g., a Flask or SQLAlchemy bump that changed an API. In the Loom, show the PR diff side-by-side with Devin's session log: *"Devin read the changelog, found the renamed function, updated 4 call sites, updated 2 tests, re-ran the suite."* **Dependabot cannot do this. A junior engineer takes 2 hours.**

That single example > every architecture diagram in the deck.

### 4.4 ROI math (memorize this for the Loom)

- **CVE remediation cost (human)**: ~2 engineering-hours @ $100/hr loaded = $200/CVE
- **CVE remediation cost (Devin)**: ~2 ACU @ $2.25 = **$4.50/CVE**
- **Time-to-PR (human, backlogged)**: 5–15 business days
- **Time-to-PR (Devin)**: 15–45 minutes
- **Throughput**: orchestrator can drive 10+ sessions in parallel; one human runs one at a time

The pitch isn't "Devin replaces engineers." It's "Devin clears the queue of bounded, miserable work so engineers do bounded, interesting work."

---

## 5. Architecture

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  Scheduled scan  │     │  GitHub webhook  │     │   Manual "fire   │
│  (pip-audit cron)│     │  (issue:created) │     │   now" button    │
└────────┬─────────┘     └────────┬─────────┘     └────────┬─────────┘
         │ creates issues          │                        │
         ▼                         ▼                        ▼
┌────────────────────────────────────────────────────────────────────┐
│                       Orchestrator (FastAPI)                       │
│                                                                    │
│  POST /webhook/github     POST /trigger      GET /api/sessions    │
│         │                       │                    ▲             │
│         └───────────┬───────────┘                    │             │
│                     ▼                                │             │
│       ┌──────────────────────────┐    ┌──────────────────────┐    │
│       │ DevinClient.create()     │───▶│  Background poller    │    │
│       │ → POST /v3/.../sessions  │    │  (every 30s)          │    │
│       └──────────────┬───────────┘    │  → GET /v3/.../{id}  │    │
│                      │                 │  → on terminal state:│    │
│                      ▼                 │     comment on issue │    │
│              ┌───────────────┐         │     update Redis     │    │
│              │     Redis     │◀────────┴──────────────────────┘    │
│              │  - sessions:* │                                     │
│              │  - events     │                                     │
│              │  - metrics    │                                     │
│              └───────┬───────┘                                     │
└──────────────────────┼─────────────────────────────────────────────┘
                       │
                       ▼
              ┌─────────────────┐
              │ Streamlit dash  │ ── live status, success rate, ACU burn
              └─────────────────┘
```

### 5.1 Components

| Component | Tech | LOC est. | What it does |
|---|---|---|---|
| Scanner | Python script invoking `pip-audit` | 80 | Cron-able; outputs JSON; creates GH issues with `devin-remediate` label |
| Orchestrator | FastAPI + httpx | 250 | Webhook receiver, Devin session creator, background poller |
| Redis layer | redis-py | 60 | JSON for session state, Streams for events, TimeSeries for throughput |
| Dashboard | Streamlit | 150 | Live session table, success/failure tiles, ACU/$ spend, throughput chart |
| Docker | docker-compose | n/a | One-command spin-up |
| Mock Devin | FastAPI sub-app | 80 | For dev without an API key — returns fake sessions that complete after 30s |

Total target: **~600 LOC** including tests. Anything more and we're over-engineering.

### 5.2 Redis schema (showcases your domain)

- `session:{session_id}` → JSON: `{issue_id, cve_id, status, pr_url, started_at, ended_at, acu_used}`
- `events` (Stream) → every state transition appended; dashboard tails the last 50
- `metrics:sessions_started` (TimeSeries) — 1-min buckets
- `metrics:sessions_completed` (TimeSeries) — 1-min buckets
- `metrics:acu_spent` (TimeSeries) — 1-min buckets
- `issue:{issue_number}` → JSON: links back to session

You can demo this in the Loom by opening `redis-cli` mid-demo: "and because this is Redis-backed, the whole queue is observable in real-time at the data layer."

### 5.3 Prompt template (the Devin playbook body)

```
You are remediating a dependency CVE in the apache/superset codebase.

CVE: {cve_id}
Package: {package_name}
Affected versions: {affected_versions}
Fixed in: {fixed_version}
Severity: {severity}
Summary: {summary}

Your task:
1. Locate where {package_name} is pinned (requirements files, pyproject.toml, lockfiles).
2. Upgrade to {fixed_version} (or the minimum non-vulnerable patch version, your judgment).
3. Search the codebase for usage of {package_name}. Read the upstream changelog for breaking changes between current and target version.
4. Fix any breaking call sites. Update affected tests.
5. Run the relevant test suite for the modules you touched. Do not run the full Superset test suite — pick the targeted modules.
6. Open a PR titled "fix({package_name}): bump to {fixed_version} for {cve_id}" against the `main` branch of {repo_url}.
7. The PR body must include: CVE summary, breaking changes you encountered, list of files modified, test command you ran with results.

Completion criteria:
- PR is opened, OR
- You determine the CVE does not apply (e.g., package is a transitive dep we don't use the vulnerable code path of) — in which case, comment on the source issue with that finding and stop.

Return structured output: { "pr_url": str|null, "status": "pr_opened"|"not_applicable"|"failed", "notes": str }.

If you get stuck or need more than 3 ACU, stop and return status="failed" with notes explaining what blocked you.
```

This is the entire engineering instruction. Devin handles the rest. **That's the demo.**

---

## 6. Day-by-day plan (Wed 5/27 → Mon 6/1)

### Wednesday (today) — Setup + knowledge + skeleton
- [ ] Email Cognition contact for API key + ACU budget. **Do this first.**
- [ ] Fork `apache/superset` to your GH org (call it `superset` or `superset-devin-demo`)
- [ ] Install Devin GitHub App on the fork
- [ ] Set up working directory: scaffold orchestrator (FastAPI), Redis, Streamlit, docker-compose
- [ ] Build **mock Devin** sub-app so we can develop without keys
- [ ] Write the scanner: run `pip-audit` against Superset's `requirements/*.txt`, output JSON, dedupe
- [ ] **End-of-day goal**: `docker compose up` starts everything; you can `curl /trigger` and see a mock session run through the dashboard

### Thursday — Real Devin integration
- [ ] Verify API key works (`GET /organizations` smoke test)
- [ ] Index Superset fork via the Repositories API
- [ ] Create the playbook + 2 knowledge entries
- [ ] Wire `DevinClient` to real API
- [ ] **First real end-to-end**: 1 CVE issue → 1 Devin session → 1 PR
- [ ] Iterate the prompt until success rate is >50% on a sample of 3 CVEs
- [ ] **End-of-day goal**: 1 real PR opened by Devin in your fork

### Friday — Issues + bulk run + polish
- [ ] Curate 5 CVE issues — mix of trivial (pure version bump) and at least 1 with breaking changes
- [ ] Run the scanner; commit the resulting issues
- [ ] Trigger all 5; let them run; capture outcomes
- [ ] Polish dashboard: success/failure tiles, ACU burn, "Engineering hours saved" headline metric
- [ ] Polish README with clear run instructions

### Saturday — Buffer + Loom rehearsal
- [ ] Re-run any failed sessions with prompt tweaks
- [ ] Write the Loom script (Section 7 below)
- [ ] Do a dry-run of the Loom; time it
- [ ] Identify the one "Devin fixed breaking code" PR — make sure its session insights are visible

### Sunday — Final dress rehearsal
- [ ] Polish the README in both repos
- [ ] Make sure the demo can run cold on a fresh laptop in <5 min
- [ ] Final Loom recording attempts (2–3 takes)

### Monday — Deliver
- [ ] Final Loom recording
- [ ] Push tagged release on both repos
- [ ] Send submission to Cognition

**Buffer is real.** Friday is the deadline for "first 5 PRs produced." Saturday onwards is just polish and recording. If you slip on Thursday's integration, Friday absorbs it.

---

## 7. Loom video — 5 minute script

| Time | Beat | What to show |
|---|---|---|
| 0:00–0:30 | **Problem hook** | "Engineering teams sit on weeks of security debt. Renovate raises PRs, humans fix breakage. The queue stalls." Show a screenshot of a real CVE backlog. |
| 0:30–1:00 | **The pitch** | "What if the queue cleared itself? Today I'll show you Devin doing exactly that on apache/superset." |
| 1:00–2:00 | **Live demo** | (a) Click "Run scan" → 5 issues created live. (b) Webhook fires, dashboard goes from 0 → 5 queued sessions. (c) Open one Devin session URL — show it actually working. |
| 2:00–3:30 | **The "why Devin" moment** | Open a completed PR where Devin fixed breaking code. Show the changelog Devin read, the fixed call sites, the test changes. "Dependabot would have raised a broken PR. Devin shipped a working one." |
| 3:30–4:00 | **Architecture** | Diagram (Section 5). 30 seconds. Highlight: orchestrator is 300 LOC; Devin is the engineering primitive. Redis-backed so it's observable. |
| 4:00–4:30 | **Business impact** | Numbers: 5 CVEs cleared in X minutes vs Y engineering-hours. Cost: $Z in ACUs vs ~$1000 in eng time. Throughput chart from the dashboard. |
| 4:30–5:00 | **Next steps** | "In a real engagement: extend to deprecated API migrations, mypy strictness uplift, flaky test triage. Same architecture, different playbook. Devin scales horizontally — your engineers don't." |

**Tone**: confident, calm, technical. You're a peer briefing peers, not a vendor selling. Don't over-promise. Acknowledge: "Devin doesn't get everything right. Here's a session that failed and what we'd do about it." That honesty wins technical audiences.

---

## 8. Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| No API key by Thu morning | Med | Mock Devin sub-app keeps us building. Push contact again Wed if no reply. |
| All CVEs are trivial bumps, no "wow" moment | Med | Pre-curate. Cherry-pick a CVE on a package with known API churn (e.g., SQLAlchemy 1.x→2.x style). If not available organically, intentionally pin an older `cryptography` or `jinja2`. |
| Devin sessions time out / produce broken PRs | High | Set `max_acu_limit=3`. Re-run with refined prompts. Have at least 3 known-good PRs captured for the Loom. |
| Webhook plumbing eats hours | Med | Skip the webhook on demo day if needed. Use the "Run scan" button to drive the demo. |
| Loom over-runs 5 min | High | Practice. Cut the architecture section if needed — it's the most expendable. |
| Streamlit looks toy | Low | It's fine. VPs don't care if the dashboard is fancy; they care if it answers "is it working?" — which Streamlit does in 1/10th the time of a real frontend. |

---

## 9. Open decisions (need your call before we start building)

1. **Use case lock-in**: dependency CVE remediation? Or do you want to consider an alternative (deprecated API migration, mypy strict mode rollout, flaky test triage)? My strong rec: **stick with CVEs** — it's the most "VP-resonant" framing.
2. **GitHub org for the fork**: which org do you want to use? (Your personal account vs a `redis-fde-demo` or similar org.)
3. **Webhook delivery**: smee.io (zero infra, very common for demos) vs ngrok (you probably have it) vs skip the webhook and use the "trigger" button. I'd lean **smee.io** — it's free, no auth, and gives a permanent URL.
4. **Dashboard**: Streamlit (fastest) or something nicer (Next.js)? I'd lean **Streamlit** hard. Save the time for the Loom.
5. **Repo naming**:
   - Orchestrator: `superset-devin-autoremediation` or shorter?
   - Fork: `superset` (default name from forking) or rename?

---

## 10. Memorize for the pitch

- **One-line value prop**: "Devin clears the security-debt queue while your engineers ship features."
- **One-line architecture**: "300 lines of glue around the Devin API; Devin does the engineering."
- **One-line moat over Dependabot**: "Dependabot bumps versions. Devin fixes the breakage."
- **One-line cost story**: "$4.50 per CVE versus 2 engineer-hours."
- **One-line extensibility**: "Same orchestrator, swap the playbook — applies to migrations, refactors, test triage."

Keep these on a sticky note during the Loom.

---

## Sources used to build this plan

- Devin API overview & endpoint index: https://docs.devin.ai/api-reference/overview
- Devin docs landing page: https://docs.devin.ai/
- Pricing (Devin official): https://devin.ai/pricing/
- Pricing breakdown 2026: https://costbench.com/software/ai-coding-assistants/devin-ai/
- API usage examples (Python): https://docs.devin.ai/api-reference/v3/usage-examples
- Create Session endpoint spec: https://docs.devin.ai/api-reference/v3/sessions/post-organizations-sessions
- Cognition's Windsurf acquisition: https://cognition.ai/blog/windsurf
- Devin vs Windsurf (comparison): https://www.13labs.au/compare/devin-vs-windsurf
- Devin 101 — automatic PR reviews via API (very close to our use case): https://cognition.ai/blog/devin-101-automatic-pr-reviews-with-the-devin-api
