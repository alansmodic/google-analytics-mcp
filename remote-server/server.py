"""Remote, multi-account, read-only wrapper around upstream analytics_mcp.

Upstream's package is imported untouched so upstream releases merge cleanly.
This module:

* loads one Google login per account from ``GA_TOKEN_<LABEL>`` env vars
  (authorized-user JSON produced by ``remote-server/login.py``),
* routes each tool call to whichever account can see the requested
  ``property_id`` (``get_account_summaries`` is merged across all accounts),
* only exposes upstream's read-only tools,
* serves MCP over Streamable HTTP behind a shared secret, sent either as
  ``Authorization: Bearer <token>`` or as a URL prefix ``/<token>/mcp`` for
  clients that can't send headers.
"""

import contextvars
import hmac
import json
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.oauth2.credentials import Credentials  # noqa: E402
from mcp import types as mcp_types  # noqa: E402

import analytics_mcp.coordinator as coordinator  # noqa: E402
from analytics_mcp.tools import client as ga_client  # noqa: E402
from analytics_mcp.tools.admin.info import get_account_summaries  # noqa: E402
from analytics_mcp.tools.utils import construct_property_rn  # noqa: E402

log = logging.getLogger("ga-remote")

READONLY_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]
TOKEN_ENV_PREFIX = "GA_TOKEN_"
PROPERTY_MAP_TTL = int(os.environ.get("GA_PROPERTY_MAP_TTL", "600"))

# The account whose credentials upstream's client factories should use for the
# current tool call. asyncio.to_thread copies context, so upstream's worker
# threads see the value set by the request that spawned them.
_current_account = contextvars.ContextVar("ga_account", default=None)


def load_accounts(environ=None):
    """Return {label: Credentials} from GA_TOKEN_<LABEL> env vars."""
    environ = os.environ if environ is None else environ
    accounts = {}
    for key in sorted(environ):
        if not key.startswith(TOKEN_ENV_PREFIX) or not environ[key].strip():
            continue
        label = key[len(TOKEN_ENV_PREFIX):].lower()
        try:
            info = json.loads(environ[key])
        except json.JSONDecodeError as e:
            raise ValueError(f"{key} is not valid JSON: {e}") from e
        accounts[label] = Credentials.from_authorized_user_info(info, READONLY_SCOPES)
    return accounts


class AccountRouter:
    """Maps each GA4 property to the account that can see it."""

    def __init__(self, accounts):
        self.accounts = accounts
        self._lock = threading.Lock()
        self._property_map = {}
        self._property_map_at = 0.0

    def credentials(self):
        """Drop-in replacement for upstream's _ClientManager._get_credentials()."""
        if not self.accounts:
            raise RuntimeError(
                "No Google accounts configured on the server. Run remote-server/login.py for "
                "each account and set the resulting GA_TOKEN_<LABEL> variables."
            )
        label = _current_account.get() or next(iter(self.accounts))
        return self.accounts[label]

    async def merged_summaries(self):
        """Account summaries across all logins, deduped by GA account."""
        merged = {}
        property_map = {}
        errors = []
        for label in self.accounts:
            token = _current_account.set(label)
            try:
                summaries = await get_account_summaries()
            except Exception as e:  # one broken login shouldn't hide the others
                log.warning("list_account_summaries failed for account %s: %s", label, e)
                errors.append((label, e))
                continue
            finally:
                _current_account.reset(token)
            for summary in summaries:
                key = summary.get("account") or summary.get("name")
                if key in merged:
                    known = {p.get("property") for p in merged[key].get("property_summaries", [])}
                    extra = [p for p in summary.get("property_summaries", []) if p.get("property") not in known]
                    merged[key].setdefault("property_summaries", []).extend(extra)
                else:
                    merged[key] = summary
                for prop in summary.get("property_summaries", []):
                    property_map.setdefault(prop.get("property"), label)
        if errors and not merged:
            raise errors[0][1]
        with self._lock:
            self._property_map = property_map
            self._property_map_at = time.monotonic()
        return list(merged.values())

    async def account_for_property(self, property_id):
        prop = construct_property_rn(property_id)
        with self._lock:
            fresh = time.monotonic() - self._property_map_at < PROPERTY_MAP_TTL
            label = self._property_map.get(prop)
        if label and fresh:
            return label
        await self.merged_summaries()
        with self._lock:
            label = self._property_map.get(prop)
        # Unknown property: let the first account make the call so upstream's
        # normal permission error comes back.
        return label or next(iter(self.accounts))


