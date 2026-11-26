"""Identity settlement and routing against a real Postgres 18 (see test_runtime.py for setup)."""

from datetime import timedelta

from conftest import T0, FakeChannel, msg, receive, runtime
from sqlalchemy import func, select

from meydan.channels.base import Address, Inbound, Outbound
from meydan.runtime import Turn

PHONE = "+905550000001"  # fake numbers and ids only: this is a public repository
BSUID = "TR.1000000000000000001"
OTHER = "TR.2000000000000000002"


def count(rt, table, *where):
    with rt.engine.connect() as c:
        return c.scalar(select(func.count()).select_from(table).where(*where))


def rows(rt, table, *where):
    with rt.engine.connect() as c:
        return c.execute(select(table).where(*where)).all()


async def noop(turn):
    return []


def bsuid(value=BSUID):
    return Address("wa-bsuid", value)


async def test_phone_then_both_then_bsuid_only_is_one_actor_and_one_conversation(db):
    rt = runtime(db, noop)
    t = rt.t
    receive(rt, [msg("w1", "selam", T0)])
    receive(rt, [msg("w2", "nasılsın", T0 + timedelta(seconds=1), also=(bsuid(),))])
    receive(rt, [msg("w3", "numaramı gizledim", T0 + timedelta(seconds=2), sender=BSUID)])
    persons = rows(rt, t.actors, t.actors.c.kind == "person")
    assert len(persons) == 1
    assert count(rt, t.conversations) == 1
    assert len({r.conversation_id for r in rows(rt, t.inbox)}) == 1
    bases = {r.basis for r in rows(rt, t.claims, t.claims.c.actor_id == persons[0].id)}
    assert bases == {"first_contact", "channel_cooccurrence"}


async def test_bsuid_first_then_phone_joins_the_same_actor(db):
    rt = runtime(db, noop)
    t = rt.t
    receive(rt, [msg("w1", "selam", T0, sender=BSUID)])
    receive(rt, [msg("w2", "şimdi numaralı", T0 + timedelta(seconds=1), also=(bsuid(),))])
    assert count(rt, t.actors, t.actors.c.kind == "person") == 1
    assert count(rt, t.conversations) == 1
    assert count(rt, t.participants, t.participants.c.role == "peer") == 2  # tel + bsuid


async def test_two_known_actors_are_never_merged_automatically(db):
    rt = runtime(db, noop)
    t = rt.t
    receive(rt, [msg("w1", "telefonla", T0)])  # actor A owns the phone
    receive(rt, [msg("w2", "kullanıcı adıyla", T0, sender=OTHER)])  # actor B owns the BSUID
    receive(rt, [msg("w3", "ikisi birden", T0, also=(bsuid(OTHER),))])
    assert count(rt, t.actors, t.actors.c.kind == "person") == 2
    proposed = rows(rt, t.claims, t.claims.c.state == "proposed")
    assert len(proposed) == 1 and proposed[0].basis == "channel_cooccurrence"
    receive(rt, [msg("w4", "yine ikisi", T0, also=(bsuid(OTHER),))])
    assert count(rt, t.claims, t.claims.c.state == "proposed") == 1  # proposed once


async def test_duplicate_delivery_settles_nothing_new(db):
    rt = runtime(db, noop)
    t = rt.t
    assert receive(rt, [msg("w1", "selam", T0, also=(bsuid(),))]) == 1
    before = (count(rt, t.actors), count(rt, t.identities), count(rt, t.claims))
    assert receive(rt, [msg("w1", "selam", T0, also=(bsuid(),))]) == 0
    assert (count(rt, t.actors), count(rt, t.identities), count(rt, t.claims)) == before


async def test_endpoints_are_agent_owned_and_separate_conversations(db):
    rt = runtime(db, noop)
    t = rt.t
    receive(rt, [msg("w1", "birinci numaraya", T0, to="222")])
    receive(rt, [msg("w2", "ikinci numaraya", T0, to="333")])
    assert count(rt, t.actors, t.actors.c.kind == "agent") == 2
    assert count(rt, t.actors, t.actors.c.kind == "person") == 1
    assert count(rt, t.conversations) == 2


