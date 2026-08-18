#!/usr/bin/env python3
"""DSH 前端真身注入器（Rikka 哨兵的本机版）。

用法: python dsh_inject.py "要注入的消息文本"
行为: POST /api/session.list 找 running=true 的 web 会话 →
      POST /api/session.prompt（type=client-request 信封, mode=queue）
      把消息写进 DSH 网页前端并触发真身一轮。
零依赖、无鉴权（localhost 信任边界）。由 TechHubSentinel 检测到新的
human/rikka 群消息时调用——这就是"前端有反馈才是真唤醒"的落地件。
"""
import json
import sys
import urllib.request
import uuid

HOST = "http://127.0.0.1:3080"


def rpc(method, payload):
    body = json.dumps({
        "type": "client-request",
        "rpcId": str(uuid.uuid4()),
        "method": method,
        "payload": payload,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        HOST + "/api/" + method, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def inject(text):
    res = rpc("session.list", {})
    items = (res.get("result") or {}).get("value", {}).get("items") or []

    def is_web_session(it):
        # headless worker 会话的 cwd 带 dsh-worker，绝不能当注入目标
        return "dsh-worker" not in (it.get("cwd") or "")

    target = None
    chosen_cwd = None
    # 优先 running=true 的 web 会话; 没有则取 updatedAt 最新且非 blank 的 web 会话
    # (页面开着但当前无运行回合时 running 全为 false, 按活跃时间选目标)
    for it in items:
        if it.get("running") is True and is_web_session(it):
            target = it["sessionId"]
            chosen_cwd = it.get("cwd")
            break
    if not target:
        live = [
            it for it in items
            if not it.get("blank") and it.get("sessionId") and is_web_session(it)
        ]
        if live:
            live.sort(key=lambda it: it.get("updatedAt") or 0, reverse=True)
            target = live[0]["sessionId"]
            chosen_cwd = live[0].get("cwd")
    if not target:
        return {"ok": False, "error": "no running session"}
    res2 = rpc("session.prompt", {
        "sessionId": target,
        "mode": "queue",
        "content": [{"type": "text", "text": text}],
        "clientTimeZone": "Asia/Shanghai",
    })
    return {"ok": True, "sessionId": target, "cwd": chosen_cwd, "response": res2}


if __name__ == "__main__":
    msg = sys.argv[1] if len(sys.argv) > 1 else "检测到群聊中有未读消息"
    out = inject(msg)
    print(json.dumps(out, ensure_ascii=False))
