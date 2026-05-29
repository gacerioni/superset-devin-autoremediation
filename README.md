# Superset Auto-Remediation · A Devin API Demo

> Event-driven dependency-CVE remediation pipeline for [Apache Superset](https://github.com/apache/superset).
>
> Scanners find facts. **Devin makes judgments and ships the PRs.**

Built as a Forward-Deployed-Engineer style engagement demonstration on top of the [Devin API](https://docs.devin.ai/api-reference/overview). A scheduled scan (or a click, or a GitHub webhook) finds CVEs in Superset's pinned packages, files structured GitHub issues, and dispatches Devin sessions that read the changelog, fix breaking changes, and open PRs against a fork — all observable from a live dashboard.

## Why this exists

Renovate and Dependabot raise the version-bump PR. Your engineers still own fixing whatever the bump breaks. That gap is where security debt accumulates.

This pipeline closes it: **`pip-audit` finds the CVE, Devin does the engineering, your team reviews and merges.**

The artifact you can inspect right now:

- **Real PR opened by Devin** in the Superset fork:
  [`gacerioni/superset#2`](https://github.com/gacerioni/superset/pull/2)
  *(fixes CVE-2026-27205 in Flask; auto-discovered cascading bumps in `flask-babel` and `flask-sqlalchemy`; targeted tests passing)*
- **GitHub issue auto-managed by the orchestrator** (comment posted with PR link + cost, issue closed):
  [`gacerioni/superset#1`](https://github.com/gacerioni/superset/issues/1)
- **Live dashboard** showing session status, ACU spend, and engineering-hours saved.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       THREE ENTRY POINTS                         │
│                                                                  │
│   ┌──────────┐    ┌──────────────┐    ┌────────────────────┐    │
│   │  Manual  │    │   Webhook    │    │  Scheduled scan    │    │
│   │  button  │    │ (GitHub)     │    │  (cron sidecar)    │    │
│   └────┬─────┘    └──────┬───────┘    └─────────┬──────────┘    │
└────────┼─────────────────┼───────────────────────┼──────────────┘
         └─────────────────┼───────────────────────┘
                           ▼
            ┌──────────────────────────────────┐
            │   ORCHESTRATOR  (FastAPI)        │
            │   - runs pip-audit on /workspace │
            │   - dedupes against open issues  │
            │   - files GitHub issues          │
            │   - dispatches Devin sessions    │
            │   - polls Devin for state        │
            │   - comments PR back on issues   │
            └──────┬─────────────────┬─────────┘
                   │                 │
                   ▼                 ▼
          ┌──────────────┐  ┌─────────────────────┐
          │ Redis Cloud  │  │  Devin API (v3)     │
          │ - sessions   │  │  - create session   │
          │ - events     │  │  - poll status      │
          │ - TimeSeries │  │  - return PR + ACU  │
          └──────┬───────┘  └──────────┬──────────┘
                 │                     │
                 ▼                     ▼
          ┌──────────────┐    ┌──────────────────┐
          │  Dashboard   │    │  GitHub PR + auto│
          │  (FastAPI +  │    │  comment on the  │
          │   Jinja +    │    │  source issue    │
          │   HTMX +     │    └──────────────────┘
          │   Tailwind)  │
          └──────────────┘
```

## Architectural decisions worth flagging

These are documented in detail in [`PLAN.md`](PLAN.md), but the highlights:

1. **Scanner outside Devin.** `pip-audit` runs in 2 seconds and costs nothing. Devin's value is in the *fix*, not the *find* — putting Devin on scanning would be lighting ACU on fire to do work a script already does.
2. **Hard ACU cap per session** (`max_acu_limit=3`, ~$6.75 worst case). Runaway sessions are bounded before they start.
3. **Structured output required.** Every Devin session must return JSON matching a schema we define — no parsing chat logs to figure out outcomes.
4. **Two channels, one source of truth.** The GitHub issue is the human audit trail (security team, compliance, queue review). The Devin API receives the prompt directly — same data, agent-formatted, no redundant scraping.
5. **Polling, not webhooks.** Devin's API doesn't push state changes. A 30-second background poller hydrates Redis from `GET /sessions/{id}` and emits events.
6. **Idempotent dedupe.** Scanner skips CVEs that already have an open `devin-remediate` issue. Safe to re-trigger.

## Tech stack

- **Orchestrator**: FastAPI 0.115 · httpx (async) · Pydantic v2 · structlog (JSON) · PyGithub · pip-audit
- **Storage**: Redis Cloud (sessions JSON, event stream, TimeSeries for throughput) — also runnable against a local Redis Stack container
- **Dashboard**: FastAPI + Jinja2 + Tailwind CSS (via CDN) + HTMX (live updates without writing JS framework code)
- **Container**: docker-compose with optional `local-redis` profile

## Quick start

### Prerequisites

- Docker + docker compose
- A **Devin API key** (`cog_...`) from https://app.devin.ai → Settings → API
- A **GitHub PAT** with `repo` scope
- A **Redis Cloud database URL** (free tier 30MB works), OR use the local Redis Stack profile

### Run it

```bash
git clone https://github.com/gacerioni/superset-devin-autoremediation
cd superset-devin-autoremediation

cp .env.example .env
# edit .env — fill in DEVIN_API_KEY, DEVIN_ORG_ID, GITHUB_TOKEN, GITHUB_REPO, REDIS_URL

# Bring up the stack (Redis Cloud by default)
docker compose up --build

# Or with a local Redis Stack instead
make up-local-redis
```

Open:

- **Dashboard** → http://localhost:8501
- **Orchestrator API docs** → http://localhost:8080/docs

### Clone the target repo (one-time setup)

The orchestrator runs `pip-audit` against a local copy of the Superset fork. Clone it shallow into `./workspace/`:

```bash
mkdir -p workspace && cd workspace
git clone --depth 1 https://github.com/gacerioni/superset.git
```

(Substitute your own fork if you're adapting this to another repo.)

### Trigger the pipeline

From the dashboard, click **"Run security scan"** (Control Panel section).

Or via curl:

```bash
curl -X POST http://localhost:8080/trigger/scan?dispatch=true&max_issues=1
```

What you'll see:

1. New issue appears in `gacerioni/superset` (or your `GITHUB_REPO`)
2. New session appears in the dashboard, status `queued` → `running`
3. Open the **Devin** column link → watch Devin live at `app.devin.ai`
4. 8–14 minutes later: PR appears in the fork, issue auto-commented + closed, dashboard tile updates

## Demo flow (for the evaluator)

If you're reproducing the demo end-to-end:

```bash
# 1. Stack up
docker compose up --build -d

# 2. Reset to clean demo state (1 completed session visible, fork issue closed)
bash scripts/reset_for_demo.sh

# 3. Open dashboard
open http://localhost:8501

# 4. Click "Run security scan" — pipeline runs live
```

The reset script is **idempotent** — safe to re-run between takes.

## Configuration

`.env.example` documents every variable. The ones you'll actually need to fill in:

| Variable | What | Where to get |
|---|---|---|
| `DEVIN_API_KEY` | Your Devin API key (prefix `cog_`) | app.devin.ai → Settings → API |
| `DEVIN_ORG_ID` | Your Devin organization id (prefix `org-`) | app.devin.ai → Settings → Organization |
| `DEVIN_MODE` | `real` for production, `mock` for local dev with no key | — |
| `DEVIN_MAX_ACU_PER_SESSION` | Hard cap per session (default `3`) | — |
| `GITHUB_TOKEN` | PAT with `repo` scope | github.com → Settings → Developer settings |
| `GITHUB_REPO` | Target repo for issues + PRs (e.g. `gacerioni/superset`) | — |
| `REDIS_URL` | Connection string (TLS recommended: `rediss://...`) | Redis Cloud console (or `redis://redis:6379/0` for local) |

## Project layout

```
.
├── orchestrator/              # FastAPI service — the brain
│   ├── app/
│   │   ├── main.py            # routes: /trigger/scan, /webhook/github, /trigger/smoke
│   │   ├── orchestrator.py    # CVE → Devin session dispatch
│   │   ├── scanner.py         # pip-audit runner + GitHub issue filer
│   │   ├── devin_client.py    # async httpx client for api.devin.ai/v3
│   │   ├── poller.py          # background task: poll Devin → hydrate Redis
│   │   ├── mock_devin.py      # in-process mock API (for dev w/o a key)
│   │   ├── prompts.py         # the prompt template Devin receives per CVE
│   │   ├── storage.py         # Redis layer (sessions, events, TimeSeries)
│   │   ├── models.py          # Pydantic models
│   │   ├── config.py          # settings loader
│   │   └── logging_config.py  # structured JSON logs
│   ├── Dockerfile
│   └── requirements.txt
│
├── dashboard/                 # Visual centerpiece
│   ├── main.py                # FastAPI server, HTMX partials
│   ├── templates/             # Jinja templates (base, index, partials)
│   ├── static/                # Devin logo, real brand SVGs, Gabriel's GitHub avatar
│   ├── Dockerfile
│   └── requirements.txt
│
├── scripts/
│   ├── reset_for_demo.sh      # idempotent reset between Loom takes
│   └── seed_demo_sessions.py  # pre-populate Redis with realistic sessions (dev only)
│
├── workspace/                 # gitignored — the cloned Superset fork (~440MB)
│   └── superset/
│
├── docker-compose.yml         # local-redis profile + orchestrator + dashboard
├── Makefile                   # make up / make trigger / make reset
├── PLAN.md                    # full design doc + architectural decisions
├── .env.example
└── README.md
```

## API surface (the parts that matter)

Orchestrator endpoints (full schema at `/docs`):

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/trigger/scan` | Run pip-audit, file issues, dispatch Devin (one-stop) |
| `POST` | `/trigger/dispatch` | Manually dispatch Devin for a specific CVE id |
| `POST` | `/trigger/smoke` | Tiny Devin session to validate API connectivity (~$0) |
| `POST` | `/webhook/github` | GitHub webhook receiver for `issues:opened` events |
| `POST` | `/poll/now` | Force an immediate Devin poll (useful during demos) |
| `GET` | `/api/sessions` | List sessions (powers the dashboard) |
| `GET` | `/api/events` | Tail recent state-transition events |
| `DELETE` | `/api/sessions/{id}` | Cancel a running Devin session |

## What's intentionally NOT in scope

These were considered and deliberately deferred — see `PLAN.md` for the reasoning:

- **PR auto-merge**: deliberately no — humans merge. Devin Review API closes the review loop, but the human is the final gate.
- **Devin reading the GitHub issue**: redundant — the orchestrator hands the same data via the API in agent-formatted form.
- **Letting Devin run the scanner**: pip-audit costs nothing, takes 2 seconds, no value-add from putting it inside a session.
- **Multi-repo fan-out**: a real engagement would shard this across the customer's portfolio. Out of scope for a 5-day demo.

## Extending this

Same orchestrator, swap the prompt — applies to:

- **Strategic migrations** (SQLAlchemy 1→2, Pydantic 1→2, framework cutovers)
- **Quality uplift** (mypy strict-mode rollout, flaky test triage)
- **Compliance refactors** (license header sweeps, code style migrations)
- **Feature acceleration** (bounded features under ~3 engineer-hours each)

The `prompts.py` module is where each playbook lives.

## Built by

[Gabriel Cerioni](https://www.linkedin.com/in/gabriel-cerioni/) · Forward Deployed Engineer · Redis
([gacerioni on GitHub](https://github.com/gacerioni))

## License

MIT
