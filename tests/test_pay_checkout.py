"""meydan.pay.checkout: one attempt to pay, through a provider the app brings.

A fake provider in memory (no network); a real Postgres for the records. Test data is fake.
"""

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import URL, unavailable
from sqlalchemy import MetaData, create_engine, insert, select, text

from meydan.bridge import connections as cx
from meydan.pay import checkout as pay
from meydan.pay.checkout import (
    AuthError,
    BadResultError,
    Checkouts,
    ConfigError,
    Consent,
    Need,
    OpenRequest,
    Person,
    ProviderError,
    Reason,
    Refused,
    Result,
    Session,
    Status,
)
from meydan.runtime import define_tables
from meydan.vault import Keyring

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
TOKEN, SECRET = "tok_test_0123456789", "sig-secret-test"
PERSON = Person("Ada Example", "ada@example.com", "+447400123456")  # fake
CONSENTS = {"terms": Consent("terms", "terms-v1", T0, "192.0.2.1")}


@dataclass
class FakePay:
    """A provider in memory: sessions by id, a status per session, scripted refusals."""

    name: str = "fakepay"
    needs: tuple = (Need("full_name", "name"), Need("email", "email"),
                    Need("phone", "phone", region="GB", mobile=True))  # fmt: skip
    consents: tuple = ("terms",)
    refuse: list = field(default_factory=list)  # next open() answers: Reason, or an exception
    statuses: dict = field(default_factory=dict)  # session_id -> Status
    status_error: Exception | None = None
    opened: list[OpenRequest] = field(default_factory=list)

    def base_url(self, environment: str) -> str:
        return f"https://{environment}.fakepay.example"

    def open(self, http, base_url, token, request):
        if token != TOKEN:
            raise AuthError("token refused", status=401, code="AUTH")
        if self.refuse:
            r = self.refuse.pop(0)
            if isinstance(r, Exception):
                raise r
            return Refused(r, f"409/{r.value.upper()}")
        self.opened.append(request)
        sid = f"s_{len(self.opened)}"
        self.statuses[sid] = Status.PENDING
        return Session(sid, f"{base_url}/pay/{sid}", Status.PENDING, T0 + timedelta(minutes=15),
                       Decimal("50.00"), "EUR")  # fmt: skip

    def status(self, http, base_url, token, session_id, reference):
        if self.status_error:
            raise self.status_error
        return self.statuses[session_id]

    @staticmethod
    def sign(secret: str, session_id: str, reference: str, status: str) -> str:
        msg = f"{session_id}.{reference}.{status}".encode()
        return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()

    def signed(self, secret: str, result: Result) -> bool:
        good = self.sign(secret, result.session_id, result.reference, result.status)
        return hmac.compare_digest(good, result.sig)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def world():
    engine = create_engine(URL)
    try:
        with engine.connect() as c:
            c.execute(text("select 1"))
    except Exception as e:
        unavailable(f"no Postgres test database: {e}")
    md = MetaData()
    t = define_tables(md)
    table = pay.define_table(md)
    md.drop_all(engine)
    md.create_all(engine)
    kr = Keyring({"k1": Keyring.new_key()}, current="k1")
    with engine.begin() as c:
        actor = c.execute(insert(t.actors).values(kind="person").returning(t.actors.c.id)).scalar()
        cx.ensure(c, t, "shop-test")
        cx.add_credential(c, t, kr, "shop-test", "token", TOKEN)
        cx.add_credential(c, t, kr, "shop-test", "hmac", SECRET)
    calls: list[tuple[str, Status, Status | None]] = []
    provider, clock = FakePay(), Clock()

    def orders(conn, checkout, before):
        calls.append((checkout.subject, checkout.status, before))

    co = Checkouts(engine, t, table, kr, {"fakepay": provider}, {"order": orders}, now=clock)
    yield co, provider, clock, calls, actor
    md.drop_all(engine)
    engine.dispose()


def open_one(co, actor, subject="order:1", **kw):
    args = dict(
        subject=subject,
        actor_id=actor,
        connection="shop-test",
        provider="fakepay",
        environment="test",
        person=PERSON,
        consents=CONSENTS,
        return_url="https://shop.example/back",
        embed_origin="https://shop.example",
    )
    return co.open(**(args | kw))  # fmt: skip


def result(c, status="paid", secret=SECRET):
    return Result(c.session_id, c.reference, status,
                  FakePay.sign(secret, c.session_id, c.reference, status))  # fmt: skip


# --- what the provider needs -------------------------------------------------------------------


def test_requirements_compare_what_is_known_with_what_the_provider_needs():
    req = pay.requirements(FakePay(), {"full_name": "  Ada   Example ", "email": "not-an-email",
                                       "phone": "07400 123456"})  # fmt: skip
    assert req.known == {"full_name": "Ada Example", "phone": "+447400123456"}
    assert [n.key for n in req.missing] == ["email"] and not req.ok
    assert req.consents == ("terms",)
    # one word is not a full name; a number from another country is not taken
    assert pay.clean(Need("n", "name"), "Ada") is None
    assert pay.clean(Need("p", "phone", region="GB"), "+15555550123") is None


# --- opening ------------------------------------------------------------------------------------


