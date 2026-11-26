"""Checkouts: a person pays for something through the seller's payment provider (meydan.pay).

Experimental (docs/pay.md). Provider-neutral: what a checkout (one attempt to pay) is, the states
it goes through, what a provider adapter must do, and how its answers reach the app. The app says
what a checkout is *for* (its `subject`, e.g. "order:<uuid>") and what each state means for that
subject (a `Subject` handler per subject kind); an adapter speaks one provider's API (`Provider`).

Pull model (no webhooks): we open a session server-side (`Checkouts.open`), the person pays on the
provider's page (an iframe on ours), and we learn the result by asking the provider:
- the browser hands us the provider's signed result. It is only a hint: `apply_result` checks the
  signature with the connection's `hmac` credential, then fetches the status from the provider
  (the truth), and only then changes anything ("verify by fetch");
- `sweep` (run by `run` beside the API) asks again about what may still change: an open session
  every `pending_every` until `margin` after it expires, then paid, refund-pending (and stale
  pending) ones every `slow_every` until the checkout's `watch_until` (refunds and withdrawals
  come after the payment). `next_check_at` holds when; null: nothing more to learn.

States: `pending` (session open, nothing paid), `paid`, `failed` (declined, nothing charged),
`expired` (not paid in time), `refund_pending`, `refunded`, `withdrawn` (left after the refund
window, not refunded); for a session that never opened: `refused` (the provider said no: `reason`
and its own `error_code`) or `error` (no usable answer: a timeout, a 5xx, an unknown code).
Each state a checkout takes is one audit row (`checkout:<status>:<provider>`, the person as the
actor, the checkout as the subject) and one call of the app's handler, in one transaction.

The checkout's own id (a uuidv7) is the reference the provider keeps with the session: unique per
attempt, no secret, no personal data. Credentials are the connection's (meydan.bridge
.connections): a `token` to call the provider, an `hmac` secret to check what it signs. A missing
keyring, a connection that is off or lacks one, or a token the provider refuses is a `ConfigError`:
loud in the logs, never shown to the person. Logs carry ids and codes only: no names, emails,
phone numbers or tokens.
"""

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol

