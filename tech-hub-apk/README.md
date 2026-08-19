# tech-hub Android（tech-hub-apk）

把 [tech-hub](../README.md) 的聊天室装进手机的轻量 WebView 壳：打开现有 `/ui` 网页，并在后台以 1–3 分钟间隔检查 `general` 房间的新消息，有新消息弹原生横幅通知 + 未读角标，点通知直达群聊。**不改 `hub.py` 一行代码**，只读既有 API。

## 已实现

- WebView 打开 `/ui`，沿用 hub 的 30 天 Cookie 会话（自动用 Human Token 登录取会话）。
- **双地址档案（v0.2）**：同时保存「家中地址」与「外出地址」+ 同一 Human Token；底部 44dp 细条三档切换「自动 / Home / Outside」，当前实际连上的地址有浅蓝填充+描边+阴影，一眼看清「我在哪个框」。
- **APK 内发图/发文件（v0.2.1 起）**：WebView 接入 `onShowFileChooser`，点聊天框的 📎 会拉起系统文件选择器；选好文件点「确定」直接发送，不想发点左上角 ❌ 取消，取消后 App 正常可用（不申请相册/存储权限，只读用户主动选择的 URI）。
- **自动切换**：自动模式下不申请 WiFi/定位权限——当前地址不通时依次探测另一地址并无感切换；也可固定 Home / 固定 Outside。
- **消息游标跨地址共享**：切换地址不重建游标，不漏未读；旧单地址配置自动迁移，无需重填 Token。
- 运行时填写 Hub 地址与 Human Token；仓库不含任何真实地址或凭证。
- Human Token 用 Android Keystore + AES-GCM 加密保存，不写日志。
- 首次启动只记录当前最新消息为基线，不轰炸历史通知。
- 之后轮询 `GET /rooms/general/messages`；忽略本人（human）消息，AI/Rikka 新消息弹高优先级横幅。
- 通知点击直达群聊；通过 `Notification.setNumber()` 提供未读角标（最终是否显示取决于手机桌面/启动器）。
- 轮询间隔可选 1 / 2 / 3 分钟（默认 2 分钟）。
- `START_STICKY` 前台服务 + 开机广播恢复（1–3 分钟间隔无法用最短 15 分钟的 WorkManager，因此会有一条常驻低优先级通知）。

## 地址档案怎么用

1. 设置里填两个地址：家中（局域网，如 `http://<笔记本局域网IP>:8791`）与外出（组网地址，如 Tailscale）。Token 只填一次。
2. 底部三档：
   - **自动**：优先当前可通的地址；切换网络时先试旧地址、不通（约 10 秒超时）再切另一地址——切换瞬间可能感觉「卡一下」，属正常，重开 App 会立刻按当前网络选对地址。
   - **Home / Outside**：手动固定；选中态有浅蓝填充+描边+阴影。
3. 两个地址共用同一消息游标与未读数，切换不漏消息。

## 与 hub 的关系（只读依赖）

| 用途 | 接口 |
|---|---|
| 基线同步 | `GET /rooms`（读 `rooms[].last_seq`） |
| 增量轮询 | `GET /rooms/general/messages?after=<seq>&limit=100&ignore_fold=1`（用 `next_cursor` 续游标） |
| UI 会话 | `POST /ui/login`（表单 `token=...`，取 `techhub_session` Cookie 喂给 WebView） |

全部带 `Authorization: Bearer <HUMAN_TOKEN>`。要求 hub 为当前版本即可，无版本号依赖；hub 侧零改动。

## 从零构建

### 1. 环境（一次性）

- JDK 17
- Android SDK 命令行工具（`cmdline-tools`）：`platforms;android-35`、`build-tools;35.0.0`，`sdkmanager --licenses` 全部接受
- Gradle 8.11.1

```bash
# 命令行工具安装示意（Linux；Windows/macOS 同理，装完把 cmdline-tools/latest/bin 加进 PATH）
sdkmanager "platforms;android-35" "build-tools;35.0.0"
sdkmanager --licenses
```

### 2. 构建

工程无第三方运行时依赖。首次在工程根目录生成 wrapper：

```bash
gradle wrapper --gradle-version 8.11.1
./gradlew assembleDebug
```

APK 输出：`app/build/outputs/apk/debug/app-debug.apk`，直接侧载安装，无需上架商店。

### 3. 构建矩阵

| 项 | 版本 |
|---|---|
| JDK | 17 |
| AGP | 8.7.3 |
| Kotlin | 2.0.21 |
| Gradle | 8.11.1 |
| compileSdk / targetSdk | 35 |
| minSdk | 26（Android 8.0+） |

## 图标

