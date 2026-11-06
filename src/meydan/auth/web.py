"""FastAPI surface for meydan.auth (extra `api`): sign-in routes and request dependencies.

    auth = Auth(engine, runtime_tables, AuthConfig(audience="ops", code_prefix="OPS"),
                sign_in_link=lambda code: f"https://wa.me/<number>?text={code}")
    app.include_router(auth.routes(), prefix="/auth")

With `parse_address` and `send_code`, the browser can also ask for a code to be sent to an address
(POST /sign-in/code) and type it back (POST /sign-in/verify): see `core.start_code_sign_in`. Once
that is the way in, `received=False` drops the code the person sends themselves (POST/GET /sign-in).

    SignedIn = Annotated[Principal, Depends(auth.signed_in)]

    @app.get("/costs", dependencies=[Depends(auth.require("costs:read"))])
    def costs(person: SignedIn) -> CostReport: ...

Browsers carry the session in an httpOnly, Secure, SameSite=Lax, host-only cookie; other clients
send `Authorization: Bearer <token>`. Both appear as security schemes in the OpenAPI document.
Requests that change something and authenticate with the cookie must come from the same origin
(the CSRF guard); bearer requests are not exposed to CSRF.
"""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import Connection, Engine

from meydan.auth import core
from meydan.auth.core import AuthConfig, CodeToSend, Principal, SignInRule
from meydan.channels.base import Address
from meydan.runtime.tables import RuntimeTables

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class SignInStarted(BaseModel):
    code: str  # show it; the person sends it from their phone
    link: str | None  # opens the channel with the code already typed (e.g. a wa.me link)
    expires_in_s: int


class SignInStatus(BaseModel):
    status: str  # "pending" | "signed_in" | "expired"; verify also: "wrong"


class CodeRequest(BaseModel):
    address: str  # as the person typed it (e.g. a phone number); `parse_address` normalizes it


class CodeSent(BaseModel):
    # Always the same answer: a code went out only if this address may sign in here.
    expires_in_s: int


class CodeTyped(BaseModel):
    code: str


