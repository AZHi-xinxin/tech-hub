#!/usr/bin/env python3
"""tech-hub v0.1.1 — 多 AI 协作总线与任务账本

部署: 任意常驻机器(家庭服务器/云主机), 端口默认 8791, SQLite(WAL) 持久化
运行: cron 看门狗 source credentials.env 后 python3 hub.py(见 README.md)
凭证: 环境变量 <IDENTITY>_TOKEN 形式(如 HUMAN_TOKEN), 只存 credentials.env(600),
      日志/事件不落 Authorization 头与明文凭证。
"""
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from urllib.parse import quote
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TECH_HUB_DB", os.path.join(BASE_DIR, "techhub.db"))
PORT = int(os.environ.get("TECH_HUB_PORT", "8791"))
ATTACH_DIR = os.environ.get("TECH_HUB_ATTACH_DIR", os.path.join(BASE_DIR, "data", "attachments"))
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 30 * 1024 * 1024
os.makedirs(ATTACH_DIR, exist_ok=True)
VERSION = "0.1.1"

LEASE_MIN = 10                      # 租约默认 10 分钟
APPROVAL_WINDOW_MIN = 30            # 审批一次批准 30 分钟有效
IDEM_TTL_H = 24                     # 幂等键 24 小时
SWEEP_INTERVAL_S = 30               # 后台清扫周期
IDENTITIES = tuple(i.strip() for i in os.environ.get(
    "TECH_HUB_IDENTITIES", "human,claude,codex,dsh,rikka").split(",") if i.strip())
WORKER_IDENTITIES = tuple(i.strip() for i in os.environ.get(
    "TECH_HUB_WORKER_IDENTITIES", "claude,codex,dsh").split(",") if i.strip())
WORKERS = {"%s-worker-1" % i: i for i in WORKER_IDENTITIES}
SYSTEM_IDENTITY = os.environ.get("TECH_HUB_SYSTEM_IDENTITY", "claude").strip()
SENTINEL_STOP_PHRASE = os.environ.get("TECH_HUB_STOP_PHRASE", "本次任务已结束")
TERMINAL = {"completed", "failed", "cancelled"}
TRANSITIONS = {
    "queued": {"running", "cancelled"},
    "running": {"waiting_input", "waiting_approval", "completed", "failed", "cancelled", "needs_human"},
    "waiting_input": {"running", "cancelled"},
    "waiting_approval": {"running", "failed", "cancelled"},
    "needs_human": {"running", "cancelled"},
    "completed": set(), "failed": set(), "cancelled": set(),
}
DEFAULT_BUDGET = {"max_events": 50, "max_duration_min": 60, "max_retries": 3}
MAX_SIZES = {"request": 4000, "chat": 4000, "summary": 4000, "log": 2000,
             "artifact": 8000, "event_payload": 16384, "task_payload": 262144}
TASK_TYPES = {"analyze", "fix", "build", "deploy", "test", "query", "other"}
RISK_LEVELS = {"read_only", "workspace_edit", "dangerous"}
TARGETS = {"codex", "dsh", "claude", "auto"}
ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# 凭证: 身份 token -> identity（不含 *_OLD 过渡变量）
TOKENS = {}
for _name in IDENTITIES:
    _v = os.environ.get(_name.upper() + "_TOKEN", "").strip()
    if _v:
        TOKENS[_v] = _name