async def test_reply_goes_to_the_latest_address_as_one_turn(db):
    seen: list[Turn] = []

    async def two(turn):
        seen.append(turn)
        return [Outbound.text_message("bir"), Outbound.text_message("iki")]

    ch = FakeChannel()
    rt = runtime(db, two, ch)
    t = rt.t
    receive(rt, [msg("w1", "selam", T0, also=(bsuid(),))])
    receive(rt, [msg("w2", "gizli", T0 + timedelta(seconds=1), sender=BSUID)])
    await rt.work_once()
    assert seen[0].sender == bsuid() and seen[0].actor_id is not None
    await rt.send_once()
    await rt.send_once()
    assert [(s[1], s[2]) for s in ch.sent] == [(BSUID, "bir"), (BSUID, "iki")]
    out = rows(rt, t.outbox)
    assert len({r.turn_id for r in out}) == 1 and sorted(r.seq for r in out) == [0, 1]


async def test_transcript_orders_both_sides_on_one_clock(db):
    async def reply(turn):
        return [Outbound.text_message(f"re: {turn.text}")]

    rt = runtime(db, reply)
    # The sender's device clock is a day ahead: it must not reorder the transcript.
    receive(rt, [msg("w1", "bir", T0 + timedelta(days=1))])
    await rt.work_once()
    receive(rt, [msg("w2", "iki", T0 + timedelta(days=1, seconds=1))])
    await rt.work_once()
    with rt.engine.connect() as c:
        conversation = c.scalar(select(rt.t.conversations.c.id))
        said = rt.transcript(c, conversation)
    assert [(s.direction, s.text) for s in said] == [
        ("in", "bir"),
        ("out", "re: bir"),
        ("in", "iki"),
        ("out", "re: iki"),
    ]


# --- contacts we start (plan_contact) and receipts that say who received them (corroborate) ---


def plan(rt, address, name="Deniz"):
    from meydan.runtime.identity import plan_contact

    with rt.engine.begin() as c:
        return plan_contact(
            c, rt.t, channel="whatsapp", to=Address("wa-pnid", "222"), address=address, name=name
        )


def receipt(mid="wamid.S1", phone=PHONE, user=BSUID, status="delivered"):
    from meydan.channels.base import StatusUpdate

    who = (Address("tel", phone),) if phone else ()
    who += (bsuid(user),) if user else ()
    return StatusUpdate("whatsapp", mid, status, T0, recipient=who)


def people(rt):
    return count(rt, rt.t.actors, rt.t.actors.c.kind == "person")


async def test_a_planned_contact_creates_no_actor(db):
    rt = runtime(db, noop)
    p = plan(rt, Address("tel", PHONE))
    assert p.actor_id is None and people(rt) == 0
    assert count(rt, rt.t.claims, rt.t.claims.c.identity_id == p.identity_id) == 0
    assert plan(rt, Address("tel", PHONE)).conversation_id == p.conversation_id  # found again
    ident = rows(rt, rt.t.identities, rt.t.identities.c.id == p.identity_id)[0]
    assert ident.attrs["name"] == "Deniz"  # the name we gave lives on the address


async def test_receipt_links_a_planned_phone_to_the_person_known_by_bsuid(db):
    rt = runtime(db, noop)
    t = rt.t
    receive(rt, [msg("w1", "kullanıcı adıyla", T0, sender=BSUID)])  # actor A: BSUID only
    p = plan(rt, Address("tel", PHONE))  # an invite to A's phone number, not knowing it's A
    assert p.actor_id is None
    receive(rt, statuses=[receipt()])  # WhatsApp: this phone's BSUID is A's
    assert people(rt) == 1
    [link] = rows(rt, t.claims, t.claims.c.identity_id == p.identity_id)
    assert link.state == "active" and link.basis == "channel_cooccurrence"
    assert link.evidence == {"status_of": "wamid.S1"}
    receive(rt, statuses=[receipt(status="read")])  # receipts repeat: nothing new
    assert count(rt, t.claims) == 3  # our endpoint's, A's BSUID, and now the phone


