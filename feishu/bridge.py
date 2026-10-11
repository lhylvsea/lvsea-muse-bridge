#!/usr/bin/env python3
"""飞书桥接：长连接收消息 -> inbox/，outbox/ -> 发消息回飞书。

用法：
  bridge.py serve    # 启动：WebSocket 长连接收消息 + outbox 发送循环
  bridge.py send     # 单次：发送 outbox/ 里 status=ready 的消息后退出
  bridge.py status   # 查看登录/服务/收发状态

环境：
  需要走 egress 代理访问 open.feishu.cn，serve 启动时会自动从环境变量读取
  https_proxy / HTTPS_PROXY。
"""
import json
import os
import sys
import time
import uuid
import urllib.request
import urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state")
INBOX = os.path.join(BASE, "inbox")
OUTBOX = os.path.join(BASE, "outbox")
CREDS = os.path.join(STATE, "credentials.json")
OWNER = os.path.join(STATE, "owner.json")
SEEN = os.path.join(STATE, "seen.json")
LOGF = os.path.join(BASE, "serve.log")

DOMAIN = "https://open.feishu.cn"


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGF, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def get_tenant_token(app_id, app_secret):
    req = urllib.request.Request(
        DOMAIN + "/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
        headers={"Content-Type": "application/json"},
    )
    r = json.load(urllib.request.urlopen(req, timeout=20))
    if r.get("code") != 0:
        raise RuntimeError(f"token failed: {r}")
    return r["tenant_access_token"]


def api_post(path, token, body, params=""):
    req = urllib.request.Request(
        DOMAIN + "/open-apis" + path + params,
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        r = json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:200]}")
    if r.get("code") != 0:
        raise RuntimeError(f"api {path} failed: code={r.get('code')} msg={r.get('msg')}")
    return r


def send_text(token, chat_id, text):
    # 长文本分段，避免超限
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)] or [""]
    for ch in chunks:
        api_post(
            "/im/v1/messages",
            token,
            {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": ch}, ensure_ascii=False),
                "uuid": str(uuid.uuid4()),
            },
            params="?receive_id_type=chat_id",
        )


# ---------------- 收消息：WebSocket 长连接 ----------------

def download_feishu_file(url_path, token, dest_path):
    """从飞书下载媒体文件（图片/文件）。url_path: /open-apis/... 完整路径。"""
    req = urllib.request.Request(
        DOMAIN + url_path,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read(100 * 1024 * 1024 + 1)
    if len(data) > 100 * 1024 * 1024:
        raise ValueError("文件超过 100MB 上限")
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest_path)
    return dest_path


MEDIA_DIR = os.path.join(BASE, "media")


