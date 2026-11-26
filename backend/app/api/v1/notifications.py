"""In-app notifications for the dashboard Overview side panel. Every route is scoped to the caller's own notifications."""
import asyncio
import json
import time
import uuid
from collections import defaultdict
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import current_membership
from app.config import get_settings
from app.db.base import SessionLocal, get_db
from app.db.models.tenancy import OrganizationMember
from app.services import notifications

router = APIRouter(prefix="/api/v1/orgs/{organization_id}/notifications", tags=["notifications"])

# One explicit Literal member per category (OpenAPI shows an enum; anything else is a 422). A test pins it to CATEGORIES.
Category = Literal["calls", "leads", "credits", "billing", "receptionist", "phone", "business", "analytics", "product"]


class NotificationOut(BaseModel):
    id: str
    seq: int
    kind: str
    category: str
    severity: str
    title: str
    body: str
    link: str | None
    created_at: str
    read_at: str | None
    is_read: bool


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    next_cursor: int | None
    unread_count: int


class NotificationState(BaseModel):
    latest_seq: int
    unread_count: int


class ReadAllResult(BaseModel):
    marked: int
    unread_count: int


class PreferenceOut(BaseModel):
    category: str
    enabled: bool
    critical_always_delivered: bool


class PreferenceIn(BaseModel):
    enabled: bool


def _out(item: dict) -> NotificationOut:
    return NotificationOut(**{**item, "created_at": item["created_at"].isoformat(), "read_at": item["read_at"].isoformat() if item["read_at"] else None})


@router.get("", response_model=NotificationPage)
def list_notifications(
    limit: int = Query(default=20, ge=1, le=notifications.MAX_PAGE),
    before: int | None = Query(default=None, ge=1),
    unread_only: bool = False,
    category: Category | None = None,
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    page = notifications.list_page(db, membership, limit=limit, before=before, unread_only=unread_only, category=category)
    return NotificationPage(items=[_out(i) for i in page["items"]], next_cursor=page["next_cursor"], unread_count=page["unread_count"])


@router.get("/preferences", response_model=list[PreferenceOut])
def get_preferences(membership: OrganizationMember = Depends(current_membership), db: Session = Depends(get_db)):
    """The caller's own in-app preferences, one entry per category (absent means on)."""
    return notifications.get_preferences(db, membership)


@router.put("/preferences/{category}", response_model=PreferenceOut)
def put_preference(category: Category, body: PreferenceIn, membership: OrganizationMember = Depends(current_membership), db: Session = Depends(get_db)):
    return notifications.set_preference(db, membership, category, body.enabled)


@router.get("/state", response_model=NotificationState)
def notification_state(membership: OrganizationMember = Depends(current_membership), db: Session = Depends(get_db)):
    """Cheap "anything new?" check: the polling fallback when the stream is unavailable."""
    return notifications.state(db, membership)


@router.post("/read-all", response_model=ReadAllResult)
def read_all(
    category: Category | None = None,
    membership: OrganizationMember = Depends(current_membership),
    db: Session = Depends(get_db),
):
    marked = notifications.mark_all_read(db, membership, category)
    return ReadAllResult(marked=marked, unread_count=notifications.unread_count(db, membership))


@router.post("/{notification_id}/read", response_model=NotificationState)
def read_one(notification_id: uuid.UUID, membership: OrganizationMember = Depends(current_membership), db: Session = Depends(get_db)):
    if not notifications.mark_read(db, membership, notification_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")  # not yours, not in your organization, or never existed
    return notifications.state(db, membership)


# ---------------------------------------------------------------- real-time: Server-Sent Events
# The browser holds ONE open request; the server pushes only when something changed. No new infrastructure: the session cookie
# authenticates the stream exactly like every other request, and the check is one indexed query per interval. The stream ends after
# a maximum age so the browser reconnects, which re-verifies the session and the membership (a removed member loses it).

_open_streams: dict[uuid.UUID, int] = defaultdict(int)


def _read_state(membership_id: uuid.UUID) -> dict | None:
    """One short-lived session per check: no database connection is held while the stream idles. None if the membership is gone."""
    db = SessionLocal()
    try:
        membership = db.get(OrganizationMember, membership_id)
        return None if membership is None else notifications.state(db, membership)
    finally:
        db.close()


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


async def stream_events(membership_id: uuid.UUID, interval: float, max_seconds: float, sleep=asyncio.sleep, clock=time.monotonic):
    """The event generator (separate from the route so it can be tested without a network)."""
    started = clock()
    last: dict | None = None
    yield "retry: 5000\n\n"  # the browser's reconnect delay
    while clock() - started < max_seconds:
        current = await run_in_threadpool(_read_state, membership_id)
        if current is None:
            yield _event("closed", {"reason": "membership_ended"})
            return
        if current != last:
            last = current
            yield _event("state", current)
        else:
            yield ": keepalive\n\n"  # also lets the server notice a disconnected client
        await sleep(interval)
    yield _event("closed", {"reason": "refresh"})


@router.get("/stream")
async def stream(membership: OrganizationMember = Depends(current_membership), db: Session = Depends(get_db)):
    cfg = get_settings()
    user_id, membership_id = membership.user_id, membership.id
    db.close()  # release the connection: the stream must not hold one
    if _open_streams[user_id] >= cfg.NOTIFICATION_STREAM_MAX_PER_USER:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many open notification streams")

    async def guarded():
        _open_streams[user_id] += 1
        try:
            async for chunk in stream_events(membership_id, cfg.NOTIFICATION_STREAM_INTERVAL_SECONDS, cfg.NOTIFICATION_STREAM_MAX_SECONDS):
                yield chunk
        finally:
            _open_streams[user_id] -= 1
            if _open_streams[user_id] <= 0:
                _open_streams.pop(user_id, None)

    return StreamingResponse(
        guarded(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