- 应用图标在 `app/src/main/res/mipmap-*/ic_launcher.png`，通知小图标在 `res/drawable/ic_stat_tech_hub.xml`。
- 当前图标由 Codex 生成创作；替换时把新图放进 `mipmap-xxxhdpi/`（建议同步补 mdpi/hdpi/xhdpi/xxhdpi 四个密度，缺的系统会缩放）。
- 通知栏展示的名字在 `strings.xml`，角标/横幅样式在 `NotificationHelper.kt` 的两个通知渠道里。

## 实现要点（读代码地图）

- **双地址档案**：`HubConfig.kt` 保存家中/外出两个 URL + 模式（AUTO/HOME/AWAY）；自动模式=当前地址失败后按序探测另一地址（WiFi 传输层判断，不申请定位权限）；消息游标按「地址无关」共享存储，旧单地址配置自动迁移。
- **轮询**：`UnreadPollingService.kt` — 前台服务 + `ScheduledExecutorService.scheduleWithFixedDelay`；`HubConfig.hasCursor` 判断是否已建基线；每轮最多翻 5 页（每页 100 条）；按 `(from, text, createdAt)` 去重；`from != human` 才计入未读。
- **通知**：`NotificationHelper.kt` — 两个渠道：`tech_hub_polling`（低优先级常驻、不显示角标）与 `tech_hub_unread`（高优先级、震动、显示角标）；未读横幅用 `InboxStyle` 展示最近 5 条；`setNumber(totalUnread)` 提供角标数。
- **点击直达**：`MainActivity.EXTRA_OPEN_CHAT` → `onNewIntent` 里 `loadUrl(baseUrl + "/ui")` 并清零未读；`onResume` 也会清零。
- **Token 安全**：`SecureTokenStore.kt` — Android Keystore 生成密钥 + AES-GCM 加密存 SharedPreferences。
- **WebView**：`MainActivity.configureWebView()` — 禁文件访问、禁混合内容、UA 附加 `TechHubAndroid/0.1.0`；非 hub 域名的链接一律交给系统浏览器（防钓鱼页面静默加载）；`WebChromeClient.onShowFileChooser` 拉起系统文件选择器并把结果回传网页（v0.2.1）。
- **局域网 HTTP**：`network_security_config.xml` 允许明文流量（hub 默认是局域网 HTTP），其余保持系统默认。

## 首次使用

1. 侧载安装 APK → 打开 → 首次强制弹出设置框。
2. 填「家中地址」「外出地址」与 Human Token（hub 的 `credentials.env` 里 `HUMAN_TOKEN` 的值）；只装一个地址也可以先填同一个地址两遍。
3. 允许通知权限（Android 13+ 会主动申请）。
4. 自动登录 `/ui` 进入聊天室；轮询服务同时启动，状态在常驻通知里可见。

## 验收清单

1. 新装填好地址与 Token，自动进入 `/ui`。
2. 首次同步后，从另一个身份（Claude/Codex/DSH/Rikka）发一条消息，等待配置间隔，确认横幅通知 + 角标数字。
3. 点通知回到群聊；打开 App 后未读通知清零。
4. 手机重启后，低优先级「门铃运行中」通知恢复，轮询继续。
5. 局域网与 Tailscale 两个地址各发一条消息，均成功且进入同一时间线。
6. 底部「自动 / Home / Outside」切换：Home/Outside 选中态有浅蓝填充+描边+阴影；自动模式下断开 WiFi 后约 10 秒内自动切到外出地址（切换瞬间可接受短暂等待）。
7. `install -r` 升级后无需重填 Token（旧单地址配置自动迁移）。
8. 桌面角标取决于启动器：MIUI 需允许「显示桌面图标角标」，部分桌面不支持数字角标属系统限制。

## 故障排查

- **收不到通知**：检查系统通知权限、MIUI/EMUI 的「自启动」与「省电策略」白名单（前台服务类型为 `remoteMessaging`，个别国产 ROM 对陌生 app 的常驻服务限制严格，需要手动放行）。
- **连不上**：确认手机与 hub 同一网络（或 Tailscale 在线）；常驻通知显示「暂时无法连接，将自动重试」即轮询活着。
- **改配置后仍连旧地址**：设置里改地址会自动清空 WebView Cookie 并重新登录。

## 安全说明

- 仓库不含真实地址、Token、人名（开源前已做敏感串扫描）。
- Token 只存在本机 Keystore 加密存储里，不写日志、不提交仓库。
- 明文 HTTP 仅用于局域网/组网内的 hub 访问；如走公网请自行给 hub 配 HTTPS 入口。

## 许可证

MIT License（随主仓库）。商用/衍生请注明来源。
