"""Minimal outbox dispatcher. Replace send_notification with a provider adapter.
Run one or more workers; SKIP LOCKED allows safe concurrent workers.
"""
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from .database import SessionLocal
from .models import OutboxEvent

def send_notification(event: OutboxEvent) -> None:
    # Deliberately a no-op adapter for the hackathon package. A real adapter should
    # call the provider here, outside the transaction that created the event.
    print(f"NOTIFICATION {event.event_type}: {event.payload}")

def dispatch_batch(limit: int = 100) -> int:
    delivered = 0
    with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        events = db.scalars(select(OutboxEvent).where(OutboxEvent.available_at <= now, ((OutboxEvent.status == "pending") | ((OutboxEvent.status == "processing") & (OutboxEvent.locked_until < now)))).order_by(OutboxEvent.created_at).limit(limit).with_for_update(skip_locked=True)).all()
        ids = [e.id for e in events]
        # Mark as processing and commit before network I/O; in production add a
        # lease/visibility timeout so crashed workers can be recovered.
        for event in events:
            event.status = "processing"
            event.locked_until = now + timedelta(minutes=5)
            event.attempts += 1
        db.commit()
    for event_id in ids:
        with SessionLocal() as db:
            event = db.get(OutboxEvent, event_id)
            if not event: continue
            try:
                send_notification(event)
                event.status = "sent"
                event.locked_until = None
                db.commit(); delivered += 1
            except Exception:
                db.rollback()
                # Production implementation: exponential backoff + dead-lettering.
                with SessionLocal() as retry_db:
                    retry = retry_db.get(OutboxEvent, event_id)
                    if retry:
                        retry.status = "pending"
                        retry.locked_until = None
                        retry.available_at = datetime.now(timezone.utc) + timedelta(seconds=min(3600, 2 ** min(retry.attempts, 10)))
                        retry_db.commit()
    return delivered

if __name__ == "__main__":
    print(f"Dispatched {dispatch_batch()} events")