class Auth:
    def __init__(
        self,
        engine: Engine,
        tables: RuntimeTables,
        config: AuthConfig | None = None,
        *,
        sign_in_link: Callable[[str], str] | None = None,
        cookie_name: str = "meydan_session",
        secure_cookies: bool = True,  # False only for local development over plain http
        trusted_origins: tuple[str, ...] = (),  # extra origins allowed to send cookie requests
        # Extra scopes per request, e.g. meydan.access.Access.scopes (roles -> scopes).
        resolve_scopes: Callable[[uuid.UUID], Awaitable[frozenset[str]]] | None = None,
        # Sent codes (POST /sign-in/code, /sign-in/verify): how a typed address is read (None:
        # not an address, 422), and how a code goes out (in the same transaction, e.g. through
        # core.send_code). Who may get one: `allowed` (default: holds the audience scope).
        parse_address: Callable[[str], Address | None] | None = None,
        send_code: Callable[[Connection, CodeToSend], object] | None = None,
        allowed: SignInRule | None = None,
        # The code the person sends us (POST /sign-in, polled by GET /sign-in). False: not
        # offered (e.g. only sent codes), so those routes don't exist.
        received: bool = True,
    ):
        self.engine = engine
        self.t = tables
        self.cfg = config or AuthConfig()
        self.sign_in_link = sign_in_link
        self.cookie_name = cookie_name
        self.secure = secure_cookies
        self.trusted_origins = trusted_origins
        self.resolve_scopes = resolve_scopes
        self.parse_address = parse_address
        self.send_code = send_code
        self.allowed = allowed
        self.received = received
        self._cookie = APIKeyCookie(name=cookie_name, auto_error=False)
        self._bearer = HTTPBearer(auto_error=False)
        self.signed_in = self._signed_in_dependency()

    # --- routes ---------------------------------------------------------------------------

    def routes(self) -> APIRouter:
        router = APIRouter(tags=["auth"])
        sign_in_cookie = f"{self.cookie_name}_sign_in"

        if self.received:
            self._received_routes(router, sign_in_cookie)

        if self.parse_address is not None and self.send_code is not None:
            parse, send = self.parse_address, self.send_code

            @router.post("/sign-in/code")
            def request_code(body: CodeRequest, response: Response) -> CodeSent:
                """Send a sign-in code to this address if it may sign in here (same answer
                either way)."""
                address = parse(body.address)
                if address is None:
                    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Not an address.")
                with self.engine.begin() as conn:
                    started = core.start_code_sign_in(conn, self.t, self.cfg, address, self.allowed)
                    if started.send is not None:
                        send(conn, started.send)
                self._set_cookie(
                    response, sign_in_cookie, started.browser_secret, started.expires_in_s
                )
                return CodeSent(expires_in_s=started.expires_in_s)

            @router.post("/sign-in/verify")
            def verify(body: CodeTyped, request: Request, response: Response) -> SignInStatus:
                """Type the code that was sent; sets the session cookie when it matches."""
                secret = request.cookies.get(sign_in_cookie)
                if not secret:
                    return SignInStatus(status="expired")
                label = request.headers.get("user-agent")
                with self.engine.begin() as conn:
                    state, token = core.verify_code(
                        conn, self.t, self.cfg, secret, body.code, label
                    )
                if token:
                    self._set_cookie(response, self.cookie_name, token, self.cfg.session_ttl_s)
                    response.delete_cookie(sign_in_cookie)
                return SignInStatus(status=state)

        @router.post("/sign-out", status_code=status.HTTP_204_NO_CONTENT)
        def sign_out(
            request: Request,
            response: Response,
            person: Annotated[Principal, Depends(self.signed_in)],
        ) -> None:
            """End this session (the cookie or bearer token stops working at once)."""
            token = self._token(request)
            if token:
                with self.engine.begin() as conn:
                    core.sign_out(conn, self.t, token)
            response.delete_cookie(self.cookie_name)

        return router

    def _received_routes(self, router: APIRouter, sign_in_cookie: str) -> None:
        @router.post("/sign-in")
        def start(response: Response) -> SignInStarted:
            """Start a sign-in: returns a code for the person to send from their phone."""
            with self.engine.begin() as conn:
                started = core.start_sign_in(conn, self.t, self.cfg)
            self._set_cookie(response, sign_in_cookie, started.browser_secret, started.expires_in_s)
            link = self.sign_in_link(started.code) if self.sign_in_link else None
            return SignInStarted(code=started.code, link=link, expires_in_s=started.expires_in_s)

        @router.get("/sign-in")
        def poll(request: Request, response: Response) -> SignInStatus:
            """Ask whether the code has been sent yet; sets the session cookie once it has."""
            secret = request.cookies.get(sign_in_cookie)
            if not secret:
                return SignInStatus(status="expired")
            label = request.headers.get("user-agent")
            with self.engine.begin() as conn:
                state, token = core.claim(conn, self.t, self.cfg, secret, label)
            if token:
                self._set_cookie(response, self.cookie_name, token, self.cfg.session_ttl_s)
                response.delete_cookie(sign_in_cookie)
            return SignInStatus(status=state)

    # --- dependencies ---------------------------------------------------------------------

    def _signed_in_dependency(self):
        async def signed_in(
            request: Request,
            cookie: Annotated[str | None, Depends(self._cookie)] = None,
            bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(self._bearer)] = None,
        ) -> Principal:
            """The signed-in principal; 401 if there is none."""
            token = bearer.credentials if bearer else cookie
            if not token:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in first.")
            if bearer is None and request.method not in SAFE_METHODS:
                self._same_origin_or_403(request)
            found = await run_in_threadpool(self._principal, token)
            if found is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in first.")
            if self.resolve_scopes is not None:
                extra = await self.resolve_scopes(found.actor_id)
                found = dataclasses.replace(found, scopes=found.scopes | extra)
            return found

        return signed_in

    def require(self, *scopes: str):
        """A dependency that lets only principals holding all `scopes` through (else 403)."""
        signed_in = self.signed_in

        def require(person: Annotated[Principal, Depends(signed_in)]) -> Principal:
            if not person.has(*scopes):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed.")
            return person

        return require

    # --- helpers --------------------------------------------------------------------------

    def _principal(self, token: str) -> Principal | None:
        with self.engine.begin() as conn:
            return core.principal(conn, self.t, token, self.cfg.audience)

    def _token(self, request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip() or None
        return request.cookies.get(self.cookie_name)

    def _set_cookie(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name, value, max_age=max_age, httponly=True, secure=self.secure, samesite="lax"
        )

    def _same_origin_or_403(self, request: Request) -> None:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if not origin:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request refused.")
        parts = urlsplit(origin)
        source = f"{parts.scheme}://{parts.netloc}"
        own = f"{request.url.scheme}://{request.url.netloc}"
        if source != own and source not in self.trusted_origins:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request refused.")
