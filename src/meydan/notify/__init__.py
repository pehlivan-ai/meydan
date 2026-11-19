"""Notifications to the team's own devices (extra `notify`): Web Push, queued in Postgres.

Not a channel: channels talk with the people the agent serves; this tells the people who run it
that something happened (someone new wrote, say). A device says yes once (`subscribe`); the app
writes a notification where the thing happened, in the same transaction (`enqueue`); a `Pusher`
sends what is due and forgets devices the push service says are gone.

    with engine.begin() as conn:
        notify.enqueue(conn, t, kind="new_person", title="Anna wrote", body="Hi…", url="/inbox/…")
    ...
    pusher = notify.Pusher(engine, t, keys=notify.Keys.from_env(), subject="mailto:ops@example.com")
    await pusher.run()  # or pusher.drain() from your own loop

What is sent is the Declarative Web Push shape (`{"web_push": 8030, "notification": {...}}`):
browsers that know it show it without a service worker, and a service worker reads the same
fields. The body is encrypted end to end (RFC 8291, aes128gcm): the push service carries it but
cannot read it. Keys are VAPID (RFC 8292): the public key is the `applicationServerKey` a page
subscribes with; changing the keys means every device subscribes again.
"""

import asyncio
import base64
import json
import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import Connection, Engine, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from meydan.runtime.tables import RuntimeTables

log = logging.getLogger(__name__)

TITLE_MAX = 120
BODY_MAX = 400  # well under what push services carry (4 KB encrypted) and what a lock screen shows
ATTEMPTS = 5


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass(frozen=True)
class Keys:
    """A VAPID key pair: `private` is base64url DER (PKCS#8), `public` the base64url uncompressed
    point a page passes as `applicationServerKey`."""

    private: str
    public: str

    @classmethod
    def generate(cls) -> "Keys":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        der = key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        point = key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return cls(private=_b64(der), public=_b64(point))

    @classmethod
    def from_env(cls, prefix: str = "MEYDAN_NOTIFY_") -> "Keys | None":
        """Keys from `<prefix>PRIVATE_KEY` and `<prefix>PUBLIC_KEY`; None if one is missing."""
        private = os.environ.get(f"{prefix}PRIVATE_KEY", "").strip()
        public = os.environ.get(f"{prefix}PUBLIC_KEY", "").strip()
        if not private or not public:
            log.warning("notify: no VAPID keys (%sPRIVATE_KEY, %sPUBLIC_KEY): nothing is sent",
                        prefix, prefix)  # fmt: skip
            return None
        return cls(private=private, public=public)


def subscribe(
    conn: Connection, t: RuntimeTables, subscriber: str, *, endpoint: str, p256dh: str, auth: str
) -> None:
    """A device says yes. The same endpoint again (a refresh, or someone else signing in on that
    device) takes the new keys and the new subscriber: an endpoint is one device."""
    s = t.notify_subscriptions
    values = {"subscriber": subscriber, "endpoint": endpoint, "p256dh": p256dh, "auth": auth}
    conn.execute(
        pg_insert(s)
        .values(values)
        .on_conflict_do_update(
            index_elements=[s.c.endpoint],
            set_={"subscriber": subscriber, "p256dh": p256dh, "auth": auth},
        )
    )


def unsubscribe(conn: Connection, t: RuntimeTables, subscriber: str, endpoint: str) -> bool:
    """Only the subscriber's own device. Returns whether there was one."""
    s = t.notify_subscriptions
    gone = conn.execute(delete(s).where(s.c.subscriber == subscriber, s.c.endpoint == endpoint))
    return gone.rowcount > 0


def subscribers(conn: Connection, t: RuntimeTables) -> list[str]:
    s = t.notify_subscriptions
    return list(conn.scalars(select(s.c.subscriber).distinct()))


def payload(
    title: str, body: str, *, url: str | None = None, tag: str | None = None, **more: Any
) -> dict[str, Any]:
    """The Declarative Web Push message. `tag` folds notifications about the same thing into one
    (with `renotify`, the newer one still rings); `url` is where a tap goes."""
    n: dict[str, Any] = {"title": title[:TITLE_MAX], "body": body[:BODY_MAX], **more}
    if url:
        n["navigate"] = url
    if tag:
        n["tag"] = tag
        n.setdefault("renotify", True)
    return {"web_push": 8030, "notification": n}


def enqueue(
    conn: Connection,
    t: RuntimeTables,
    *,
    kind: str,
    title: str,
    body: str,
    url: str | None = None,
    tag: str | None = None,
    to: Iterable[str] | None = None,
) -> int:
    """Write a notification for every device of `to` (None: every subscribed device). Call it in
    the transaction where the thing happened: it goes if and only if that commits. Returns how
    many devices it goes to (0: nobody has said yes yet; the notification is still recorded)."""
    s, d = t.notify_subscriptions, t.notify_deliveries
    nid = conn.scalar(
        insert(t.notifications)
        .values(kind=kind, payload=payload(title, body, url=url, tag=tag))
        .returning(t.notifications.c.id)
    )
    devices = select(s.c.id)
    if to is not None:
        devices = devices.where(s.c.subscriber.in_(list(to)))
    ids = list(conn.scalars(devices))
    if ids:
        conn.execute(insert(d), [{"notification_id": nid, "subscription_id": i} for i in ids])
    return len(ids)


