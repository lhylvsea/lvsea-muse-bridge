#!/usr/bin/env python3
"""WeChat iLink Bot bridge.

Implements the official iLink Bot channel protocol (see
https://github.com/tencent/openclaw-weixin/blob/HEAD/docs/protocol_zh_CN.md):
QR login -> getupdates long-poll -> sendmessage.

Subcommands:
  login       fetch a login QR code (prints QR image path / display URL)
  wait-login  poll QR status until confirmed; atomically writes state/session.json
  serve       run the receive+send daemon (getupdates long-poll -> inbox/,
              outbox/ ready -> sendmessage)
  send        send one text message: --to USER_ID --text TEXT [--context-token T]
  status      health + queue overview (credentials masked)
"""
import argparse
import base64
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
import uuid

BASE_URL_DEFAULT = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"
CLIENT_VERSION = str(0x00010000)  # 1.0.0 encoded as 0x00MMNNPP -> decimal
CHANNEL_VERSION = "1.0.0"
BOT_AGENT = "Muse"

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
INBOX_DIR = os.path.join(ROOT, "inbox")
OUTBOX_DIR = os.path.join(ROOT, "outbox")
SESSION_FILE = os.path.join(STATE_DIR, "session.json")
CURSOR_FILE = os.path.join(STATE_DIR, "cursor.json")
PENDING_QR_FILE = os.path.join(STATE_DIR, "pending_qr.json")
PID_FILE = os.path.join(STATE_DIR, "serve.pid")
LOG_FILE = os.path.join(ROOT, "serve.log")


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def mask(s, keep=4):
    s = str(s or "")
    if len(s) <= keep * 2:
        return "***"
    return s[:keep] + "***" + s[-keep:]


def atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def load_session():
    with open(SESSION_FILE, encoding="utf-8") as f:
        return json.load(f)


def base_url(sess=None):
    if sess:
        return sess.get("baseurl") or BASE_URL_DEFAULT
    return BASE_URL_DEFAULT


def headers(auth_session=None):
    # X-WECHAT-UIN: random uint32 decimal string, base64-encoded
    uin = base64.b64encode(str(random.randint(1, 2**32 - 1)).encode()).decode()
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": CLIENT_VERSION,
    }
    if auth_session:
        h["Authorization"] = "Bearer " + auth_session["bot_token"]
    return h


def base_info():
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def api_post(path, body, auth_session=None, timeout=30):
    url = base_url(auth_session) + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers(auth_session), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} on POST {path}: {e.read()[:300]!r}")


def api_get(path, auth_session=None, timeout=30):
    url = base_url(auth_session) + path
    # QR status polling sends app headers only (no auth per protocol doc)
    req = urllib.request.Request(url, headers=headers(auth_session), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} on GET {path}: {e.read()[:300]!r}")


# ---------------- login ----------------

def do_login_fetch():
    """Fetch a fresh QR code, save pending file, return the pending dict."""
    resp = api_post("/ilink/bot/get_bot_qrcode?bot_type=3",
                    {"local_token_list": []}, auth_session=None)
    qrcode = resp.get("qrcode", "")
    img_content = resp.get("qrcode_img_content", "")
    if not qrcode:
        raise RuntimeError("no qrcode in response: " + json.dumps(resp)[:300])
    pending = {"qrcode": qrcode, "at": time.time()}
    atomic_write(PENDING_QR_FILE, pending)
    # Decide what to display
    display_url = ""
    encode_text = ""
    for cand in (img_content, qrcode):
        if isinstance(cand, str) and cand.startswith("http"):
            display_url = cand
            break
    if not display_url:
        encode_text = img_content or qrcode
    if display_url:
        print(f"QR_DISPLAY_URL={display_url}")
    if encode_text:
        import qrcode as qrlib
        img_path = "/tmp/wechat-bridge-qrcode.png"
        img = qrlib.make(encode_text)
        img.save(img_path)
        print(f"QR_IMAGE_PATH={img_path}")
    return pending


def cmd_login(args):
    """Fetch QR code. Prints QR image path and/or display URL."""
    try:
        pending = do_login_fetch()
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 1
    print(f"QR_VALUE_LEN={len(pending['qrcode'])}")
    print("Scan the QR code with WeChat, then run: bridge.py wait-login")
    return 0


