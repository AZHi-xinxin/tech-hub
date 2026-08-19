# tech-hub MCP

将现有 tech-hub HTTP API 包装为一个独立的、带 Bearer 鉴权的
Streamable HTTP MCP，供 RikkaHub 等 MCP 客户端直接读取群聊、发消息和
创建、跟踪任务。

它不是网页抓取器，也不修改 hub.py。MCP 服务与 tech-hub 分进程运行，
默认端口为 8793；原来的 8791 服务出现问题时仍可独立排查。

## 工具

| 工具 | 用途 | 是否写入 |
|---|---|---|
| hub_health | 检查 tech-hub 状态 | 否 |
| list_rooms | 列出房间与最新序号 | 否 |
| read_messages | 按游标增量读取聊天 | 否 |
| send_message | 以 Rikka 身份向房间发消息 | 是 |
| create_task | 创建受 tech-hub 安全策略约束的任务 | 是 |
| get_task | 查询任务及增量事件 | 否 |

刻意不开放：人工审批、历史折叠、worker 认领、管理接口。即便创建
dangerous 风险任务，也不会绕过 tech-hub 的人工审批。

## 部署

要求 Python 3.10+。以下示例假定项目位于
$HOME/tech-hub-mcp，tech-hub 位于 $HOME/tech-hub。

    cd "$HOME/tech-hub-mcp"
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    cp credentials.env.example credentials.env
    chmod 600 credentials.env

若需要与已验收环境完全一致，可将安装命令改为：

    .venv/bin/pip install -r requirements.lock.txt

编辑 credentials.env：

    TECHHUB_MCP_TOKEN=<单独生成的高强度随机值>
    TECHHUB_BASE_URL=http://127.0.0.1:8791
    TECHHUB_MCP_HOST=0.0.0.0
    TECHHUB_MCP_PORT=8793
    TECHHUB_MCP_ISSUER_URL=http://<Tailscale-IP>:8793
    TECHHUB_MCP_RESOURCE_URL=http://<Tailscale-IP>:8793/mcp
    TECHHUB_MCP_ALLOWED_HOSTS=127.0.0.1:*,localhost:*,<局域网IP>:8793,<Tailscale-IP>:8793
    TECHHUB_MCP_ALLOWED_ORIGINS=

start-techhub-mcp.sh 会从现有 tech-hub 的 credentials.env 读取
RIKKA_TOKEN，只在进程内映射为 TECHHUB_TOKEN。不要把它复制进本项目，
也不要把任一 Token 提交到 Git。

若 tech-hub 不在默认的 $HOME/tech-hub，可在启动前设置
TECHHUB_CREDENTIALS_FILE 指向它的 credentials.env。

    chmod +x start-techhub-mcp.sh
    ./start-techhub-mcp.sh

## 测试

单元测试不连接真实服务：

    .venv/bin/python -m unittest -v test_server.py

启动 MCP 后，运行官方 MCP Python 客户端烟雾测试：

    set -a
    . ./credentials.env
    set +a
    TECHHUB_MCP_TEST_URL=http://127.0.0.1:8793/mcp .venv/bin/python smoke_test.py

成功输出：

    MCP_SMOKE_OK
    TOOLS=create_task,get_task,hub_health,list_rooms,read_messages,send_message

## RikkaHub 配置

- 名称：TechHubMCP
- 类型：Streamable HTTP
- 家中 URL：http://<笔记本局域网IP>:8793/mcp
- 外出 URL：http://<笔记本Tailscale-IP>:8793/mcp
- 自定义请求头名称：Authorization
- 自定义请求头值：Bearer <TECHHUB_MCP_TOKEN>

首次连接后先让 AI 调用 hub_health、list_rooms 和
read_messages(room="general", after_seq=0, limit=5)。之后应保存
next_cursor 并在下一次传给 after_seq，避免反复读取旧消息。

## 安全边界

- MCP 客户端 Token 与 tech-hub 身份 Token 必须不同。
- Token 长度至少 32 字符，并使用常量时间比较。
- DNS rebinding 防护默认启用，只允许配置中的精确 Host。
- 正常日志不打印请求头；后端错误中的 Bearer 内容也会被二次脱敏。
- 每个写请求由适配器生成 UUIDv4 幂等键。
- 房间名、任务 UUID、文本长度、枚举参数都在转发前校验。
- credentials.env、日志、虚拟环境和缓存均被 .gitignore 排除。

## 依赖版本

为兼容现有 RikkaHub 客户端，本项目固定使用 mcp==1.29.0。服务采用
官方推荐的 Streamable HTTP，并开启 stateless JSON response 模式。
