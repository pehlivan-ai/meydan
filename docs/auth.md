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

## Sign in by a code we send (since v0.15.0)
The way most apps do it: type your number, get a code on WhatsApp, type it in. When this is the
only way in, `Auth(received=False)` (since v0.17.0) leaves out the code people send themselves
(`POST/GET /sign-in`).

1. `POST /auth/sign-in/code {"address": "+90 555 …"}`: the app reads the address
   (`parse_address`), and if its owner may sign in here (`allowed`, default: holds the audience
   scope) and hasn't had 3 codes in 15 minutes, sends a 6-digit code (`send_code`). **The answer
   is the same either way** (`{"expires_in_s": 300}`), so the form tells nobody who has access.
   The browser gets its secret in an httpOnly cookie.
2. The code goes **where the person talks to us** (`core.send_code`): from the number and to the
   address they last wrote from, else from `default_from` to the typed address. It is an
   authentication template (WhatsApp: fixed text, a copy-code button; `Outbound.code_message`),
   with `ref="signin:<id>"`. The outbox blanks the code once it has left.
3. `POST /auth/sign-in/verify {"code": "123456"}` → `signed_in` (session cookie), `wrong`, or
   `expired` (time, used, 5 wrong tries, or cancelled).
4. **"I didn't request a code"** (WhatsApp's button on authentication templates, beta): the
   interceptor finds the sign-in by the message it answers, cancels it, calls `on_reject` (e.g.
   audit it, tell the owners) and replies `rejected_reply`.

A code we sent typed into the chat does nothing: it is only good in the browser that asked.

```python
auth = Auth(
    engine,
    tables,
    AuthConfig(audience="ops"),
    parse_address=lambda typed: ...,  # e.g. libphonenumber -> Address("tel", e164)
    send_code=lambda conn, code: send_code(
        conn,
        runtime,
        code,
        template="signin_code",
        language="tr",
        channel="whatsapp",
        default_from=Address("wa-pnid", phone_number_id),
    ),
)
runtime = Runtime(..., intercept=[sign_in_interceptor(tables, on_reject=..., rejected_reply="...")])
```

Why not `expect`: a sign-in proves control of an address, a typed code checked in the browser;
an expectation waits for a person's answer to a question. The one answer a sent code can get
("I didn't request a code") is matched by the same parts `expect` uses (`ref`, `replied_to`).

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
| sent codes: 6 digits, 5 wrong tries, 3 sends per address per 15 minutes, salted per browser | guessing is hopeless; a code alone (without the browser) is worth nothing |
| the code request answers the same for anyone | the form doesn't reveal who has access |
| only actors with the audience scope can confirm | deny by default; strangers' codes are ordinary messages |
| no special answer to wrong or expired codes | nothing to probe |
| codes, browser secrets, tokens stored only as hashes | a database leak doesn't give sessions away |
| cookie requests that change something must come from the same origin | CSRF |
| sessions last 30 days (set less for admin consoles) | |
