import json
import pathlib
import tempfile
import unittest
import uuid
from unittest.mock import patch

from codex_worker import (
    WorkerConfig,
    acquire_instance_lock,
    build_prompt,
    doorbell_prompt,
    handle_task,
    read_room_cursor,
    release_instance_lock,
    sandbox_for,
    write_room_cursor,
)


class WorkerTests(unittest.TestCase):
    def test_single_instance_lock(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "worker.lock"
            first = acquire_instance_lock(path)
            self.assertIsNotNone(first)
            self.assertIsNone(acquire_instance_lock(path))
            release_instance_lock(first)
            second = acquire_instance_lock(path)
            self.assertIsNotNone(second)
            release_instance_lock(second)
    def test_sandbox_mapping(self):
        self.assertEqual(sandbox_for("read_only"), "read-only")
        self.assertEqual(sandbox_for("workspace_edit"), "workspace-write")
        with self.assertRaises(ValueError):
            sandbox_for("dangerous")

    def test_prompt_scope(self):
        prompt = build_prompt({"task_id": "t1", "type": "test", "request": "Run unit tests"})
        self.assertIn("Run unit tests", prompt)
        self.assertIn("Do not expand scope", prompt)

    def test_doorbell_prompt_is_short_and_counts_only_relevant_messages(self):
        prompt = doorbell_prompt([
            {"seq": 10, "from": "codex", "payload": {"text": "echo"}},
            {"seq": 11, "from": "human", "payload": {"text": "真实消息"}},
            {"seq": 12, "from": "rikka", "payload": {"text": "手机反馈"}},
            {"seq": 13, "from": "rikka", "to": "claude", "payload": {"text": "只给Claude"}},
            {"seq": 14, "from": "rikka", "to": "codex", "payload": {"text": "只给Codex"}},
        ])
        self.assertEqual(prompt, "检测到群聊中有 3 条未读消息。请先去 tech-hub general 领取任务。\n")
        self.assertNotIn("echo", prompt)
        self.assertNotIn("真实消息", prompt)
        self.assertNotIn("手机反馈", prompt)
        self.assertNotIn("只给Claude", prompt)
        self.assertNotIn("只给Codex", prompt)
        self.assertNotIn("existing frontend conversation", prompt)
        self.assertNotIn("seq 11", prompt)

    def test_room_cursor_is_monotonic(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "cursor.json"
            self.assertEqual(read_room_cursor(path, "general"), 0)
            write_room_cursor(path, "general", 12)
            write_room_cursor(path, "general", 8)
            self.assertEqual(read_room_cursor(path, "general"), 12)

    def test_doorbell_dry_run_relays_model_result_and_advances_cursor(self):
        class FakeClient:
            def __init__(self):
                self.chats = []
                self.results = []

            def room_messages(self, room, after):
                return {
                    "events": [{"seq": 42, "from": "human", "payload": {"text": "请本人处理"}}],
                    "next_cursor": 42,
                }

            def event(self, *args, **kwargs):
                return {}

            def heartbeat(self, *args, **kwargs):
                return {}

            def chat(self, room, text, idem):
                self.chats.append((room, text, idem))
                return {}

            def result(self, payload):
                self.results.append(payload)
                return {}

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cursor = root / "cursor.json"
            config = WorkerConfig(
                hub_url="http://example.invalid",
                worker_id="codex-worker-1",
                workspaces={"tech-hub": root},
                dry_run=True,
                ui_injector_command=root / "inject.ps1",
                room_cursor_path=cursor,
            )
            (root / "inject.ps1").write_text("# test", encoding="utf-8")
            client = FakeClient()
            task_id = str(uuid.uuid4())
            handle_task(config, client, {
                "task_id": task_id,
                "task": {
                    "task_id": task_id,
                    "project": "tech-hub",
                    "room": "general",
                    "risk_level": "read_only",
                    "request": "【门铃】general 有新消息",
                },
            })
            self.assertEqual(read_room_cursor(cursor, "general"), 42)
            self.assertEqual(client.chats, [])
            self.assertIn("DRY_RUN", client.results[0]["result_summary"])
            self.assertNotIn("收到，正在处理", client.results[0]["result_summary"])
            self.assertEqual(client.results[0]["status"], "completed")

    def test_doorbell_injection_failure_does_not_advance_cursor(self):
        class FakeClient:
            def __init__(self):
                self.results = []

            def room_messages(self, room, after):
                return {
                    "events": [{"seq": 52, "from": "human", "payload": {"text": "请本人处理"}}],
                    "next_cursor": 52,
                }

            def event(self, *args, **kwargs):
                return {}

            def heartbeat(self, *args, **kwargs):
                return {}

            def result(self, payload):
                self.results.append(payload)
                return {}

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cursor = root / "cursor.json"
            injector = root / "inject.ps1"
            injector.write_text("# test", encoding="utf-8")
            config = WorkerConfig(
                hub_url="http://example.invalid",
                worker_id="codex-worker-1",
                workspaces={"tech-hub": root},
                dry_run=False,
                ui_injector_command=injector,
                room_cursor_path=cursor,
            )
            client = FakeClient()
            task_id = str(uuid.uuid4())
            with patch("codex_worker.inject_codex_ui", return_value=(2, "injection failed", [])):
                handle_task(config, client, {
                    "task_id": task_id,
                    "task": {
                        "task_id": task_id,
                        "project": "tech-hub",
                        "room": "general",
                        "risk_level": "read_only",
                        "request": "【门铃】general 有新消息",
                    },
                })
            self.assertEqual(read_room_cursor(cursor, "general"), 0)
            self.assertEqual(client.results[0]["status"], "failed")

    def test_doorbell_with_only_irrelevant_events_advances_cursor_without_injection(self):
        class FakeClient:
            def __init__(self):
                self.results = []

            def room_messages(self, room, after):
                return {
                    "events": [{"seq": 62, "from": "claude", "payload": {"text": "无关回执"}}],
                    "next_cursor": 62,
                }

            def result(self, payload):
                self.results.append(payload)
                return {}

        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cursor = root / "cursor.json"
            injector = root / "inject.ps1"
            injector.write_text("# test", encoding="utf-8")
            config = WorkerConfig(
                hub_url="http://example.invalid",
                worker_id="codex-worker-1",
                workspaces={"tech-hub": root},
                dry_run=False,
                ui_injector_command=injector,
                room_cursor_path=cursor,
            )
            client = FakeClient()
            task_id = str(uuid.uuid4())
            with patch("codex_worker.inject_codex_ui") as inject:
                handle_task(config, client, {
                    "task_id": task_id,
                    "task": {
                        "task_id": task_id,
                        "project": "tech-hub",
                        "room": "general",
                        "risk_level": "read_only",
                        "request": "【门铃】general 有新消息",
                    },
                })
            inject.assert_not_called()
            self.assertEqual(read_room_cursor(cursor, "general"), 62)
            self.assertEqual(client.results[0]["status"], "completed")

    def test_config_requires_codex_worker_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = root / "config.json"
            cfg.write_text(json.dumps({"worker_id": "dsh-worker-1", "workspaces": {"x": td}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                WorkerConfig.load(cfg)


if __name__ == "__main__":
    unittest.main()
