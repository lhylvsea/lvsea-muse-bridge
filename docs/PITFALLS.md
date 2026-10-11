# 踩坑大全

实测踩出来的坑，按桥接分。动手前读一遍。

## 微信（iLink Bot API）

- 扫码登录：`bridge.py login` 取二维码，微信扫码后 `wait-login` 确认。`session.json` 长期有效，掉线重扫即可。二维码约 2 分钟过期，`wait-login` 会自动刷新 3 次。
- 图片/视频/文件从微信 CDN 下载是 AES-128-ECB + PKCS7 加密的，`aes_key` 有 4 种格式：16 字节原文 / 32 hex 字符 / base64(16字节) / base64(32hex)。解析要严格，base64 需同时试标准和 URL 安全两种。
- **视频必须用 `encrypt_query_param` 拼的 CDN 地址下载**，`full_url` 返回的数据解密会失败。图片/文件两者都可用。
- 语音消息只有转写文本（`voice_item.text`），**没有音频文件下载接口**，这是微信侧限制，不是桥没接。
- 发消息时 `msg.from_user_id` 必须填空字符串，否则发不出去还不报错。
- 单条文本超 2000 字要分片多条发出。
- 官方限制：只支持一对一私聊；用户必须先发消息，机器人才能回。

## 飞书（自建应用 + 长连接）

- `lark-oapi` 的 `ws.Client` 默认强制 `proxy=None`，走代理的环境必须 monkeypatch 恢复环境变量代理发现：
  ```python
  import lark_oapi.ws.client as ws_client
  ws_client._ws_connect_kwargs = lambda: {}
  ```
- SDK 事件对象结构是 `sender.sender_id.open_id` / `message.message_type` / `message.content`，不是文档里常见的 Sender/Message 命名。
- **飞书长连接是单播**：多余残留连接会吞事件。重启前务必杀干净旧进程，否则新进程收不到消息。
- 图片下载：`GET /open-apis/im/v1/images/:image_key`；文件下载：`GET /open-apis/im/v1/files/:file_key/download`。都要带 tenant access token。
- WebSocket 会**静默断开**：进程活着但不收消息，lark SDK 的 `auto_reconnect` 不可靠。只能靠看门狗查日志活性，超时无日志就杀掉重启。
- 权限：`im:message`、`im:message.p2p_msg:readonly`。控制台里没有独立的 `im:message:send_as_bot`，`im:message` 已含发送能力。

## 企业微信（智能机器人 + WebSocket）

- 用智能机器人的 **API 模式 + WebSocket 长连接**，地址 `wss://openws.work.weixin.qq.com`。凭证是 **BotID + 长连接专用 Secret**，不是 CorpID、普通应用 Secret、Token 或 EncodingAESKey。
- **每个机器人同一时间只允许一个有效长连接**：新连接订阅成功会踢掉旧连接。收发必须复用**同一个** WebSocket，不能收发各建一个（会互相踢下线）。
- `websockets` 库自带 ping 与代理冲突会导致 `1002 incorrect masking` 错误。禁用库心跳（`ping_interval=None, ping_timeout=None`），改用官方 JSON 心跳命令，每 30 秒一次：
  ```json
  {"cmd": "ping", "headers": {"req_id": "..."}}
  ```
- `aibot_respond_msg` 回复无送达（API 返回 ok 但用户收不到）。改用 `aibot_send_msg` **主动推送**，`chatid` 填用户 userid，`chat_type` 填 1，`msgtype` 用 `markdown`。
- `aibot_send_msg` 的 `msgtype` 只支持 `template_card` / `markdown` / `image` / `file` / `video`，**不支持 `text`**（会报 `40008 invalid message type`）。
- 发图片/视频/文件要先分片上传拿 `media_id`，再推送：
  1. `aibot_upload_media_init` → 拿 `upload_id`（单个分片 ≤512KB，最多 100 片）
  2. `aibot_upload_media_chunk` 逐片上传（base64）
  3. `aibot_upload_media_finish` → 拿 `media_id`
  4. `aibot_send_msg` 带 `media_id` 推送
- **webp 格式图片会被拒**（`errcode 846697 invalid image format`）。发图前先转成 png/jpg/gif。
- 请求/响应靠 `headers.req_id` 关联：receiver 统一消费所有帧，命中 `PENDING` 字典里的 req_id 就唤醒对应的 Future；sender 用 `await` 等响应。注意 `outbox` 的 `sent` 状态只代表指令发到 socket，送达与否要看 serve 日志里的异步响应帧。

## 通用

- 三个桥接都只处理 owner 本人私聊：首个私聊发送者自动绑定为 owner，之后忽略其他用户；群聊一律忽略；按消息 ID 去重。
- 花钱、给真人发消息、删除数据、填写用户真实身份/隐私信息前，必须先问用户，不能直接执行。
- 图片/语音/文件等非文字消息，如果该通道没接对应能力，固定回复：「这条通道暂时没有配置这类内容处理，请先用文字描述」。不要假装处理。
- **VM 重启后所有 serve 进程都会死**，看门狗 cron 本身也依赖 runtime。重启后手动跑一次 `watchdog/bridge-watchdog.sh` 或三个 serve。
