#!/usr/bin/env python3
"""DSH worker for tech-hub v0.1.1 — 取件登记模式（分身闭嘴）。

常驻轮询 claim target=dsh 的任务：
- 门铃任务：只记录本地 inbox.txt，result completed 注明由 DSH 真身（网页会话）处理。
  本 worker 绝不回帖——headless 分身冒充真身发言是血泪教训。
- 非门铃任务：同样记录 inbox.txt，由 DSH 会话醒来后人工执行。

凭证来源（依次）：env DSH_TOKEN → <SECRETS_DIR>\dsh.token → ssh 到 hub 主机自取（见 main()）。
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

# 部署时改成你自己的 hub 地址；8791 为默认端口
HUB = "http://<HUB_HOST>:8791"
ROOM = "general"
WORKER_ID = "dsh-worker-1"
BASE_DIR = pathlib.Path(__file__).resolve().parent
INBOX = BASE_DIR / "inbox.txt"


def _request(url, token, method="GET", payload=None, timeout=45):
    headers = {"Authorization": "Bearer " + token}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Idempotency-Key"] = str(uuid.uuid4())
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("HTTP %s %s: %s" % (exc.code, url, detail[:500])) from exc
    except OSError as exc:
        raise RuntimeError("hub unavailable %s: %s" % (url, exc)) from exc
    return json.loads(body) if body else {}


def post(path, payload, token):
    return _request(HUB + path, token, method="POST", payload=payload)


def get(path, token):
    return _request(HUB + path, token, method="GET")


def claim(token, wait_seconds=20):
    return post("/claim", {
        "worker_id": WORKER_ID,
        "wait_seconds": wait_seconds,
        "filter": {"target": "dsh"},
    }, token)


def room_latest_seq(token):
    """分页走到房间真正的最新 seq（next_cursor 只反映返回窗口内最后一条）。"""
    cursor = 0
    latest = 0
    for _ in range(20):
        data = get("/rooms/%s/messages?after=%d&limit=100" % (ROOM, cursor), token)
        events = data.get("events") or []
        if not events:
            break
        latest = max((e.get("seq") or 0) for e in events)
        if len(events) < 100:
            break
        cursor = latest
    return latest


def room_messages(token, limit=30):
    try:
        latest = room_latest_seq(token)
        if latest <= 0:
            return []
        after = max(0, latest - 60)
        data = get("/rooms/%s/messages?after=%d&limit=60" % (ROOM, after), token)
        events = data.get("events") or []
        events = [e for e in events if (e.get("from") or "") != "dsh"]
        return events[-limit:]
    except Exception:
        return []


def do_task(token, task):
    task_id = task.get("task_id", "")
    request = task.get("request") or ""
    note = "门铃任务已记录本地 inbox。DSH 真身由 3080 前端注入唤醒处理；本 worker 不回帖、不冒充前端。"
    if "门铃" not in request:
        note = "DSH worker 非门铃任务已记录到本地 inbox.txt，由 DSH 会话醒来后人工执行。request: " + request[:500]
    try:
        with open(INBOX, "a", encoding="utf-8") as f:
            f.write(json.dumps({"doorbell": ("门铃" in request), "task": task}, ensure_ascii=False) + "\n")
    except Exception as exc:
        print("inbox err:", exc, flush=True)
    post("/result", {
        "worker_id": WORKER_ID, "task_id": task_id, "status": "completed",
        "result_summary": note,
    }, token)
    print("inboxed:", task_id, flush=True)


def run_once(token):
    resp = claim(token, wait_seconds=0)
    if not resp.get("task_id"):
        return False
    do_task(token, resp["task"])
    return True


def run_loop(token, poll_seconds):
    while True:
        try:
            resp = claim(token, wait_seconds=20)
            if resp.get("task_id"):
                do_task(token, resp["task"])
            else:
                time.sleep(poll_seconds)
        except Exception as exc:
            print("worker error:", exc, flush=True)
            time.sleep(poll_seconds)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll", type=int, default=15)
    args = parser.parse_args()
    token = (os.environ.get("DSH_TOKEN") or "").strip()
    if not token:
        secret_path = pathlib.Path(r"<SECRETS_DIR>\dsh.token")
        if secret_path.exists():
            token = secret_path.read_text(encoding="utf-8").strip()
    if not token:
        # 可选：ssh 到 hub 主机读取 DSH_TOKEN（需先配好本机密钥免密登录）。
        # 示例命令形态：
        #   ssh <USER>@<HUB_HOST> "grep -E '^DSH_TOKEN=' <CREDENTIALS_FILE>"
        # 值只进本进程内存，不要打印、不要落盘。
        print("DSH_TOKEN missing (env/secrets/ssh 三源均未取到)", flush=True)
        sys.exit(2)
    if args.once:
        print("claimed:", run_once(token), flush=True)
    else:
        run_loop(token, args.poll)


if __name__ == "__main__":
    main()
