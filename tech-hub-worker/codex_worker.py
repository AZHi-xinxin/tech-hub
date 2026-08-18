#!/usr/bin/env python3
"""Windows Codex worker for tech-hub v0.1.1.

The worker is outbound-only. It claims allow-listed tasks, keeps the lease alive,
runs Codex CLI with a risk-appropriate sandbox, and writes a compact result back.
Secrets are read from environment variables and are never logged.
"""

from __future__ import annotations

import argparse
import json
import msvcrt
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any


TERMINAL = {"completed", "failed", "cancelled"}


class HubError(RuntimeError):
    pass


def acquire_instance_lock(path: pathlib.Path):
    """Keep exactly one live worker, even if its PowerShell parent is killed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def release_instance_lock(handle) -> None:
    if handle is None:
        return
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()

class HubClient:
    def __init__(self, base_url: str, token: str, timeout: int = 45) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def post(self, path: str, payload: dict[str, Any], idem: str | None = None) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": idem or str(uuid.uuid4()),
        }
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HubError(f"HTTP {exc.code} {path}: {detail[:1000]}") from exc
        except OSError as exc:
            raise HubError(f"hub unavailable at {path}: {exc}") from exc
        return json.loads(body) if body else {}

    def get(self, path: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"}
        req = urllib.request.Request(self.base_url + path, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise HubError(f"HTTP {exc.code} {path}: {detail[:1000]}") from exc
        except OSError as exc:
            raise HubError(f"hub unavailable at {path}: {exc}") from exc
        return json.loads(body) if body else {}

    def claim(self, worker_id: str, wait_seconds: int) -> dict[str, Any]:
        return self.post(
            "/claim",
            {"worker_id": worker_id, "wait_seconds": wait_seconds, "filter": {"target": "codex"}},
        )

    def heartbeat(self, worker_id: str, task_id: str) -> dict[str, Any]:
        return self.post("/heartbeat", {"worker_id": worker_id, "task_id": task_id})

    def event(self, task_id: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post(f"/task/{task_id}/events", {"kind": kind, "payload": payload})

    def chat(self, room: str, text: str, idem: str) -> dict[str, Any]:
        return self.post(f"/rooms/{room}/messages", {"text": text}, idem=idem)

    def room_messages(self, room: str, after: int, limit: int = 50) -> dict[str, Any]:
        room_name = urllib.parse.quote(room, safe="")
        query = urllib.parse.urlencode({"after": after, "limit": limit, "wait_seconds": 0})
        return self.get(f"/rooms/{room_name}/messages?{query}")

    def result(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/result", payload)


@dataclass(frozen=True)
class WorkerConfig:
    hub_url: str
    worker_id: str
    workspaces: dict[str, pathlib.Path]
    poll_seconds: int = 25
    heartbeat_seconds: int = 240
    codex_command: str = "codex"
    dry_run: bool = False
    ui_injector_command: pathlib.Path | None = None
    room_cursor_path: pathlib.Path | None = None

    @classmethod
    def load(cls, path: pathlib.Path, dry_run: bool = False) -> "WorkerConfig":
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        workspaces: dict[str, pathlib.Path] = {}
        for workspace_id, value in raw.get("workspaces", {}).items():
            resolved = pathlib.Path(value).expanduser().resolve()
            if not resolved.is_dir():
                raise ValueError(f"workspace does not exist: {workspace_id} -> {resolved}")
            workspaces[workspace_id] = resolved
        if not workspaces:
            raise ValueError("at least one workspace_id mapping is required")
        worker_id = raw.get("worker_id", "codex-worker-1")
        if not worker_id.startswith("codex-worker-"):
            raise ValueError("worker_id must start with codex-worker-")
        return cls(
            hub_url=raw.get("hub_url", "http://127.0.0.1:8791"),
            worker_id=worker_id,
            workspaces=workspaces,
            poll_seconds=max(0, min(30, int(raw.get("poll_seconds", 25)))),
            heartbeat_seconds=max(30, int(raw.get("heartbeat_seconds", 240))),
            codex_command=raw.get("codex_command", "codex"),
            dry_run=dry_run or bool(raw.get("dry_run", False)),
            ui_injector_command=(
                pathlib.Path(raw["ui_injector_command"]).expanduser().resolve()
                if raw.get("ui_injector_command")
                else None
            ),
            room_cursor_path=(
                pathlib.Path(raw["room_cursor_path"]).expanduser().resolve()
                if raw.get("room_cursor_path")
                else None
            ),
        )


class Heartbeat:
    def __init__(self, client: HubClient, worker_id: str, task_id: str, interval: int) -> None:
        self.client = client
        self.worker_id = worker_id
        self.task_id = task_id
        self.interval = interval
        self.stop_event = threading.Event()
        self.errors: queue.Queue[str] = queue.Queue()
        self.thread = threading.Thread(target=self._run, name=f"heartbeat-{task_id}", daemon=True)

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.client.heartbeat(self.worker_id, self.task_id)
            except Exception as exc:  # heartbeat failure is reported after the subprocess exits
                self.errors.put(str(exc))

    def __enter__(self) -> "Heartbeat":
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)


def sandbox_for(risk_level: str) -> str:
    if risk_level == "read_only":
        return "read-only"
    if risk_level == "workspace_edit":
        return "workspace-write"
    raise ValueError("dangerous tasks require human approval and are not auto-executed by v0.1")


def build_prompt(task: dict[str, Any]) -> str:
    request = str(task.get("request", "")).strip()
    if not request:
        raise ValueError("task request is empty")
    if len(request) > 4000:
        raise ValueError("task request exceeds 4000 characters")
    return (
        "You are executing a tech-hub task. Stay strictly within the supplied request and workspace.\n"
        "Do not expand scope, access credentials, publish, deploy, message third parties, control physical "
        "devices, or perform destructive actions. If any such action is needed, stop and report that human "
        "approval is required. Verify proportionally and end with a concise result summary.\n\n"
        f"Task ID: {task.get('task_id', '<unknown>')}\n"
        f"Task type: {task.get('type', 'other')}\n"
        f"Request:\n{request}\n"
    )


def read_room_cursor(path: pathlib.Path | None, room: str) -> int:
    if path is None or not path.exists():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        return max(0, int(raw.get(room, 0)))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def write_room_cursor(path: pathlib.Path | None, room: str, seq: int) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raw = {}
    raw[room] = max(int(raw.get(room, 0) or 0), int(seq))
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def doorbell_event_relevant(event: dict[str, Any]) -> bool:
    sender = str(event.get("from") or "").strip().lower()
    payload = event.get("payload") or {}
    if sender not in {"human", "rikka"} or not str(payload.get("text") or "").strip():
        return False
    target = event.get("to", payload.get("to"))
    if target in (None, ""):
        return True
    targets = target if isinstance(target, (list, tuple, set)) else [target]
    return any(str(item).strip().lower() in {"codex", "all", "*"} for item in targets)


def doorbell_prompt(events: list[dict[str, Any]]) -> str:
    count = sum(doorbell_event_relevant(event) for event in events)
    if not count:
        raise ValueError("doorbell contained no unread human/rikka messages")
    return f"检测到群聊中有 {count} 条未读消息。请先去 tech-hub general 领取任务。\n"

def inject_codex_ui(config: WorkerConfig, prompt: str) -> tuple[int, str, list[str]]:
    script = config.ui_injector_command
    if script is None or not script.is_file():
        return 2, "VS Codex UI injector is not configured", []
    if config.dry_run:
        return 0, "DRY_RUN: would inject unread messages into the live VS Codex composer", []
    with tempfile.TemporaryDirectory(prefix="techhub-codex-ui-") as temp_dir:
        message_file = pathlib.Path(temp_dir) / "message.txt"
        message_file.write_text(prompt, encoding="utf-8")
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-MessageFile",
            str(message_file),
        ]
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return 124, "VS Codex UI injection timed out", []
        logs = [line[:2000] for line in process.stderr.splitlines()[-5:] if line.strip()]
        summary = process.stdout.strip() or (
            "VS Codex UI injection completed" if process.returncode == 0 else "VS Codex UI injection failed"
        )
        return process.returncode, summary[:4000], logs


def execute_codex(config: WorkerConfig, task: dict[str, Any], workspace: pathlib.Path) -> tuple[int, str, list[str]]:
    risk = task.get("risk_level") or "read_only"
    sandbox = sandbox_for(risk)
    prompt = build_prompt(task)
    budget = task.get("budget") or {}
    timeout_seconds = max(60, min(24 * 3600, int(budget.get("max_duration_min", 60)) * 60))
    if config.dry_run:
        return 0, f"DRY_RUN: would execute Codex in {workspace} with sandbox={sandbox}", []

    with tempfile.TemporaryDirectory(prefix="techhub-codex-") as temp_dir:
        last_message = pathlib.Path(temp_dir) / "last-message.txt"
        command = [
            config.codex_command,
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            sandbox,
            "--skip-git-repo-check",
            "--cd",
            str(workspace),
            "--output-last-message",
            str(last_message),
            "-",
        ]
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, stderr = process.communicate(prompt, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            return 124, "Codex execution timed out", [stderr[-2000:]] if stderr else []

        summary = last_message.read_text(encoding="utf-8", errors="replace").strip() if last_message.exists() else ""
        if not summary:
            summary = "Codex completed without a final message" if process.returncode == 0 else "Codex execution failed"
        fatal_host_error = "codex-code-mode-host" in (stdout + stderr + summary).lower() and "os error 2" in (stdout + stderr + summary).lower()
        if fatal_host_error and process.returncode == 0:
            process.returncode = 1
        logs = [line[:2000] for line in stderr.splitlines()[-5:] if line.strip()]
        if process.returncode and stdout:
            logs.append(stdout[-2000:])
        return process.returncode or 0, summary[:4000], logs

def handle_task(config: WorkerConfig, client: HubClient, claim: dict[str, Any]) -> None:
    task = claim.get("task") or {}
    task_id = claim.get("task_id") or task.get("task_id")
    if not task_id:
        raise ValueError("claimed response has no task_id")
    project = task.get("project")
    workspace = config.workspaces.get(project)
    if workspace is None:
        client.result({
            "worker_id": config.worker_id,
            "task_id": task_id,
            "status": "needs_human",
            "result_summary": f"Unknown workspace_id: {project!r}",
            "artifacts": [],
            "cost_tokens": 0,
        })
        return
    if task.get("risk_level") == "dangerous":
        client.event(task_id, "approval_request", {
            "op_summary": "Task is marked dangerous; Codex worker refused automatic execution",
            "op_scope": [project],
            "risk_level": "dangerous",
            "expires_at": None,
            "one_shot": True,
        })
        client.result({
            "worker_id": config.worker_id,
            "task_id": task_id,
            "status": "waiting_approval",
            "result_summary": "Human approval and a dedicated execution path are required.",
            "artifacts": [],
            "cost_tokens": 0,
        })
        return

    request = str(task.get("request") or "").lstrip()
    is_doorbell = request.startswith("\u3010\u95e8\u94c3\u3011")
    room = str(task.get("room") or "general")
    if is_doorbell:
        if config.ui_injector_command is None or config.room_cursor_path is None:
            raise ValueError("doorbell requires ui_injector_command and room_cursor_path")
        current_cursor = read_room_cursor(config.room_cursor_path, room)
        unread = client.room_messages(room, current_cursor)
        events = list(unread.get("events") or [])
        doorbell_cursor = max(
            [current_cursor, int(unread.get("next_cursor") or current_cursor)]
            + [int(event.get("seq") or 0) for event in events]
        )
        relevant = [event for event in events if doorbell_event_relevant(event)]
        if not relevant:
            write_room_cursor(config.room_cursor_path, room, doorbell_cursor)
            client.result({
                "worker_id": config.worker_id,
                "task_id": task_id,
                "status": "completed",
                "result_summary": "Doorbell had no unread human/Rikka messages; nothing was injected.",
                "artifacts": [],
                "cost_tokens": 0,
            })
            return
        prompt = doorbell_prompt(relevant)
        client.event(task_id, "log", {
            "worker_id": config.worker_id,
            "level": "info",
            "text": "Live VS Codex UI injection started",
        })
        with Heartbeat(client, config.worker_id, task_id, config.heartbeat_seconds) as heartbeat:
            code, summary, logs = inject_codex_ui(config, prompt)
        for line in logs[:5]:
            client.event(task_id, "log", {
                "worker_id": config.worker_id,
                "level": "error" if code else "info",
                "text": line,
            })
        if not heartbeat.errors.empty():
            client.event(task_id, "log", {
                "worker_id": config.worker_id,
                "level": "warn",
                "text": heartbeat.errors.get()[:2000],
            })
        if code == 0:
            write_room_cursor(config.room_cursor_path, room, doorbell_cursor)
        client.result({
            "worker_id": config.worker_id,
            "task_id": task_id,
            "status": "completed" if code == 0 else "failed",
            "result_summary": summary,
            "artifacts": [],
            "cost_tokens": 0,
        })
        return

    client.event(task_id, "log", {"worker_id": config.worker_id, "level": "info", "text": "Codex execution started"})
    with Heartbeat(client, config.worker_id, task_id, config.heartbeat_seconds) as heartbeat:
        code, summary, logs = execute_codex(config, task, workspace)
    for line in logs[:5]:
        client.event(task_id, "log", {"worker_id": config.worker_id, "level": "error" if code else "info", "text": line})
    if not heartbeat.errors.empty():
        client.event(task_id, "log", {"worker_id": config.worker_id, "level": "warn", "text": heartbeat.errors.get()[:2000]})
    client.result({
        "worker_id": config.worker_id,
        "task_id": task_id,
        "status": "completed" if code == 0 else "failed",
        "result_summary": summary,
        "artifacts": [],
        "cost_tokens": 0,
    })

def run(config: WorkerConfig, once: bool = False) -> int:
    token = os.environ.get("TECH_HUB_CODEX_TOKEN", "").strip()
    if not token:
        print("TECH_HUB_CODEX_TOKEN is not set", file=sys.stderr)
        return 2
    client = HubClient(config.hub_url, token, timeout=config.poll_seconds + 20)
    while True:
        try:
            claim = client.claim(config.worker_id, config.poll_seconds)
            if claim.get("task_id") is not None:
                handle_task(config, client, claim)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"worker error: {exc}", file=sys.stderr)
            if once:
                return 1
            time.sleep(5)
        if once:
            return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=pathlib.Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    lock = acquire_instance_lock(args.config.resolve().with_name("codex-worker.lock"))
    if lock is None:
        print("another Codex worker is already running", file=sys.stderr)
        return 0
    try:
        return run(WorkerConfig.load(args.config, dry_run=args.dry_run), once=args.once)
    finally:
        release_instance_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
