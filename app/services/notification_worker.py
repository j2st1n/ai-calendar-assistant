from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from sqlalchemy import select, update

from app.db.models import NotificationDelivery, NotificationEvent, NotificationTarget
from app.db.session import SessionLocal
from app.services.notification_sender import DeliveryError, Destination, destination, send_notification
from app.services.notification_service import PENDING, plan_deliveries

logger = logging.getLogger(__name__)


async def run_once(session_factory=SessionLocal,
                   sender: Callable[[Destination, str], Awaitable[None]] = send_notification,
                   now: float | None = None) -> None:
    fixed_now = now
    now = time.time() if fixed_now is None else fixed_now
    with session_factory() as session:
        plan_deliveries(session, now)
        ids = list(session.scalars(select(NotificationDelivery.id).where(
            NotificationDelivery.status.in_(PENDING), NotificationDelivery.next_attempt_at <= now,
            NotificationDelivery.expires_at >= now,
        ).order_by(NotificationDelivery.due_at).limit(50)))
    for ident in ids:
        claim_time = time.time() if fixed_now is None else fixed_now
        # Commit the conditional claim before network I/O, so another worker cannot send it.
        with session_factory() as session:
            claimed = session.execute(update(NotificationDelivery).where(
                NotificationDelivery.id == ident, NotificationDelivery.status.in_(PENDING),
                NotificationDelivery.expires_at >= claim_time,
                NotificationDelivery.next_attempt_at <= claim_time,
            ).values(status="sending", claimed_at=claim_time, attempts=NotificationDelivery.attempts + 1))
            if claimed.rowcount != 1:
                session.rollback()
                continue
            session.commit()
            job = session.get(NotificationDelivery, ident)
            event = session.get(NotificationEvent, job.event_key)
            target = session.get(NotificationTarget, job.target_id)
            if not event or not event.active or event.version != job.event_version or not target or not target.selected:
                job.status = "cancelled"
                job.error = "日程或接收目标已变更"
                session.commit()
                continue
            try:
                dest = destination(session, target)
            except DeliveryError as exc:
                job.status = "failed"
                job.error = str(exc)
                session.commit()
                continue
            text = job.text
        error = None
        try:
            await sender(dest, text)
        except asyncio.CancelledError:
            # Leave a durable sending marker. Recovery marks it unknown without resending.
            raise
        except DeliveryError as exc:
            error = exc
        except Exception:
            error = DeliveryError("发送异常，投递结果待核对", uncertain=True)
        completed = time.time() if fixed_now is None else fixed_now
        with session_factory() as session:
            job = session.get(NotificationDelivery, ident)
            if error is None:
                job.status = "sent"
                job.sent_at = completed
                job.error = None
            else:
                job.error = str(error)
                delay = max(error.retry_after, 30 * (2 ** (job.attempts - 1)))
                if error.uncertain:
                    job.status = "unknown"
                elif error.retry and job.attempts < 3 and completed + delay <= job.expires_at:
                    job.status = "retry"
                    job.next_attempt_at = completed + delay
                else:
                    job.status = "failed"
            session.commit()


async def notification_loop() -> None:
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not log API exception URLs: Telegram embeds the Bot token in the path.
            logger.error("Notification scheduler iteration failed (%s)", type(exc).__name__)
        await asyncio.sleep(30)
