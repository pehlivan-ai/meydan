"""Sign-in, sessions and grants on a database connection.

Sign in by sending a message (proof of control): the app shows a short code; the person sends it
from an address meydan already knows (e.g. their WhatsApp); the runtime catches the message before
the agent (`sign_in_interceptor`) and confirms the sign-in for that person's actor; the browser,
which holds a separate secret, then claims its session. Nothing to remember, nothing to leak:
codes, browser secrets and session tokens are stored only as SHA-256 hashes.

Secure defaults (AuthConfig): codes live 5 minutes; only actors holding the sign-in's audience
scope can confirm it (deny by default); a code that matches nothing gets no special treatment, so
there is nothing to probe.
"""

import hashlib
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Connection, func, insert, select, update

from meydan.channels.base import Inbound, Outbound
from meydan.runtime.core import Intercepted
from meydan.runtime.identity import Routed
from meydan.runtime.tables import RuntimeTables

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: easy to read and type


@dataclass(frozen=True)
class AuthConfig:
    audience: str = "app"  # the scope a person must hold to sign in here (e.g. "ops")
    code_prefix: str = "SIGNIN"  # codes look like SIGNIN-K7Q4XM
    code_ttl_s: int = 300
    session_ttl_s: int = 30 * 24 * 3600
    confirmed_reply: str | None = None  # sent back on the channel when a sign-in is confirmed


@dataclass(frozen=True)
class SignInStart:
    code: str  # show it to the person; they send it from their phone
    browser_secret: str  # keep it in the browser (httpOnly cookie); claims the session later
    expires_in_s: int


@dataclass(frozen=True)
class Principal:
    """Who is making a request: the actor, through which session, with which scopes."""

    actor_id: uuid.UUID
    session_id: uuid.UUID
    audience: str
    scopes: frozenset[str]

    def has(self, *scopes: str) -> bool:
        return set(scopes) <= self.scopes


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _normalize(text: str) -> str:
    return "".join(text.split()).upper()


def _now():
    return func.clock_timestamp()


def start_sign_in(conn: Connection, t: RuntimeTables, cfg: AuthConfig) -> SignInStart:
    code = f"{cfg.code_prefix}-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
    browser_secret = secrets.token_urlsafe(32)
    conn.execute(
        insert(t.sign_ins).values(
            code_hash=hash_secret(_normalize(code)),
            browser_hash=hash_secret(browser_secret),
            audience=cfg.audience,
            expires_at=_now() + timedelta(seconds=cfg.code_ttl_s),
        )
    )
    return SignInStart(code, browser_secret, cfg.code_ttl_s)


def scopes_of(conn: Connection, t: RuntimeTables, actor_id: uuid.UUID) -> frozenset[str]:
    g = t.grants
    rows = conn.scalars(select(g.c.scope).where(g.c.actor_id == actor_id, g.c.revoked_at.is_(None)))
    return frozenset(rows)


def grant(
    conn: Connection,
    t: RuntimeTables,
    actor_id: uuid.UUID,
    scope: str,
    granted_by: uuid.UUID | None = None,
) -> None:
    if scope not in scopes_of(conn, t, actor_id):
        conn.execute(insert(t.grants).values(actor_id=actor_id, scope=scope, granted_by=granted_by))


def revoke(conn: Connection, t: RuntimeTables, actor_id: uuid.UUID, scope: str) -> None:
    g = t.grants
    conn.execute(
        update(g)
        .where(g.c.actor_id == actor_id, g.c.scope == scope, g.c.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


# Who may confirm a sign-in for an audience. Default: holding the audience as a scope; with
# meydan.access: holding a platform role (meydan.access.may_sign_in).
SignInRule = Callable[[Connection, uuid.UUID, str], bool]


def _holds_scope(t: RuntimeTables) -> SignInRule:
    return lambda conn, actor_id, audience: audience in scopes_of(conn, t, actor_id)


def confirm_from_message(
    conn: Connection,
    t: RuntimeTables,
    text: str,
    routed: Routed,
    allowed: SignInRule | None = None,
) -> bool:
    """Confirm a pending sign-in if `text` is its code and the sender may sign in there."""
    s = t.sign_ins
    row = conn.execute(
        select(s.c.id, s.c.audience)
        .where(
            s.c.code_hash == hash_secret(_normalize(text)),
            s.c.confirmed_at.is_(None),
            s.c.expires_at > _now(),
        )
        .with_for_update()
    ).first()
    rule = allowed or _holds_scope(t)
    if row is None or not rule(conn, routed.actor_id, row.audience):
        return False
    conn.execute(
        update(s)
        .where(s.c.id == row.id)
        .values(
            confirmed_at=_now(), actor_id=routed.actor_id, identity_id=routed.sender_identity_id
        )
    )
    return True


def sign_in_interceptor(
    t: RuntimeTables, confirmed_reply: str | None = None, allowed: SignInRule | None = None
):
    """For `Runtime(intercept=[...])`: catches sign-in codes so they never reach the agent."""

    def intercept(conn: Connection, message: Inbound, routed: Routed) -> Intercepted | None:
        text = message.text.strip()
        if not text or len(text) > 40:
            return None
        if not confirm_from_message(conn, t, text, routed, allowed):
            return None
        reply = (Outbound.text_message(confirmed_reply),) if confirmed_reply else ()
        return Intercepted(reply)

    return intercept


def claim(
    conn: Connection,
    t: RuntimeTables,
    cfg: AuthConfig,
    browser_secret: str,
    label: str | None = None,
) -> tuple[str, str | None]:
    """The browser asks: signed in yet? Returns (status, session token if it was just created).

    status: "pending" (not confirmed yet), "signed_in" (token returned once), "expired".
    """
    s = t.sign_ins
    row = conn.execute(
        select(s, (s.c.expires_at > _now()).label("live"))
        .where(s.c.browser_hash == hash_secret(browser_secret))
        .with_for_update()
    ).first()
    if row is None or row.claimed_at is not None:
        return "expired", None
    if row.confirmed_at is None:
        return ("pending", None) if row.live else ("expired", None)
    token = secrets.token_urlsafe(32)
    conn.execute(
        insert(t.sessions).values(
            token_hash=hash_secret(token),
            actor_id=row.actor_id,
            audience=row.audience,
            expires_at=_now() + timedelta(seconds=cfg.session_ttl_s),
            label=label[:200] if label else None,
        )
    )
    conn.execute(update(s).where(s.c.id == row.id).values(claimed_at=_now()))
    return "signed_in", token


def principal(conn: Connection, t: RuntimeTables, token: str, audience: str) -> Principal | None:
    """The principal behind a session token, or None (unknown, expired, revoked, other audience)."""
    ss = t.sessions
    row = conn.execute(
        select(ss.c.id, ss.c.actor_id).where(
            ss.c.token_hash == hash_secret(token),
            ss.c.audience == audience,
            ss.c.revoked_at.is_(None),
            ss.c.expires_at > _now(),
        )
    ).first()
    if row is None:
        return None
    conn.execute(update(ss).where(ss.c.id == row.id).values(last_seen_at=_now()))
    return Principal(row.actor_id, row.id, audience, scopes_of(conn, t, row.actor_id))


def sign_out(conn: Connection, t: RuntimeTables, token: str) -> None:
    ss = t.sessions
    conn.execute(
        update(ss)
        .where(ss.c.token_hash == hash_secret(token), ss.c.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