def cmd_wait_login(args):
    """Poll QR status until confirmed; writes state/session.json atomically."""
    try:
        with open(PENDING_QR_FILE, encoding="utf-8") as f:
            pending = json.load(f)
    except OSError:
        print("ERROR: no pending QR; run bridge.py login first")
        return 1
    qrcode = pending["qrcode"]
    deadline = time.time() + (args.timeout or 300)
    while time.time() < deadline:
        path = "/ilink/bot/get_qrcode_status?qrcode=" + urllib.parse.quote(qrcode, safe="")
        if args.verify_code:
            path += "&verify_code=" + urllib.parse.quote(args.verify_code, safe="")
        try:
            resp = api_get(path)
        except Exception as e:
            log(f"wait-login poll error (will retry): {e}")
            time.sleep(3)
            continue
        status = resp.get("status", "")
        log(f"qr status: {status}")
        if status == "confirmed":
            sess = {
                "bot_token": resp.get("bot_token", ""),
                "ilink_bot_id": resp.get("ilink_bot_id", ""),
                "ilink_user_id": resp.get("ilink_user_id", ""),
                "baseurl": resp.get("baseurl") or BASE_URL_DEFAULT,
                "at": time.time(),
            }
            if not sess["bot_token"]:
                print("ERROR: confirmed but no bot_token in response")
                return 1
            atomic_write(SESSION_FILE, sess)
            try:
                os.remove(PENDING_QR_FILE)
            except OSError:
                pass
            print(f"OK confirmed. bot_id={sess['ilink_bot_id']} user_id={mask(sess['ilink_user_id'])}")
            return 0
        if status == "expired":
            # 二维码过期自动刷新（最多 3 次）
            refresh_count = pending.get("refresh_count", 0)
            if refresh_count >= 3:
                print("EXPIRED: QR code expired 3 times, run bridge.py login again for a fresh one")
                return 2
            log(f"QR expired, auto-refreshing ({refresh_count + 1}/3)...")
            try:
                new_pending = do_login_fetch()
            except Exception as e:
                print(f"EXPIRED: auto-refresh failed: {e}")
                return 2
            new_pending["refresh_count"] = refresh_count + 1
            atomic_write(PENDING_QR_FILE, new_pending)
            pending = new_pending
            qrcode = pending["qrcode"]
            deadline = time.time() + (args.timeout or 300)
            print(f"QR refreshed ({refresh_count + 1}/3), new QR saved. Please scan again.")
            continue
        if status == "need_verifycode":
            print("NEED_VERIFYCODE: WeChat shows a verification code on your phone; "
                  "re-run with --verify-code <code>")
            return 3
        if status == "verify_code_blocked":
            print("VERIFY_CODE_BLOCKED: too many wrong codes; run bridge.py login again")
            return 4
        if status == "binded_redirect":
            print("BINDED_REDIRECT: bot already bound to this instance")
            return 5
        # wait / scaned / scaned_but_redirect -> keep polling
        time.sleep(3)
    print("TIMEOUT: QR not confirmed in time; run bridge.py login again for a fresh one")
    return 6


# ---------------- messaging ----------------

def extract_text(msg):
    parts = []
    for item in msg.get("item_list", []) or []:
        if item.get("type") == 1 and isinstance(item.get("text_item"), dict):
            t = item["text_item"].get("text", "")
            if t:
                parts.append(t)
        elif item.get("type") == 3 and isinstance(item.get("voice_item"), dict):
            # 语音转写文本
            t = item["voice_item"].get("text", "")
            if t:
                parts.append("[语音转写]" + t)
    return "\n".join(parts)


# ---------------- media download (from wechat-bridge ref) ----------------

CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
MEDIA_DIR = os.path.join(ROOT, "media")


