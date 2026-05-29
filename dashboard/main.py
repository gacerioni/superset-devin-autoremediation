"""FastAPI-based dashboard for the Devin auto-remediation orchestrator.

Server-side rendered Jinja templates, Tailwind via CDN, HTMX for live updates.
Read-only: triggering happens through the orchestrator API, not here.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8080")
APP_ENV = os.environ.get("APP_ENV", "dev")
DEVIN_ORG_ID = os.environ.get("DEVIN_ORG_ID", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "gacerioni/superset")
REFRESH_SECONDS = 5

BASE = Path(__file__).parent
app = FastAPI(title="Devin Auto-Remediation Dashboard")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


# ---------- data layer ----------
async def fetch_orchestrator(path: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{ORCHESTRATOR_URL}{path}")
            r.raise_for_status()
            return r.json()
    except Exception:  # noqa: BLE001
        return {}


def severity_class(sev: str) -> str:
    return {
        "critical": "text-red-300 bg-red-500/15 border-red-500/40",
        "high":     "text-orange-300 bg-orange-500/15 border-orange-500/40",
        "medium":   "text-amber-300 bg-amber-500/15 border-amber-500/40",
        "low":      "text-emerald-300 bg-emerald-500/15 border-emerald-500/40",
    }.get((sev or "").lower(), "text-slate-300 bg-slate-500/15 border-slate-500/40")


def severity_label(sev: str) -> str:
    return {
        "critical": "CRIT",
        "high": "HIGH",
        "medium": "MED",
        "low": "LOW",
    }.get((sev or "").lower(), "—")


def status_class(status: str) -> str:
    return {
        "queued":    "text-slate-300 bg-slate-500/15 border-slate-500/40",
        "running":   "text-sky-300 bg-sky-500/15 border-sky-500/40",
        "completed": "text-emerald-300 bg-emerald-500/15 border-emerald-500/40",
        "failed":    "text-red-300 bg-red-500/15 border-red-500/40",
        "blocked":   "text-amber-300 bg-amber-500/15 border-amber-500/40",
        "cancelled": "text-slate-400 bg-slate-700/15 border-slate-600/40",
    }.get((status or "").lower(), "text-slate-300 bg-slate-500/15 border-slate-500/40")


def outcome_class(outcome: str) -> str:
    return {
        "pr_opened":      "text-emerald-300",
        "not_applicable": "text-sky-300",
        "failed":         "text-red-300",
    }.get((outcome or "").lower(), "text-slate-400")


def pr_short(pr_url: str | None) -> str:
    if not pr_url:
        return ""
    return "#" + pr_url.rstrip("/").rsplit("/", 1)[-1]


def short_id(sid: str | None, length: int = 12) -> str:
    if not sid:
        return ""
    return sid if len(sid) <= length else sid[:length] + "…"


def format_started(value: str | None) -> str:
    if not value:
        return ""
    return value[:19].replace("T", " ")


# Make helpers available to templates.
templates.env.globals.update(
    severity_class=severity_class,
    severity_label=severity_label,
    status_class=status_class,
    outcome_class=outcome_class,
    pr_short=pr_short,
    short_id=short_id,
    format_started=format_started,
)


# ---------- context builder ----------
async def build_context(request: Request) -> dict:
    sessions_payload = await fetch_orchestrator("/api/sessions?limit=200")
    events_payload = await fetch_orchestrator("/api/events?limit=50")
    sessions = sessions_payload.get("sessions", []) or []
    events = events_payload.get("events", []) or []

    total = len(sessions)
    by_status: dict[str, int] = {}
    pr_count = 0
    acu_total = 0.0
    for s in sessions:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
        if s.get("pr_url"):
            pr_count += 1
        acu_total += float(s.get("acu_used") or 0)

    completed = by_status.get("completed", 0)
    running = by_status.get("running", 0) + by_status.get("queued", 0)
    success_rate = int(round((completed / total * 100))) if total else 0
    eng_hours = pr_count * 2
    acu_spent = acu_total * 2.25
    cost_saved = int(round((pr_count * 200) - acu_spent))

    return {
        "request": request,
        "sessions": sessions,
        "events": events,
        "total": total,
        "running": running,
        "completed": completed,
        "pr_count": pr_count,
        "success_rate": success_rate,
        "eng_hours": eng_hours,
        "acu_total": acu_total,
        "acu_spent": acu_spent,
        "cost_saved": cost_saved,
        "app_env": APP_ENV,
        "orchestrator_url": ORCHESTRATOR_URL,
        "github_repo": GITHUB_REPO,
        "devin_org_id_short": (DEVIN_ORG_ID[:14] + "…") if DEVIN_ORG_ID and len(DEVIN_ORG_ID) > 14 else DEVIN_ORG_ID,
        "refresh_seconds": REFRESH_SECONDS,
        "now_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------- routes ----------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ctx = await build_context(request)
    return templates.TemplateResponse("index.html", ctx)


@app.get("/_partials/data", response_class=HTMLResponse)
async def partial_data(request: Request):
    ctx = await build_context(request)
    return templates.TemplateResponse("partials/data.html", ctx)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ---------- thin proxies to the orchestrator (used by control-panel buttons) ----------
@app.post("/proxy/trigger/scan")
async def proxy_trigger_scan(dispatch: bool = True, max_issues: int = 1):
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{ORCHESTRATOR_URL}/trigger/scan",
            params={"dispatch": str(dispatch).lower(), "max_issues": max_issues},
        )
        return r.json()


@app.post("/proxy/poll/now")
async def proxy_poll_now():
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{ORCHESTRATOR_URL}/poll/now")
        return r.json()


@app.post("/proxy/trigger/smoke")
async def proxy_trigger_smoke():
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{ORCHESTRATOR_URL}/trigger/smoke")
        return r.json()


@app.delete("/proxy/sessions/{session_id}")
async def proxy_cancel_session(session_id: str):
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.delete(f"{ORCHESTRATOR_URL}/api/sessions/{session_id}")
        return r.json()
