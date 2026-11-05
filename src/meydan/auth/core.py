"""Sign-in, sessions and grants on a database connection.

Two ways to prove control of an address, both ending in a session for the browser that started:

- **received:** the app shows a short code; the person sends it from an address meydan already
  knows (e.g. their WhatsApp); the runtime catches the message before the agent
  (`sign_in_interceptor`) and confirms the sign-in; the browser, which holds a separate secret,
  then claims its session (`claim`).
- **sent:** the person types their address in the browser; if they may sign in, we send a code
  where they talk to us (`start_code_sign_in` + `send_code`, an authentication template) and they
  type it into the browser (`verify_code`). The answer is the same whether or not a code was sent,
  so the form reveals nothing about who has access. "I didn't request a code" on the message
  cancels that sign-in (`sign_in_interceptor(..., on_reject=)`).

Nothing to remember, nothing to leak: codes, browser secrets and session tokens are stored only as
SHA-256 hashes (a sent code is also blanked in the outbox once it has left).

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
from typing import TYPE_CHECKING, Literal

from sqlalchemy import Connection, func, insert, select, update

from meydan.channels.base import Address, Inbound, Outbound
from meydan.runtime.core import Intercepted
from meydan.runtime.identity import Routed, find, owner, plan_contact
from meydan.runtime.inbound import replied_to
from meydan.runtime.tables import RuntimeTables

if TYPE_CHECKING:
    from meydan.runtime.core import Runtime

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: easy to read and type


@dataclass(frozen=True)
class AuthConfig:
    audience: str = "app"  # the scope a person must hold to sign in here (e.g. "ops")
    code_prefix: str = "SIGNIN"  # codes look like SIGNIN-K7Q4XM
    code_ttl_s: int = 300
    session_ttl_s: int = 30 * 24 * 3600
    confirmed_reply: str | None = None  # sent back on the channel when a sign-in is confirmed
    # Sent codes: digits, wrong tries allowed, and how many codes one address gets per window.
    code_digits: int = 6
    max_attempts: int = 5
    sends_per_window: int = 3
    send_window_s: int = 900


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


# What a channel sends when the person says a code wasn't asked for (WhatsApp's button on
# authentication templates).
REJECT_PAYLOADS = ("DID_NOT_REQUEST_CODE",)
REF_PREFIX = "signin"  # Outbound.ref of a sent code: signin:<sign-in id>

OnReject = Callable[[Connection, uuid.UUID, Routed], None]


def sign_in_interceptor(
    t: RuntimeTables,
    confirmed_reply: str | None = None,
    allowed: SignInRule | None = None,
    *,
    on_reject: OnReject | None = None,
    rejected_reply: str | None = None,
    payload_of: Callable[[Inbound], str | None] | None = None,
):
    """For `Runtime(intercept=[...])`: catches sign-in codes so they never reach the agent, and
    "I didn't request a code" on a code we sent: that sign-in is cancelled, `on_reject` is told
    (e.g. audit it, tell the owners) and the person gets `rejected_reply`."""
    if payload_of is None:
        from meydan.channels.whatsapp import reply_payload as payload_of

    def intercept(conn: Connection, message: Inbound, routed: Routed) -> Intercepted | None:
        if payload_of(message) in REJECT_PAYLOADS:
            answered = replied_to(conn, t, message)
            ref = answered.ref if answered else None
            if not ref or not ref.startswith(f"{REF_PREFIX}:"):
                return None
            sign_in_id = uuid.UUID(ref.split(":", 1)[1])
            if reject(conn, t, sign_in_id) and on_reject is not None:
                on_reject(conn, sign_in_id, routed)
            return Intercepted((Outbound.text_message(rejected_reply),) if rejected_reply else ())
        text = message.text.strip()
        if not text or len(text) > 40:
            return None
        if not confirm_from_message(conn, t, text, routed, allowed):
            return None
        reply = (Outbound.text_message(confirmed_reply),) if confirmed_reply else ()
        return Intercepted(reply)

    return intercept


# --- sent codes --------------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeToSend:
    """A code to send (never shown to the browser): to whom, as which sign-in."""

    sign_in_id: uuid.UUID
    code: str
    identity_id: uuid.UUID  # the address it was asked for
    actor_id: uuid.UUID


@dataclass(frozen=True)
class CodeSignIn:
    browser_secret: str  # keep it in the browser (httpOnly cookie); verify_code needs it
    expires_in_s: int
    send: CodeToSend | None  # None: nothing to send (unknown, not allowed, too many): say nothing


def _code_hash(browser_secret: str, code: str) -> str:
    # Salted with the browser's secret: short codes can repeat across sign-ins, and a code
    # alone (without the browser that asked) is worth nothing.
    return hash_secret(f"{hash_secret(browser_secret)}:{code}")


def start_code_sign_in(
    conn: Connection,
    t: RuntimeTables,
    cfg: AuthConfig,
    address: Address,
    allowed: SignInRule | None = None,
) -> CodeSignIn:
    """Start a sign-in by a code we send to `address`. A row is written either way, so the browser
    gets the same answer whether or not a code goes out; `send` says whether one should."""
    browser_secret = secrets.token_urlsafe(32)
    code = "".join(secrets.choice("0123456789") for _ in range(cfg.code_digits))
    identity_id = find(conn, t, address)
    actor_id = owner(conn, t, identity_id) if identity_id else None
    rule = allowed or _holds_scope(t)
    may = actor_id is not None and rule(conn, actor_id, cfg.audience)
    s = t.sign_ins
    if may:
        recent = conn.scalar(
            select(func.count()).where(
                s.c.identity_id == identity_id,
                s.c.method == "sent",
                s.c.created_at > _now() - timedelta(seconds=cfg.send_window_s),
            )
        )
        may = recent < cfg.sends_per_window
    sign_in_id = conn.scalar(
        insert(s)
        .values(
            method="sent",
            code_hash=_code_hash(browser_secret, code if may else secrets.token_hex(16)),
            browser_hash=hash_secret(browser_secret),
            audience=cfg.audience,
            expires_at=_now() + timedelta(seconds=cfg.code_ttl_s),
            # Who the code is for: set only when one is sent, so the window counts real sends.
            identity_id=identity_id if may else None,
            actor_id=actor_id if may else None,
        )
        .returning(s.c.id)
    )
    send = CodeToSend(sign_in_id, code, identity_id, actor_id) if may else None  # type: ignore[arg-type]
    return CodeSignIn(browser_secret, cfg.code_ttl_s, send)


def send_code(
    conn: Connection,
    runtime: "Runtime",
    code: CodeToSend,
    *,
    template: str,
    language: str,
    channel: str,
    default_from: Address,
) -> uuid.UUID | None:
    """Queue the code as an authentication template where the person talks to us: from the
    endpoint and to the address they last wrote from (any address of theirs); if they never
    wrote, from `default_from` to the address the code was asked for. Returns the turn id."""
    t = runtime.t
    ib, c = t.inbox, t.claims
    theirs = select(c.c.identity_id).where(
        c.c.actor_id == code.actor_id, c.c.state == "active", c.c.valid_to.is_(None)
    )
    last = conn.execute(
        select(ib.c.to, ib.c.sender_identity_id)
        .where(ib.c.channel == channel, ib.c.sender_identity_id.in_(theirs))
        .order_by(ib.c.sent_at.desc())
        .limit(1)
    ).first()
    endpoint = Address(default_from.scheme, last.to) if last else default_from
    identity_id = last.sender_identity_id if last else code.identity_id
    i = t.identities
    to = conn.execute(select(i.c.scheme, i.c.scope, i.c.value).where(i.c.id == identity_id)).one()
    planned = plan_contact(
        conn, t, channel=channel, to=endpoint, address=Address(to.scheme, to.value, to.scope)
    )
    message = Outbound.code_message(
        template, language, code.code, ref=f"{REF_PREFIX}:{code.sign_in_id}"
    )
    return runtime.enqueue(
        conn, channel=channel, conversation_id=planned.conversation_id,
        from_number=endpoint.value, to=to.value, recipient_identity_id=identity_id,
        messages=[message],
    )  # fmt: skip


Verified = Literal["signed_in", "wrong", "expired"]


def verify_code(
    conn: Connection,
    t: RuntimeTables,
    cfg: AuthConfig,
    browser_secret: str,
    code: str,
    label: str | None = None,
) -> tuple[Verified, str | None]:
    """The browser types the code it was sent. Returns ("signed_in", session token) once,
    ("wrong", None) while tries are left, ("expired", None) after that, after its time, after
    "I didn't request a code", or for a browser that didn't start a sent sign-in."""
    s = t.sign_ins
    row = conn.execute(
        select(s, (s.c.expires_at > _now()).label("live"))
        .where(s.c.browser_hash == hash_secret(browser_secret), s.c.method == "sent")
        .with_for_update()
    ).first()
    if (
        row is None
        or not row.live
        or row.claimed_at is not None
        or row.rejected_at is not None
        or row.attempts >= cfg.max_attempts
    ):
        return "expired", None
    typed = "".join(ch for ch in code if ch.isdigit())
    if row.actor_id is None or not secrets.compare_digest(
        _code_hash(browser_secret, typed), row.code_hash
    ):
        conn.execute(update(s).where(s.c.id == row.id).values(attempts=s.c.attempts + 1))
        return ("wrong" if row.attempts + 1 < cfg.max_attempts else "expired"), None
    conn.execute(update(s).where(s.c.id == row.id).values(confirmed_at=_now()))
    return _new_session(conn, t, cfg, row, label)


def reject(conn: Connection, t: RuntimeTables, sign_in_id: uuid.UUID) -> bool:
    """Cancel a sent sign-in the person says they didn't ask for. False if already used/ended."""
    s = t.sign_ins
    return (
        conn.execute(
            update(s)
            .where(s.c.id == sign_in_id, s.c.claimed_at.is_(None), s.c.rejected_at.is_(None))
            .values(rejected_at=_now(), expires_at=_now())
        ).rowcount
        > 0
    )


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
    return _new_session(conn, t, cfg, row, label)


def _new_session(conn, t, cfg, row, label) -> tuple[Literal["signed_in"], str]:
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
    conn.execute(update(t.sign_ins).where(t.sign_ins.c.id == row.id).values(claimed_at=_now()))
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
