"""Tests run on their own database and never read your `.env`.

Before anything of the app is imported, the environment is fixed here; it wins over `.env`. The
database is your app's with `_test` added to its name (`app` -> `app_test`), made and migrated
when the tests start. Without this, tests run against the database you develop on, and a test
app with a fake channel can take a turn a real person is waiting for: it happened in an app built
from this template.
"""

import os

import pytest
from sqlalchemy.engine import make_url

from app.config import Env  # reads settings only; connects to nothing

_dev = make_url(os.environ.get("DATABASE_URL") or Env().database_url)
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or _dev.set(
    database=f"{_dev.database}_test"
).render_as_string(hide_password=False)
os.environ.update(
    DATABASE_URL=TEST_DATABASE_URL,
    # Nothing a test does may reach Meta: no token to send with, no account to read.
    WHATSAPP_ACCESS_TOKEN="",
    WHATSAPP_BUSINESS_ACCOUNT_ID="",
    OPENROUTER_API_KEY="",
)


def _migrated_test_database() -> None:
    """Create the test database if it is missing and bring it to the latest migration."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    url = make_url(TEST_DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.scalar(
            text("select 1 from pg_database where datname = :n"), {"n": url.database}
        )
        if not exists:
            conn.execute(text(f'create database "{url.database}"'))
    admin.dispose()
    command.upgrade(Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini")), "head")


_migrated_test_database()


@pytest.fixture(autouse=True)
def leave_no_work_behind():
    """A test leaves no message waiting and no reply unsent: the next test's app would take it
    (its fake channel would answer a message from a test that already ended)."""
    yield
    from sqlalchemy import create_engine, text

    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(
            text(
                "update meydan_inbox set processed_at = now(), "
                "error = 'left by a test' where processed_at is null"
            )
        )
        conn.execute(
            text(
                "update meydan_outbox set status = 'dead', "
                "last_error = 'left by a test' where status = 'pending'"
            )
        )
    engine.dispose()
