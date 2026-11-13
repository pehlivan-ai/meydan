"""Migrations as a deploy step, safe for rollback: `python -m app.migrate`.

A rollback starts the previous image on a database the newer image already migrated. Plain
`alembic upgrade head` then fails ("Can't locate revision"), so the previous image could never
come back. Here the database being *ahead* of this image is a known state: it is logged and the
schema is left alone. That is only safe because migrations stay backward compatible with the
previous release (expand first, contract in a later release).

Anything else (a broken migration, no database) still fails the step.
"""

import logging
import sys
from typing import Literal

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from app.config import Env

log = logging.getLogger("app.migrate")
Outcome = Literal["upgraded", "ahead"]


def database_revisions(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return set(MigrationContext.configure(conn).get_current_heads())
    finally:
        engine.dispose()


def migrate(config: Config, url: str) -> Outcome:
    script = ScriptDirectory.from_config(config)
    known = {rev.revision for rev in script.walk_revisions()}
    unknown = database_revisions(url) - known
    if unknown:
        log.warning(
            "database is at %s, newer than this image (head %s): starting without migrating "
            "(a rollback; migrations are kept backward compatible)",
            ", ".join(sorted(unknown)),
            ", ".join(script.get_heads()),
        )
        return "ahead"
    command.upgrade(config, "head")
    return "upgraded"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    migrate(Config("alembic.ini"), Env().database_url)


if __name__ == "__main__":
    sys.exit(main())