def download_wechat_media(media, dest_path):
    """从微信 CDN 下载并解密媒体文件。media: dict 含 encrypt_query_param/full_url/aes_key。"""
    import crypto
    raw_key = media.get("aes_key") or ""
    key = crypto.parse_aes_key(raw_key)

    urls = []
    param = media.get("encrypt_query_param") or ""
    if param:
        urls.append(("param", CDN_BASE + "/download?encrypted_query_param=" +
                     urllib.parse.quote(param, safe="")))
    if media.get("full_url"):
        urls.append(("full_url", media["full_url"]))
    if not urls:
        raise ValueError("media 既无 full_url 也无 encrypt_query_param")

    encrypted = None
    for name, url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                encrypted = resp.read(100 * 1024 * 1024 + 1)
            log(f"media download from {name}: {len(encrypted)} bytes")
            break
        except Exception as e:
            log(f"media download {name} failed: {e}")
    if encrypted is None:
        raise ValueError("所有下载源均失败")

    try:
        plain = crypto._aes_ecb_decrypt(encrypted, key)
    except Exception:
        plain, strategy = crypto.try_decrypt_variants(encrypted, raw_key)
        if plain is None:
            raise ValueError("AES 解密失败，所有组合均无效")
        log(f"media decrypt ok via {strategy}")

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(plain)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest_path)
    return dest_path


# ---------------- media send (from wechat-bridge ref) ----------------
ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE = 2, 5, 4
UPLOAD_IMAGE, UPLOAD_VIDEO, UPLOAD_FILE = 1, 2, 3
MAX_MEDIA_BYTES = 100 * 1024 * 1024


def media_type_from_path(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return ITEM_IMAGE
    if ext in (".mp4", ".mov", ".avi", ".mkv"):
        return ITEM_VIDEO
    return ITEM_FILE


def upload_media(sess, to_user_id, data, item_type):
    """上传媒体到微信 CDN，返回 (encrypt_query_param, aes_key_b64, cipher_size)。
    思路参考 lawrencewzen/wx-bot 的 Go 实现：随机 AES 密钥加密 → getuploadurl
    → POST 密文到 CDN → 返回的 x-encrypted-param 供 sendmessage 引用。"""
    import crypto
    import hashlib
    aes_key = os.urandom(16)
    filekey = os.urandom(16).hex()
    encrypted = crypto._aes_ecb_encrypt(data, aes_key)
    upload_media_type = {ITEM_IMAGE: UPLOAD_IMAGE, ITEM_VIDEO: UPLOAD_VIDEO}.get(
        item_type, UPLOAD_FILE)
    body = {
        "filekey": filekey,
        "media_type": upload_media_type,
        "to_user_id": to_user_id,
        "rawsize": len(data),
        "rawfilemd5": hashlib.md5(data).hexdigest(),
        "filesize": len(encrypted),
        "no_need_thumb": True,
        "aeskey": aes_key.hex(),
        "base_info": base_info(),
    }
    resp = api_post("/ilink/bot/getuploadurl", body, auth_session=sess, timeout=30)
    ret = resp.get("ret")
    if ret not in (0, None):
        raise RuntimeError(f"getuploadurl 失败: ret={ret} errmsg={str(resp.get('errmsg'))[:200]}")
    upload_url = (resp.get("upload_full_url") or "").strip()
    if not upload_url:
        param = resp.get("upload_param") or ""
        if not param:
            raise RuntimeError("getuploadurl 未返回上传地址")
        upload_url = (CDN_BASE + "/upload?encrypted_query_param=" +
                      urllib.parse.quote(param, safe="") +
                      "&filekey=" + urllib.parse.quote(filekey, safe=""))

    req = urllib.request.Request(
        upload_url, data=encrypted,
        headers={"Content-Type": "application/octet-stream",
                 "User-Agent": "Mozilla/5.0"},
        method="POST")
    download_param = ""
    last_err = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                if 400 <= r.status < 500:
                    raise RuntimeError(f"CDN 上传客户端错误: {r.status}")
                if r.status != 200:
                    raise RuntimeError(f"CDN 上传服务端错误: {r.status}")
                download_param = r.headers.get("x-encrypted-param") or ""
            if not download_param:
                raise RuntimeError("CDN 响应缺少 x-encrypted-param")
            break
        except Exception as e:
            last_err = e
            time.sleep(2)
    else:
        raise RuntimeError(f"CDN 上传失败: {last_err}")

    # 与官方插件一致：aes_key 取 32 位 hex 字符串再 base64
    aes_key_b64 = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")
    return download_param, aes_key_b64, len(encrypted)


def do_send_media(sess, to_user_id, file_path, context_token="", caption=""):
    """发图片/视频/文件到微信。先上传到 CDN（易失败，无用户可见副作用），
    上传成功后再发配文和媒体消息。这样上传失败重试时不会重复发送配文。"""
    with open(file_path, "rb") as f:
        data = f.read()
    if len(data) > MAX_MEDIA_BYTES:
        raise ValueError("文件超过 100MB 上限")
    item_type = media_type_from_path(file_path)
    download_param, aes_key_b64, cipher_size = upload_media(
        sess, to_user_id, data, item_type)
    if caption:
        do_send(sess, to_user_id, caption, context_token)
    media_ref = {"encrypt_query_param": download_param,
                 "aes_key": aes_key_b64, "encrypt_type": 1}
    if item_type == ITEM_IMAGE:
        item = {"type": ITEM_IMAGE,
                "image_item": {"media": media_ref, "mid_size": cipher_size}}
    elif item_type == ITEM_VIDEO:
        item = {"type": ITEM_VIDEO,
                "video_item": {"media": media_ref, "video_size": cipher_size}}
    else:
        item = {"type": ITEM_FILE,
                "file_item": {"media": media_ref,
                              "file_name": os.path.basename(file_path),
                              "len": str(len(data))}}
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": uuid.uuid4().hex,
            "message_type": 2,
            "message_state": 2,
            "context_token": context_token or "",
            "item_list": [item],
        },
        "base_info": base_info(),
    }
    resp = api_post("/ilink/bot/sendmessage", body, auth_session=sess, timeout=60)
    ret = resp.get("ret")
    if ret is not None and ret != 0:
        raise RuntimeError(f"sendmessage(媒体) ret={ret} errmsg={str(resp.get('errmsg'))[:200]}")
    return resp


