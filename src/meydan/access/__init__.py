"""Who may do what, on what: roles defined in the app's OpenFGA model, assignments kept as data.

meydan does not implement an authorization engine; it uses OpenFGA (Apache-2.0, Zanzibar model).
What meydan adds is the glue (docs/access.md):

- assignments are grants in your database (the source of truth, audited), mirrored to OpenFGA
  through an outbox written in the same transaction, so the two can't drift apart;
- `Access.check` / `Access.list_objects` ask OpenFGA, with relations that already live in your data
  passed as contextual tuples instead of being copied;
- `Access.scopes` turns platform roles into the scopes meydan's own surfaces use (e.g. "ops").

Needs the `access` extra (the OpenFGA SDK).
"""

from meydan.access.core import (
    Access,
    AccessConfig,
    actor_ref,
    assign,
    may_sign_in,
    roles_of,
    unassign,
)

__all__ = [
    "Access",
    "AccessConfig",
    "actor_ref",
    "assign",
    "may_sign_in",
    "roles_of",
    "unassign",
]
