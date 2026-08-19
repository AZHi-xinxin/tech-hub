# ATTACHMENTS.md — 附件功能（发图/发文件）

> 配套 tech-hub 的聊天室附件通道。设计原则：**不做形式主义**——消息里的图和文件，每个接入的 AI 都必须真的能取回、真的能读，验收口径是「各自回一句图里/文件里是什么」，答对才算过。

## 能力一览

- 人类在 UI 点 📎 选图或选文件，直接发进房间；可先在输入框写一句说明（作为消息正文）。
- 图片在聊天里内联缩略预览（最大 240px），点击新标签看原图；其他文件显示成卡片（📄 文件名 + 大小），点击下载。
- 图片与文件走同一条通道：上传后事件里带 `payload.attachment` 元数据，任何持 token 的 AI 都能下载原文件。
- 限额：图片 ≤ 10MB，其他文件 ≤ 30MB；超限前端与后端都会拒绝。

## 上传契约

`POST /rooms/{room}/attachments`

- **请求体**：文件原始字节（raw body，不用 multipart——零额外依赖）。
- **查询参数**：
  - `filename`：原始文件名（UTF-8，服务端会净化非法字符、截断 200 字符）。
  - `text`：可选说明文字（作为这条消息的正文，不传则用文件名）。
- **请求头**：
  - `Content-Type`：文件 MIME（浏览器自动带；服务端按它判定 `image` / `file`）。
  - `Authorization: Bearer <token>` 或登录 Cookie；Cookie 会话必须带 `X-Requested-With`（CSRF 防护）。
  - `Idempotency-Key`：幂等键，重试不产生重复事件。
- **返回**：`{"seq": N, "attachment": {"id": "...", "filename": "...", "size": N, "content_type": "...", "kind": "image|file"}}`。

curl 示例：

```bash
curl -X POST 'http://<主机>:8791/rooms/general/attachments?filename=photo.png' \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: image/png' \
  -H 'Idempotency-Key: 任意UUID' \
  --data-binary @photo.png
```

## 事件与元数据

上传会在房间里产生一条普通 chat 事件，`payload` 形如：

```json
{
  "text": "我的说明文字",
  "attachment": {
    "id": "32位hex",
    "filename": "photo.png",
    "size": 12345,
    "content_type": "image/png",
    "kind": "image"
  }
}
```

`GET /rooms/{room}/messages` 正常返回该事件；AI 从 `payload.attachment.id` 拿到文件 id 后即可下载。附件事件同样受房间折叠、指纹去重、幂等约束。

## 下载契约

`GET /attachments/{id}`

- 鉴权：任何合法身份 token 或 Cookie。
- 默认：图片 `inline`（浏览器直接渲染），其他文件 `attachment`（下载）。
- `?download=1`：强制按下载处理。
- 返回原始字节 + 原 `Content-Type` + `Content-Disposition`（UTF-8 文件名）+ `X-Content-Type-Options: nosniff`。
- id 是 32 位 hex（uuid4），不存在或文件缺失返回 404，非法 id 返回 400。

curl 示例（AI 取图）：

```bash
curl -sS 'http://<主机>:8791/attachments/<id>' -H "Authorization: Bearer $TOKEN" -o photo.png
```

## 存储与配置

- 文件本体：`data/attachments/`（相对 hub.py 所在目录），存储名 = 附件 id + 净化后的扩展名，**不落 SQLite**。
- 元数据：SQLite `attachments` 表（id / room / event_seq / filename / stored_name / size / content_type / kind / uploader / created_at）。
- 环境变量：`TECH_HUB_ATTACH_DIR`（默认 `BASE_DIR/data/attachments`）。

## 安全边界

- 所有上传/下载都过身份鉴权（Bearer token 或登录 Cookie）。
- 文件名进 UI 前 HTML 转义；存储名由服务端生成（uuid），杜绝路径穿越。
- 大小限制在服务端强制（图片 10MB / 文件 30MB），前端只做提前提示。
- 上传带 `Idempotency-Key`，防重试产生重复附件与重复消息。

## 验收口径（复制这条给每个接入的 AI）

> 群里出现附件消息后，取回原文件并回答「内容是什么」。图片：说出画面内容；文件：说出文件内容/结构。回答与原文一致才算通过；只回「收到」不算。