@dataclass(frozen=True)
class Outcome:
    """What the push service said: `status` its HTTP status, None when it could not be reached."""

    status: int | None
    error: str | None = None
    retry_after: int | None = None


Send = Callable[[dict[str, Any], str], Outcome]  # (subscription_info, data) -> Outcome


class Pusher:
    """Sends due deliveries. 404/410: the device is gone, its subscription and rows go with it.
    400/413: this message can never go to it (failed). 429, 5xx, no answer: again later, up to
    `ATTEMPTS` times, waiting longer each time (or as long as Retry-After says)."""

    def __init__(
        self,
        engine: Engine,
        t: RuntimeTables,
        *,
        keys: Keys | None,
        subject: str,
        ttl: timedelta = timedelta(hours=12),
        urgency: str = "high",
        send: Send | None = None,
    ):
        self.engine, self.t, self.keys = engine, t, keys
        self.subject, self.ttl, self.urgency = subject, ttl, urgency
        self._send = send or self._webpush

    def _webpush(self, info: dict[str, Any], data: str) -> Outcome:
        from pywebpush import WebPushException, webpush

        assert self.keys is not None
        try:
            r = webpush(
                info,
                data=data,
                vapid_private_key=self.keys.private,
                vapid_claims={"sub": self.subject},
                ttl=int(self.ttl.total_seconds()),
                headers={"Urgency": self.urgency},
                timeout=10,
            )
            return Outcome(getattr(r, "status_code", 201))
        except WebPushException as e:
            ra = e.retry_after if isinstance(getattr(e, "retry_after", None), int) else None
            return Outcome(e.status_code, str(e)[:300], ra)
        except Exception as e:  # network: no answer at all
            return Outcome(None, f"{type(e).__name__}: {e}"[:300])

    def drain(self, limit: int = 50) -> int:
        """Send what is due, one transaction per delivery (rows are locked while they go, so two
        pushers never send the same one). Returns how many were tried."""
        if self.keys is None:
            return 0
        tried = 0
        for _ in range(limit):
            if not self._one():
                break
            tried += 1
        return tried

    def _one(self) -> bool:
        t, d, s = self.t, self.t.notify_deliveries, self.t.notify_subscriptions
        with self.engine.begin() as conn:
            row = conn.execute(
                select(
                    d.c.notification_id,
                    d.c.subscription_id,
                    d.c.attempts,
                    s.c.endpoint,
                    s.c.p256dh,
                    s.c.auth,
                    t.notifications.c.payload,
                )  # fmt: skip
                .join(s, s.c.id == d.c.subscription_id)
                .join(t.notifications, t.notifications.c.id == d.c.notification_id)
                .where(d.c.status == "pending", d.c.next_at <= func.now())
                .order_by(d.c.next_at)
                .limit(1)
                .with_for_update(of=d, skip_locked=True)
            ).first()
            if row is None:
                return False
            info = {"endpoint": row.endpoint, "keys": {"p256dh": row.p256dh, "auth": row.auth}}
            out = self._send(info, json.dumps(row.payload, ensure_ascii=False))
            this = (d.c.notification_id == row.notification_id) & (
                d.c.subscription_id == row.subscription_id
            )
            if out.status is not None and out.status <= 202:
                conn.execute(update(d).where(this).values(status="sent", sent_at=func.now(),
                                                          attempts=d.c.attempts + 1))  # fmt: skip
                conn.execute(
                    update(s).where(s.c.id == row.subscription_id).values(last_ok_at=func.now())
                )
            elif out.status in (404, 410):
                log.info("notify: a device is gone (%s), forgetting it", out.status)
                conn.execute(delete(s).where(s.c.id == row.subscription_id))
            elif out.status in (400, 401, 403, 413) or row.attempts + 1 >= ATTEMPTS:
                log.warning("notify: not delivered (%s): %s", out.status, out.error)
                conn.execute(update(d).where(this).values(status="failed", last_error=out.error,
                                                          attempts=d.c.attempts + 1))  # fmt: skip
            else:
                wait = out.retry_after or 30 * 2**row.attempts
                conn.execute(
                    update(d)
                    .where(this)
                    .values(
                        attempts=d.c.attempts + 1,
                        last_error=out.error,
                        next_at=func.now() + timedelta(seconds=wait),
                    )
                )
        return True

    async def run(self, every: float = 2.0) -> None:
        """Drain forever (cancel the task to stop)."""
        if self.keys is None:
            return
        while True:
            try:
                await asyncio.to_thread(self.drain)
            except Exception:
                log.exception("notify: drain failed")
            await asyncio.sleep(every)


__all__ = [
    "ATTEMPTS", "Keys", "Outcome", "Pusher", "enqueue", "payload", "subscribe", "subscribers",
    "unsubscribe",
]  # fmt: skip