async def test_receipt_about_strangers_settles_nothing_and_the_reply_adopts_the_conversation(db):
    rt = runtime(db, noop)
    t = rt.t
    p = plan(rt, Address("tel", PHONE))
    receive(rt, statuses=[receipt()])  # a receipt is not a contact: nobody is created
    assert people(rt) == 0 and count(rt, t.claims) == 1  # only our endpoint's claim
    receive(rt, [msg("w1", "Kabul ediyorum", T0, also=(bsuid(),))])  # their first message
    assert people(rt) == 1
    assert count(rt, t.conversations) == 1  # the planned conversation, now theirs
    [row] = rows(rt, t.inbox)
    assert row.conversation_id == p.conversation_id


async def test_planning_a_known_number_uses_its_owner_and_conversation(db):
    rt = runtime(db, noop)
    receive(rt, [msg("w1", "selam", T0)])
    [conversation] = rows(rt, rt.t.conversations)
    p = plan(rt, Address("tel", PHONE))
    assert p.actor_id is not None and p.conversation_id == conversation.id


async def test_receipt_about_two_known_people_only_proposes(db):
    rt = runtime(db, noop)
    t = rt.t
    receive(rt, [msg("w1", "telefonla", T0)])  # A owns the phone
    receive(rt, [msg("w2", "kullanıcı adıyla", T0, sender=OTHER)])  # B owns another BSUID
    receive(rt, statuses=[receipt(user=OTHER)])
    assert people(rt) == 2
    assert count(rt, t.claims, t.claims.c.state == "proposed") == 1


async def test_a_receipt_with_one_address_changes_nothing(db):
    rt = runtime(db, noop)
    plan(rt, Address("tel", PHONE))
    receive(rt, statuses=[receipt(user=None)])
    assert people(rt) == 0


# --- reactions: recorded, read back, never answered -----------------------------------------


def test_describe_names_what_text_alone_would_not():
    from meydan.channels.base import describe

    assert describe("text", "selam") == "selam"
    assert describe("image", "afiş bu mu?") == "[image] afiş bu mu?"
    assert describe("audio", "") == "[audio]"
    assert describe("location", "") == "[location]"
    assert (
        describe("reaction", "🙏", "Tamamdır, bekleriz") == '[reaction 🙏 to "Tamamdır, bekleriz"]'
    )
    assert describe("reaction", "") == ""  # taken back


async def test_a_reaction_opens_no_turn_and_the_transcript_reads_it_with_its_message(db):
    turns = []

    async def reply(turn):
        turns.append(turn)
        return [Outbound.text_message("Tamamdır, bekleriz")]

    rt = runtime(db, reply)
    receive(rt, [msg("w1", "olursa yazarım", T0)])
    await rt.work_once()
    await rt.send_once()  # ours goes out as wamid.1
    react = Inbound(
        "whatsapp", "w2", Address("wa-pnid", "222"), Address("tel", "+905550000001"),
        T0 + timedelta(seconds=5), "reaction", "🙏", {"id": "w2"}, reply_to="wamid.1",
    )  # fmt: skip
    receive(rt, [react])
    await rt.work_once()
    assert len(turns) == 1  # nobody answers a 🙏
    with rt.engine.connect() as c:
        conversation = c.scalar(select(rt.t.conversations.c.id))
        said = rt.transcript(c, conversation)
    assert [s.text for s in said] == [
        "olursa yazarım",
        "Tamamdır, bekleriz",
        '[reaction 🙏 to "Tamamdır, bekleriz"]',
    ]


async def test_an_app_may_answer_reactions(db):
    turns = []

    async def reply(turn):
        turns.append(turn)
        return []

    rt = runtime(db, reply, answer_reactions=True)
    react = Inbound(
        "whatsapp", "w9", Address("wa-pnid", "222"), Address("tel", "+905550000001"),
        T0, "reaction", "❤️", {"id": "w9"}, reply_to="wamid.x",
    )  # fmt: skip
    receive(rt, [react])
    await rt.work_once()
    assert len(turns) == 1
