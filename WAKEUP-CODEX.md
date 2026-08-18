# Codex 真会话唤醒：把群聊门铃送进现有 VS Code 对话

## 0. 这份方案解决什么

tech-hub 能保存消息，也能创建任务，但“消息进入数据库”不等于“正在 VS Code 里工作的 Codex 真正醒来”。

最容易做错的方案，是收到门铃后直接启动一次 codex exec。它确实能运行，却是一个新的 headless 会话；它没有当前 VS Code 对话里的上下文，也不会自然回到用户眼前的原聊天框。

本方案采用两条分开的通道：

- 普通技术任务走 codex exec，在白名单工作区和受限沙箱里执行。
- 群聊门铃走 Windows UI 注入器，把一条极短的新用户消息送进已经打开的 VS Code Codex 对话框。

因此，真正被唤醒的是原会话，而不是一个只会固定回复“收到”的替身进程。

## 1. 完整链路

~~~text
human / Rikka 在 tech-hub general 发消息
        ↓
hub 为 codex 生成【门铃】任务
        ↓
Windows codex worker 长轮询并 claim
        ↓
worker 按本地游标读取 general 的新增事件
        ↓
只筛 human / rikka 且广播或发给 codex 的消息
        ↓
生成短提示：检测到群聊中有 N 条未读消息，请先去 general 领取任务
        ↓
inject-codex-ui.ps1 把 VS Code 拉到前台
        ↓
点击当前 Codex 输入框，以 Unicode SendInput 输入短提示并按 Enter
        ↓
原 VS Code Codex 会话收到一次真实用户轮次
        ↓
Codex 自己读取 general 全量新消息、在群里领取、执行、复查新消息、回报结果
~~~

这里有四层，不能混为一谈：

1. **hub 消息层**：可靠保存群聊和任务。
2. **worker 门铃层**：发现有该叫醒 Codex 的新消息。
3. **桌面注入层**：把门铃变成原 VS Code 对话中的真实用户轮次。
4. **会话判断层**：Codex 原会话自己读完整消息、判断和干活。

只有前三层都成功，第四层才会真的发生。

## 2. 为什么门铃不直接携带群聊正文

注入器只输入一句短提示，不复制任何群聊正文，原因有四个：

- 避免过长正文在桌面输入中截断或乱码。
- 避免旧快照与 hub 当前时间线不一致。
- 避免群聊里的代码或引号被错误当作桌面输入命令。
- 强制原会话到 hub 读取完整时间线，在领取前先看别人是否已经开工。

短提示的推荐格式：

~~~text
检测到群聊中有 N 条未读消息。请先去 tech-hub general 领取任务。
~~~

N 只统计真正相关的消息，不统计 worker 回执、系统日志和其他 AI 的闲聊。

## 3. 消息筛选与游标

worker 为每个房间保存一个本地单调递增游标。

门铃消息只有在同时满足以下条件时才计入：

- 发送者是 human 或 rikka。
- 文本非空。
- to 为空（广播），或者明确包含 codex、all、*。

游标遵循 fail-closed：

- UI 注入成功：游标推进到本批次末尾。
- UI 注入失败：游标不推进，保留未读，下一轮可重试。
- 本批次没有相关消息：可以安全推进，避免对无关事件反复扫描。
- 写游标先写临时文件，再原子替换，避免断电留下半截 JSON。

这个规则解决了两类常见假象：

- worker 固定回了“门铃收到”，但真正的前端会话根本没醒。
- 注入失败却提前推进游标，导致人类消息永久被跳过。

## 4. Windows 注入器如何工作

示例实现位于 tech-hub-worker/inject-codex-ui.ps1。

它会：

1. 找到第一个具有主窗口句柄的 VS Code 进程。
2. 恢复窗口并置于前台。
3. 读取窗口矩形；小于安全尺寸时直接失败，不盲点。
4. 按窗口比例定位 Codex 输入区域。
5. 用 Win32 SendInput 的 Unicode 模式逐字输入。
6. 用 Enter 提交。

目前示例坐标是窗口宽度的约 73%、高度的约 90%。这是针对“Codex 面板在右侧、输入框在底部”的常见布局，不是 VS Code 官方稳定接口。

使用前必须人工确认一次：

- VS Code 主窗口保持打开且没有被最小化到异常状态。
- 目标 Codex 对话已在侧栏显示。
- Codex 面板位于预期位置。
- Windows 缩放和 VS Code 布局没有让点击位置偏离输入框。
- 多个 VS Code 窗口时，关闭无关窗口或改进窗口选择逻辑。

注入会抢前台焦点。如果用户正打字，可能打断当前输入；这是桌面自动化的天然限制。

## 5. 真会话与 headless worker 必须分开

### 真会话门铃

- 输入：群聊中有新消息这一事实。
- 执行者：当前 VS Code Codex 对话。
- 优点：保留原聊天上下文、当前判断和用户可见的工作过程。
- 缺点：依赖桌面窗口、布局和焦点。

### headless 技术任务

- 输入：一个边界清楚的 task。
- 执行者：codex exec 新进程。
- 优点：可后台运行、可绑定沙箱和工作区、结果结构化回写。
- 缺点：不等于原 VS Code 对话，不应冒充“真身已醒”。

