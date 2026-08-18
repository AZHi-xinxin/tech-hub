# tech-hub Windows Codex worker

这个目录包含 Codex 的两个独立执行通道：

1. **任务 worker**：领取 tech-hub 任务，在白名单工作区内调用 codex exec，把结果写回 hub。
2. **真会话门铃**：发现给 Codex 的群聊新消息后，只把一条很短的门铃提示注入当前 VS Code Codex 对话框，让原会话自己读群、领取、判断和执行。

真会话门铃不是另起一个无上下文的 Agent。它唤醒的是已经打开、已经带着当前上下文的 VS Code Codex 会话。完整设计见同级文档 ../开源文件/WAKEUP-CODEX.md。

## 文件

- codex_worker.py：长轮询、任务领取、心跳、风险映射、结果回写、门铃过滤与游标。
- inject-codex-ui.ps1：Windows 前台注入器，仅输入门铃提示，不输入群聊正文。
- start-codex-worker.ps1：定位当前用户安装的 Codex 扩展与 Python，循环守护 worker。
- codex-worker.config.example.json：不含凭证的通用配置。
- test_codex_worker.py：离线单元测试。

## 环境要求

- Windows 10/11。
- Python 3.10 或更高版本。
- VS Code 已安装 Codex 扩展。
- tech-hub 可从本机访问；可以使用本机、局域网或私有组网地址。
- 使用真会话门铃时，VS Code 主窗口必须保持打开，目标 Codex 会话必须已经显示在侧栏中。

Windows worker 只主动连接 hub，不需要开放 Windows 入站端口。

## 配置

复制示例文件：

~~~powershell
Copy-Item .\codex-worker.config.example.json .\codex-worker.config.json
~~~

编辑工作副本：

- hub_url：你的 tech-hub 地址。
- workspaces：允许任务访问的 workspace_id 到本机绝对路径映射。
- ui_injector_command：本目录中 inject-codex-ui.ps1 的绝对路径。
- room_cursor_path：门铃成功位置的本地游标文件路径。
- 初次验证保持 dry_run: true。

Token 只放在当前 Windows 用户可读的本地文件中，默认位置：

~~~text
%USERPROFILE%\.tech-hub\secrets\codex.token
~~~

文件只写一行 token。不要把 token 写进 JSON、日志、Git、聊天记录或记忆库。

## 离线验证

~~~powershell
python -m py_compile .\codex_worker.py .\test_codex_worker.py
python -m unittest -v .\test_codex_worker.py
~~~

再保持 dry_run: true 执行一次：

~~~powershell
$env:TECH_HUB_CODEX_TOKEN = (Get-Content "$env:USERPROFILE\.tech-hub\secrets\codex.token" -Raw).Trim()
python .\codex_worker.py --config .\codex-worker.config.json --once --dry-run
~~~

确认领取、日志、结果和游标行为都正确后，才把工作副本中的 dry_run 改为 false。

## 启动

~~~powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-codex-worker.ps1
~~~

也可以显式指定路径：

~~~powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start-codex-worker.ps1 -WorkerRoot "C:\path\to\tech-hub-worker" -TokenPath "C:\path\to\private\codex.token"
~~~

## 门铃的安全规则

- 只统计 human 与 rikka 发出的非空消息。
- 只处理广播或明确发给 codex、all、* 的消息。
- 注入内容只有“检测到 N 条未读消息，请先去 general 领取任务”，不复制群聊正文。
- 只有 UI 注入器成功后才推进游标；失败时保留未读，方便重试。
- 单实例文件锁阻止两个 worker 同时消费同一队列。
- 门铃任务不调用无上下文的 codex exec；普通技术任务才走 headless 执行器。

## 普通任务的安全规则

- 未登记的 workspace_id 直接转为 needs_human。
- read_only 映射到 Codex read-only 沙箱。
- workspace_edit 映射到 workspace-write 沙箱。
- dangerous 不自动执行，只请求人类审批。
- 不使用 danger-full-access，worker 不能替人类批准。

## 已知边界

UI 注入器是 Windows 桌面自动化：

- 会把 VS Code 拉到前台，因此可能抢走当前输入焦点。
- 点击位置按窗口比例计算；缩放、布局、侧栏位置或多窗口可能导致点错。
- 当前选择第一个可见的 VS Code 主窗口，多窗口环境应先关闭无关窗口。
- 窗口过小会 fail-closed，不尝试盲点。
- 它不是 VS Code 官方扩展 API；升级 VS Code/Codex UI 后应先 dry-run 并人工观察一次。

如果环境不适合 UI 注入，可关闭真会话门铃，只保留 headless 任务 worker；二者互不依赖。
