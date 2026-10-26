"""Tests for the remote wrapper. No Google credentials needed — API calls are mocked.

    uv run --with pytest pytest remote-server/test_server.py -q
"""

import asyncio
import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402
from mcp import types as mcp_types  # noqa: E402

TOKEN = "x" * 40


def _token_json(n):
    return json.dumps({
        "refresh_token": f"r{n}",
        "client_id": "cid",
        "client_secret": "secret",
        "token_uri": "https://oauth2.googleapis.com/token",
    })


ENV = {"GA_TOKEN_AGENCY": _token_json(1), "GA_TOKEN_PERSONAL": _token_json(2), "MCP_AUTH_TOKEN": TOKEN}

SUMMARIES = {
    "agency": [
        {"account": "accounts/1", "display_name": "JMT", "property_summaries": [
            {"property": "properties/100", "display_name": "Camarda"},
            {"property": "properties/101", "display_name": "Brandon"},
        ]},
    ],
    "personal": [
        {"account": "accounts/1", "display_name": "JMT", "property_summaries": [
            {"property": "properties/100", "display_name": "Camarda"},
        ]},
        {"account": "accounts/2", "display_name": "904", "property_summaries": [
            {"property": "properties/200", "display_name": "904 Tech"},
        ]},
    ],
}


async def _fake_summaries():
    return SUMMARIES[server._current_account.get()]


@pytest.fixture
def configured():
    with mock.patch.object(server, "get_account_summaries", _fake_summaries):
        router, srv = server.configure(ENV)
        yield router, srv


def _call(srv, name, arguments=None):
    handler = srv.get_request_handler("tools/call").handler
    params = mcp_types.CallToolRequestParams(name=name, arguments=arguments or {})
    return asyncio.run(handler(None, params))


def test_load_accounts_reads_prefixed_vars():
    accounts = server.load_accounts({**ENV, "GA_TOKEN_EMPTY": " ", "OTHER": "1"})
    assert list(accounts) == ["agency", "personal"]
    assert accounts["agency"].refresh_token == "r1"


def test_load_accounts_rejects_bad_json():
    with pytest.raises(ValueError, match="GA_TOKEN_BAD"):
        server.load_accounts({"GA_TOKEN_BAD": "{nope"})


def test_account_summaries_are_merged_and_deduped(configured):
    _, srv = configured
    result = _call(srv, "get_account_summaries")
    data = json.loads(result.content[0].text)
    assert [a["account"] for a in data] == ["accounts/1", "accounts/2"]
    assert [p["property"] for p in data[0]["property_summaries"]] == ["properties/100", "properties/101"]


def test_property_routes_to_owning_account(configured):
    router, _ = configured
    assert asyncio.run(router.account_for_property("200")) == "personal"
    assert asyncio.run(router.account_for_property("properties/101")) == "agency"
    assert asyncio.run(router.account_for_property(100)) == "agency"
    # Unknown property falls back to the first account.
    assert asyncio.run(router.account_for_property("999")) == "agency"


def test_tool_call_uses_routed_credentials(configured):
    router, srv = configured
    seen = {}

    async def fake_upstream(ctx, params):
        seen["creds"] = await asyncio.to_thread(ga_client_creds)
        return server._text_result({"ok": True})

    def ga_client_creds():
        return server.ga_client._client_manager._get_credentials()

    entry = srv.get_request_handler("tools/call")
    # Re-wrap with a fake upstream to observe which credentials are used.
    with mock.patch.object(server.coordinator, "create_server") as create:
        fake = mock.MagicMock()
        fake.get_request_handler.return_value = mock.MagicMock(handler=fake_upstream, params_type=entry.params_type)
        create.return_value = fake
        with mock.patch.object(server, "get_account_summaries", _fake_summaries):
            router, _ = server.configure(ENV)
            wrapped = fake.add_request_handler.call_args[0][2]
            params = mcp_types.CallToolRequestParams(name="run_report", arguments={"property_id": "200"})
            asyncio.run(wrapped(None, params))
    assert seen["creds"] is router.accounts["personal"]


def test_write_tools_not_listed(configured):
    _, srv = configured
    handler = srv.get_request_handler("tools/list").handler
    names = {t.name for t in asyncio.run(handler(None, None)).tools}
    assert "run_report" in names
    assert "create_property_annotation" not in names


def test_no_accounts_returns_error():
    _, srv = server.configure({})
    result = _call(srv, "run_report", {"property_id": "1"})
    assert result.is_error


async def _asgi(app, path, headers=()):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    called = {}

    async def inner(scope, receive, send):
        called["path"] = scope["path"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    gate = server.TokenGate(inner, TOKEN) if app is None else app
    await gate({"type": "http", "path": path, "headers": list(headers)}, receive, send)
    return sent[0]["status"], called.get("path")


def test_token_gate():
    assert asyncio.run(_asgi(None, "/health")) == (200, None)
    assert asyncio.run(_asgi(None, "/mcp"))[0] == 401
    assert asyncio.run(_asgi(None, "/mcp", [(b"authorization", b"Bearer wrong")]))[0] == 401
    assert asyncio.run(_asgi(None, "/mcp", [(b"authorization", f"Bearer {TOKEN}".encode())])) == (200, "/mcp")
    assert asyncio.run(_asgi(None, f"/{TOKEN}/mcp")) == (200, "/mcp")
    assert asyncio.run(_asgi(None, "/.well-known/oauth-protected-resource"))[0] == 404


def test_build_app_requires_long_token():
    with pytest.raises(SystemExit):
        server.build_app({"MCP_AUTH_TOKEN": "short"})