def test_opening_records_the_attempt_and_tells_the_subject_once(world):
    co, provider, _, calls, actor = world
    c = open_one(co, actor)
    assert c.status == Status.PENDING and c.url.endswith("/pay/s_1")
    assert (c.amount, c.currency, c.kind, c.subject_id) == (Decimal("50.00"), "EUR", "order", "1")
    assert provider.opened[0].reference == str(c.id)  # our id is the provider's reference
    assert calls == [("order:1", Status.PENDING, None)]
    assert c.next_check_at == T0 + co.pending_every
    with co.engine.connect() as conn:
        audit = conn.execute(select(co.tables.audit.c.action)).scalars().all()
    assert audit == ["checkout:pending:fakepay"]


def test_a_used_reference_is_retried_once_under_a_new_checkout(world):
    co, provider, _, _, actor = world
    provider.refuse = [Reason.REF_USED]
    c = open_one(co, actor)
    assert c.status == Status.PENDING
    with co.engine.connect() as conn:
        rows = conn.execute(select(co.table.c.status, co.table.c.reason)).all()
    assert sorted(rows) == [("pending", None), ("refused", "ref_used")]


def test_refusals_and_no_answer_are_recorded_with_why(world):
    co, provider, _, calls, actor = world
    provider.refuse = [Reason.FULL]
    full = open_one(co, actor, "order:2")
    assert (full.status, full.reason, full.error_code) == (Status.REFUSED, Reason.FULL, "409/FULL")
    provider.refuse = [Reason.UNAVAILABLE]
    down = open_one(co, actor, "order:3")
    assert down.status == Status.ERROR and down.next_check_at is None
    assert [s for _, s, _ in calls] == [Status.REFUSED, Status.ERROR]


def test_bad_input_and_our_setup_are_errors_not_attempts(world):
    co, _, _, _, actor = world
    with pytest.raises(ValueError, match="no handler"):
        open_one(co, actor, "ticket:1")
    with pytest.raises(ValueError, match="consent needed"):
        open_one(co, actor, consents={})
    with pytest.raises(ValueError, match="email"):
        open_one(co, actor, person=Person("Ada Example", "nope", "+447400123456"))
    with pytest.raises(ConfigError, match="no payment provider"):
        open_one(co, actor, provider="other")
    with co.engine.begin() as conn:
        cx.set_status(conn, co.tables, "shop-test", False)
    with pytest.raises(ConfigError, match="no live token"):
        open_one(co, actor)
    with co.engine.connect() as conn:
        assert conn.execute(select(co.table.c.id)).all() == []


def test_a_refused_token_is_loud(world):
    co, provider, _, _, actor = world
    provider.refuse = [AuthError("no", status=401, code="AUTH")]
    with pytest.raises(AuthError):
        open_one(co, actor)


# --- learning the result -------------------------------------------------------------------------


def test_the_browser_result_is_a_hint_the_provider_decides(world):
    co, provider, _, calls, actor = world
    c = open_one(co, actor)
    with pytest.raises(BadResultError):  # not signed with this connection's secret
        co.apply_result(result(c, secret="someone-else"))
    with pytest.raises(BadResultError):  # another session
        co.apply_result(Result("s_9", c.reference, "paid", "x"))
    with pytest.raises(BadResultError):
        co.apply_result(Result(c.session_id, "not-a-uuid", "paid", "x"))
    # signed "paid", but the provider still says pending: nothing is paid
    assert co.apply_result(result(c)).status == Status.PENDING
    provider.statuses[c.session_id] = Status.PAID
    paid = co.apply_result(result(c))
    assert paid.status == Status.PAID
    assert calls[-1] == ("order:1", Status.PAID, Status.PENDING)


def test_the_same_status_twice_tells_the_subject_once(world):
    co, provider, _, calls, actor = world
    c = open_one(co, actor)
    provider.statuses[c.session_id] = Status.PAID
    co.refresh(c.id)
    co.refresh(c.id)  # the poll and the browser report it again
    with co.engine.begin() as conn:
        co.apply(conn, c.id, Status.PAID)
    assert [s for _, s, _ in calls].count(Status.PAID) == 1


def test_no_answer_keeps_the_state_and_asks_again_later(world):
    co, provider, clock, _, actor = world
    c = open_one(co, actor)
    clock.t = T0 + timedelta(minutes=3)
    provider.status_error = ProviderError("timeout")
    with pytest.raises(ProviderError):
        co.refresh(c.id)
    with co.engine.connect() as conn:
        again = co.get(conn, c.id)
    assert again.status == Status.PENDING and again.next_check_at == clock.t + co.pending_every


# --- the sweep -----------------------------------------------------------------------------------


def test_the_sweep_asks_open_sessions_often_then_paid_ones_daily_until_watch_until(world):
    co, provider, clock, calls, actor = world
    c = open_one(co, actor, watch_until=T0 + timedelta(days=3))
    with co.engine.connect() as conn:
        assert co.due(conn) == []  # not yet
    clock.t = T0 + co.pending_every
    provider.statuses[c.session_id] = Status.PAID
    assert co.sweep() == 1
    with co.engine.connect() as conn:
        paid = co.get(conn, c.id)
    assert paid.status == Status.PAID and paid.next_check_at == clock.t + co.slow_every
    clock.t = paid.next_check_at
    provider.statuses[c.session_id] = Status.REFUND_PENDING  # refunds come after the payment
    assert co.sweep() == 1 and calls[-1][1] == Status.REFUND_PENDING
    clock.t = T0 + timedelta(days=4)  # past watch_until: nothing more to learn
    with co.engine.connect() as conn:
        assert co.due(conn) == []


def test_the_table_name_is_the_apps():
    md = MetaData()
    define_tables(md)
    t = pay.define_table(md, name="checkouts")
    assert t.name == "checkouts" and "ix_checkouts_due" in {i.name for i in t.indexes}
