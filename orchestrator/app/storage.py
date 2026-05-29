"""Redis-backed state layer.

Keys:
- session:{session_id}        JSON document, current state of a session
- sessions:index              Sorted set of session_ids by started_at
- issue:{issue_number}        JSON: maps GH issue -> session_id
- events                      Stream, every state transition (capped)
- metrics:sessions_started    TimeSeries (Redis Stack)
- metrics:sessions_completed  TimeSeries
- metrics:sessions_failed     TimeSeries
- metrics:acu_spent           TimeSeries
"""
from __future__ import annotations

import json
import time
from typing import Iterable

import redis

from app.config import settings
from app.logging_config import get_logger
from app.models import RemediationSession, SessionEvent

log = get_logger(__name__)

_pool: redis.ConnectionPool | None = None


def get_redis() -> redis.Redis:
    global _pool
    if _pool is None:
        _pool = redis.ConnectionPool.from_url(settings.redis_url, decode_responses=True)
    return redis.Redis(connection_pool=_pool)


def session_key(session_id: str) -> str:
    return f"session:{session_id}"


def issue_key(issue_number: int) -> str:
    return f"issue:{issue_number}"


# -------------- Session CRUD --------------

def save_session(session: RemediationSession) -> None:
    r = get_redis()
    payload = session.model_dump_json()
    pipe = r.pipeline()
    pipe.set(session_key(session.session_id), payload)
    pipe.zadd("sessions:index", {session.session_id: time.time()})
    if session.issue_number is not None:
        pipe.set(issue_key(session.issue_number), session.session_id)
    pipe.execute()


def get_session(session_id: str) -> RemediationSession | None:
    raw = get_redis().get(session_key(session_id))
    if raw is None:
        return None
    return RemediationSession.model_validate_json(raw)


def get_session_by_issue(issue_number: int) -> RemediationSession | None:
    sid = get_redis().get(issue_key(issue_number))
    if not sid:
        return None
    return get_session(sid)


def list_sessions(limit: int = 100) -> list[RemediationSession]:
    r = get_redis()
    ids = r.zrevrange("sessions:index", 0, limit - 1)
    if not ids:
        return []
    raws = r.mget(*(session_key(sid) for sid in ids))
    return [RemediationSession.model_validate_json(raw) for raw in raws if raw]


def active_session_ids() -> list[str]:
    """Session IDs that aren't in a terminal state AND are real (pollable) — the poller's worklist."""
    sessions = list_sessions(limit=500)
    return [s.session_id for s in sessions if (not s.status.is_terminal) and s.pollable]


# -------------- Events stream --------------

def emit_event(event: SessionEvent) -> None:
    r = get_redis()
    r.xadd(
        "events",
        {
            "session_id": event.session_id,
            "event": event.event,
            "timestamp": event.timestamp,
            "payload": json.dumps(event.payload),
        },
        maxlen=2000,
        approximate=True,
    )


def tail_events(count: int = 50) -> list[dict]:
    r = get_redis()
    raw = r.xrevrange("events", count=count)
    out: list[dict] = []
    for entry_id, fields in raw:
        rec = dict(fields)
        rec["entry_id"] = entry_id
        if "payload" in rec:
            try:
                rec["payload"] = json.loads(rec["payload"])
            except json.JSONDecodeError:
                pass
        out.append(rec)
    return out


# -------------- TimeSeries metrics --------------

def _ts_add(key: str, value: float, labels: dict[str, str] | None = None) -> None:
    """TS.ADD with auto-create. Falls back silently if RedisTimeSeries is unavailable."""
    r = get_redis()
    ts_ms = int(time.time() * 1000)
    args: list = [key, ts_ms, value]
    if labels:
        args.append("LABELS")
        for k, v in labels.items():
            args.extend([k, v])
    try:
        r.execute_command("TS.ADD", *args)
    except redis.exceptions.ResponseError as e:
        log.warning("timeseries_add_failed", key=key, error=str(e))


def record_session_started() -> None:
    _ts_add("metrics:sessions_started", 1, {"kind": "started"})


def record_session_completed(success: bool) -> None:
    _ts_add(
        "metrics:sessions_completed" if success else "metrics:sessions_failed",
        1,
        {"kind": "completed" if success else "failed"},
    )


def record_acu_spent(amount: float) -> None:
    _ts_add("metrics:acu_spent", amount, {"kind": "acu"})


def ts_range(key: str, from_ms: int, to_ms: int = -1) -> list[tuple[int, float]]:
    """Return [(ts_ms, value), ...] over a window. Empty list on missing series."""
    r = get_redis()
    try:
        raw = r.execute_command("TS.RANGE", key, from_ms, to_ms)
        return [(int(ts), float(v)) for ts, v in raw]
    except redis.exceptions.ResponseError:
        return []


# -------------- Counters (cheap fallbacks if TS unavailable) --------------

def incr_counter(name: str, by: int = 1) -> int:
    return int(get_redis().incrby(f"counter:{name}", by))


def get_counter(name: str) -> int:
    raw = get_redis().get(f"counter:{name}")
    return int(raw) if raw else 0


def get_counters(names: Iterable[str]) -> dict[str, int]:
    return {n: get_counter(n) for n in names}
