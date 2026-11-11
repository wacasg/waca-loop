"""ASGI assembly for the hosted server — metadata parity + a connector-visible access log.

Two things FastMCP's own app does not do, both needed for MCP clients (such as ChatGPT's web
connector) to connect reliably:

1. :class:`MetadataCompatMiddleware` — serve the OAuth metadata at *every* path a client
   may look for it, and advertise what the AS really accepts. Clients differ on where they look
   (RFC 9728 path-suffixed vs. bare origin) and a 404 at the first place they try is enough to
   end the connection attempt before any login happens.
2. :class:`ConnectorAccessLogMiddleware` — one log line per OAuth/MCP request saying **how
   far a connector got**. ``initialize`` and ``tools/list`` are both ``POST /mcp`` in Cloud Run's
   request log, so without this "did it reach tools/list?" is unanswerable.

Both are plain ASGI middleware, deliberately not ``BaseHTTPMiddleware``: the MCP endpoint streams
SSE, and buffering middleware breaks streaming responses.

**What the access log may contain (§2.2③ の姿勢を運用ログにも適用).** Cloud Logging is durable and
read by more people than ``oauth_identity_map``, so this logs only *protocol facts*: the HTTP
method and path, the response status, the JSON-RPC **method name** (``initialize`` /
``tools/list`` / ``tools/call``), and the OAuth **scope string a client asked for**. Never
arguments, never tool payloads, never anything a user typed, never a token — the same
summary-only rule the ledger lives under.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs

from waca_loop.auth.as_provider import GOOGLE_APPROVE_PATH, GRANTED_SCOPES, MCP_PATH

logger = logging.getLogger(__name__)

#: What the AS actually asks Google for and is willing to put in a token
#: (:data:`..as_provider.GRANTED_SCOPES`), and therefore the only scopes it can honestly claim to
#: support. Registration accepts anything (see :func:`..as_provider.build_auth_settings`); this is
#: the advertised set in ``scopes_supported``.
ADVERTISED_SCOPES = list(GRANTED_SCOPES)

AS_METADATA_PATH = "/.well-known/oauth-authorization-server"
PRM_PATH = "/.well-known/oauth-protected-resource"

# The JSON-RPC method NAME only. Bounded and character-restricted so a crafted body cannot inject
# newlines or megabytes into the log line.
_RPC_METHOD_RE = re.compile(rb'"method"\s*:\s*"([A-Za-z0-9_./-]{1,64})"')
_JSON_SCOPE_RE = re.compile(rb'"scope"\s*:\s*"([^"\\]{0,200})"')
_SNIFF_BYTES = 4096
#: Non-2xx lines written per minute before the log starts counting instead of writing. Every
#: watched path is reachable unauthenticated, so an anonymous flood must not be able to turn a
#: durable, billed sink into its own amplifier.
_ERROR_LINES_PER_MINUTE = 120


class MetadataCompatMiddleware:
    """Alias the OAuth metadata paths, and patch the AS metadata to match the implementation.

    - ``/.well-known/oauth-authorization-server/mcp`` → the root AS metadata (the MCP spec's
      path-insertion form; the SDK only registers the root).
    - ``/.well-known/oauth-protected-resource`` → the RFC 9728 path-suffixed PRM the SDK
      registers for ``resource_server_url=…/mcp`` (so bare-origin lookups keep working).
    - ``token_endpoint_auth_methods_supported`` gains ``none``: the SDK's client authenticator
      **accepts** public PKCE clients (``token_endpoint_auth_method: "none"``), but
      ``build_metadata`` hard-codes only the two secret-bearing methods. A client that trusts
      the advertisement would refuse to register as public — advertise what actually works.
    - ``scopes_supported`` is (re)stated as :data:`ADVERTISED_SCOPES`; the SDK derives it from
      ``valid_scopes``, which is ``None`` (permissive registration) and would serialize as
      ``null``.
    """

    def __init__(self, app: Any, *, mcp_path: str = MCP_PATH) -> None:
        self.app = app
        self._as_alias = AS_METADATA_PATH + mcp_path
        self._prm_target = PRM_PATH + mcp_path

    def _route_to(self, path: str) -> str:
        """Path this request should actually be served by (identity when no alias applies)."""
        stripped = path.rstrip("/") or "/"
        if stripped == self._as_alias:
            return AS_METADATA_PATH
        if stripped == PRM_PATH:
            return self._prm_target
        return path

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        target = self._route_to(path)
        if target != path:
            scope = {**scope, "path": target, "raw_path": target.encode()}
        if (target.rstrip("/") or "/") != AS_METADATA_PATH:
            await self.app(scope, receive, send)
            return
        await self.app(scope, receive, _patching_send(send))


def _patching_send(send: Any) -> Any:
    """Buffer the (small, JSON) AS-metadata response and hand back a corrected copy."""
    state: dict[str, Any] = {"start": None, "body": b""}

    async def wrapped(message: dict) -> None:
        if message["type"] == "http.response.start":
            state["start"] = message
            return
        if message["type"] != "http.response.body":
            await send(message)
            return
        state["body"] += message.get("body", b"")
        if message.get("more_body"):
            return
        start = state["start"]
        original = state["body"]
        body = _patch_metadata(original) if start["status"] == 200 else original
        headers = start["headers"]
        if body != original:
            # Only when we actually rewrote it — a HEAD response carries no body and must keep
            # the content-length the app declared.
            headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
            headers = [*headers, (b"content-length", str(len(body)).encode())]
        await send({**start, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    return wrapped


def _patch_metadata(body: bytes) -> bytes:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):  # pragma: no cover — SDK always emits JSON
        return body
    if not isinstance(data, dict):  # pragma: no cover
        return body
    methods = list(data.get("token_endpoint_auth_methods_supported") or [])
    if "none" not in methods:
        methods.append("none")
    data["token_endpoint_auth_methods_supported"] = methods
    data["scopes_supported"] = list(ADVERTISED_SCOPES)
    return json.dumps(data).encode()


class ConnectorAccessLogMiddleware:
    """Log how far a connector got: path, status, JSON-RPC method name, requested scope.

    Answers "DCR → authorize → token → initialize → tools/list, where did it stop?" from the
    logs alone. See the module docstring for what may and may not appear here.

    **Read the timestamps as completion times, not arrival times.** The line is written when the
    response finishes, so a long-lived SSE ``GET /mcp`` lands minutes after the POSTs it was
    interleaved with. For "how far did it get" that is harmless; for reconstructing an exact
    order, pair it with Cloud Run's own request log.
    """

    def __init__(self, app: Any, *, mcp_path: str = MCP_PATH) -> None:
        self.app = app
        self._mcp_path = mcp_path
        # EXACT paths only — never a prefix match. The line below logs this *label*, not the
        # request's own path: uvicorn percent-decodes the target into ``scope["path"]``, so a
        # request for `/.well-known/oauth-%0A…` would otherwise write a second, forged log entry
        # indistinguishable from a real one.
        self._labels = {
            mcp_path,
            "/register",
            "/authorize",
            "/token",
            "/auth/google/callback",
            # The approval page's answer is a step in the connect flow, so
            # "how far did it get" has to be able to show it — including a decline.
            GOOGLE_APPROVE_PATH,
            # A 404 at the first place a client looks for the metadata is exactly the failure
            # this log exists to surface.
            AS_METADATA_PATH,
            AS_METADATA_PATH + mcp_path,
            PRM_PATH,
            PRM_PATH + mcp_path,
        }
        self._window = -1
        self._written = 0
        self._suppressed = 0

    def _label(self, path: str) -> str | None:
        """The watched path this request matched — the only path text that may be logged."""
        stripped = path.rstrip("/") or "/"
        return stripped if stripped in self._labels else None

    def _may_write(self, status: int | str) -> bool:
        """2xx/3xx always; error lines are capped per minute (see _ERROR_LINES_PER_MINUTE)."""
        if isinstance(status, int) and 200 <= status < 400:
            return True
        window = int(time.monotonic() // 60)
        if window != self._window:
            if self._suppressed:
                logger.info(
                    "waca-loop-access: suppressed %d further error lines in the previous "
                    "minute", self._suppressed
                )
            self._window, self._written, self._suppressed = window, 0, 0
        if self._written >= _ERROR_LINES_PER_MINUTE:
            self._suppressed += 1
            return False
        self._written += 1
        return True

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        label = self._label(path) if scope.get("type") == "http" else None
        if label is None:
            await self.app(scope, receive, send)
            return

        detail = _query_detail(scope)
        sniffed: dict[str, bytes] = {}

        async def _receive() -> dict:
            message = await receive()
            if message.get("type") == "http.request" and "body" not in sniffed:
                sniffed["body"] = bytes(message.get("body") or b"")[:_SNIFF_BYTES]
            return message

        status: dict[str, int] = {}

        async def _send(message: dict) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, _receive, _send)
        finally:
            code = status.get("code", "-")
            if self._may_write(code):
                body = sniffed.get("body", b"")
                fields = [f"{_http_method(scope)} {label}", f"status={code}"]
                rpc = _RPC_METHOD_RE.search(body)
                if rpc:
                    fields.append(f"rpc={rpc.group(1).decode('ascii', 'replace')}")
                scope_asked = detail or _body_scope(label, body)
                if scope_asked:
                    fields.append(f"scope_requested={scope_asked!r}")
                logger.info("waca-loop-access: %s", " ".join(fields))


def _http_method(scope: dict) -> str:
    """The request verb, reduced to letters — nothing from the wire reaches the log unfiltered."""
    raw = str(scope.get("method", "?"))[:10]
    return "".join(c for c in raw if c.isalpha()) or "?"


def _query_detail(scope: dict) -> str | None:
    """The ``scope`` query parameter of an /authorize request (labels only, never a secret)."""
    if (scope.get("path", "").rstrip("/") or "/") != "/authorize":
        return None
    try:
        params = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — a malformed query string must never break a request
        return None
    asked = params.get("scope", [""])[0]
    return asked[:200] if asked else None


def _body_scope(path: str, body: bytes) -> str | None:
    """The ``scope`` a DCR body asked for — the single fact that decides scope-related diagnoses.

    **Only for /register.** A JSON-RPC body can carry a field called ``scope`` too
    (``set_session_scope``'s argument, say), and a tool argument is user-supplied text that must
    never reach a durable log. Matching on the path keeps this to client metadata.
    """
    if (path.rstrip("/") or "/") != "/register":
        return None
    found = _JSON_SCOPE_RE.search(body)
    return found.group(1).decode("utf-8", "replace") if found else None


def build_http_app(mcp: Any, *, mcp_path: str = MCP_PATH) -> Any:
    """The Starlette app FastMCP builds, wrapped with the two middlewares above.

    Kept out of ``build_mcp`` so tests can exercise either the bare FastMCP app or the
    production-shaped one, and so ``mcp.run()`` (stdio) is untouched.
    """
    served = getattr(mcp.settings, "streamable_http_path", mcp_path)
    if served != mcp_path:
        # build_auth_settings bakes `mcp_path` into the advertised resource identifier, so a
        # server listening elsewhere would publish metadata pointing at a 404. Fail at startup
        # rather than ship a connector-visible lie.
        raise RuntimeError(
            f"MCP is served at {served!r} but the OAuth metadata advertises {mcp_path!r} — "
            "keep waca_loop.auth.as_provider.MCP_PATH and FastMCP's streamable_http_path "
            "in step"
        )
    app = mcp.streamable_http_app()
    return ConnectorAccessLogMiddleware(
        MetadataCompatMiddleware(app, mcp_path=mcp_path), mcp_path=mcp_path
    )
