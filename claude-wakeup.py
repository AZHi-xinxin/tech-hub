#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claude-wakeup.py — Claude 门铃的通道脚本（poll / send）。

只做通道两件事：按游标拉新消息、发一条消息（带幂等键）。
「读什么、回什么、怎么回」由 Claude 会话本人判断——本脚本不做任何智能。
设计要点与 WAKEUP-CLAUDE.md 对齐：游标不写进脚本，由会话上下文记住。

用法：
  # 1) 轮询：拉取 after 之后的新消息，打印到 stdout（UTF-8）
  python claude-wakeup.py poll --after 120 --token-file <凭证目录>/<身份>.token

  # 2) 回信：发一条文本（自动生成 Idempotency-Key）
  python claude-wakeup.py send --text "..." --token-file <凭证目录>/<身份>.token

可选参数：
  --base        hub 地址，默认 http://127.0.0.1:8791
  --room        房间名，默认 general
  --after       游标：只拉 seq 大于该值的消息；省略=拉最近 limit 条
  --limit       拉取条数上限，默认 50
  --to          定向收件人（hub 注册身份名；省略=广播到房间）
  --idem-key    重试时复用同一个 Idempotency-Key（UUID v4），防重复落库

依赖：仅 Python 标准库（urllib），无第三方包。
"""
import argparse
import json
import sys
import uuid
import urllib.request


def read_token(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def poll(base: str, room: str, token: str, after, limit: int) -> None:
    url = f"{base}/rooms/{room}/messages"
    if after is not None:
        url += f"?after={after}&limit={limit}"
    else:
        url += f"?limit={limit}"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    for ev in data.get("events", []):
        payload = ev.get("payload") or {}
        text = (payload.get("text") or "").replace("\n", " ")
        print(f"seq{ev.get('seq')} [{ev.get('from')}] {text}")


def send(base: str, room: str, token: str, text: str, to, idem_key) -> None:
    body_obj = {"text": text}
    if to:
        body_obj["to"] = to
    body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        # hub 要求必带；重试必须复用同一个，否则超时重试会重复落库
        "Idempotency-Key": idem_key or str(uuid.uuid4()),
    }
    req = urllib.request.Request(
        f"{base}/rooms/{room}/messages", data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    print(f"sent seq{data.get('seq')}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("action", choices=["poll", "send"])
    ap.add_argument("--base", default="http://127.0.0.1:8791")
    ap.add_argument("--room", default="general")
    ap.add_argument("--token-file", default="")
    ap.add_argument("--after", type=int, default=None)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--text", default="")
    ap.add_argument("--to", default=None)
    ap.add_argument("--idem-key", default=None)
    args = ap.parse_args()

    if not args.token_file:
        print("error: --token-file 必填（token 只存本地凭证文件，绝不进命令行/提示词/日志）", file=sys.stderr)
        return 2
    try:
        token = read_token(args.token_file)
    except OSError as exc:
        print(f"error: cannot read token file: {exc}", file=sys.stderr)
        return 2
    if not token:
        print("error: token file is empty", file=sys.stderr)
        return 2

    base = args.base.rstrip("/")
    if args.action == "poll":
        poll(base, args.room, token, args.after, args.limit)
        return 0
    if not args.text.strip():
        print("error: send 需要 --text", file=sys.stderr)
        return 2
    send(base, args.room, token, args.text, args.to, args.idem_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
