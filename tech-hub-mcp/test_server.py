"""Unit tests for the tech-hub MCP adapter.

The tests use dummy credentials and replace the backend request function, so
they never contact a real hub and never need production secrets.
"""

from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx


os.environ.setdefault("TECHHUB_TOKEN", "h" * 48)
os.environ.setdefault("TECHHUB_MCP_TOKEN", "m" * 48)
os.environ.setdefault("TECHHUB_MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*")

import server  # noqa: E402  (environment must be populated before import)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_bearer_verifier_accepts_only_adapter_token(self) -> None:
        verifier = server.StaticBearerVerifier()
        accepted = await verifier.verify_token("m" * 48)
        rejected = await verifier.verify_token("wrong")
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted.subject, "rikka")
        self.assertIsNone(rejected)

    async def test_read_messages_maps_incremental_cursor_and_fold_flag(self) -> None:
        backend = AsyncMock(return_value={"messages": [], "next_cursor": 88})
        with patch.object(server, "_hub_request", backend):
            result = await server.read_messages(
                room="general",
                after_seq=42,
                limit=25,
                wait_seconds=3,
                include_folded=True,
            )
        self.assertEqual(result["next_cursor"], 88)
        backend.assert_awaited_once_with(
            "GET",
            "/rooms/general/messages",
            params={
                "after": 42,
                "limit": 25,
                "wait_seconds": 3,
                "ignore_fold": 1,
            },
        )

    async def test_send_message_deduplicates_recipients(self) -> None:
        backend = AsyncMock(return_value={"seq": 123})
        with patch.object(server, "_hub_request", backend):
            result = await server.send_message(
                text="  验收消息  ",
                recipients=["codex", "claude", "codex"],
                reply_to="msg-1",
            )
        self.assertEqual(result, {"seq": 123})
        backend.assert_awaited_once_with(
            "POST",
            "/rooms/general/messages",
            body={
                "text": "验收消息",
                "to": ["claude", "codex"],
                "reply_to": "msg-1",
            },
        )

    async def test_create_task_keeps_safety_fields(self) -> None:
        backend = AsyncMock(return_value={"task_id": str(uuid.uuid4())})
        with patch.object(server, "_hub_request", backend):
            await server.create_task(
                request="检查服务，不执行修改",
                target="codex",
                task_type="query",
                risk_level="read_only",
                project="tech-hub",
            )
        backend.assert_awaited_once_with(
            "POST",
            "/task",
            body={
                "request": "检查服务，不执行修改",
                "target": "codex",
                "type": "query",
                "risk_level": "read_only",
                "room": "general",
                "project": "tech-hub",
            },
        )

    async def test_get_task_can_include_incremental_events(self) -> None:
        task_id = str(uuid.uuid4())
        backend = AsyncMock(
            side_effect=[
                {"task_id": task_id, "status": "running"},
                {"events": [], "next_cursor": 9},
            ]
        )
        with patch.object(server, "_hub_request", backend):
            result = await server.get_task(
                task_id,
                include_events=True,
                after_seq=4,
                event_limit=20,
            )
        self.assertEqual(result["event_page"]["next_cursor"], 9)
        self.assertEqual(backend.await_count, 2)
        backend.assert_any_await("GET", f"/task/{task_id}")
        backend.assert_any_await(
            "GET",
            f"/task/{task_id}/events",
            params={"after": 4, "limit": 20, "exclude_logs": "true"},
        )

    async def test_invalid_room_and_task_id_are_rejected_before_backend(self) -> None:
        backend = AsyncMock()
        with patch.object(server, "_hub_request", backend):
            with self.assertRaises(ValueError):
                await server.read_messages(room="../admin")
            with self.assertRaises(ValueError):
                await server.get_task("not-a-uuid")
        backend.assert_not_awaited()

    def test_error_message_redacts_bearer_token(self) -> None:
        secret = "super-secret-token-value"
        response = httpx.Response(
            401,
            json={
                "code": "unauthorized",
                "message": f"bad Authorization: Bearer {secret}",
            },
        )
        message = server._error_message(response)
        self.assertNotIn(secret, message)
        self.assertIn("Bearer <redacted>", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
