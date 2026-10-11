# 微信桥接

走微信官方 iLink Bot API（bot_type=3），扫码登录，无需公网回调。

## Setup

```bash
pip install qrcode pycryptodome
python3 bridge.py login        # 终端出二维码，微信扫码
python3 bridge.py wait-login   # 等待确认（过期自动刷新 3 次）
nohup python3 bridge.py serve &  # 常驻收发
```

`state/session.json` 权限 600，长期有效，掉线重扫即可。

## 能力

- 收：文本、图片、视频、文件（自动下载解密到 `media/`）；语音转写文本
- 发：文本（超 2000 字自动分片）、图片、视频、文件
- 只处理扫码用户本人的私聊，群聊忽略

## 踩坑

见 [docs/PITFALLS.md](../docs/PITFALLS.md) 的「微信」章节，重点：
- 视频下载必须用 `encrypt_query_param` 拼地址
- 发消息时 `from_user_id` 必须空字符串
- 语音无音频下载接口，只有转写文本
