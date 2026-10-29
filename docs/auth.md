# Auth: who may use the app

Available since v0.5.0 (`meydan.auth`; FastAPI surface in `meydan.auth.web`, extra `api`).

Your app's users are already actors with identities (their WhatsApp number, later their email).
So they sign in by **proving control of an address meydan already knows**. No passwords, nothing
to remember, nothing stored that would be useful if leaked.

## Sign in by sending a message
1. The app asks for a code: `POST /auth/sign-in` → `OPS-K7Q4XM` (plus a link that opens WhatsApp
   with the code already typed). The browser gets a separate secret in an httpOnly cookie.
2. The person sends the code from their phone. The runtime catches it **before the agent**
   (`sign_in_interceptor`; it never becomes a turn) and confirms the sign-in for the person who
   sent it, if they hold the sign-in's audience scope.
3. The browser asks `GET /auth/sign-in` (every few seconds) and, once confirmed, receives its
   session cookie.

It works inside the channel's normal rules (the person writes first), so it needs no message
templates.

## Sessions and scopes
- **Sessions:** a random token; browsers keep it in an httpOnly, Secure, SameSite=Lax, host-only
  cookie, other clients send `Authorization: Bearer <token>`. Stored only as a SHA-256 hash;
  signing out revokes it at once.
- **Scopes (grants):** which actor may do what (`ops`, `costs:read`, ...). Deny by default.
  `grant(conn, tables, actor_id, "ops")` / `revoke(...)`.
- **Audience:** a sign-in and its sessions belong to one audience (e.g. `ops` for an admin
  console): an ops session is useless in a user app and the other way round.

## In a FastAPI app
```python
from meydan.auth import AuthConfig, Principal, sign_in_interceptor
from meydan.auth.web import Auth

runtime = Runtime(
    ..., intercept=[sign_in_interceptor(runtime_tables, confirmed_reply="Signed in.")]
)
auth = Auth(
    engine,
    runtime_tables,
    AuthConfig(audience="ops", code_prefix="OPS"),
    sign_in_link=lambda code: f"https://wa.me/<your number>?text={code}",
)
app.include_router(auth.routes(), prefix="/auth")

SignedIn = Annotated[Principal, Depends(auth.signed_in)]  # 401 without a session


@app.get("/costs", dependencies=[Depends(auth.require("costs:read"))])  # 403 without the scope
def costs(person: SignedIn) -> CostReport: ...
```

## Secure defaults (all adjustable in `AuthConfig` / `Auth`)
| Default | Why |
|---|---|
| codes live 5 minutes, one session per code | a leaked code is useless soon and only once |
| only actors with the audience scope can confirm | deny by default; strangers' codes are ordinary messages |
| no special answer to wrong or expired codes | nothing to probe |
| codes, browser secrets, tokens stored only as hashes | a database leak doesn't give sessions away |
| cookie requests that change something must come from the same origin | CSRF |
| sessions last 30 days (set less for admin consoles) | |
