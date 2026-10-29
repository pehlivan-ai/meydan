"""Access on OpenFGA: assignments as data, mirrored through the outbox; checks, lists, scopes.

Needs an OpenFGA server: MEYDAN_TEST_OPENFGA_URL (default http://127.0.0.1:18080), e.g.
`docker run -p 127.0.0.1:18080:8080 openfga/openfga:v1.22.0 run`. Skipped when unreachable.
"""

import os
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import insert, select

from meydan.access import Access, AccessConfig, assign, may_sign_in, roles_of, unassign

URL = os.environ.get("MEYDAN_TEST_OPENFGA_URL", "http://127.0.0.1:18080")
MODEL = Path(__file__).parent / "fixtures" / "access.json"


@pytest.fixture
async def access(db):
    try:
        httpx.get(f"{URL}/healthz", timeout=2).raise_for_status()
    except Exception as e:
        pytest.skip(f"no OpenFGA server: {e}")
    engine, t = db
    config = AccessConfig(
        api_url=URL,
        model=MODEL,
        store=f"test-{uuid.uuid4()}",  # isolated per test
        instance="instance:main",
        scopes={"ops": "can_operate", "costs:read": "can_read_costs"},
    )
    a = Access(engine, t, config)
    await a.start()
    yield a
    await a.close()


def person(access) -> uuid.UUID:
    actors = access.t.actors
    with access.engine.begin() as c:
        return c.execute(insert(actors).values(kind="person").returning(actors.c.id)).scalar_one()


def state(access) -> dict[str, str]:
    s = access.t.access_state
    with access.engine.connect() as c:
        return dict(c.execute(select(s.c.key, s.c.value)).all())


def do(access, fn, *args):
    with access.engine.begin() as c:
        return fn(c, access.t, *args)


async def test_restart_reuses_the_store_and_unchanged_model(access):
    before = state(access)
    again = Access(access.engine, access.t, access.cfg)
    await again.start()
    after = state(access)
    assert after["store_id"] == before["store_id"] and after["model_id"] == before["model_id"]
    await again.close()


async def test_starting_twice_closes_the_previous_client(access):
    first = access.client
    await access.start()
    assert access.client is not first
    assert first._api_client.rest_client.pool_manager.closed  # the old connection pool is closed


async def test_a_lost_store_is_rebuilt_from_grants(access):
    ali = person(access)
    do(access, assign, ali, "manager", "space:yoga")
    await access.sync_once()
    await access.client.delete_store()  # OpenFGA loses everything (new server, wiped datastore)
    await access.start()
    await access.sync_once()
    assert await access.check(ali, "can_edit", "space:yoga")  # rebuilt from our grants


async def test_assignments_are_data_first_then_synced(access):
    ali = person(access)
    assert do(access, assign, ali, "manager", "space:yoga")
    assert not do(access, assign, ali, "manager", "space:yoga")  # once
    assert not await access.check(ali, "can_edit", "space:yoga")  # not synced yet
    assert await access.sync_once() == 1
    assert await access.check(ali, "can_edit", "space:yoga")
    assert not await access.check(ali, "can_edit", "space:chess")  # spaces are separate
    assert do(access, roles_of, ali) == [("manager", "space:yoga")]

    assert do(access, unassign, ali, "manager", "space:yoga")
    await access.sync_once()
    assert not await access.check(ali, "can_edit", "space:yoga")


async def test_relations_from_your_data_are_passed_not_copied(access):
    ali, host = person(access), person(access)
    do(access, assign, ali, "manager", "space:yoga")
    do(access, assign, host, "host", "event:sunrise")
    await access.sync_once()
    in_space = [("space:yoga", "space", "event:sunrise")]  # the event's space, from your tables
    assert await access.check(ali, "can_edit", "event:sunrise", context=in_space)
    assert not await access.check(ali, "can_edit", "event:sunrise")  # without the fact: no
    assert await access.check(host, "can_edit", "event:sunrise")


async def test_list_objects_and_platform_scopes(access):
    owner, manager = person(access), person(access)
    do(access, assign, owner, "owner", "instance:main")
    do(access, assign, manager, "manager", "space:yoga")
    do(access, assign, manager, "manager", "space:chess")
    await access.sync_once()
    editable = await access.list_objects(manager, "can_edit", "space")
    assert sorted(editable) == ["space:chess", "space:yoga"]
    assert await access.scopes(owner) == frozenset({"ops", "costs:read"})
    assert await access.scopes(manager) == frozenset()
    # instance admins manage every space (inheritance in the model)
    linked = [("instance:main", "instance", "space:yoga")]
    assert await access.check(owner, "can_edit", "space:yoga", context=linked)


async def test_sign_in_rule_from_roles(access):
    owner, stranger = person(access), person(access)
    do(access, assign, owner, "owner", "instance:main")
    rule = may_sign_in(access.t, roles=("owner", "admin"), obj="instance:main")
    with access.engine.connect() as c:
        assert rule(c, owner, "ops") and not rule(c, stranger, "ops")