def on_message(data):
    """im.message.receive_v1 事件回调：只做落盘，不做重活。"""
    try:
        ev = data.event
        msg = ev.message
        sender = ev.sender
        message_id = str(getattr(msg, "message_id", "") or "")
        chat_id = str(getattr(msg, "chat_id", "") or "")
        chat_type = str(getattr(msg, "chat_type", "") or "")
        msg_type = str(getattr(msg, "message_type", "") or "")
        # EventSender.sender_id 是 UserId 对象，取 open_id 字符串
        _sid = getattr(sender, "sender_id", None)
        if _sid is not None and hasattr(_sid, "open_id"):
            sender_id = str(getattr(_sid, "open_id") or "")
        else:
            sender_id = str(_sid or "")

        # 去重
        seen = load_json(SEEN, {})
        if message_id in seen:
            return
        seen[message_id] = int(time.time())
        # 只保留最近 500 条
        if len(seen) > 500:
            for k in sorted(seen, key=seen.get)[:-500]:
                del seen[k]
        save_json_atomic(SEEN, seen)

        # 只处理私聊文本，忽略群聊/机器人自己/其他用户在绑定 owner 后
        if chat_type != "p2p":
            log(f"ignore non-p2p message {message_id} chat_type={chat_type}")
            return

        owner = load_json(OWNER, {})
        owner_id = owner.get("open_id")
        if owner_id and sender_id != owner_id:
            log(f"ignore message from non-owner {sender_id}")
            return
        if not owner_id and sender_id:
            save_json_atomic(OWNER, {"open_id": sender_id, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
            try:
                os.chmod(OWNER, 0o600)
            except OSError:
                pass
            log(f"owner bound: {sender_id}")

        text = ""
        kind = "other"
        media_paths = []
        media_kind = ""
        if msg_type == "text":
            try:
                content = getattr(msg, "content", "") or ""
                text = json.loads(content).get("text", "")
                kind = "text"
            except (ValueError, AttributeError):
                pass
        elif msg_type in ("image", "file"):
            # 下载图片/文件到本地
            try:
                content = json.loads(getattr(msg, "content", "") or "{}")
                creds = load_json(CREDS, {})
                token = get_tenant_token(creds.get("app_id", ""), creds.get("app_secret", ""))
                day = time.strftime("%Y%m%d")
                if msg_type == "image":
                    image_key = content.get("image_key", "")
                    if image_key:
                        dest = os.path.join(MEDIA_DIR, day, f"{message_id}_image.jpg")
                        download_feishu_file(f"/open-apis/im/v1/images/{image_key}",
                                             token, dest)
                        media_paths = [dest]
                        media_kind = "image"
                        text = f"[图片] {os.path.basename(dest)}"
                        kind = "media"
                else:  # file
                    file_key = content.get("file_key", "")
                    file_name = "".join(c for c in content.get("file_name", "file") if c not in "/\\")
                    if file_key:
                        dest = os.path.join(MEDIA_DIR, day, f"{message_id}_{file_name}")
                        download_feishu_file(f"/open-apis/im/v1/files/{file_key}/download",
                                             token, dest)
                        media_paths = [dest]
                        media_kind = "file"
                        text = f"[文件] {file_name}"
                        kind = "media"
                log(f"media downloaded: {media_paths}")
            except Exception as e:
                log(f"media download failed: {e}")

        inbox_obj = {
            "message_id": message_id,
            "chat_id": chat_id,
            "from_user_id": sender_id,
            "kind": kind,
            "text": text,
            "msg_type": msg_type,
            "status": "new",
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if media_paths:
            inbox_obj["media_paths"] = media_paths
            inbox_obj["media_kind"] = media_kind
        save_json_atomic(os.path.join(INBOX, f"{message_id}.json"), inbox_obj)
        log(f"inbox <- {message_id} kind={kind} len={len(text)}")
    except Exception as e:
        log(f"on_message error: {e}")


def run_ws(app_id, app_secret):
    sys.path.insert(0, os.path.join(BASE, ".venv", "lib",
                                    f"python{sys.version_info.major}.{sys.version_info.minor}",
                                    "site-packages"))
    import lark_oapi as lark
    import lark_oapi.ws.client as ws_client
    from lark_oapi.ws import Client as WSClient

    # 本机出站必须走 egress 代理；SDK 默认强制 proxy=None 直连，
    # 在此恢复 websockets 15 的环境变量代理自动发现。
    ws_client._ws_connect_kwargs = lambda: {}

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        # 忽略进聊天等无关事件，避免 "processor not found" 报错干扰
        .register_p2_customized_event(
            "im.chat.access_event.bot_p2p_chat_entered_v1", lambda d: None)
        .register_p2_customized_event(
            "im.message.message_read_v1", lambda d: None)
        .build()
    )
    ws = WSClient(
        app_id=app_id,
        app_secret=app_secret,
        log_level=lark.LogLevel.WARNING,
        event_handler=handler,
        domain=DOMAIN,
        auto_reconnect=True,
    )
    log(f"ws starting (long connection) pid={os.getpid()}")
    ws.start()  # 阻塞


# ---------------- 发消息：outbox 发送循环 ----------------

def send_pending(app_id, app_secret):
    """发送 outbox/ 中 status=ready 的消息，返回发送条数。"""
    token = get_tenant_token(app_id, app_secret)
    sent = 0
    for name in sorted(os.listdir(OUTBOX)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(OUTBOX, name)
        obj = load_json(path)
        if not obj or obj.get("status") != "ready":
            continue
        chat_id = obj.get("chat_id") or obj.get("to_chat_id")
        text = obj.get("text", "")
        if not chat_id or not text:
            obj["status"] = "failed"
            obj["error"] = "missing chat_id or text"
            save_json_atomic(path, obj)
            continue
        try:
            send_text(token, chat_id, text)
            obj["status"] = "sent"
            obj["sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_json_atomic(path, obj)
            sent += 1
            log(f"outbox -> {chat_id} ({name}) sent")
        except Exception as e:
            # token 过期则刷新重试一次
            try:
                token = get_tenant_token(app_id, app_secret)
                send_text(token, chat_id, text)
                obj["status"] = "sent"
                obj["sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                save_json_atomic(path, obj)
                sent += 1
                log(f"outbox -> {chat_id} ({name}) sent after token refresh")
            except Exception as e2:
                obj["status"] = "failed"
                obj["error"] = str(e2)[:200]
                save_json_atomic(path, obj)
                log(f"outbox -> {chat_id} ({name}) FAILED: {e2}")
    return sent


def cmd_serve():
    creds = load_json(CREDS)
    if not creds:
        print("no credentials", flush=True)
        sys.exit(1)
    for d in (STATE, INBOX, OUTBOX):
        os.makedirs(d, exist_ok=True)
    try:
        os.chmod(CREDS, 0o600)
    except OSError:
        pass

    import threading
    t = threading.Thread(target=run_ws,
                         args=(creds["app_id"], creds["app_secret"]),
                         daemon=True)
    t.start()
    log("serve started: ws thread + outbox sender loop")
    # 心跳：看门狗用 serve.log 的 mtime 判定假死（飞书阈值 600 秒），
    # 空闲时也必须定期写日志，否则会被误判重启（每 480 秒心跳一次）
    last_hb = time.time()
    # 主线程跑发送循环
    while True:
        try:
            send_pending(creds["app_id"], creds["app_secret"])
        except Exception as e:
            log(f"sender loop error: {e}")
        time.sleep(5)
        if time.time() - last_hb >= 480:
            log("heartbeat ok")
            last_hb = time.time()


def cmd_send():
    creds = load_json(CREDS)
    if not creds:
        print("no credentials")
        sys.exit(1)
    n = send_pending(creds["app_id"], creds["app_secret"])
    print(f"sent {n}")


def cmd_status():
    import subprocess
    creds = load_json(CREDS)
    owner = load_json(OWNER, {})
    out = {
        "credentials": bool(creds),
        "app_id": (creds.get("app_id", "")[:8] + "***") if creds else None,
        "owner_bound": bool(owner.get("open_id")),
        "serve": "not running",
        "inbox": {},
        "outbox": {},
    }
    try:
        ps = subprocess.run(["pgrep", "-f", "feishu-bridge/bridge.py serve"],
                            capture_output=True, text=True)
        if ps.stdout.strip():
            out["serve"] = f"running (pid {ps.stdout.strip().split()[0]})"
    except OSError:
        pass
    for d, key in ((INBOX, "inbox"), (OUTBOX, "outbox")):
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                if name.endswith(".json"):
                    obj = load_json(os.path.join(d, name)) or {}
                    out[key][name] = obj.get("status")
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "serve":
        cmd_serve()
    elif cmd == "send":
        cmd_send()
    else:
        cmd_status()