SCHEMA = """
CREATE TABLE IF NOT EXISTS identities(name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS workspaces(
  workspace_id TEXT PRIMARY KEY, description TEXT, registered_at TEXT);
CREATE TABLE IF NOT EXISTS workers(
  worker_id TEXT PRIMARY KEY, identity TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS rooms(room TEXT PRIMARY KEY, description TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  task_id TEXT PRIMARY KEY, room TEXT NOT NULL DEFAULT 'general',
  target TEXT, project TEXT, type TEXT NOT NULL, request TEXT NOT NULL,
  risk_level TEXT NOT NULL DEFAULT 'read_only',
  budget TEXT NOT NULL DEFAULT '{}', reply_to TEXT,
  from_ TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
  claimed_by TEXT, lease_until TEXT, retries INTEGER NOT NULL DEFAULT 0,
  cost_tokens INTEGER NOT NULL DEFAULT 0, result_summary TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT UNIQUE NOT NULL, task_id TEXT, room TEXT,
  from_ TEXT NOT NULL, to_ TEXT, kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}', reply_to TEXT,
  urgent INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_room ON events(room, seq);
CREATE TABLE IF NOT EXISTS deliveries(
  seq INTEGER NOT NULL, recipient TEXT NOT NULL, PRIMARY KEY(seq, recipient));
CREATE INDEX IF NOT EXISTS idx_deliveries_recipient ON deliveries(recipient, seq);
CREATE TABLE IF NOT EXISTS ack_cursors(identity TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS idempotency(
  identity TEXT NOT NULL, endpoint TEXT NOT NULL, key TEXT NOT NULL,
  status INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(identity, endpoint, key));
CREATE TABLE IF NOT EXISTS approvals(
  event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
  decision TEXT, note TEXT, expires_at TEXT NOT NULL, decided_at TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY, identity TEXT NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sentinel(
  room TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS room_fold(
  room TEXT PRIMARY KEY, fold_after_seq INTEGER NOT NULL DEFAULT 0,
  summary TEXT, folded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS room_folds(
  room TEXT NOT NULL, fold_after_seq INTEGER NOT NULL,
  summary TEXT NOT NULL DEFAULT '', folded_at TEXT NOT NULL DEFAULT '',
  folded_by TEXT NOT NULL DEFAULT '', marker_seq INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(room, fold_after_seq));
CREATE TABLE IF NOT EXISTS attachments(
  id TEXT PRIMARY KEY, room TEXT NOT NULL, event_seq INTEGER NOT NULL DEFAULT 0,
  filename TEXT NOT NULL, stored_name TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
  content_type TEXT NOT NULL DEFAULT 'application/octet-stream', kind TEXT NOT NULL DEFAULT 'file',
  uploader TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, identity TEXT,
  detail TEXT, created_at TEXT NOT NULL);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ts_to_dt(s) -> datetime:
    return datetime.fromisoformat(s)


def conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    return c


def init_db():
    c = conn()
    c.executescript(SCHEMA)
    c.execute("PRAGMA journal_mode=WAL")
    now = utcnow()
    for i in IDENTITIES:
        c.execute("INSERT OR IGNORE INTO identities(name) VALUES(?)", (i,))
        c.execute("INSERT OR IGNORE INTO ack_cursors(identity, cursor) VALUES(?, 0)", (i,))
    c.execute("INSERT OR IGNORE INTO rooms(room, description, created_at) VALUES('general', '默认房间', ?)", (now,))
    c.execute("INSERT OR IGNORE INTO workspaces(workspace_id, description, registered_at) "
              "VALUES('tech-hub', 'default workspace', ?)", (now,))
    for w, i in WORKERS.items():
        c.execute("INSERT OR IGNORE INTO workers(worker_id, identity, note) VALUES(?, ?, 'seeded v0.1.1')", (w, i))
    c.commit()
    c.close()


# ---------------- 脱敏 ----------------

_REDACT_PATTERNS = [
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I), "Bearer <redacted>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "<redacted private-key>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}(\.[A-Za-z0-9_-]{10,})?"), "<redacted jwt>"),
]
# 任务文本禁止携带的可执行绝对路径
_EXEC_PATH = re.compile(r"[A-Za-z]:[\\/]|/(?:usr|bin|sbin|etc)(?:/|$)|\S+\.(?:exe|bat|cmd)\b", re.I)


def redact_text(text: str) -> str:
    for tok in TOKENS:
        if tok:
            text = text.replace(tok, "<redacted>")
    for pat, rep in _REDACT_PATTERNS:
        text = pat.sub(rep, text)
    return text


def redact_obj(obj):
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


# ---------------- 事件系统 ----------------

def event_row(c, seq):
    row = c.execute("SELECT * FROM events WHERE seq=?", (seq,)).fetchone()
    return row


def event_api(row) -> dict:
    return {
        "seq": row["seq"], "event_id": row["event_id"], "task_id": row["task_id"], "room": row["room"],
        "from": row["from_"], "to": row["to_"], "kind": row["kind"],
        "payload": json.loads(row["payload"] or "{}"),
        "reply_to": row["reply_to"], "created_at": row["created_at"],
    }


def task_row(c, task_id):
    return c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()


def budget_of(t) -> dict:
    b = json.loads(t["budget"] or "{}")
    return {**DEFAULT_BUDGET, **{k: v for k, v in b.items() if isinstance(v, int)}}


def task_api(row) -> dict:
    return {
        "task_id": row["task_id"], "room": row["room"], "target": row["target"], "project": row["project"],
        "type": row["type"], "request": row["request"], "risk_level": row["risk_level"],
        "budget": json.loads(row["budget"] or "{}"), "reply_to": row["reply_to"],
        "from": row["from_"], "status": row["status"], "claimed_by": row["claimed_by"],
        "lease_until": row["lease_until"], "retries": row["retries"], "cost_tokens": row["cost_tokens"],
        "result_summary": row["result_summary"], "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def summary_of(row) -> str:
    p = json.loads(row["payload"] or "{}")
    kind = row["kind"]
    if kind == "chat":
        s = "%s: %s" % (row["from_"], str(p.get("text", "")))
    elif kind == "task_created":
        s = "新任务 %s [%s] %s" % ((row["task_id"] or "")[:8], str(p.get("type", "")), str(p.get("request", "")))
    elif kind == "claim":
        s = "%s 领取任务 %s" % (str(p.get("worker_id", "")), (row["task_id"] or "")[:8])
    elif kind == "log":
        s = "[%s] %s" % (str(p.get("level", "info")), str(p.get("text", "")))
    elif kind == "result":
        s = "%s: %s" % (str(p.get("status", "")), str(p.get("result_summary", "")))
    elif kind == "control":
        s = "control %s: %s" % (str(p.get("action", "")), str(p.get("text") or p.get("note") or ""))
    elif kind == "approval_request":
        s = "待审批: %s" % str(p.get("op_summary", ""))
    elif kind == "approval":
        s = "审批 %s: %s" % (str(p.get("decision", "")), str(p.get("note", "")))
    else:
        s = str(p.get("text", "")) or kind
    return (s or kind)[:200]


def recipients_for(c, kind, from_, task_id, room):
    t = task_row(c, task_id) if task_id else None
    out = set()
    if kind == "chat":
        out = set(IDENTITIES)
    elif kind == "task_created":
        out.add(from_)
        tgt = t["target"] if t else None
        if tgt in ("codex", "dsh", "claude"):
            out.add(tgt)
        elif tgt == "auto":
            out.add("codex")
    elif kind == "claim":
        out.add(t["from_"] if t else from_)
    elif kind == "log":
        if t:
            out.add(t["from_"])
        if room:
            out |= set(IDENTITIES)
    elif kind == "result":
        if t:
            out.add(t["from_"])
    elif kind == "control":
        if t:
            out.add(t["from_"])
            if t["claimed_by"]:
                out.add(WORKERS.get(t["claimed_by"], ""))
    elif kind == "approval_request":
        out.add("human")
    elif kind == "approval":
        if t:
            out.add(t["from_"])
            if t["claimed_by"]:
                out.add(WORKERS.get(t["claimed_by"], ""))
    elif kind == "system":
        out |= {"human", "claude"}
    out.discard("")
    return sorted(out)


def create_event(c, *, kind, from_, task_id=None, room=None, to_=None, payload=None, reply_to=None, urgent=False):
    payload = redact_obj(payload if payload is not None else {})
    now = utcnow()
    cur = c.execute(
        "INSERT INTO events(event_id,task_id,room,from_,to_,kind,payload,reply_to,urgent,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), task_id, room, from_, to_, kind,
         json.dumps(payload, ensure_ascii=False), reply_to, 1 if urgent else 0, now))
    seq = cur.lastrowid
    for r in recipients_for(c, kind, from_, task_id, room):
        c.execute("INSERT OR IGNORE INTO deliveries(seq, recipient) VALUES(?, ?)", (seq, r))
    c.commit()
    return event_api(event_row(c, seq))

# 指纹去重: 3 秒内 同房间/同身份/同收件人/同文本 的 chat 事件只落一条(防误重发), 命中返回已有事件行
DEDUP_WINDOW_S = 3.0


def find_dupe_event(c, room, from_, to_, text):
    row = c.execute(
        "SELECT * FROM events WHERE room=? AND from_=? AND kind='chat' AND to_ IS ? "
        "ORDER BY seq DESC LIMIT 1", (room, from_, to_)).fetchone()
    if not row:
        return None
    try:
        p = json.loads(row["payload"] or "{}")
    except Exception:
        return None
    if p.get("text") != text:
        return None
    if datetime.now(timezone.utc) - ts_to_dt(row["created_at"]) > timedelta(seconds=DEDUP_WINDOW_S):
        return None
    return row




# 审计事件（system kind），60 秒内同键只记一次
_audit_throttle = {}
_audit_lock = threading.Lock()


def audit_event(c, detail, identity="unknown", throttle_key=None):
    key = throttle_key or (identity + "|" + detail[:40])
    with _audit_lock:
        now_t = time.monotonic()
        last = _audit_throttle.get(key, 0)
        if now_t - last < 60:
            return
        _audit_throttle[key] = now_t
    c.execute("INSERT INTO audit(kind, identity, detail, created_at) VALUES('system', ?, ?, ?)",
              (identity, detail, utcnow()))
    c.commit()
    create_event(c, kind="system", from_=SYSTEM_IDENTITY,
                 payload={"text": "audit: %s" % detail, "code": "audit"})


def task_payload_size(c, task_id) -> int:
    row = c.execute("SELECT COALESCE(SUM(length(payload)), 0) AS n FROM events WHERE task_id=?", (task_id,)).fetchone()
    return row["n"]


def enforce_event_budget(c, task_id):
    """任务事件数超预算 → running 自动置 needs_human（非终态，人工接管）"""
    t = task_row(c, task_id)
    if t is None or t["status"] not in ("running", "waiting_input", "waiting_approval"):
        return
    b = budget_of(t)
    cnt = c.execute("SELECT COUNT(*) AS n FROM events WHERE task_id=?", (task_id,)).fetchone()["n"]
    if cnt > b["max_events"]:
        if t["status"] == "running":
            c.execute("UPDATE tasks SET status='needs_human', updated_at=? WHERE task_id=?", (utcnow(), task_id))
            c.commit()
            create_event(c, kind="system", from_=SYSTEM_IDENTITY, task_id=task_id, room=t["room"],
                         payload={"text": "事件数(%d)超过预算上限(%d)，任务转入 needs_human" % (cnt, b["max_events"]),
                                  "code": "budget_max_events"})
        else:
            create_event(c, kind="system", from_=SYSTEM_IDENTITY, task_id=task_id, room=t["room"],
                         payload={"text": "事件数(%d)超过预算上限(%d)" % (cnt, b["max_events"]),
                                  "code": "budget_max_events"})


# ---------------- 鉴权 / 幂等 ----------------

def get_identity(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        ident = TOKENS.get(auth[7:].strip())
        if ident:
            request.state.auth_mode = "bearer"
            return ident
    sess = request.cookies.get("techhub_session")
    if sess:
        c = conn()
        row = c.execute("SELECT identity, expires_at FROM sessions WHERE token=?", (sess,)).fetchone()
        c.close()
        if row and row["expires_at"] >= utcnow():
            request.state.auth_mode = "cookie"
            return row["identity"]
    c = conn()
    audit_event(c, "auth failure from %s" % request.client.host if request.client else "auth failure",
                throttle_key="authfail|%s" % (request.client.host if request.client else "?"))
    raise HTTPException(401, "invalid or missing token")


def check_csrf(request: Request):
    """Cookie 会话的写请求必须带自定义头（X-Requested-With），配合 SameSite=Lax 防 CSRF"""
    if getattr(request.state, "auth_mode", "") == "cookie" and request.method != "GET":
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            raise HTTPException(403, "CSRF: X-Requested-With header required for cookie sessions")


_IDEM_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")


def idem_begin(request: Request, identity: str, endpoint: str):
    key = request.headers.get("Idempotency-Key", "")
    if not _IDEM_RE.match(key):
        raise HTTPException(400, "Idempotency-Key header required (UUID v4)")
    c = conn()
    row = c.execute("SELECT status, body, created_at FROM idempotency WHERE identity=? AND endpoint=? AND key=?",
                    (identity, endpoint, key)).fetchone()
    c.close()
    if row and ts_to_dt(row["created_at"]) + timedelta(hours=IDEM_TTL_H) >= datetime.now(timezone.utc):
        return key, JSONResponse(status_code=row["status"], content=json.loads(row["body"]))
    return key, None


def idem_end(identity: str, endpoint: str, key: str, status: int, body):
    c = conn()
    c.execute("INSERT OR REPLACE INTO idempotency(identity, endpoint, key, status, body, created_at) "
              "VALUES(?,?,?,?,?,?)",
              (identity, endpoint, key, status, json.dumps(body, ensure_ascii=False), utcnow()))
    c.commit()
    c.close()


def err(code: str, message: str, status: int):
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def size_ok(value: str, limit: int) -> bool:
    return len(value or "") <= limit


def check_worker(identity, worker_id):
    """worker 身份绑定：token 身份与 worker_id 前缀一致且已登记，违规 403"""
    if identity not in WORKER_IDENTITIES:
        raise HTTPException(403, "worker identity required")
    if not isinstance(worker_id, str) or not worker_id.startswith(identity + "-worker-"):
        raise HTTPException(403, "worker_id prefix must match token identity")
    c = conn()
    w = c.execute("SELECT identity FROM workers WHERE worker_id=?", (worker_id,)).fetchone()
    c.close()
    if not w or w["identity"] != identity:
        raise HTTPException(403, "worker_id not registered")


def claimer_identity(c, task_id):
    t = task_row(c, task_id)
    if t and t["claimed_by"]:
        return WORKERS.get(t["claimed_by"])
    return None


# ---------------- FastAPI ----------------

@asynccontextmanager
async def lifespan(app):
    init_db()
    threading.Thread(target=sweeper_loop, daemon=True, name="sweeper").start()
    c = conn()
    create_event(c, kind="system", from_=SYSTEM_IDENTITY,
                 payload={"text": "tech-hub v%s started" % VERSION, "code": "startup"})
    c.close()
    yield


app = FastAPI(title="tech-hub", version=VERSION, lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def on_validation_error(request, exc):
    return err("bad_request", "invalid request body", 400)


@app.exception_handler(HTTPException)
async def on_http_exception(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code,
                        content={"code": "forbidden" if exc.status_code == 403 else
                                 ("unauthorized" if exc.status_code == 401 else
                                  ("not_found" if exc.status_code == 404 else "conflict" if exc.status_code == 409 else "bad_request")),
                                 "message": str(exc.detail)})


# ---------------- /health ----------------

@app.get("/health")
def health():
    return {"status": "ok", "version": VERSION, "time": utcnow()}


# ---------------- 任务 ----------------

@app.post("/task")
def create_task(request: Request, body: dict):
    identity = get_identity(request)
    check_csrf(request)
    key, replay = idem_begin(request, identity, "/task")
    if replay:
        return replay
    if not isinstance(body.get("type"), str) or body["type"] not in TASK_TYPES:
        return err("bad_request", "type required: analyze|fix|build|deploy|test|query|other", 400)
    req_text = body.get("request")
    if not isinstance(req_text, str) or not req_text.strip():
        return err("bad_request", "request required", 400)
    if not size_ok(req_text, MAX_SIZES["request"]):
        return err("size_limit", "request exceeds %d chars" % MAX_SIZES["request"], 400)
    if _EXEC_PATH.search(req_text):
        return err("bad_request", "request 不得携带可执行绝对路径(scope_lock)，请用 workspace 相对表述", 400)
    req_text = redact_text(req_text.strip())
    room = body.get("room") or "general"
    if not isinstance(room, str) or not ROOM_RE.match(room):
        return err("bad_request", "invalid room name", 400)
    target = body.get("target")
    if target is not None and target not in TARGETS:
        return err("bad_request", "invalid target", 400)
    project = body.get("project")
    if project is not None:
        c = conn()
        ws = c.execute("SELECT 1 FROM workspaces WHERE workspace_id=?", (project,)).fetchone()
        c.close()
        if not ws:
            return err("bad_request", "unknown workspace_id: %s" % project, 400)
    risk = body.get("risk_level") or "read_only"
    if risk not in RISK_LEVELS:
        return err("bad_request", "invalid risk_level", 400)
    budget = body.get("budget") if isinstance(body.get("budget"), dict) else {}
    bad = [k for k, v in budget.items()
           if (k not in DEFAULT_BUDGET and k != "max_cost_tokens") or not isinstance(v, int) or v < 0]
    if bad:
        return err("bad_request", "invalid budget field: %s" % bad[0], 400)
    reply_to = body.get("reply_to")
    task_id = str(uuid.uuid4())
    now = utcnow()
    c = conn()
    c.execute(
        "INSERT INTO tasks(task_id, room, target, project, type, request, risk_level, budget, reply_to, "
        "from_, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, room, target, project, body["type"], req_text, risk,
         json.dumps(budget, ensure_ascii=False), reply_to, identity, "queued", now, now))
    c.execute("INSERT OR IGNORE INTO rooms(room, description, created_at) VALUES(?, '', ?)", (room, now))
    c.commit()
    create_event(c, kind="task_created", from_=identity, task_id=task_id, room=room,
                 to_=target, payload={"task_id": task_id, "request": req_text,
                                      "type": body["type"], "risk_level": risk})
    t = task_api(task_row(c, task_id))
    c.close()
    idem_end(identity, "/task", key, 201, t)
    return JSONResponse(status_code=201, content=t)


def _task_visible(c, identity, t) -> bool:
    if identity in ("human", "claude"):
        return True
    if t["from_"] == identity:
        return True
    if t["claimed_by"] and WORKERS.get(t["claimed_by"]) == identity:
        return True
    return False


@app.get("/task/{task_id}")
def get_task(request: Request, task_id: str):
    identity = get_identity(request)
    c = conn()
    t = task_row(c, task_id)
    if t is None:
        c.close()
        return err("not_found", "no such task", 404)
    if not _task_visible(c, identity, t):
        c.close()
        return err("forbidden", "not permitted", 403)
    cnt = c.execute("SELECT COUNT(*) AS n FROM events WHERE task_id=?", (task_id,)).fetchone()["n"]
    apps = c.execute("SELECT a.event_id, a.decision, a.expires_at, e.payload AS payload "
                     "FROM approvals a JOIN events e ON e.event_id=a.event_id "
                     "WHERE a.task_id=?", (task_id,)).fetchall()
    out = {"task": task_api(t), "events_count": cnt,
           "approvals": [{"event_id": a["event_id"], "decision": a["decision"],
                          "expires_at": a["expires_at"],
                          "summary": json.loads(a["payload"] or "{}").get("op_summary", "")}
                         for a in apps]}
    c.close()
    return out


@app.get("/task/{task_id}/events")
def get_task_events(request: Request, task_id: str,
                    after: int = 0, limit: int = 50, exclude_logs: bool = True):
    identity = get_identity(request)
    if not (0 <= after <= 10 ** 12) or not (1 <= limit <= 200):
        return err("bad_request", "invalid after/limit", 400)
    c = conn()
    t = task_row(c, task_id)
    if t is None:
        c.close()
        return err("not_found", "no such task", 404)
    if not _task_visible(c, identity, t):
        c.close()
        return err("forbidden", "not permitted", 403)
    q = "SELECT * FROM events WHERE task_id=? AND seq>?"
    args = [task_id, after]
    if exclude_logs:
        q += " AND kind!='log'"
    q += " ORDER BY seq LIMIT ?"
    args.append(limit)
    rows = c.execute(q, args).fetchall()
    c.close()
    events = [event_api(r) for r in rows]
    next_cursor = events[-1]["seq"] if events else after
    return {"events": events, "next_cursor": next_cursor}


@app.post("/task/{task_id}/events")
def post_task_events(request: Request, task_id: str, body: dict):
    identity = get_identity(request)
    check_csrf(request)
    key, replay = idem_begin(request, identity, "/task/%s/events" % task_id)
    if replay:
        return replay
    c = conn()
    t = task_row(c, task_id)
    if t is None:
        c.close()
        return err("not_found", "no such task", 404)
    if identity not in WORKER_IDENTITIES or claimer_identity(c, task_id) != identity:
        c.close()
        return err("forbidden", "only the claiming worker may post events", 403)
    kind = body.get("kind")
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    if kind not in ("log", "approval_request"):
        c.close()
        return err("forbidden", "kind limited to log/approval_request at this endpoint", 403)
    if t["status"] in TERMINAL:
        c.close()
        return err("conflict", "task already terminal", 409)
    if json.dumps(payload, ensure_ascii=False).__len__() > MAX_SIZES["event_payload"]:
        c.close()
        return err("size_limit", "event payload exceeds 16KB", 400)
    if task_payload_size(c, task_id) + len(json.dumps(payload, ensure_ascii=False)) > MAX_SIZES["task_payload"]:
        c.close()
        return err("size_limit", "task cumulative payload exceeds 256KB, use {path,sha256} ref", 400)
    if kind == "log":
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            c.close()
            return err("bad_request", "log payload.text required", 400)
        if not size_ok(text, MAX_SIZES["log"]):
            c.close()
            return err("size_limit", "log text exceeds %d chars" % MAX_SIZES["log"], 400)
        if payload.get("worker_id") != t["claimed_by"]:
            c.close()
            return err("forbidden", "payload.worker_id must equal task.claimed_by", 403)
    if kind == "approval_request":
        if payload.get("risk_level") != "dangerous":
            c.close()
            return err("bad_request", "approval_request 仅用于 dangerous 操作", 400)
        if not isinstance(payload.get("op_summary"), str) or not payload["op_summary"].strip():
            c.close()
            return err("bad_request", "op_summary required", 400)
        if t["status"] == "running":
            c.execute("UPDATE tasks SET status='waiting_approval', updated_at=? WHERE task_id=?",
                      (utcnow(), task_id))
            c.commit()
        elif t["status"] != "waiting_approval":
            c.close()
            return err("conflict", "approval_request not allowed in status %s" % t["status"], 409)
        expires = payload.get("expires_at")
        try:
            exp_dt = datetime.fromisoformat(expires) if expires else None
        except (TypeError, ValueError):
            exp_dt = None
        if exp_dt is None:
            exp_dt = datetime.now(timezone.utc) + timedelta(minutes=APPROVAL_WINDOW_MIN)
        payload["expires_at"] = exp_dt.isoformat(timespec="milliseconds")
        payload["one_shot"] = True
    ev = create_event(c, kind=kind, from_=identity, task_id=task_id, room=t["room"],
                      to_="human" if kind == "approval_request" else t["from_"], payload=payload)
    if kind == "approval_request":
        c.execute("INSERT OR REPLACE INTO approvals(event_id, task_id, decision, note, expires_at, decided_at) "
                  "VALUES(?,?,NULL,NULL,?,NULL)", (ev["event_id"], task_id, payload["expires_at"]))
        c.commit()
    enforce_event_budget(c, task_id)
    c.close()
    idem_end(identity, "/task/%s/events" % task_id, key, 201, ev)
    return JSONResponse(status_code=201, content=ev)


@app.post("/task/{task_id}/control")
def control_task(request: Request, task_id: str, body: dict):
    identity = get_identity(request)
    check_csrf(request)
    key, replay = idem_begin(request, identity, "/task/%s/control" % task_id)
    if replay:
        return replay
    action = body.get("action")
    if action not in ("continue", "steer", "interrupt", "cancel"):
        return err("bad_request", "invalid action", 400)
    c = conn()
    t = task_row(c, task_id)
    if t is None:
        c.close()
        return err("not_found", "no such task", 404)
    is_creator = t["from_"] == identity
    is_claimer = bool(t["claimed_by"]) and WORKERS.get(t["claimed_by"]) == identity
    if not (identity in ("human", "claude") or is_creator or is_claimer):
        c.close()
        return err("forbidden", "only task creator, current executor or human may control", 403)
    if t["status"] in TERMINAL:
        c.close()
        return err("conflict", "task already terminal", 409)
    now = utcnow()
    new_status = None
    if action == "continue":
        if t["status"] in ("waiting_input", "waiting_approval"):
            new_status = "running"
        elif t["status"] == "needs_human" and identity in ("human", "claude"):
            new_status = "running"
        else:
            c.close()
            return err("conflict", "continue not allowed in status %s" % t["status"], 409)
    elif action == "interrupt":
        if t["status"] != "running":
            c.close()
            return err("conflict", "interrupt only from running", 409)
        new_status = "waiting_input"
    elif action == "cancel":
        new_status = "cancelled"
    # steer: 仅追加方向性事件，不改状态
    payload = dict(body.get("payload") or {})
    if new_status:
        c.execute("UPDATE tasks SET status=?, updated_at=? WHERE task_id=?", (new_status, now, task_id))
        c.commit()
    ev = create_event(c, kind="control", from_=identity, task_id=task_id, room=t["room"],
                      to_=t["from_"], payload={"action": action,
                                               "text": payload.get("text"), "note": payload.get("note")})
    c.close()
    idem_end(identity, "/task/%s/control" % task_id, key, 200, ev)
    return ev


# ---------------- worker ----------------

@app.post("/claim")
def claim(request: Request, body: dict):
    identity = get_identity(request)
    check_csrf(request)
    worker_id = body.get("worker_id")
    check_worker(identity, worker_id)
    wait_seconds = body.get("wait_seconds", 0)
    if not isinstance(wait_seconds, int) or not (0 <= wait_seconds <= 30):
        return err("bad_request", "wait_seconds must be 0..30", 400)
    filt = None
    if isinstance(body.get("filter"), dict):
        filt = body["filter"].get("target")
    if filt is not None and filt not in TARGETS:
        return err("bad_request", "invalid filter.target", 400)
    key, replay = idem_begin(request, identity, "/claim")
    if replay:
        return replay
    deadline = time.monotonic() + wait_seconds
    claimed = None
    while True:
        claimed = try_claim(identity, worker_id, filt)
        if claimed:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if claimed:
        resp = {"task_id": claimed["task_id"], "lease_until": claimed["lease_until"], "task": claimed}
        idem_end(identity, "/claim", key, 200, resp)
        return resp
    resp = {"task_id": None}
    idem_end(identity, "/claim", key, 200, resp)
    return resp


def try_claim(identity, worker_id, filt):
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        if filt:
            rows = c.execute("SELECT * FROM tasks WHERE status='queued' AND target=? "
                             "ORDER BY created_at LIMIT 1", (filt,)).fetchall()
        elif identity == "codex":
            rows = c.execute("SELECT * FROM tasks WHERE status='queued' AND "
                             "(target IS NULL OR target IN ('codex','auto')) ORDER BY created_at LIMIT 1").fetchall()
        else:
            rows = c.execute("SELECT * FROM tasks WHERE status='queued' AND target=? "
                             "ORDER BY created_at LIMIT 1", (identity,)).fetchall()
        if not rows:
            c.rollback()
            return None
        t = rows[0]
        lease = (datetime.now(timezone.utc) + timedelta(minutes=LEASE_MIN)).isoformat(timespec="milliseconds")
        c.execute("UPDATE tasks SET status='running', claimed_by=?, lease_until=?, updated_at=? "
                  "WHERE task_id=? AND status='queued'",
                  (worker_id, lease, utcnow(), t["task_id"]))
        c.commit()
        t = task_row(c, t["task_id"])
        create_event(c, kind="claim", from_=identity, task_id=t["task_id"], room=t["room"],
                     to_=t["from_"], payload={"worker_id": worker_id, "lease_until": lease})
        return task_api(t)
    finally:
        c.close()


@app.post("/heartbeat")
def heartbeat(request: Request, body: dict):
    identity = get_identity(request)
    check_csrf(request)
    worker_id = body.get("worker_id")
    check_worker(identity, worker_id)
    task_id = body.get("task_id")
    key, replay = idem_begin(request, identity, "/heartbeat")
    if replay:
        return replay
    c = conn()
    t = task_row(c, task_id)
    if t is None:
        c.close()
        return err("not_found", "no such task", 404)
    if t["claimed_by"] != worker_id:
        c.close()
        return err("conflict", "lease holder mismatch", 409)
    if t["status"] in TERMINAL:
        c.close()
        return err("conflict", "task already terminal", 409)
    lease = (datetime.now(timezone.utc) + timedelta(minutes=LEASE_MIN)).isoformat(timespec="milliseconds")
    c.execute("UPDATE tasks SET lease_until=?, updated_at=? WHERE task_id=?", (lease, utcnow(), task_id))
    c.commit()
    c.close()
    resp = {"lease_until": lease}
    idem_end(identity, "/heartbeat", key, 200, resp)
    return resp


@app.post("/result")
def result(request: Request, body: dict):
    identity = get_identity(request)
    check_csrf(request)
    worker_id = body.get("worker_id")
    check_worker(identity, worker_id)
    task_id = body.get("task_id")
    status = body.get("status")
    if status not in ("completed", "failed", "needs_human", "waiting_input", "waiting_approval"):
        return err("bad_request", "invalid result status", 400)
    summary = body.get("result_summary")
    if not isinstance(summary, str) or not summary.strip():
        return err("bad_request", "result_summary required", 400)
    if not size_ok(summary, MAX_SIZES["summary"]):
        return err("size_limit", "result_summary exceeds %d chars" % MAX_SIZES["summary"], 400)
    artifacts = body.get("artifacts") if isinstance(body.get("artifacts"), list) else []
    if len(artifacts) > 10:
        return err("bad_request", "artifacts max 10", 400)
    for a in artifacts:
        if not isinstance(a, dict) or a.get("type") not in ("diff", "log", "file", "link", "note") \
                or not isinstance(a.get("label"), str):
            return err("bad_request", "invalid artifact", 400)
        if not size_ok(a.get("content"), MAX_SIZES["artifact"]):
            return err("size_limit", "artifact.content exceeds %d chars" % MAX_SIZES["artifact"], 400)
    cost = body.get("cost_tokens") or 0
    if not isinstance(cost, int) or cost < 0:
        return err("bad_request", "invalid cost_tokens", 400)
    key, replay = idem_begin(request, identity, "/result")
    if replay:
        return replay
    c = conn()
    t = task_row(c, task_id)
    if t is None:
        c.close()
        return err("not_found", "no such task", 404)
    if t["claimed_by"] != worker_id:
        c.close()
        return err("conflict", "lease holder mismatch", 409)
    if t["status"] in TERMINAL:
        c.close()
        return err("conflict", "task already terminal", 409)
    old = t["status"]
    new_status = status
    if status == old:
        new_status = old  # 同状态幂等接受（worker 危险流程先发 approval_request 再回 waiting_approval）
    if new_status != old and not (new_status in TRANSITIONS.get(old, set())):
        c.close()
        return err("conflict", "illegal transition %s -> %s" % (old, new_status), 409)
    new_cost = t["cost_tokens"] + cost
    c.execute("UPDATE tasks SET status=?, result_summary=?, cost_tokens=?, updated_at=? WHERE task_id=?",
              (new_status, redact_text(summary.strip()), new_cost, utcnow(), task_id))
    c.commit()
    payload = {"status": new_status, "result_summary": summary.strip(),
               "artifacts": artifacts, "cost_tokens": cost}
    ev = create_event(c, kind="result", from_=identity, task_id=task_id, room=t["room"],
                      to_=t["from_"], payload=payload)
    b = budget_of(t)
    if b.get("max_cost_tokens") and new_cost > b["max_cost_tokens"] and new_status not in TERMINAL:
        if new_status == "running":
            c.execute("UPDATE tasks SET status='needs_human', updated_at=? WHERE task_id=?", (utcnow(), task_id))
            c.commit()
        create_event(c, kind="system", from_=SYSTEM_IDENTITY, task_id=task_id, room=t["room"],
                     payload={"text": "cost_tokens(%d) 超过预算上限(%d)，任务转入 needs_human"
                              % (new_cost, b["max_cost_tokens"]), "code": "budget_cost"})
    c.close()
    idem_end(identity, "/result", key, 200, ev)
    return ev


# ---------------- 信箱 ----------------

@app.get("/results")
def results(request: Request, target: str, after: int = -1, limit: int = 20, wait_seconds: int = 0):
    identity = get_identity(request)
    if target != identity:
        c = conn()
        audit_event(c, "forbidden /results?target=%s by %s" % (target, identity), identity,
                    throttle_key="forbid|results|%s" % identity)
        c.close()
        return err("forbidden", "target must equal your identity", 403)
    if not (1 <= limit <= 50):
        return err("bad_request", "limit must be 1..50", 400)
    if not (0 <= wait_seconds <= 30):
        return err("bad_request", "wait_seconds must be 0..30", 400)
    c = conn()
    cur = c.execute("SELECT cursor FROM ack_cursors WHERE identity=?", (identity,)).fetchone()["cursor"]
    base = after if after >= 0 else cur
    deadline = time.monotonic() + wait_seconds
    events = []
    while True:
        rows = c.execute(
            "SELECT e.* FROM deliveries d JOIN events e ON e.seq=d.seq "
            "WHERE d.recipient=? AND d.seq>? ORDER BY d.seq LIMIT ?", (identity, base, limit)).fetchall()
        if rows:
            events = [{
                "seq": r["seq"], "event_id": r["event_id"], "task_id": r["task_id"],
                "from": r["from_"], "kind": r["kind"], "summary": summary_of(r),
                "urgent": bool(r["urgent"]), "created_at": r["created_at"],
            } for r in rows]
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    unread = c.execute("SELECT COUNT(*) AS n FROM deliveries WHERE recipient=? AND seq>?",
                       (identity, cur)).fetchone()["n"]
    c.close()
    next_cursor = events[-1]["seq"] if events else base
    return {"events": events, "next_cursor": next_cursor, "unread_count": unread}


@app.post("/ack")
def ack(request: Request, body: dict):
    identity = get_identity(request)
    check_csrf(request)
    cursor = body.get("cursor")
    if not isinstance(cursor, int) or cursor < 0:
        return err("bad_request", "cursor required (int >= 0)", 400)
    key, replay = idem_begin(request, identity, "/ack")
    if replay:
        return replay
    c = conn()
    cur = c.execute("SELECT cursor FROM ack_cursors WHERE identity=?", (identity,)).fetchone()["cursor"]
    max_del = c.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM deliveries WHERE recipient=?",
                        (identity,)).fetchone()["m"]
    if cursor < cur:
        c.close()
        return err("conflict", "ack cursor 不允许倒退 (current %d)" % cur, 409)
    if cursor > max_del:
        c.close()
        return err("conflict", "ack cursor 超过已投递最高 seq %d" % max_del, 409)
    if cursor == cur:
        c.close()
        resp = {"status": "ok", "cursor": cur}
        idem_end(identity, "/ack", key, 200, resp)
        return resp
    c.execute("UPDATE ack_cursors SET cursor=? WHERE identity=?", (cursor, identity))
    c.commit()
    c.close()
    resp = {"status": "ok", "cursor": cursor}
    idem_end(identity, "/ack", key, 200, resp)
    return resp


# ---------------- 聊天室 ----------------

@app.get("/rooms")
def rooms(request: Request):
    identity = get_identity(request)
    c = conn()
    rows = c.execute("SELECT * FROM rooms ORDER BY room").fetchall()
    out = []
    for r in rows:
        last = c.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE room=? AND kind='chat'",
                         (r["room"],)).fetchone()["m"]
        out.append({"room": r["room"], "description": r["description"], "last_seq": last})
    c.close()
    return {"rooms": out}


@app.get("/rooms/{room}/messages")
def room_messages(request: Request, room: str, after: int = 0, limit: int = 50, wait_seconds: int = 0, ignore_fold: int = 0, until: int = 0):
    identity = get_identity(request)
    if not (0 <= after <= 10 ** 12) or not (1 <= limit <= 100) or not (0 <= wait_seconds <= 30) or ignore_fold not in (0, 1) or not (0 <= until <= 10 ** 12) or (until > 0 and until < after):
        return err("bad_request", "invalid after/limit/wait_seconds/ignore_fold", 400)
    c = conn()
    exists = c.execute("SELECT 1 FROM rooms WHERE room=?", (room,)).fetchone()
    if not exists:
        c.close()
        return err("not_found", "no such room", 404)
    frow = c.execute("SELECT fold_after_seq, summary, folded_at FROM room_fold WHERE room=?", (room,)).fetchone()
    fold = {"fold_after_seq": frow["fold_after_seq"], "summary": frow["summary"],
            "folded_at": frow["folded_at"]} if frow else None
    # 客户端未显式指定起点(after=0)且未要求展开、也非分段查看时, 默认从折叠锚点之后开始, 避免历史过长反复加载
    if until == 0 and after == 0 and not ignore_fold and fold and fold["fold_after_seq"] > 0:
        after = fold["fold_after_seq"]
    deadline = time.monotonic() + wait_seconds
    events = []
    while True:
        if until:
            rows = c.execute("SELECT * FROM events WHERE room=? AND kind='chat' AND seq>? AND seq<=? ORDER BY seq LIMIT ?",
                             (room, after, until, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM events WHERE room=? AND kind='chat' AND seq>? ORDER BY seq LIMIT ?",
                             (room, after, limit)).fetchall()
        if rows:
            events = [event_api(r) for r in rows]
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    c.close()
    next_cursor = events[-1]["seq"] if events else after
    return {"events": events, "next_cursor": next_cursor, "fold": fold}


@app.post("/rooms/{room}/messages")
def post_room_message(request: Request, room: str, body: dict):
    identity = get_identity(request)
    check_csrf(request)
    key, replay = idem_begin(request, identity, "/rooms/%s/messages" % room)
    if replay:
        return replay
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return err("bad_request", "text required", 400)
    if not size_ok(text, MAX_SIZES["chat"]):
        return err("size_limit", "chat text exceeds %d chars" % MAX_SIZES["chat"], 400)
    to = body.get("to")
    if to is not None:
        if isinstance(to, str):
            to = [to]
        if not isinstance(to, list) or not (1 <= len(to) <= 5) or any(x not in IDENTITIES for x in to):
            return err("bad_request", "to must be identity or array of identities (<=5)", 400)
        to = sorted(set(to))
    reply_to = body.get("reply_to")
    if not ROOM_RE.match(room):
        return err("bad_request", "invalid room name", 400)
    c = conn()
    c.execute("INSERT OR IGNORE INTO rooms(room, description, created_at) VALUES(?, '', ?)", (room, utcnow()))
    c.commit()
    ptext = redact_text(text.strip())
    tos = to or [None]
    dupes = {}
    for x in tos:
        d = find_dupe_event(c, room, identity, x, ptext)
        if d:
            dupes[x] = d
    if dupes and len(dupes) == len(tos):
        out_evs = []
        for x in tos:
            ev = event_api(dupes[x])
            ev["dedup"] = True
            out_evs.append(ev)
        out = out_evs[0] if len(tos) == 1 else {"events": out_evs}
        c.close()
        idem_end(identity, "/rooms/%s/messages" % room, key, 200, out)
        return JSONResponse(status_code=200, content=out)
    payload = {"text": ptext, "to": to}
    if len(tos) > 1:
        new_evs = {}
        for x in tos:
            if x not in dupes:
                new_evs[x] = create_event(c, kind="chat", from_=identity, room=room, to_=x,
                                          payload={"text": ptext, "to": x}, reply_to=reply_to)
        out_evs = []
        for x in tos:
            if x in dupes:
                ev = event_api(dupes[x])
                ev["dedup"] = True
            else:
                ev = new_evs[x]
            out_evs.append(ev)
        c.close()
        out = {"events": out_evs}
        idem_end(identity, "/rooms/%s/messages" % room, key, 201, out)
        return JSONResponse(status_code=201, content=out)
    ev = create_event(c, kind="chat", from_=identity, room=room, to_=to[0] if to else None,
                      payload=payload, reply_to=reply_to)
    c.close()
    idem_end(identity, "/rooms/%s/messages" % room, key, 201, ev)
    return JSONResponse(status_code=201, content=ev)


# ---------------- 房间折叠 ----------------
# 折叠 = 把「折叠锚点」推进到当前最大 chat seq; GET /rooms/{room}/messages 在 after=0
# 且未显式 ignore_fold=1 时默认从锚点之后返回, UI 打开即见最新; 历史不删, 可全量拉取。
@app.post("/rooms/{room}/fold")
def room_fold(request: Request, room: str, body: dict):
    identity = get_identity(request)
    check_csrf(request)
    key, replay = idem_begin(request, identity, "/rooms/%s/fold" % room)
    if replay:
        return replay
    if not ROOM_RE.match(room):
        return err("bad_request", "invalid room name", 400)
    summary = body.get("summary") or ""
    if not isinstance(summary, str) or not size_ok(summary, MAX_SIZES["summary"]):
        return err("bad_request", "invalid summary", 400)
    c = conn()
    c.execute("INSERT OR IGNORE INTO rooms(room, description, created_at) VALUES(?, '', ?)", (room, utcnow()))
    c.commit()
    last_chat = c.execute("SELECT COALESCE(MAX(seq),0) m FROM events WHERE room=? AND kind='chat'",
                          (room,)).fetchone()["m"]
    c.execute("INSERT INTO room_fold(room, fold_after_seq, summary, folded_at) VALUES(?,?,?,?) "
              "ON CONFLICT(room) DO UPDATE SET fold_after_seq=excluded.fold_after_seq, "
              "summary=excluded.summary, folded_at=excluded.folded_at",
              (room, last_chat, summary.strip(), utcnow()))
    c.commit()
    # 折叠留痕: 一条 chat 标记, 位于锚点之后, 折叠后依然可见
    ev = create_event(c, kind="chat", from_=identity, room=room,
                      payload={"text": "【折叠标记】%s 执行折叠: seq<=%d 的历史已折叠%s" % (
                          identity, last_chat, ("，摘要: " + summary.strip()) if summary.strip() else "")})
    # 折叠历史: 每次折叠命令留一条, 供前端折叠卡片列表(可跳转分段)
    c.execute("INSERT OR IGNORE INTO room_folds(room, fold_after_seq, summary, folded_at, folded_by, marker_seq) "
              "VALUES(?,?,?,?,?,?)",
              (room, last_chat, summary.strip(), utcnow(), identity, ev["seq"]))
    audit_event(c, "fold room=%s fold_after=%s by %s" % (room, last_chat, identity), identity)
    c.close()
    out = {"room": room, "fold_after_seq": last_chat, "summary": summary.strip(), "event": ev}
    idem_end(identity, "/rooms/%s/fold" % room, key, 200, out)
    return JSONResponse(status_code=200, content=out)


@app.get("/rooms/{room}/folds")
def room_folds(request: Request, room: str):
    identity = get_identity(request)
    if not ROOM_RE.match(room):
        return err("bad_request", "invalid room name", 400)
    c = conn()
    c.execute("CREATE TABLE IF NOT EXISTS room_folds(room TEXT NOT NULL, fold_after_seq INTEGER NOT NULL, summary TEXT NOT NULL DEFAULT '', folded_at TEXT NOT NULL DEFAULT '', folded_by TEXT NOT NULL DEFAULT '', marker_seq INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(room, fold_after_seq))")
    # 懒迁移: 该房间尚无历史时, 从折叠标记事件重建(幂等), 兼容升级前的折叠记录
    if c.execute("SELECT COUNT(*) n FROM room_folds WHERE room=?", (room,)).fetchone()["n"] == 0:
        marker_re = re.compile(r"^【折叠标记】(\S+) 执行折叠: seq<=(\d+) 的历史已折叠(?:，摘要: (.*))?$")
        for row in c.execute("SELECT seq, from_, payload, created_at FROM events WHERE room=? AND kind='chat'", (room,)).fetchall():
            try:
                text = json.loads(row["payload"] or "{}").get("text", "")
            except Exception:
                text = ""
            m = marker_re.match(text or "")
            if not m:
                continue
            who, after_seq, sum_text = m.group(1), int(m.group(2)), (m.group(3) or "").strip()
            c.execute("INSERT OR IGNORE INTO room_folds(room, fold_after_seq, summary, folded_at, folded_by, marker_seq) "
                      "VALUES(?,?,?,?,?,?)",
                      (room, after_seq, sum_text, row["created_at"], who, row["seq"]))
        frow = c.execute("SELECT fold_after_seq, summary, folded_at FROM room_fold WHERE room=?", (room,)).fetchone()
        if frow and frow["fold_after_seq"] > 0:
            c.execute("INSERT OR IGNORE INTO room_folds(room, fold_after_seq, summary, folded_at, folded_by, marker_seq) "
                      "VALUES(?,?,?,?,?,?)",
                      (room, frow["fold_after_seq"], frow["summary"] or "", frow["folded_at"], "", 0))
        c.commit()
    folds = [{"fold_after_seq": r["fold_after_seq"], "summary": r["summary"],
              "folded_at": r["folded_at"], "folded_by": r["folded_by"], "marker_seq": r["marker_seq"]}
             for r in c.execute("SELECT * FROM room_folds WHERE room=? ORDER BY fold_after_seq ASC", (room,)).fetchall()]
    c.close()
    current = folds[-1]["fold_after_seq"] if folds else 0
    return {"folds": folds, "current": current}


@app.post("/rooms/{room}/attachments")
async def room_attachment(request: Request, room: str, filename: str = "", text: str = ""):
    identity = get_identity(request)
    check_csrf(request)
    key, replay = idem_begin(request, identity, "/rooms/%s/attachments" % room)
    if replay:
        return replay
    if not ROOM_RE.match(room):
        return err("bad_request", "invalid room name", 400)
    filename = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff()（） ]", "_", filename.strip())[:200] or "file"
    if not size_ok(text, MAX_SIZES["chat"]):
        return err("size_limit", "caption too long", 400)
    data = await request.body()
    if not data:
        return err("bad_request", "empty body", 400)
    ctype = request.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip().lower()
    kind = "image" if ctype.startswith("image/") else "file"
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_FILE_BYTES
    if len(data) > limit:
        return err("size_limit", "attachment exceeds %d bytes" % limit, 400)
    aid = uuid.uuid4().hex
    ext = re.sub(r"[^A-Za-z0-9.]", "", os.path.splitext(filename)[1])[:16]
    stored = aid + ext
    with open(os.path.join(ATTACH_DIR, stored), "wb") as f:
        f.write(data)
    c = conn()
    if not c.execute("SELECT 1 FROM rooms WHERE room=?", (room,)).fetchone():
        c.execute("INSERT OR IGNORE INTO rooms(room, description, created_at) VALUES(?, '', ?)", (room, utcnow()))
    ev = create_event(c, kind="chat", from_=identity, room=room,
                      payload={"text": text.strip() or filename,
                               "attachment": {"id": aid, "filename": filename, "size": len(data),
                                              "content_type": ctype, "kind": kind}})
    c.execute("INSERT INTO attachments(id, room, event_seq, filename, stored_name, size, content_type, kind, uploader, created_at) "
              "VALUES(?,?,?,?,?,?,?,?,?,?)",
              (aid, room, ev["seq"], filename, stored, len(data), ctype, kind, identity, utcnow()))
    c.commit()
    c.close()
    out = {"seq": ev["seq"], "attachment": {"id": aid, "filename": filename, "size": len(data),
                                            "content_type": ctype, "kind": kind}}
    idem_end(identity, "/rooms/%s/attachments" % room, key, 200, out)
    return JSONResponse(status_code=200, content=out)


@app.get("/attachments/{aid}")
def get_attachment(request: Request, aid: str, download: int = 0):
    identity = get_identity(request)
    if not re.fullmatch(r"[0-9a-f]{32}", aid):
        return err("bad_request", "invalid attachment id", 400)
    c = conn()
    row = c.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
    c.close()
    if not row:
        return err("not_found", "no such attachment", 404)
    path = os.path.join(ATTACH_DIR, row["stored_name"])
    if not os.path.exists(path):
        return err("not_found", "attachment file missing", 404)
    disp = "attachment" if download else ("inline" if row["kind"] == "image" else "attachment")
    headers = {"Content-Disposition": "%s; filename*=UTF-8''%s" % (disp, quote(row["filename"])),
               "X-Content-Type-Options": "nosniff"}
    return FileResponse(path, media_type=row["content_type"] or "application/octet-stream", headers=headers)


# ---------------- 门铃哨兵 ----------------
# 计划任务每 3 分钟 poll 一次: 只返回 human/rikka 的新消息(agent 输出永不触发),
# 停会词(仅 from=human 且逐字匹配)置 paused, 下一条 human/rikka 新消息自动恢复。
@app.post("/sentinel/poll")
def sentinel_poll(request: Request, body: dict):
    identity = get_identity(request)
    check_csrf(request)
    key, replay = idem_begin(request, identity, "/sentinel/poll")
    if replay:
        return replay
    room = body.get("room", "general")
    if not isinstance(room, str) or not ROOM_RE.match(room):
        return err("bad_request", "invalid room name", 400)
    c = conn()
    c.execute("INSERT OR IGNORE INTO sentinel(room, cursor, paused) VALUES(?, 0, 0)", (room,))
    row = c.execute("SELECT cursor, paused FROM sentinel WHERE room=?", (room,)).fetchone()
    cur, paused = row["cursor"], bool(row["paused"])
    rows = c.execute(
        "SELECT * FROM events WHERE room=? AND kind='chat' AND from_ IN ('human','rikka') AND seq>? ORDER BY seq",
        (room, cur)).fetchall()
    meeting_end = False
    for r in rows:
        if r["from_"] == "human" and (json.loads(r["payload"] or "{}").get("text") or "").strip() == SENTINEL_STOP_PHRASE:
            paused = True
            meeting_end = True
        else:
            paused = False
    if rows:
        c.execute("UPDATE sentinel SET cursor=?, paused=? WHERE room=?",
                  (rows[-1]["seq"], 1 if paused else 0, room))
    c.commit()
    c.close()
    msgs = [event_api(r) for r in rows]
    out = {"room": room, "paused": bool(paused), "meeting_end": meeting_end,
           "messages": msgs, "next_cursor": msgs[-1]["seq"] if msgs else cur}
    idem_end(identity, "/sentinel/poll", key, 200, out)
    return JSONResponse(status_code=200, content=out)


# ---------------- 审批 ----------------

@app.post("/approval")
def approval(request: Request, body: dict):
    identity = get_identity(request)
    check_csrf(request)
    if identity != "human":
        c = conn()
        audit_event(c, "forbidden /approval by %s" % identity, identity, throttle_key="forbid|approval|%s" % identity)
        return err("forbidden", "approval is human-only", 403)
    task_id = body.get("task_id")
    event_id = body.get("event_id")
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        return err("bad_request", "decision required: approve|reject", 400)
    note = body.get("note")
    if note is not None and not size_ok(note, 1000):
        return err("size_limit", "note exceeds 1000 chars", 400)
    key, replay = idem_begin(request, identity, "/approval")
    if replay:
        return replay
    c = conn()
    t = task_row(c, task_id)
    if t is None:
        c.close()
        return err("not_found", "no such task", 404)
    ap = c.execute("SELECT * FROM approvals WHERE event_id=? AND task_id=?", (event_id, task_id)).fetchone()
    if ap is None:
        c.close()
        return err("not_found", "no such approval_request", 404)
    if ap["decision"] is not None:
        c.close()
        return err("conflict", "approval already decided (one-shot)", 409)
    if ts_to_dt(ap["expires_at"]) < datetime.now(timezone.utc):
        c.close()
        return err("conflict", "approval expired (%s)" % ap["expires_at"], 409)
    now = utcnow()
    if decision == "approve":
        if t["status"] != "waiting_approval":
            c.close()
            return err("conflict", "task not in waiting_approval", 409)
        lease = (datetime.now(timezone.utc) + timedelta(minutes=LEASE_MIN)).isoformat(timespec="milliseconds")
        c.execute("UPDATE tasks SET status='running', lease_until=?, updated_at=? WHERE task_id=?",
                  (lease, now, task_id))
    else:
        c.execute("UPDATE tasks SET status='failed', updated_at=? WHERE task_id=?", (now, task_id))
    c.execute("UPDATE approvals SET decision=?, note=?, decided_at=? WHERE event_id=?",
              (decision, note, now, event_id))
    c.commit()
    ev = create_event(c, kind="approval", from_=identity, task_id=task_id, room=t["room"],
                      to_=t["from_"], payload={"decision": decision, "note": note})
    c.execute("INSERT INTO audit(kind, identity, detail, created_at) VALUES('approval', ?, ?, ?)",
              (identity, "approval %s on %s/%s" % (decision, task_id, event_id), now))
    c.commit()
    c.close()
    idem_end(identity, "/approval", key, 200, ev)
    return ev


# ---------------- 前端（/ui，不入 OpenAPI） ----------------

@app.get("/ui")
def ui():
    return HTMLResponse(UI_HTML)


@app.post("/ui/login")
async def ui_login(request: Request):
    token = None
    ctype = request.headers.get("Content-Type", "")
    if ctype.startswith("application/json"):
        try:
            token = (await request.json()).get("token")
        except Exception:
            token = None
    else:
        form = await request.form()
        token = form.get("token")
    if token == TOKEN_HUMAN:
        sess = secrets.token_hex(32)
        exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="milliseconds")
        c = conn()
        c.execute("INSERT INTO sessions(token, identity, created_at, expires_at) VALUES(?,?,?,?)",
                  (sess, "human", utcnow(), exp))
        c.commit()
        c.close()
        resp = RedirectResponse(url="/ui", status_code=303)
        resp.set_cookie("techhub_session", sess, httponly=True, samesite="lax", max_age=30 * 86400, path="/")
        return resp
    c = conn()
    audit_event(c, "ui login failure", throttle_key="authfail|ui-login")
    return err("unauthorized", "wrong token", 401)


@app.post("/ui/logout")
def ui_logout(request: Request):
    sess = request.cookies.get("techhub_session")
    if sess:
        c = conn()
        c.execute("DELETE FROM sessions WHERE token=?", (sess,))
        c.commit()
        c.close()
    resp = RedirectResponse(url="/ui", status_code=303)
    resp.delete_cookie("techhub_session", path="/")
    return resp


@app.get("/ui/tasks")
def ui_tasks(request: Request, limit: int = 50):
    identity = get_identity(request)
    if identity != "human":
        return err("forbidden", "human only", 403)
    c = conn()
    rows = c.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (min(limit, 200),)).fetchall()
    out = []
    for t in rows:
        cnt = c.execute("SELECT COUNT(*) AS n FROM events WHERE task_id=?", (t["task_id"],)).fetchone()["n"]
        d = task_api(t)
        d["events_count"] = cnt
        out.append(d)
    c.close()
    return {"tasks": out}


@app.get("/ui/approvals")
def ui_approvals(request: Request):
    identity = get_identity(request)
    if identity != "human":
        return err("forbidden", "human only", 403)
    c = conn()
    rows = c.execute(
        "SELECT a.event_id, a.task_id, a.expires_at, a.decision, t.status AS task_status, e.payload "
        "FROM approvals a JOIN tasks t ON t.task_id=a.task_id JOIN events e ON e.event_id=a.event_id "
        "WHERE a.decision IS NULL ORDER BY a.expires_at").fetchall()
    out = []
    for r in rows:
        p = json.loads(r["payload"] or "{}")
        out.append({
            "event_id": r["event_id"], "task_id": r["task_id"], "expires_at": r["expires_at"],
            "task_status": r["task_status"], "op_summary": p.get("op_summary", ""),
            "op_scope": p.get("op_scope", []), "risk_level": p.get("risk_level"),
        })
    c.close()
    return {"approvals": out}


# ---------------- 后台清扫 ----------------

def sweeper_loop():
    while True:
        time.sleep(SWEEP_INTERVAL_S)
        try:
            sweep_once()
        except Exception as exc:  # 清扫失败不致命，下轮重试
            print("sweeper error: %s" % exc, flush=True)


def sweep_once():
    c = conn()
    now_dt = datetime.now(timezone.utc)
    now = utcnow()
    # 1) 租约过期回收
    rows = c.execute("SELECT * FROM tasks WHERE status='running' AND lease_until IS NOT NULL AND lease_until < ?",
                     (now,)).fetchall()
    for t in rows:
        b = budget_of(t)
        retries = t["retries"] + 1
        if retries > b["max_retries"]:
            c.execute("UPDATE tasks SET status='failed', claimed_by=NULL, lease_until=NULL, retries=?, updated_at=? "
                      "WHERE task_id=?", (retries, now, t["task_id"]))
            c.commit()
            create_event(c, kind="system", from_=SYSTEM_IDENTITY, task_id=t["task_id"], room=t["room"],
                         payload={"text": "租约过期回收 %d 次超过上限，任务 failed" % retries,
                                  "code": "lease_exhausted"})
        else:
            c.execute("UPDATE tasks SET status='queued', claimed_by=NULL, lease_until=NULL, retries=?, updated_at=? "
                      "WHERE task_id=?", (retries, now, t["task_id"]))
            c.commit()
            create_event(c, kind="system", from_=SYSTEM_IDENTITY, task_id=t["task_id"], room=t["room"],
                         payload={"text": "租约过期，任务重新排队 (retries=%d)" % retries, "code": "lease_expired"})
        c.execute("INSERT INTO audit(kind, identity, detail, created_at) VALUES('system', 'claude', ?, ?)",
                  ("lease reclaim %s" % t["task_id"], now))
        c.commit()
    # 2) 存活时长超预算 → cancelled（needs_human 除外，人工接管）
    rows = c.execute("SELECT * FROM tasks WHERE status IN ('queued','running','waiting_input','waiting_approval')",
                     ).fetchall()
    for t in rows:
        b = budget_of(t)
        if ts_to_dt(t["created_at"]) + timedelta(minutes=b["max_duration_min"]) <= now_dt:
            c.execute("UPDATE tasks SET status='cancelled', updated_at=? WHERE task_id=?", (now, t["task_id"]))
            c.commit()
            create_event(c, kind="system", from_=SYSTEM_IDENTITY, task_id=t["task_id"], room=t["room"],
                         payload={"text": "任务存活超过 max_duration_min(%d) 分钟，自动 cancelled"
                                  % b["max_duration_min"], "code": "budget_duration"})
    # 3) 幂等键 / 会话过期清理
    c.execute("DELETE FROM idempotency WHERE created_at < ?",
              ((now_dt - timedelta(hours=IDEM_TTL_H)).isoformat(timespec="milliseconds"),))
    c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    c.commit()
    c.close()


# ---------------- UI HTML ----------------

TOKEN_HUMAN = next((t for t, i in TOKENS.items() if i == "human"), None)

UI_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tech-hub</title>
<style>
 body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;margin:0;background:#f5f6f8;color:#1c1e21}
 header{background:#fff;border-bottom:1px solid #e0e2e6;padding:10px 16px;display:flex;align-items:center;gap:12px}
 header h1{font-size:17px;margin:0}
 .tabs button{border:none;background:none;font-size:14px;padding:8px 14px;cursor:pointer;border-radius:6px}
 .tabs button.on{background:#e7f0ff;color:#1a66cc;font-weight:600}
 main{max-width:860px;margin:16px auto;padding:0 12px}
 .card{background:#fff;border:1px solid #e0e2e6;border-radius:10px;padding:12px 16px;margin-bottom:12px}
 #msgs{margin-top:10px;min-height:360px;max-height:64vh;overflow-y:auto;padding:16px;background:linear-gradient(180deg,#f8fafc 0%,#f3f6fa 100%);border:1px solid #e4e8ef;border-radius:14px;scroll-behavior:smooth}
 .msg{display:flex;align-items:flex-start;gap:9px;padding:6px 0;border:0;font-size:14px;line-height:1.55}
 .msg .avatar{width:34px;height:34px;flex:0 0 34px;display:flex;align-items:center;justify-content:center;border-radius:50%;color:#fff;font-size:12px;font-weight:700;box-shadow:0 2px 7px rgba(15,23,42,.12);user-select:none}
 .msg .msg-stack{display:flex;flex-direction:column;align-items:flex-start;gap:4px;max-width:min(74%,620px)}
 .msg .msg-meta{display:flex;align-items:center;gap:6px;min-height:18px;padding:0 4px;color:#7b8493;font-size:11px}
 .msg .who{font-size:12px;font-weight:700;color:#4b5563}
 .msg .at{display:inline-block;padding:1px 6px;border-radius:999px;background:#eef2ff;color:#4338ca;font-size:10px;line-height:16px}
 .msg .time{color:#9aa1ad;font-size:10px;white-space:nowrap}
 .msg .bubble{padding:9px 12px;border:1px solid transparent;border-radius:5px 15px 15px 15px;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;box-shadow:0 1px 2px rgba(15,23,42,.06)}
 .sender-human{flex-direction:row-reverse}
 .sender-human .msg-stack{align-items:flex-end}
 .sender-human .msg-meta{justify-content:flex-end}
.sender-human .bubble{background:#fff0e6;border-color:#fed7bd;border-radius:15px 5px 15px 15px;color:#663115}
.sender-human .avatar{background:#ea580c}
.sender-rikka .bubble{background:#dceeff;border-color:#bddcff;color:#173b68}
.sender-rikka .avatar{background:#2563eb}
 .sender-claude .bubble{background:#f3e8ff;border-color:#e4c8ff;color:#4c1d66}
 .sender-claude .avatar{background:#7e22ce}
 .sender-codex .bubble{background:#e0f7f4;border-color:#b8e8e2;color:#164e49}
 .sender-codex .avatar{background:#0f766e}
 .sender-dsh .bubble{background:#fff7d6;border-color:#f4df92;color:#5c4212}
 .sender-dsh .avatar{background:#b45309}
 .sender-other .bubble{background:#fff;border-color:#e1e5eb;color:#374151}
 .sender-other .avatar{background:#64748b}
 .row{display:flex;gap:8px;margin-top:10px}
 input[type=text],input[type=password],textarea{flex:1;padding:9px 11px;border:1px solid #ccd0d5;border-radius:8px;font-size:14px;font-family:inherit}
 #inbox{flex:1;box-sizing:border-box;resize:none;min-height:38px;max-height:40vh;overflow-y:auto;line-height:1.5}
 button.act{background:#1a66cc;color:#fff;border:none;border-radius:8px;padding:9px 16px;font-size:14px;cursor:pointer}
 button.act.gray{background:#6b7280}
 button.ok{background:#16a34a} button.no{background:#dc2626}
 .login{max-width:360px;margin:80px auto}
 .pill{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;background:#eef1f4;margin-left:6px}
 .pill.queued{background:#fef3c7}.pill.running{background:#dbeafe}.pill.completed{background:#dcfce7}
 .pill.failed,.pill.cancelled{background:#fee2e2}.pill.needs_human,.pill.waiting_approval{background:#fde68a}
 .pill.waiting_input{background:#e9d5ff}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td,th{padding:7px 8px;border-bottom:1px solid #f0f1f3;text-align:left;vertical-align:top}
 .hidden{display:none}
 #err{color:#dc2626;font-size:13px;margin-top:6px}
 @media (max-width:600px){
  header{padding:8px 10px;gap:6px} header h1{font-size:15px}
  .tabs button{padding:7px 9px;font-size:13px}
  main{margin:8px auto;padding:0 6px}
  .card{padding:10px;margin-bottom:8px;border-radius:12px}
  #msgs{min-height:420px;max-height:calc(100vh - 245px);padding:11px 9px}
  .msg{gap:7px;padding:5px 0}
  .msg .avatar{width:30px;height:30px;flex-basis:30px;font-size:11px}
  .msg .msg-stack{max-width:82%}
  .msg .bubble{padding:8px 10px}
  .row{gap:6px}
  button.act{padding:9px 12px}
 }
</style>
</head>
<body>
<header id="topbar" class="hidden">
 <h1>tech-hub</h1>
 <div class="tabs">
  <button id="tab-chat" class="on">群聊</button>
  <button id="tab-task">任务</button>
  <button id="tab-ap">审批</button>
 </div>
 <span style="flex:1"></span>
 <button class="act gray" onclick="doLogout()">退出</button>
</header>
<div id="login" class="login card hidden">
 <h2>tech-hub 登录</h2>
 <p style="color:#6b7280;font-size:13px">使用 human Token 登录（仅存于 httpOnly Cookie）</p>
 <input type="password" id="tok" placeholder="human token">
 <div style="margin-top:10px"><button class="act" onclick="doLogin()">登录</button></div>
 <div id="err"></div>
</div>
<main id="main" class="hidden">
 <div id="pane-chat">
  <div class="card">
   <b>房间</b>
   <select id="roomsel" onchange="loadMsgs(true)"></select>
   <button class="act gray" style="padding:3px 10px;font-size:12px" onclick="jumpBottom()">↓ 回到最新</button>
   <div id="foldbar" style="display:none;margin-top:8px;font-size:12px">
    <div id="foldcards"></div>
   </div>
   <div id="segbar" style="display:none;background:#eef2ff;border:1px solid #a5b4fc;border-radius:8px;padding:4px 10px;margin-top:8px;font-size:12px;color:#3730a3">
    <span id="segtext"></span>
    <button class="act gray" style="padding:2px 8px;font-size:12px;margin-left:8px" onclick="backToLatest()">返回最新</button>
   </div>
   <div id="msgs" aria-live="polite" aria-label="群聊消息"></div>
   <div class="row">
    <textarea id="inbox" rows="1" placeholder="发消息…"></textarea>
    <button class="act gray" id="attachbtn" style="padding:6px 12px;font-size:14px" title="发送图片/文件">📎</button>
    <input type="file" id="filein" style="display:none">
    <button class="act" onclick="sendMsg()">发送</button>
    <span id="senderr" style="color:#c0392b;font-size:13px"></span>
   </div>
  </div>
 </div>
 <div id="pane-task" class="hidden">
  <div class="card"><table id="tasklist"><thead><tr><th>任务</th><th>状态</th><th>来自</th><th>请求</th><th>更新</th></tr></thead><tbody></tbody></table></div>
 </div>
 <div id="pane-ap" class="hidden">
  <div class="card" id="aplist">暂无待审批项</div>
 </div>
</main>
<script>
const H = {'X-Requested-With':'XMLHttpRequest'};
function uid(){ return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c=>{ const r=Math.random()*16|0; return (c==='x'?r:(r&0x3|0x8)).toString(16); }); }
async function j(method, path, body){
  const opt = {method, headers: H, credentials:'same-origin'};
  opt.headers['Idempotency-Key'] = uid();
  if (body !== undefined){ opt.headers['Content-Type']='application/json'; opt.body=JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status === 401){ showLogin(); throw new Error('401'); }
  const data = await r.json().catch(()=>null);
  if (!r.ok) throw new Error((data && data.message) || r.status);
  return data;
}
function showLogin(){ document.getElementById('login').classList.remove('hidden');
  document.getElementById('topbar').classList.add('hidden'); document.getElementById('main').classList.add('hidden'); }
function showMain(){ document.getElementById('login').classList.add('hidden');
  document.getElementById('topbar').classList.remove('hidden'); document.getElementById('main').classList.remove('hidden'); }
async function doLogin(){
  const tok = document.getElementById('tok').value;
  const fd = new URLSearchParams({token: tok});
  const r = await fetch('/ui/login', {method:'POST', headers:{'X-Requested-With':'XMLHttpRequest','Content-Type':'application/x-www-form-urlencoded'}, body: fd, redirect:'manual'});
  if (r.ok || r.type === 'opaqueredirect'){ await boot(); } else { document.getElementById('err').textContent = '登录失败'; }
}
async function doLogout(){
  await fetch('/ui/logout', {method:'POST', headers:H, credentials:'same-origin'}).catch(()=>{});
  showLogin();
}
function esc(s){ const d=document.createElement('div'); d.textContent=s==null?'':String(s); return d.innerHTML; }
const PEOPLE = {
  human:{label:'Human', avatar:'H'},
  rikka:{label:'Rikka', avatar:'R'},
  claude:{label:'Claude', avatar:'C'},
  codex:{label:'Codex', avatar:'G'},
  dsh:{label:'DSH', avatar:'D'}
};
function person(id){ return PEOPLE[id] || {label:id || '未知', avatar:'?'}; }
function senderClass(id){ return Object.prototype.hasOwnProperty.call(PEOPLE,id) ? id : 'other'; }
function parseHubTime(s){
  if (!s) return null;
  // Hub timestamps are UTC. Older rows may omit an explicit offset, while
  // newer rows already end in Z or +00:00; never append a second suffix.
  const raw = /(?:Z|[+-][0-9][0-9]:[0-9][0-9])$/i.test(s) ? s : s + 'Z';
  const d = new Date(raw);
  return isNaN(d.getTime()) ? null : d;
}
const cnTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone:'Asia/Shanghai', hour12:false,
  hour:'2-digit', minute:'2-digit', second:'2-digit'
});
const cnDateTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone:'Asia/Shanghai', hour12:false,
  month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'
});
function tlocal(s){ const d=parseHubTime(s); return d ? cnTime.format(d) : (s||''); }
function tcn(s){ const d=parseHubTime(s); return d ? cnDateTime.format(d).replaceAll('/','-') : (s||''); }
let room='general', cur=0, loading=false, foldsList=[], foldsExpanded=false, viewSeg=null;
async function boot(){
  try { await j('GET','/ui/tasks'); showMain(); loadRooms(); loadFolds(); loadMsgs(true); setInterval(()=>{ loadMsgs(false).catch(()=>{}); loadTasks().catch(()=>{}); loadAps().catch(()=>{}); }, 4000); }
  catch(e){ showLogin(); }
}
async function loadRooms(){
  const d = await j('GET','/rooms');
  const sel = document.getElementById('roomsel'); sel.innerHTML='';
  for (const r of d.rooms){ const o=document.createElement('option'); o.value=r.room; o.textContent=r.room; sel.appendChild(o); }
}
async function loadMsgs(reset){
  if (loading) return;
  loading = true;
  try {
  if (reset) cur = 0;
  const box = document.getElementById('msgs');
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  if (reset) box.innerHTML='';
  let rounds = 0;
  while (true){
    let q = '/rooms/'+room+'/messages?after='+cur+'&limit=50';
    if (viewSeg) q += '&until='+viewSeg.until;
    const d = await j('GET', q);
    if (rounds === 0 && cur === 0){
      if (!viewSeg && d.fold && d.fold.fold_after_seq > 0){
        cur = d.fold.fold_after_seq;
      }
    }
    // 相邻同 from 同 text 的事件折叠为一条(多收件人扇出/客户端重发), @徽标合并收件人
    const groups = [];
    for (const e of d.events){
      const p = e.payload || {};
      const att = p.attachment || null;
      const g = groups[groups.length-1];
      if (g && !att && !g.att && g.from === e.from && g.text === (p.text||'')){
        if (e.to) g.tos.push(e.to); g.seq = e.seq; g.at = e.created_at;
      } else {
        groups.push({from:e.from, text:p.text||'', tos:e.to?[e.to]:[], seq:e.seq, at:e.created_at, att:att});
      }
    }
    for (const g of groups){
      cur = Math.max(cur, g.seq);
      const identity = person(g.from);
      const div = document.createElement('div'); div.className='msg sender-'+senderClass(g.from);
      div.dataset.from = g.from || 'unknown';
      const targets = g.tos.map(t=>'<span class="at">@'+esc(person(t).label)+'</span>').join('');
      const attBox = g.att ? '<div style="margin-top:6px">'+attHtml(g.att)+'</div>' : '';
      div.innerHTML = '<div class="avatar" title="'+esc(identity.label)+'">'+esc(identity.avatar)+'</div>'+
        '<div class="msg-stack"><div class="msg-meta"><span class="who">'+esc(identity.label)+'</span>'+targets+
        '<span class="time">'+tlocal(g.at)+'</span></div><div class="bubble">'+esc(g.text)+attBox+'</div></div>';
      box.appendChild(div);
    }
    if (!reset || d.events.length < 50 || ++rounds > 20) break;
  }
  if (reset || nearBottom) box.scrollTop = box.scrollHeight;
  const sb = document.getElementById('segbar');
  const st = document.getElementById('segtext');
  if (viewSeg){
    sb.style.display = '';
    st.textContent = '正在查看折叠段 seq' + (viewSeg.after+1) + '–' + viewSeg.until + '：' + (viewSeg.label || '该段历史');
  } else {
    sb.style.display = 'none';
  }
  } finally { loading = false; }
}
async function loadFolds(){
  try {
    const d = await j('GET','/rooms/'+room+'/folds');
    foldsList = d.folds || [];
    renderFolds();
  } catch(e){}
}
function foldCardHtml(f, idx){
  const s = f.summary || '无摘要';
  const short = s.length > 30 ? s.slice(0,30) + '…' : s;
  return '<div class="foldcard" style="display:flex;align-items:center;gap:6px;padding:4px 10px;margin-top:4px;background:#fff7ed;border:1px solid #fdba74;border-radius:8px;cursor:pointer;font-size:12px;color:#7c2d12" onclick="openFold(' + idx + ')">' +
    '<span>📁</span><span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:340px">' + esc(short) + '</span>' +
    '<span style="margin-left:auto;white-space:nowrap;color:#9a3412;opacity:.75">seq≤' + f.fold_after_seq + ' · ' + tcn(f.folded_at) + '</span></div>';
}
function renderFolds(){
  const fb = document.getElementById('foldbar');
  const box = document.getElementById('foldcards');
  if (!foldsList.length){ fb.style.display='none'; box.innerHTML=''; return; }
  fb.style.display = '';
  let html = '';
  if (foldsExpanded || foldsList.length <= 2){
    for (let i=0;i<foldsList.length;i++) html += foldCardHtml(foldsList[i], i);
  } else {
    html = foldCardHtml(foldsList[0], 0);
    const hidden = foldsList.length - 1;
    html += '<div class="foldcard" style="display:flex;align-items:center;justify-content:center;gap:6px;padding:4px 10px;margin-top:4px;background:#fef3c7;border:1px dashed #f59e0b;border-radius:8px;cursor:pointer;font-size:12px;color:#92400e" onclick="event.stopPropagation();foldsExpanded=true;renderFolds()">📚 还有 ' + hidden + ' 段已折叠 · 展开列表 ▾</div>';
  }
  if (foldsExpanded && foldsList.length > 2){
    html += '<div class="foldcard" style="display:flex;align-items:center;justify-content:center;gap:6px;padding:4px 10px;margin-top:4px;background:#fef3c7;border:1px dashed #f59e0b;border-radius:8px;cursor:pointer;font-size:12px;color:#92400e" onclick="event.stopPropagation();foldsExpanded=false;renderFolds()">▲ 收起列表</div>';
  }
  box.innerHTML = html;
}
async function openFold(idx){
  const f = foldsList[idx];
  if (!f) return;
  const prev = idx > 0 ? foldsList[idx-1].fold_after_seq : 0;
  viewSeg = {after: prev, until: f.fold_after_seq, label: f.summary || ''};
  await loadMsgs(true);
}
function backToLatest(){
  viewSeg = null;
  loadMsgs(true);
}
function jumpBottom(){
  backToLatest();
}
function attHtml(a){
  const url = '/attachments/'+a.id;
  if (a.kind === 'image'){
    return '<a href="'+url+'" target="_blank"><img src="'+url+'" alt="'+esc(a.filename)+'" style="max-width:240px;max-height:240px;border-radius:8px;display:block"></a>';
  }
  const kb = a.size > 1024 ? Math.round(a.size/1024) + ' KB' : a.size + ' B';
  return '<a href="'+url+'?download=1" style="display:inline-flex;gap:8px;align-items:center;padding:6px 10px;background:#fff;border:1px solid #e2e8f0;border-radius:8px;text-decoration:none;color:#1e293b;font-size:13px">📄 '+esc(a.filename)+' <span style="color:#94a3b8">'+kb+'</span></a>';
}
async function sendFile(f){
  const err = document.getElementById('senderr');
  err.textContent = '';
  const text = document.getElementById('inbox').value.trim();
  if (f.size > 30*1024*1024){ err.textContent = '文件超过 30MB 上限'; return; }
  if (f.type && f.type.startsWith('image/') && f.size > 10*1024*1024){ err.textContent = '图片超过 10MB 上限'; return; }
  try {
    const q = '/rooms/'+room+'/attachments?filename='+encodeURIComponent(f.name)+'&text='+encodeURIComponent(text.slice(0,2000));
    const r = await fetch(q, {method:'POST', headers:{'X-Requested-With':'XMLHttpRequest','Idempotency-Key':uid()}, credentials:'same-origin', body:f});
    if (r.status === 401){ showLogin(); throw new Error('401'); }
    const data = await r.json().catch(()=>null);
    if (!r.ok) throw new Error((data && data.message) || r.status);
    document.getElementById('inbox').value='';
    loadMsgs(false);
  } catch(e){ err.textContent = '发送失败: ' + (e.message || e); }
}
document.getElementById('attachbtn').onclick = ()=> document.getElementById('filein').click();
document.getElementById('filein').addEventListener('change', (e)=>{ const f = e.target.files[0]; e.target.value=''; if (f) sendFile(f); });
async function sendMsg(){
  const t = document.getElementById('inbox').value.trim();
  if (!t) return;
  const err = document.getElementById('senderr');
  err.textContent = '';
  try {
    await j('POST','/rooms/'+room+'/messages',{text:t});
    document.getElementById('inbox').value='';
    fitInbox();
    // Append the newly sent event instead of rebuilding the whole timeline.
    await loadMsgs(false);
    const box = document.getElementById('msgs');
    box.scrollTop = box.scrollHeight;
  } catch(e) {
    err.textContent = '发送失败: ' + (e.message || e);
  }
}
async function loadTasks(){
  const d = await j('GET','/ui/tasks');
  const tb = document.querySelector('#tasklist tbody'); tb.innerHTML='';
  for (const t of d.tasks){
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>'+esc(t.task_id).slice(0,8)+'</td><td><span class="pill '+esc(t.status)+'">'+esc(t.status)+'</span></td><td>'+esc(t.from)+'</td><td>'+esc(t.request).slice(0,80)+'</td><td>'+esc(tcn(t.updated_at))+'</td>';
    tb.appendChild(tr);
  }
}
async function loadAps(){
  const d = await j('GET','/ui/approvals');
  const box = document.getElementById('aplist');
  if (!d.approvals.length){ box.textContent='暂无待审批项'; return; }
  box.innerHTML='';
  for (const a of d.approvals){
    const div = document.createElement('div'); div.className='card';
    div.innerHTML = '<b>'+esc(a.op_summary)+'</b><div style="font-size:13px;color:#6b7280">task '+esc(a.task_id).slice(0,8)+' · 风险 '+esc(a.risk_level)+' · 截止 '+esc(tcn(a.expires_at))+'</div><div class="row"><button class="act ok" data-eid="'+esc(a.event_id)+'" data-tid="'+esc(a.task_id)+'" data-d="approve">批准</button><button class="act no" data-eid="'+esc(a.event_id)+'" data-tid="'+esc(a.task_id)+'" data-d="reject">拒绝</button></div>';
    box.appendChild(div);
  }
  box.querySelectorAll('button[data-eid]').forEach(b=>{
    b.onclick = ()=> j('POST','/approval',{task_id:b.dataset.tid, event_id:b.dataset.eid, decision:b.dataset.d}).then(loadAps).catch(()=>loadAps());
  });
}
document.getElementById('tab-chat').onclick=()=>{sw('chat')};
document.getElementById('tab-task').onclick=()=>{sw('task')};
document.getElementById('tab-ap').onclick=()=>{sw('ap')};
function sw(t){
  document.getElementById('pane-chat').classList.toggle('hidden', t!=='chat');
  document.getElementById('pane-task').classList.toggle('hidden', t!=='task');
  document.getElementById('pane-ap').classList.toggle('hidden', t!=='ap');
  for (const x of ['chat','task','ap']) document.getElementById('tab-'+x).classList.toggle('on', t===x);
}
document.getElementById('roomsel').onchange = ()=>{ room = document.getElementById('roomsel').value; loadFolds(); loadMsgs(true); };
document.getElementById('inbox').addEventListener('keydown', e=>{ if(e.key==='Enter' && !e.shiftKey && !e.isComposing){ e.preventDefault(); sendMsg(); } });
// Auto-fit the human input, WeChat style: width is always the full row;
// only the height grows with the line count, up to half the chat card,
// then the box scrolls internally while editing.
const inboxEl=document.getElementById('inbox');
function fitInbox(){
  const card=document.getElementById('msgs').parentElement;
  const maxH=Math.max(120, card.clientHeight*0.5);
  inboxEl.style.height='auto';
  inboxEl.style.height=Math.min(Math.max(38, inboxEl.scrollHeight), maxH)+'px';
}
inboxEl.addEventListener('input', fitInbox);
window.addEventListener('resize', fitInbox);
boot();
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning", access_log=False)
