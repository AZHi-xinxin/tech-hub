#!/usr/bin/env python3
"""Authenticated MCP adapter for the tech-hub HTTP API.

The adapter deliberately keeps two credentials separate:

* TECHHUB_TOKEN authenticates this service to tech-hub as the Rikka identity.
* TECHHUB_MCP_TOKEN authenticates RikkaHub (or another MCP client) to this service.

Neither credential is returned by a tool or written to normal logs.
"""

from __future__ import annotations

import hmac
import os
import re
import uuid
from typing import Any, Literal

import httpx
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
IDENTITIES = {"human", "claude", "codex", "dsh", "rikka"}
TASK_TYPES = {"analyze", "fix", "build", "deploy", "test", "query", "other"}
RISK_LEVELS = {"read_only", "workspace_edit", "dangerous"}
TARGETS = {"codex", "dsh", "claude", "auto"}


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


HUB_BASE_URL = os.environ.get("TECHHUB_BASE_URL", "http://127.0.0.1:8791").rstrip("/")
HUB_TOKEN = _required_env("TECHHUB_TOKEN")
MCP_TOKEN = _required_env("TECHHUB_MCP_TOKEN")
MCP_HOST = os.environ.get("TECHHUB_MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("TECHHUB_MCP_PORT", "8793"))
MCP_ISSUER_URL = os.environ.get(
    "TECHHUB_MCP_ISSUER_URL",
    f"http://127.0.0.1:{MCP_PORT}",
)
MCP_RESOURCE_URL = os.environ.get(
    "TECHHUB_MCP_RESOURCE_URL",
    f"http://127.0.0.1:{MCP_PORT}/mcp",
)
ALLOWED_HOSTS = _csv_env(
    "TECHHUB_MCP_ALLOWED_HOSTS",
    "127.0.0.1:*,localhost:*",
)
ALLOWED_ORIGINS = _csv_env("TECHHUB_MCP_ALLOWED_ORIGINS")

if len(MCP_TOKEN) < 32:
    raise RuntimeError("TECHHUB_MCP_TOKEN must contain at least 32 characters")
if hmac.compare_digest(HUB_TOKEN, MCP_TOKEN):
    raise RuntimeError("TECHHUB_TOKEN and TECHHUB_MCP_TOKEN must be different")
if not 1 <= MCP_PORT <= 65535:
    raise RuntimeError("TECHHUB_MCP_PORT must be a valid TCP port")


class StaticBearerVerifier:
    """Validate the adapter's single bearer credential in constant time."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, MCP_TOKEN):
            return None
        return AccessToken(
            token=token,
            client_id="rikkahub-tech-hub",
            scopes=["techhub:rikka"],
            subject="rikka",
        )


transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=ALLOWED_HOSTS,
    allowed_origins=ALLOWED_ORIGINS,
)

auth_settings = AuthSettings(
    issuer_url=MCP_ISSUER_URL,
    resource_server_url=MCP_RESOURCE_URL,
    required_scopes=["techhub:rikka"],
)

mcp = FastMCP(
    "TechHubRikka",
    instructions=(
        "Use read_messages for the general-room timeline and carry next_cursor into the next call. "
        "Writes are performed with the Rikka tech-hub identity. This adapter intentionally does not "
        "expose human approvals, history folding, worker claims, or administrative endpoints."
    ),
    token_verifier=StaticBearerVerifier(),
    auth=auth_settings,
    host=MCP_HOST,
    port=MCP_PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=transport_security,
)


def _validate_room(room: str) -> str:
    if not isinstance(room, str) or not ROOM_RE.fullmatch(room):
        raise ValueError("room must match [A-Za-z0-9_-]{1,32}")
    return room


def _clean_task_id(task_id: str) -> str:
    try:
        return str(uuid.UUID(task_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("task_id must be a UUID") from exc


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    code = str(payload.get("code") or "http_error")
    message = str(payload.get("message") or payload.get("detail") or response.reason_phrase)
    message = BEARER_RE.sub(r"\1<redacted>", message)
    return f"tech-hub request failed ({response.status_code}, {code}): {message[:300]}"


async def _hub_request(
    method: Literal["GET", "POST"],
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {HUB_TOKEN}",
        "Accept": "application/json",
    }
    if method == "POST":
        headers["Idempotency-Key"] = str(uuid.uuid4())
        headers["Content-Type"] = "application/json; charset=utf-8"
    timeout = httpx.Timeout(35.0, connect=5.0)
    try:
        async with httpx.AsyncClient(
            base_url=HUB_BASE_URL,
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
        ) as client:
            response = await client.request(method, path, params=params, json=body)
    except httpx.RequestError as exc:
        raise RuntimeError(f"tech-hub is unavailable ({type(exc).__name__})") from exc
    if response.is_error:
        raise RuntimeError(_error_message(response))
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("tech-hub returned non-JSON data") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("tech-hub returned an unexpected JSON shape")
    return payload


@mcp.tool()
async def hub_health() -> dict[str, Any]:
    """Check whether tech-hub is online and return its version and UTC time."""
    return await _hub_request("GET", "/health")


@mcp.tool()
async def list_rooms() -> dict[str, Any]:
    """List tech-hub rooms and each room's latest chat sequence number."""
    return await _hub_request("GET", "/rooms")


@mcp.tool()
async def read_messages(
    room: str = "general",
    after_seq: int = 0,
    limit: int = 50,
    wait_seconds: int = 0,
    include_folded: bool = False,
) -> dict[str, Any]:
    """Read a room timeline incrementally.

    Start with after_seq=0. For the next call, pass the returned next_cursor so
    old messages are not reread. By default, history before the active fold is
    omitted. Set include_folded only when an older record is genuinely needed.
    """
    room = _validate_room(room)
    if not isinstance(after_seq, int) or not 0 <= after_seq <= 10**12:
        raise ValueError("after_seq must be an integer from 0 to 10^12")
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    if not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= 30:
        raise ValueError("wait_seconds must be an integer from 0 to 30")
    return await _hub_request(
        "GET",
        f"/rooms/{room}/messages",
        params={
            "after": after_seq,
            "limit": limit,
            "wait_seconds": wait_seconds,
            "ignore_fold": 1 if include_folded else 0,
        },
    )


@mcp.tool()
async def send_message(
    text: str,
    room: str = "general",
    recipients: list[str] | None = None,
    reply_to: str | None = None,
) -> dict[str, Any]:
    """Send a chat message as Rikka to a tech-hub room.

    Omit recipients to post to the whole room. Otherwise use one or more of:
    human, claude, codex, dsh, rikka. An idempotency key is generated internally.
    """
    room = _validate_room(room)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must not be empty")
    if len(text) > 4000:
        raise ValueError("text must not exceed 4000 characters")
    if recipients is not None:
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= 5:
            raise ValueError("recipients must contain between 1 and 5 identities")
        bad = [item for item in recipients if item not in IDENTITIES]
        if bad:
            raise ValueError(f"unknown recipient: {bad[0]}")
        recipients = sorted(set(recipients))
    body: dict[str, Any] = {"text": text.strip()}
    if recipients:
        body["to"] = recipients
    if reply_to:
        body["reply_to"] = reply_to
    return await _hub_request("POST", f"/rooms/{room}/messages", body=body)


@mcp.tool()
async def create_task(
    request: str,
    target: Literal["codex", "dsh", "claude", "auto"] = "auto",
    task_type: Literal["analyze", "fix", "build", "deploy", "test", "query", "other"] = "query",
    risk_level: Literal["read_only", "workspace_edit", "dangerous"] = "read_only",
    room: str = "general",
    project: str | None = "tech-hub",
) -> dict[str, Any]:
    """Create a tech-hub task as Rikka.

    Dangerous work is not automatically approved; tech-hub's normal human
    approval and worker safety rules still apply.
    """
    room = _validate_room(room)
    if not isinstance(request, str) or not request.strip():
        raise ValueError("request must not be empty")
    if len(request) > 4000:
        raise ValueError("request must not exceed 4000 characters")
    if target not in TARGETS or task_type not in TASK_TYPES or risk_level not in RISK_LEVELS:
        raise ValueError("invalid target, task_type, or risk_level")
    body: dict[str, Any] = {
        "request": request.strip(),
        "target": target,
        "type": task_type,
        "risk_level": risk_level,
        "room": room,
    }
    if project:
        body["project"] = project
    return await _hub_request("POST", "/task", body=body)


@mcp.tool()
async def get_task(
    task_id: str,
    include_events: bool = True,
    after_seq: int = 0,
    event_limit: int = 50,
) -> dict[str, Any]:
    """Read a task created by Rikka, optionally including incremental events."""
    task_id = _clean_task_id(task_id)
    if not isinstance(after_seq, int) or after_seq < 0:
        raise ValueError("after_seq must be a non-negative integer")
    if not isinstance(event_limit, int) or not 1 <= event_limit <= 200:
        raise ValueError("event_limit must be an integer from 1 to 200")
    result = await _hub_request("GET", f"/task/{task_id}")
    if include_events:
        result["event_page"] = await _hub_request(
            "GET",
            f"/task/{task_id}/events",
            params={"after": after_seq, "limit": event_limit, "exclude_logs": "true"},
        )
    return result


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
