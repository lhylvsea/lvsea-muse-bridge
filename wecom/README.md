# 企业微信桥接

企业微信智能机器人，API 模式 + WebSocket 长连接，无需公网回调。

## Setup

1. 企业微信管理后台 → 应用管理 → 智能机器人，新建机器人
2. 开启 API 模式，选择长连接，获取 **BotID** 和**长连接 Secret**
3. 允许机器人私聊
4. 写入 `state/credentials.json`（权限 600）：
   ```json
   {"bot_id": "xxx", "secret": "xxx"}
   ```
5. 启动：
   ```bash
   pip install websockets  # 到 .venv 里
   nohup ./.venv/bin/python bridge.py serve &
   ```

首个私聊发送者自动绑定为 owner（`state/owner.json`），之后只处理 owner 的私聊。

## 能力

- 收：文本（`aibot_msg_callback`，`chattype=single`）
- 发：文本（markdown）、图片、视频、文件（分片上传后推送）
- 发图：worker 在 outbox 写 `media_path` 字段即可

## 踩坑

见 [docs/PITFALLS.md](../docs/PITFALLS.md) 的「企业微信」章节，重点：
- 同一时间只允许**一个**有效长连接，收发必须复用同一连接
- 禁用 `websockets` 库自带心跳，改用官方 JSON `ping`（30 秒）
- 回复用 `aibot_send_msg` 主动推送，`msgtype` 不支持 `text`
- 发图前先分片上传拿 `media_id`；webp 会被拒，转 png/jpg