def cmd_sendmedia(args):
    sess = load_session()
    try:
        do_send_media(sess, args.to, args.path, args.context_token or "",
                      args.caption or "")
    except Exception as e:
        print(f"SENDMEDIA_FAILED: {e}")
        return 1
    print("SENDMEDIA_OK")
    return 0


def msg_kind(msg):
    """Return 'text' | 'media' | 'other' for an inbound message."""
    types = {i.get("type") for i in msg.get("item_list", []) or []}
    if types & {2, 4, 5}:
        return "media"
    if 3 in types:
        # 纯语音：有转写按文本处理，无转写按媒体（下载音频）
        has_transcript = any(
            (i.get("voice_item") or {}).get("text")
            for i in msg.get("item_list", []) or []
            if i.get("type") == 3
        )
        return "text" if has_transcript else "media"
    if 1 in types:
        return "text"
    return "other"


def is_own_user_msg(sess, msg):
    if msg.get("message_type") != 1:  # 1 = user, 2 = bot
        return False
    if msg.get("group_id"):
        return False
    return msg.get("from_user_id") == sess.get("ilink_user_id")


def inbox_path(msg_id):
    return os.path.join(INBOX_DIR, f"{msg_id}.json")


def store_inbox(msg):
    msg_id = str(msg.get("message_id", ""))
    if not msg_id:
        return None
    path = inbox_path(msg_id)
    if os.path.exists(path):
        return None  # dedup
    kind = msg_kind(msg)
    text = extract_text(msg) if kind in ("text", "media") else ""
    media_paths = []
    media_kind = ""
    if kind == "media":
        # 下载图片/视频/文件到本地
        for it in msg.get("item_list", []) or []:
            itype = it.get("type")
            sub = it.get("image_item") or it.get("video_item") or it.get("file_item")
            if itype in (2, 4, 5) and sub:
                kind_name = {2: "image", 5: "video"}.get(itype, "file")
                ext = {2: "jpg", 5: "mp4"}.get(itype, "bin")
                fname = sub.get("file_name") if itype == 4 else ""
                if fname:
                    fname = "".join(c for c in fname if c not in "/\\")
                    dest_name = f"{msg_id}_{fname}"
                else:
                    dest_name = f"{msg_id}_{kind_name}.{ext}"
                day = time.strftime("%Y%m%d")
                dest = os.path.join(MEDIA_DIR, day, dest_name)
                try:
                    download_wechat_media(sub.get("media") or {}, dest)
                    media_paths.append(dest)
                    media_kind = kind_name
                    log(f"media downloaded -> {dest}")
                except Exception as e:
                    log(f"media download failed: {e}")
        if media_paths:
            label = {"image": "[图片]", "video": "[视频]", "file": "[文件]"}.get(media_kind, "[媒体]")
            text = f"{label} {os.path.basename(media_paths[0])}"
            if not text.strip().startswith("["):
                text = extract_text(msg)
        elif not text.strip():
            # 纯语音无转写或其他未知媒体
            return None
    rec = {
        "message_id": msg_id,
        "seq": msg.get("seq"),
        "from_user_id": msg.get("from_user_id"),
        "create_time_ms": msg.get("create_time_ms"),
        "kind": kind,
        "text": text,
        "context_token": msg.get("context_token", ""),
        "session_id": msg.get("session_id", ""),
        "status": "new",
        "at": time.time(),
    }
    if media_paths:
        rec["media_paths"] = media_paths
        rec["media_kind"] = media_kind
    atomic_write(path, rec)
    return rec


