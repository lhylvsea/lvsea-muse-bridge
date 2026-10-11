# 架构

三个桥接统一模式：收消息落盘 → worker 执行 → 发回。serve 进程只负责 IO，不做业务逻辑。

```
                        ┌──────────────┐
                        │  微信/飞书/企微 │
                        │   官方服务端   │
                        └──────┬───────┘
                               │ 长连接/长轮询
                               ▼
┌─────────────────────────────────────────────────┐
│  serve 进程 (bridge.py serve)                    │
│                                                  │
│  收：官方事件 → inbox/<msg_id>.json (status=new)  │
│  发：扫描 outbox/ 中 status=ready → 调官方接口发出 │
│      → status=sent                               │
└────────┬────────────────────────┬────────────────┘
         │                        ▲
         │ hook 轮询               │ worker 写 outbox/<id>.json
         │ (每15秒)                │ (status=ready)
         ▼                        │
┌─────────────────────────────────────────────────┐
│  worker（带完整 Muse 工具能力）                   │
│  1. 读 inbox/<id>.json，status new→processing     │
│  2. 看 kind 执行任务（调工具、读文件、搜网页…）     │
│  3. 写 outbox/<id>.json，inbox 状态→done          │
└─────────────────────────────────────────────────┘
```

## inbox JSON 格式

```json
{
  "message_id": "7514850406679165704",
  "from_user_id": "o9cq***chat",
  "kind": "text",
  "text": "帮我查一下今天天气",
  "media_paths": ["/path/to/media/20261011/xxx.jpg"],
  "media_kind": "image",
  "context_token": "...",
  "status": "new",
  "at": "2026-10-11 09:48:17"
}
```

## outbox JSON 格式

```json
{
  "message_id": "7514850406679165704",
  "text": "今天晴，22°C",
  "media_path": "/path/to/generated.png",
  "status": "ready",
  "at": "2026-10-11 09:48:35"
}
```

`media_path` 可选：worker 生成图片/文件后填本地绝对路径，serve 负责上传并推送。

## kind 字段

| kind | 含义 | worker 动作 |
|---|---|---|
| `text` | 纯文字任务（含语音转写文本） | 执行任务，必须实际调工具 |
| `media` | 图片/视频/文件 | 用工具查看 `media_paths` 里的文件再回复 |
| `other` | 未配置的类型 | 固定回复「这条通道暂时没有配置这类内容处理，请先用文字描述」 |

## 去重与 owner

- 按 `message_id` 去重：inbox 文件已存在就跳过；飞书/企微另有 `seen.json` 记录最近 500 条。
- owner 绑定：首个私聊发送者自动成为 owner（`state/owner.json`），之后只处理 owner 的私聊，其他用户和群聊忽略。