import httpx
import phonenumbers
from sqlalchemy import (
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    Engine,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import UUID

from meydan.bridge import connections as cx
from meydan.ids import uuid7
from meydan.runtime.tables import RuntimeTables
from meydan.vault import Keyring

log = logging.getLogger(__name__)


class Status(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"
    WITHDRAWN = "withdrawn"
    REFUSED = "refused"  # never opened: the provider said no (reason, error_code)
    ERROR = "error"  # never opened: no usable answer


# What may still change, so the sweep keeps asking (until watch_until).
WATCHED = (Status.PENDING, Status.PAID, Status.REFUND_PENDING)


class Reason(StrEnum):
    """Why a session didn't open. The app branches on these, never on a provider's text."""

    INVALID = "invalid"  # a field is missing or wrong (an email, a consent, our URLs)
    INVALID_PHONE = "invalid_phone"  # the phone isn't one the provider takes
    REF_USED = "ref_used"  # the reference was used before: a new checkout is the fix
    CLOSED = "closed"  # the provider takes no more payments for it
    FULL = "full"  # nothing left to sell
    BUSY = "busy"  # too many open sessions at once: try again shortly
    ALREADY = "already"  # this person (by email) already has one
    NOT_ELIGIBLE = "not_eligible"  # the person doesn't meet the provider's rules
    REMOVED = "removed"  # the provider's organizer removed this person
    UNAVAILABLE = "unavailable"  # no usable answer (status `error`)


ENVIRONMENTS = ("test", "live")


def _in(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"


STATUS_CHECK = _in("status", Status)
REASON_CHECK = f"reason IS NULL OR {_in('reason', Reason)}"
ENVIRONMENT_CHECK = _in("environment", ENVIRONMENTS)
PROVIDER_CHECK = "provider ~ '^[a-z][a-z0-9_-]{0,31}$'"


def define_table(
    metadata: MetaData, *, prefix: str = "meydan_", name: str = "pay_checkouts"
) -> Table:
    """The checkouts table, on the app's metadata, named `name`; it refers to meydan's runtime
    tables (actors, connections), which carry `prefix`."""
    table = Table(
        name,
        metadata,
        # The reference the provider keeps (externalReference): unique per attempt.
        # made here (meydan.ids.uuid7), not by Postgres: uuidv7() exists only from Postgres 18
        Column("id", UUID(as_uuid=True), primary_key=True, default=uuid7),
        Column("subject", Text, nullable=False),  # what it pays for, the app's: "order:<uuid>"
        Column("actor_id", UUID(as_uuid=True), ForeignKey(f"{prefix}actors.id"), nullable=False),
        Column("connection", String(64), ForeignKey(f"{prefix}connections.name"), nullable=False),
        Column("provider", Text, nullable=False),  # the adapter, e.g. a provider's name
        Column("environment", Text, nullable=False),  # test | live
        Column("status", Text, nullable=False),
        Column("reason", Text),  # refused / error: why (Reason)
        Column("error_code", Text),  # refused / error: the provider's own, "409/CODE", "timeout"
        Column("session_id", Text),  # the provider's session id
        Column("url", Text),  # where the person pays (the provider's page)
        Column("amount", Numeric(10, 2)),
        Column("currency", Text),
        Column("expires_at", DateTime(timezone=True)),  # the session's hold ends
        Column("watch_until", DateTime(timezone=True), nullable=False),
        Column("checked_at", DateTime(timezone=True)),  # last time we asked the provider
        Column("next_check_at", DateTime(timezone=True)),  # when the sweep asks again; null: never
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
        Column(
            "updated_at",
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        ),
        CheckConstraint(STATUS_CHECK, name="status"),
        CheckConstraint(REASON_CHECK, name="reason"),
        CheckConstraint(ENVIRONMENT_CHECK, name="environment"),
        CheckConstraint(PROVIDER_CHECK, name="provider"),
        CheckConstraint("length(subject) BETWEEN 1 AND 200", name="subject"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency"),
        CheckConstraint("length(error_code) <= 100", name="error_code"),
        CheckConstraint("length(session_id) <= 200", name="session_id"),
        CheckConstraint("length(url) <= 2000", name="url"),
        UniqueConstraint("provider", "session_id"),
    )
    Index(f"ix_{name}_subject", table.c.subject, table.c.created_at)
    Index(
        f"ix_{name}_due",
        table.c.next_check_at,
        postgresql_where=table.c.next_check_at.isnot(None),
    )
    return table


# --- what a provider needs from the person ---------------------------------------------------


@dataclass(frozen=True)
class Need:
    """One thing a provider needs from the person. `name`: a full name (at least two words);
    `email`; `phone` (E.164; `region` and `mobile` narrow it). `confirm`: ask it twice."""

    key: str  # "full_name", "email", "phone": the same keys in `Person` and `requirements`
    kind: Literal["name", "email", "phone"]
    region: str | None = None  # phone: the country it must be from ("TR")
    mobile: bool = False  # phone: a mobile number
    confirm: bool = False


EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean(need: Need, value: str | None) -> str | None:
    """The value as the provider takes it (trimmed; a phone in E.164), or None if it can't."""
    value = (value or "").strip()
    if not value:
        return None
    if need.kind == "name":
        words = value.split()
        return " ".join(words) if len(words) >= 2 and len(value) <= 200 else None
    if need.kind == "email":
        return value if len(value) <= 254 and EMAIL.match(value) else None
    try:
        number = phonenumbers.parse(value, need.region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    if need.region and phonenumbers.region_code_for_number(number) != need.region:
        return None
    kinds = phonenumbers.PhoneNumberType
    if need.mobile and phonenumbers.number_type(number) not in (
        kinds.MOBILE,
        kinds.FIXED_LINE_OR_MOBILE,
    ):
        return None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


@dataclass(frozen=True)
class Requirements:
    known: dict[str, str]  # key -> the value the person already has, as the provider takes it
    missing: list[Need]  # to ask: nothing yet, or a value this provider can't take
    consents: tuple[str, ...]  # consents to ask for (each checkout records its own)

    @property
    def ok(self) -> bool:
        return not self.missing


def requirements(provider: "Provider", have: Mapping[str, str | None]) -> Requirements:
    """What the person already has (`have`, by key) against what the provider needs."""
    known: dict[str, str] = {}
    missing: list[Need] = []
    for need in provider.needs:
        value = clean(need, have.get(need.key))
        if value is None:
            missing.append(need)
        else:
            known[need.key] = value
    return Requirements(known, missing, provider.consents)


@dataclass(frozen=True)
class Person:
    """Who pays, as the provider needs them (checked with `clean`); never logged."""

    full_name: str
    email: str
    phone: str  # E.164


@dataclass(frozen=True)
class Consent:
    """A consent (or acknowledgement) the person gave on the app's page, by the name the
    provider knows it by: which text, when, from where."""

    name: str
    text_version: str
    accepted_at: datetime
    ip: str


# --- the provider adapter --------------------------------------------------------------------


@dataclass(frozen=True)
class OpenRequest:
    reference: str  # the checkout's id
    person: Person
    consents: Mapping[str, Consent]
    return_url: str
    embed_origin: str


@dataclass(frozen=True)
class Session:
    """A session the provider opened."""

    session_id: str
    url: str
    status: Status
    expires_at: datetime | None = None
    amount: Decimal | None = None
    currency: str | None = None


@dataclass(frozen=True)
class Refused:
    """No session: the provider said no (`reason`), or gave no usable answer (UNAVAILABLE)."""

    reason: Reason
    error_code: str  # the provider's own: "409/CODE", "502/", "timeout", "malformed"


@dataclass(frozen=True)
class Result:
    """What the browser says the provider's page told it. A hint: checked, then fetched."""

    session_id: str
    reference: str
    status: str
    sig: str


class Provider(Protocol):
    name: str
    needs: tuple[Need, ...]
    consents: tuple[str, ...]

    def base_url(self, environment: str) -> str: ...

    def open(
        self, http: httpx.Client, base_url: str, token: str, request: OpenRequest
    ) -> Session | Refused:
        """Open a session. Raises AuthError if the token is refused; anything else is an answer."""
        ...

    def status(
        self, http: httpx.Client, base_url: str, token: str, session_id: str, reference: str
    ) -> Status:
        """The session's status now. Raises AuthError, or ProviderError (no usable answer)."""
        ...

    def signed(self, secret: str, result: Result) -> bool:
        """Whether `result` carries the provider's signature (timing-safe)."""
        ...


class CheckoutError(Exception):
    """`status` / `code`: the provider's HTTP status and its own error code, when it answered
    (None: no answer at all), so callers branch on them and never on a message."""

    def __init__(self, message: str = "", *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status, self.code = status, code


class ConfigError(CheckoutError):
    """Our setup is wrong (no keyring, a connection off or without a credential, an unknown
    provider, a token refused). Loud in the logs, never shown to the person."""


class AuthError(ConfigError):
    """The provider refused our token."""


class ProviderError(CheckoutError):
    """No usable answer from the provider (a timeout, a 5xx, an unexpected body): try later."""


class NotFoundError(CheckoutError):
    pass


class BadResultError(CheckoutError):
    """A result from the browser that doesn't check out (signature, session): nothing changes."""


# --- records ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Checkout:
    id: uuid.UUID
    subject: str
    actor_id: uuid.UUID
    connection: str
    provider: str
    environment: str
    status: Status
    reason: Reason | None
    error_code: str | None
    session_id: str | None
    url: str | None
    amount: Decimal | None
    currency: str | None
    expires_at: datetime | None
    watch_until: datetime
    checked_at: datetime | None
    next_check_at: datetime | None
    created_at: datetime

    @property
    def reference(self) -> str:
        return str(self.id)

    @property
    def kind(self) -> str:
        """The subject's kind: "order" in "order:<uuid>"."""
        return self.subject.partition(":")[0]

    @property
    def subject_id(self) -> str:
        return self.subject.partition(":")[2]

    @classmethod
    def of(cls, row) -> "Checkout":
        return cls(
            id=row.id, subject=row.subject, actor_id=row.actor_id, connection=row.connection,
            provider=row.provider, environment=row.environment, status=Status(row.status),
            reason=Reason(row.reason) if row.reason else None, error_code=row.error_code,
            session_id=row.session_id, url=row.url, amount=row.amount, currency=row.currency,
            expires_at=row.expires_at, watch_until=row.watch_until, checked_at=row.checked_at,
            next_check_at=row.next_check_at, created_at=row.created_at,
        )  # fmt: skip


class Subject(Protocol):
    def __call__(self, conn: Connection, checkout: Checkout, before: Status | None) -> None:
        """What the checkout's new state means for its subject, in the change's transaction.
        `before`: its previous state; None when it was just recorded."""
        ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Checkouts:
    """Opening checkouts, learning their results, and the sweep. Built once (composition root).

    `subjects`: subject kind -> its handler. `now`: the app's clock. Times are seconds."""

    def __init__(
        self,
        engine: Engine,
        tables: RuntimeTables,
        table: Table,
        keyring: Keyring | None,
        providers: Mapping[str, Provider],
        subjects: Mapping[str, Subject],
        *,
        http: httpx.Client | None = None,
        now: Callable[[], datetime] = _utcnow,
        timeout: float = 10,
        sweep_every: float = 30,
        pending_every: float = 120,
        slow_every: float = 86400,
        margin: float = 600,
        watch: float = 30 * 86400,
        batch: int = 50,
    ):
        self.engine, self.tables, self.table, self.keyring = engine, tables, table, keyring
        self.providers, self.subjects, self.now = dict(providers), dict(subjects), now
        self.http = http or httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=False)
        self.sweep_every, self.batch = sweep_every, batch
        self.pending_every = timedelta(seconds=pending_every)
        self.slow_every = timedelta(seconds=slow_every)
        self.margin = timedelta(seconds=margin)
        self.watch = timedelta(seconds=watch)

    # -- lookups ------------------------------------------------------------------------------

    def provider(self, name: str) -> Provider:
        try:
            return self.providers[name]
        except KeyError:
            raise ConfigError(f"no payment provider {name!r}") from None

    def credential(self, conn: Connection, connection: str, kind: str) -> str:
        """The connection's live `token` or `hmac`, opened; ConfigError if there is none."""
        if self.keyring is None:
            raise ConfigError("MEYDAN_KEYRING is not set: connection credentials can't be opened")
        value = cx.value(conn, self.tables, self.keyring, connection, kind)
        if value is None:
            raise ConfigError(
                f"{connection}: no live {kind} (none given, retired, or the connection is off)"
            )
        return value

    def get(self, conn: Connection, checkout_id: uuid.UUID, *, lock: bool = False) -> Checkout:
        q = select(self.table).where(self.table.c.id == checkout_id)
        row = conn.execute(q.with_for_update() if lock else q).first()
        if row is None:
            raise NotFoundError(f"no checkout {checkout_id}")
        return Checkout.of(row)

    def latest(self, conn: Connection, subject: str) -> Checkout | None:
        """The subject's most recent checkout (any state)."""
        row = conn.execute(
            select(self.table)
            .where(self.table.c.subject == subject)
            .order_by(self.table.c.created_at.desc(), self.table.c.id.desc())
            .limit(1)
        ).first()
        return Checkout.of(row) if row else None

    # -- opening ------------------------------------------------------------------------------

    def open(
        self,
        *,
        subject: str,
        actor_id: uuid.UUID,
        connection: str,
        provider: str,
        environment: str,
        person: Person,
        consents: Mapping[str, Consent],
        return_url: str,
        embed_origin: str,
        watch_until: datetime | None = None,
    ) -> Checkout:
        """Open a session for `subject`, paid by `actor_id` (`person` as the provider needs it),
        and record the outcome: `pending` with the provider's url, or `refused` / `error` with
        why. A reference the provider has seen before (REF_USED) is retried once under a new
        checkout. Raises ConfigError (and AuthError) for our setup, ValueError for bad input."""
        p = self.provider(provider)
        if environment not in ENVIRONMENTS:
            raise ValueError(f"environment is one of {ENVIRONMENTS}")
        if subject.partition(":")[0] not in self.subjects:
            raise ValueError(f"no handler for subject {subject!r}")
        if missing := [c for c in p.consents if c not in consents]:
            raise ValueError(f"consent needed: {', '.join(missing)}")
        given = {"full_name": person.full_name, "email": person.email, "phone": person.phone}
        cleaned = {n.key: clean(n, given.get(n.key)) for n in p.needs}
        if bad := [k for k, v in cleaned.items() if v is None]:
            raise ValueError(f"not as {provider} takes it: {', '.join(bad)}")
        person = Person(**(given | cleaned))
        with self.engine.connect() as conn:
            token = self.credential(conn, connection, "token")
        base = p.base_url(environment)
        watch_until = watch_until or self.now() + self.watch
        for attempt in (1, 2):
            checkout_id = uuid7()
            request = OpenRequest(str(checkout_id), person, consents, return_url, embed_origin)
            try:
                answer = p.open(self.http, base, token, request)
            except AuthError:
                log.error("checkout: %s refused the token of connection %s", provider, connection)
                raise
            checkout = self._record(
                checkout_id, answer, subject=subject, actor_id=actor_id, connection=connection,
                provider=provider, environment=environment, watch_until=watch_until,
            )  # fmt: skip
            if isinstance(answer, Refused):
                log.warning(
                    "checkout %s: not opened (%s, %s)",
                    checkout_id, answer.reason, answer.error_code,
                )  # fmt: skip
                if answer.reason == Reason.REF_USED and attempt == 1:
                    continue  # a reference is good for one attempt: a new checkout is the fix
            return checkout
        return checkout  # the second REF_USED: recorded, given back

    def _record(
        self,
        checkout_id: uuid.UUID,
        answer: Session | Refused,
        *,
        subject: str,
        actor_id: uuid.UUID,
        connection: str,
        provider: str,
        environment: str,
        watch_until: datetime,
    ) -> Checkout:
        now = self.now()
        values: dict = {
            "id": checkout_id, "subject": subject, "actor_id": actor_id,
            "connection": connection, "provider": provider, "environment": environment,
            "watch_until": watch_until,
        }  # fmt: skip
        if isinstance(answer, Session):
            values |= {
                "status": answer.status, "session_id": answer.session_id, "url": answer.url,
                "expires_at": answer.expires_at, "amount": answer.amount,
                "currency": answer.currency, "checked_at": now,
            }  # fmt: skip
            values["next_check_at"] = self._next(
                answer.status, answer.expires_at, now, watch_until, now
            )
        else:
            status = Status.ERROR if answer.reason == Reason.UNAVAILABLE else Status.REFUSED
            values |= {
                "status": status,
                "reason": answer.reason,
                "error_code": answer.error_code[:100],
            }
        with self.engine.begin() as conn:
            conn.execute(insert(self.table).values(**values))
            checkout = self.get(conn, checkout_id)
            self._changed(conn, checkout, None)
        return checkout

    # -- learning results ---------------------------------------------------------------------

    def _next(
        self,
        status: Status,
        expires_at: datetime | None,
        created_at: datetime,
        watch_until: datetime,
        now: datetime,
    ) -> datetime | None:
        """When to ask again: soon while a session is open, then about daily until watch_until."""
        if status not in WATCHED or now >= watch_until:
            return None
        if status == Status.PENDING and now < (expires_at or created_at) + self.margin:
            return now + self.pending_every
        return now + self.slow_every

    def apply(self, conn: Connection, checkout_id: uuid.UUID, status: Status | None) -> Checkout:
        """The provider's status for a checkout, in the caller's transaction: a change is
        recorded (audit + the subject's handler) once; the same status again changes nothing
        but the check times. None: we asked and got no usable answer (only reschedules)."""
        c = self.get(conn, checkout_id, lock=True)
        now = self.now()
        new = status or c.status
        values = {
            "checked_at": now,
            "next_check_at": self._next(new, c.expires_at, c.created_at, c.watch_until, now),
        }
        if new != c.status:
            values["status"] = new
        conn.execute(update(self.table).where(self.table.c.id == c.id).values(**values))
        checkout = self.get(conn, c.id)
        if new != c.status:
            self._changed(conn, checkout, c.status)
        return checkout

    def _changed(self, conn: Connection, checkout: Checkout, before: Status | None) -> None:
        action = f"checkout:{checkout.status}:{checkout.provider}"[:60]
        conn.execute(
            insert(self.tables.audit).values(
                actor_id=checkout.actor_id, action=action, subject_id=checkout.id
            )
        )
        handler = self.subjects.get(checkout.kind)
        if handler is None:
            log.warning("checkout %s: no handler for its subject kind", checkout.id)
            return
        handler(conn, checkout, before)

    def refresh(self, checkout_id: uuid.UUID) -> Checkout:
        """Ask the provider for the checkout's status now and apply it. Raises ConfigError or
        ProviderError when there is no answer (the next check is still scheduled)."""
        with self.engine.connect() as conn:
            c = self.get(conn, checkout_id)
        if c.session_id is None:
            return c  # never opened: nothing to ask
        try:
            with self.engine.connect() as conn:
                token = self.credential(conn, c.connection, "token")
            p = self.provider(c.provider)
            status = p.status(
                self.http, p.base_url(c.environment), token, c.session_id, c.reference
            )
        except CheckoutError:
            self._unanswered(c.id)
            raise
        with self.engine.begin() as conn:
            return self.apply(conn, c.id, status)

    def _unanswered(self, checkout_id: uuid.UUID) -> None:
        with self.engine.begin() as conn:
            self.apply(conn, checkout_id, None)

    def apply_result(self, result: Result) -> Checkout:
        """A result the browser passed on from the provider's page: the signature is checked
        with the connection's `hmac`, then the status is fetched from the provider and applied
        (the hinted status itself is never applied). Raises BadResultError / NotFoundError when
        it doesn't check out (nothing changes), ConfigError / ProviderError as `refresh`."""
        try:
            checkout_id = uuid.UUID(result.reference)
        except (ValueError, TypeError, AttributeError):
            raise BadResultError("not one of our references") from None
        with self.engine.connect() as conn:
            c = self.get(conn, checkout_id)
            if c.session_id is None or c.session_id != result.session_id:
                log.warning("checkout %s: a result for another session", c.id)
                raise BadResultError("the session doesn't match")
            secret = self.credential(conn, c.connection, "hmac")
        if not self.provider(c.provider).signed(secret, result):
            log.warning("checkout %s: a result with a bad signature", c.id)
            raise BadResultError("bad signature")
        return self.refresh(c.id)

    # -- the sweep ----------------------------------------------------------------------------

    def due(self, conn: Connection, now: datetime | None = None, limit: int | None = None):
        """Checkouts whose next check has come (and that are still watched)."""
        now = now or self.now()
        ck = self.table
        return (
            conn.execute(
                select(ck.c.id)
                .where(
                    ck.c.next_check_at.isnot(None),
                    ck.c.next_check_at <= now,
                    ck.c.watch_until > now,
                    ck.c.status.in_(WATCHED),
                )
                .order_by(ck.c.next_check_at)
                .limit(limit or self.batch)
            )
            .scalars()
            .all()
        )

    def sweep(self) -> int:
        """Ask about every due checkout once. Returns how many got an answer."""
        with self.engine.connect() as conn:
            ids = self.due(conn)
        answered = 0
        for checkout_id in ids:
            try:
                self.refresh(checkout_id)
                answered += 1
            except ConfigError as e:
                log.error("checkout %s: %s", checkout_id, e)
            except CheckoutError as e:
                log.warning("checkout %s: no answer (%s)", checkout_id, e)
        return answered

    async def run(self, stop: asyncio.Event) -> None:
        """`sweep` every `sweep_every` seconds until `stop` is set."""
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.sweep)
            except Exception:
                log.exception("checkout sweep error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.sweep_every)


__all__ = [
    "ENVIRONMENTS", "WATCHED", "AuthError", "BadResultError", "Checkout", "CheckoutError",
    "Checkouts", "ConfigError", "Consent", "Need", "NotFoundError", "OpenRequest", "Person",
    "Provider", "ProviderError", "Reason", "Refused", "Requirements", "Result", "Session",
    "Status", "Subject", "clean", "define_table", "requirements",
]  # fmt: skip
