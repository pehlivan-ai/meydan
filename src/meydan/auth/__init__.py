"""Who may use the app: sign in by proving control of an address, sessions, grants (docs/auth.md).

The core (this package) has no web framework; `meydan.auth.web` (extra `api`) turns it into FastAPI
routes and dependencies.
"""

from meydan.auth.core import (
    AuthConfig,
    CodeSignIn,
    CodeToSend,
    Principal,
    SignInStart,
    claim,
    grant,
    principal,
    reject,
    revoke,
    scopes_of,
    send_code,
    sign_in_interceptor,
    sign_out,
    start_code_sign_in,
    start_sign_in,
    verify_code,
)

__all__ = [
    "AuthConfig",
    "CodeSignIn",
    "CodeToSend",
    "Principal",
    "SignInStart",
    "claim",
    "grant",
    "principal",
    "reject",
    "revoke",
    "scopes_of",
    "send_code",
    "sign_in_interceptor",
    "sign_out",
    "start_code_sign_in",
    "start_sign_in",
    "verify_code",
]
