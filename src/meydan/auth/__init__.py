"""Who may use the app: sign in by proving control of an address, sessions, grants (docs/auth.md).

The core (this package) has no web framework; `meydan.auth.web` (extra `api`) turns it into FastAPI
routes and dependencies.
"""

from meydan.auth.core import (
    AuthConfig,
    Principal,
    SignInStart,
    claim,
    grant,
    principal,
    revoke,
    scopes_of,
    sign_in_interceptor,
    sign_out,
    start_sign_in,
)

__all__ = [
    "AuthConfig",
    "Principal",
    "SignInStart",
    "claim",
    "grant",
    "principal",
    "revoke",
    "scopes_of",
    "sign_in_interceptor",
    "sign_out",
    "start_sign_in",
]