def load_cursor():
    try:
        with open(CURSOR_FILE, encoding="utf-8") as f:
            return json.load(f).get("get_updates_buf", "")
    except OSError:
        return ""


def save_cursor(buf):
    if buf:
        atomic_write(CURSOR_FILE, {"get_updates_buf": buf, "at": time.time()})


def do_send(sess, to_user_id, text, context_token=""):
    last_resp = {}
    # 超 2000 字自动分片多条发出
    chunks = [text[i:i + 2000] for i in range(0, len(text), 2000)] or [""]
    for chunk in chunks:
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": uuid.uuid4().hex,
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token or "",
                "item_list": [{"type": 1, "text_item": {"text": chunk}}],
            },
            "base_info": base_info(),
        }
        resp = api_post("/ilink/bot/sendmessage", body, auth_session=sess, timeout=30)
        ret = resp.get("ret")
        if ret is not None and ret != 0:
            raise RuntimeError(f"sendmessage business error ret={ret} errmsg={resp.get('errmsg','')[:200]}")
        last_resp = resp
    return last_resp


def cmd_send(args):
    sess = load_session()
    try:
        do_send(sess, args.to, args.text, args.context_token or "")
    except Exception as e:
        print(f"SEND_FAILED: {e}")
        return 1
    print("SEND_OK")
    return 0


# ---------------- serve daemon ----------------

def poll_once(sess, cursor):
    """One getupdates long-poll. Returns (messages, new_cursor)."""
    body = {"get_updates_buf": cursor or "", "base_info": base_info()}
    resp = api_post("/ilink/bot/getupdates", body, auth_session=sess, timeout=60)
    ret = resp.get("ret")
    errcode = resp.get("errcode")
    if ret == -14 or errcode == -14:
        raise _SessionPaused()
    if ret not in (None, 0):
        raise RuntimeError(f"getupdates ret={ret} errcode={errcode} errmsg={str(resp.get('errmsg'))[:200]}")
    return resp.get("msgs", []) or [], resp.get("get_updates_buf", "")


class _SessionPaused(Exception):
    pass