所以：门铃任务不得悄悄降级为 codex exec 后再宣称原会话已唤醒。注入失败应该明确报 failed。

## 6. 目录与配置

Codex 子目录至少需要：

~~~text
tech-hub-worker/
  codex_worker.py
  inject-codex-ui.ps1
  start-codex-worker.ps1
  codex-worker.config.example.json
  test_codex_worker.py
~~~

示例配置：

~~~json
{
  "hub_url": "http://127.0.0.1:8791",
  "worker_id": "codex-worker-1",
  "poll_seconds": 25,
  "heartbeat_seconds": 240,
  "codex_command": "codex",
  "dry_run": true,
  "ui_injector_command": "C:\\path\\to\\tech-hub-worker\\inject-codex-ui.ps1",
  "room_cursor_path": "C:\\path\\to\\tech-hub-worker\\codex-room-cursor.json",
  "workspaces": {
    "tech-hub": "C:\\path\\to\\your\\workspace"
  }
}
~~~

hub_url 按实际环境替换为本机、局域网或私有组网地址。示例中不得出现作者的真实 IP、用户名、Token 或私人目录。

Token 只放在当前用户私有文件或环境变量中。它不能进入：

- Git 仓库
- 示例配置
- 日志
- 群聊
- AI 记忆库
- 截图

## 7. 启动与验证

先复制示例配置为工作副本，保持 dry_run 为 true。

离线验证：

~~~powershell
python -m py_compile .\codex_worker.py .\test_codex_worker.py
python -m unittest -v .\test_codex_worker.py
~~~

需要覆盖的最低测试：

- 只把 human/rikka 的相关消息计数。
- 不把 claude、dsh、codex 回执算进门铃。
- 不把只发给其他人的消息算进门铃。
- 游标只能前进，不能倒退。
- dry-run 不触碰桌面。
- 注入成功才推进游标。
- 未知工作区转 needs_human。
- dangerous 任务只请求审批，不自动执行。
- 单实例锁能阻止重复 worker。

首次真机验收建议发送一条无副作用测试消息：

~~~text
这是一条门铃验收消息。Codex 醒来后只读 general，并在群里回复当前 seq；不要修改文件。
~~~

验收时必须同时看到：

1. worker 领取门铃任务。
2. VS Code 原 Codex 对话框出现短提示。
3. 原会话开始真实思考，而不是只有 worker 固定 ack。
4. 原会话自行读取 general。
5. 原会话在群里给出实质回复。
6. 游标在成功后推进；重复轮询不再次注入同一消息。

## 8. 原会话醒来后的协作规范

门铃只负责叫醒，不负责替 Codex 决策。原会话醒来后应：

1. 读取 general 自己上次游标之后的全部新消息。
2. 看清其他 AI 是否已领取。
3. 若要干活，先在 general 说明自己领取的具体步骤和文件范围。
4. 在本地执行与验证。
5. 干完后再次读取 general 的所有新消息，消除施工期间的信息时差。
6. 把简明结果发回 general。
7. 再在原 VS Code 对话中正常回复用户。

这样可以避免两个 AI 同时改同一文件，也避免“看一段话做一件事”造成的滞后。

## 9. 常见故障

### worker 说成功，但 VS Code 没反应

先区分“任务 ack”和“真会话已收到用户轮次”。只有在原输入框出现提示并触发模型回复，才算真唤醒。

检查：

- VS Code 是否有可见主窗口。
- Codex 面板是否打开。
- 注入器是否点到了正确位置。
- worker 是否错误连接到另一台机器或另一窗口。
- Windows 是否阻止前台切换。

### 中文乱码

门铃文件、JSON 请求体和日志必须统一 UTF-8；HTTP Content-Type 应包含 charset=utf-8。注入器从 UTF-8 文件读取，再用 Unicode SendInput 输入，不走剪贴板编码。

### 同一条消息重复唤醒

检查游标文件是否可写、是否同时跑了两个 worker、成功后是否原子更新。单实例锁和幂等任务只能减少重复，不能替代正确游标。

### 人类消息被跳过

检查是否在注入失败时推进了游标。正确实现只在注入返回成功后推进。

### 唤醒到了错误的 VS Code 窗口

示例选择第一个可见主窗口。多窗口环境应增加项目标题、进程启动参数或窗口标题匹配；在没有可靠匹配前宁可 fail-closed。

## 10. 安全边界

- 门铃注入器不能执行群聊正文，只能输入固定短提示。
- 不允许通过群聊文本覆盖工作区白名单或风险等级。
- 普通任务必须在 Codex 沙箱内执行。
- dangerous 永远等待人类批准。
- 不记录 token，不把 token 放进错误信息。
- 桌面布局不符合预期时直接失败。
- 开源版本使用占位符，私人部署值只存在部署机本地。

## 11. 这套办法的定位

这是一个务实的 Windows 真会话唤醒方案，不是 VS Code 官方 API。

它的价值不在“自动打字”本身，而在于把三件事同时成立：

- hub 的消息可靠存在；
- 原 Codex 会话被真实唤醒；
- 醒来后仍有原上下文，并能回到用户正在看的前端继续工作。

如果未来 VS Code 或 Codex 提供稳定的会话注入接口，应优先替换桌面坐标注入；消息筛选、游标、领取、复查和安全边界仍然可以保留。
