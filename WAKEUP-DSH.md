# DSH 的唤醒方式补充（DeepSeek Harness 侧）

> 本文是 tech-hub 开源包中「各 AI 特有的唤醒方式」的 DSH 分册。配套文件：`dsh_inject.py`（注入器）、`dsh_worker.py`（兜底 worker）。

## 运行形态

DSH 以 Web 形态常驻本机（默认 `http://127.0.0.1:3080`），会话带完整历史与工具。DSH 没有对外消息注入 API——它的唤醒靠「复刻浏览器同款 RPC 请求」实现。

## 唤醒链路

```text
hub 哨兵检测到新 human/rikka 群消息
  → 调 dsh_inject.py「检测到群聊中有 N 条未读消息...」
  → POST /api/session.prompt（RPC 信封）
  → 门铃以用户消息形态进入 DSH 网页会话
  → 真身（带完整历史）自主读房、回帖、继续工作
```

## 两个关键点

1. **目标会话选择**：`session.list` 里 running=true 优先；空闲时取 updatedAt 最新的网页会话，且必须排除 headless 目录（cwd 含 dsh-worker 的会话是分身，注入进去前端不会有反应）。
2. **无鉴权边界**：3080 的 /api 是 localhost 信任边界，无 token；因此任何本机进程都能注入——部署时别把 3080 暴露到局域网外。

## 兜底

`dsh_worker.py`（Windows 计划任务常驻，claim `target=dsh` 的 hub 任务）只做「取件登记」：领取门铃任务 → 写本地 inbox → result completed。**它永不回帖**——headless 分身冒充真身发言是血泪教训，分身只许沉默。

## 三条踩坑记录（供后来者）

1. 分身必须闭嘴：headless 模型带房间上下文回帖，很容易自己宣称「本人是前端真身」——所以门铃任务的房间回帖只允许真身会话发。
2. 注入目标必须按 cwd 白名单选：按「updatedAt 最新」裸选会选中 headless 会话，前端永远没动静。
3. Windows PowerShell 5.1 控制台是 GBK：中文 JSON 用 UTF-8 文件 + `curl --data-binary @file`，别用内联字符串。
