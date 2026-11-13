"""Every table of the app in one metadata: meydan's (`meydan_*`), then yours next to them.
Alembic reads this (`migrations/env.py`)."""

from meydan.runtime import define_tables
from sqlalchemy import MetaData

metadata = MetaData()
runtime_tables = define_tables(metadata)

# Your tables go here, on the same `metadata`.