def _text_result(payload, is_error=False):
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=json.dumps(payload, indent=2))],
        is_error=is_error,
    )


def configure(environ=None):
    """Patch upstream for remote use and return (router, server)."""
    router = AccountRouter(load_accounts(environ))
    # Upstream calls self._get_credentials() under its own lock; an instance
    # attribute shadows the method, so every client it builds uses our router.
    ga_client._client_manager._get_credentials = router.credentials

    server = coordinator.create_server(allow_write=False)
    upstream_call = server.get_request_handler("tools/call")

    async def call_tool(ctx, params: mcp_types.CallToolRequestParams):
        if not router.accounts:
            return _text_result({"error": "No Google accounts configured on the server."}, is_error=True)
        if params.name == "get_account_summaries":
            try:
                return _text_result(await router.merged_summaries())
            except Exception as e:
                return _text_result({"error": f"Failed to execute tool 'get_account_summaries': {e}"}, is_error=True)

        property_id = (params.arguments or {}).get("property_id")
        label = None
        if property_id is not None:
            try:
                label = await router.account_for_property(property_id)
            except ValueError as e:  # malformed property_id; upstream reports it
                log.info("Couldn't route property %r: %s", property_id, e)
        token = _current_account.set(label)
        try:
            return await upstream_call.handler(ctx, params)
        finally:
            _current_account.reset(token)

    server.add_request_handler("tools/call", upstream_call.params_type, call_tool)
    return router, server


class TokenGate:
    """ASGI middleware: require the shared secret on every request but /health."""

    def __init__(self, app, token, allow_url_token=True):
        self.app = app
        self.token = token.encode()
        self.allow_url_token = allow_url_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope["path"]
        if path == "/health":
            return await _respond(send, 200, {"status": "ok"})
        # Clients probing for OAuth metadata should see "no OAuth here" rather
        # than a 401 that implies an OAuth flow exists.
        if path.startswith("/.well-known/"):
            return await _respond(send, 404, {"error": "not_found"})

        if self.allow_url_token:
            first, sep, rest = path.lstrip("/").partition("/")
            if first and hmac.compare_digest(first.encode(), self.token):
                scope = dict(scope)
                scope["path"] = "/" + rest if sep else "/"
                scope["raw_path"] = scope["path"].encode()
                return await self.app(scope, receive, send)

        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme, _, presented = value.partition(b" ")
                if scheme.lower() == b"bearer" and hmac.compare_digest(presented.strip(), self.token):
                    return await self.app(scope, receive, send)

        return await _respond(send, 401, {"error": "unauthorized"})


async def _respond(send, status, payload):
    body = json.dumps(payload).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


def build_app(environ=None):
    environ = os.environ if environ is None else environ
    token = environ.get("MCP_AUTH_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("MCP_AUTH_TOKEN must be set to a random string of at least 32 characters.")

    router, server = configure(environ)
    log.info("Loaded %d Google account(s): %s", len(router.accounts), ", ".join(router.accounts) or "none")

    # Binding to a non-localhost host leaves the SDK's DNS-rebinding check off;
    # the token gate is the access control.
    app = server.streamable_http_app(stateless_http=True, host="0.0.0.0")
    allow_url_token = environ.get("MCP_ALLOW_URL_TOKEN", "true").lower() in ("true", "1", "yes")
    return TokenGate(app, token, allow_url_token)


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = build_app()
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )


if __name__ == "__main__":
    main()