def outbox_scan_and_send(sess):
    sent = 0
    for name in sorted(os.listdir(OUTBOX_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(OUTBOX_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if rec.get("status") != "ready":
            continue
        # claim it
        rec["status"] = "sending"
        atomic_write(path, rec)
        try:
            if rec.get("media_path"):
                # 媒体 outbox：text 字段作为配文先发，再发媒体
                do_send_media(sess, rec["to_user_id"], rec["media_path"],
                              rec.get("context_token", ""), rec.get("text", ""))
            else:
                do_send(sess, rec["to_user_id"], rec["text"], rec.get("context_token", ""))
        except Exception as e:
            rec["status"] = "failed"
            rec["error"] = str(e)[:300]
            atomic_write(path, rec)
            log(f"outbox {name}: send failed: {e}")
            continue
        rec["status"] = "sent"
        rec["sent_at"] = time.time()
        atomic_write(path, rec)
        sent += 1
        log(f"outbox {name}: sent")
    return sent


def cmd_serve(args):
    sess = load_session()
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    log("serve starting")
    try:
        try:
            api_post("/ilink/bot/msg/notifystart", {"base_info": base_info()},
                     auth_session=sess, timeout=15)
        except Exception as e:
            log(f"notifystart warning (non-fatal): {e}")
        cursor = load_cursor()
        backoff = 5
        paused_until = 0
        # 心跳：看门狗用 serve.log 的 mtime 判定假死，空闲时也必须定期写日志，
        # 否则会被误判重启（阈值 300 秒，这里每 240 秒心跳一次）
        last_hb = time.time()
        while True:
            if time.time() < paused_until:
                time.sleep(10)
                continue
            try:
                msgs, new_cursor = poll_once(sess, cursor)
                backoff = 5
                if new_cursor:
                    cursor = new_cursor
                    save_cursor(cursor)
                for m in msgs:
                    if not is_own_user_msg(sess, m):
                        continue
                    rec = store_inbox(m)
                    if rec:
                        log(f"inbox new message_id={rec['message_id']} kind={rec['kind']} "
                            f"text={rec['text'][:40]!r}")
                outbox_scan_and_send(sess)
            except _SessionPaused:
                paused_until = time.time() + 3600
                log("getupdates ret=-14: pausing session for 1h per protocol")
            except Exception as e:
                log(f"poll error: {e} (backoff {backoff}s)")
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
            if time.time() - last_hb >= 240:
                log("heartbeat ok")
                last_hb = time.time()
    finally:
        try:
            api_post("/ilink/bot/msg/notifystop", {"base_info": base_info()},
                     auth_session=sess, timeout=15)
        except Exception:
            pass
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        log("serve stopped")


# ---------------- status ----------------

def cmd_status(args):
    out = {}
    try:
        sess = load_session()
        out["logged_in"] = True
        out["ilink_bot_id"] = sess.get("ilink_bot_id")
        out["ilink_user_id"] = mask(sess.get("ilink_user_id"))
        out["baseurl"] = sess.get("baseurl")
        out["bot_token"] = mask(sess.get("bot_token"))
    except OSError:
        out["logged_in"] = False
    pid = None
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        out["serve"] = f"running (pid {pid})"
    except (OSError, ValueError):
        out["serve"] = "not running" if pid is None else f"stale pidfile (pid {pid} dead)"
    try:
        with open(CURSOR_FILE) as f:
            out["cursor_at"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                            time.localtime(json.load(f).get("at", 0)))
    except OSError:
        out["cursor_at"] = None

    def summarize(d, key="status"):
        counts = {}
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    st = json.load(f).get(key, "?")
            except (OSError, ValueError):
                st = "unreadable"
            counts[st] = counts.get(st, 0) + 1
        return counts

    out["inbox"] = summarize(INBOX_DIR)
    out["outbox"] = summarize(OUTBOX_DIR)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def main():
    ap = argparse.ArgumentParser(prog="bridge.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    w = sub.add_parser("wait-login")
    w.add_argument("--timeout", type=int, default=300)
    w.add_argument("--verify-code", default="")
    s = sub.add_parser("send")
    s.add_argument("--to", required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--context-token", default="")
    sm = sub.add_parser("sendmedia")
    sm.add_argument("--to", required=True)
    sm.add_argument("--path", required=True)
    sm.add_argument("--caption", default="")
    sm.add_argument("--context-token", default="")
    sub.add_parser("serve")
    sub.add_parser("status")
    args = ap.parse_args()
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(INBOX_DIR, exist_ok=True)
    os.makedirs(OUTBOX_DIR, exist_ok=True)
    fn = {"login": cmd_login, "wait-login": cmd_wait_login, "send": cmd_send,
          "sendmedia": cmd_sendmedia, "serve": cmd_serve,
          "status": cmd_status}[args.cmd]
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
