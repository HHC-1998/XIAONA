# -*- coding: utf-8 -*-
"""验证 webhook 新逻辑：加密 challenge 请求不带签名头也能返回 challenge。"""
import base64
import hashlib
import json
import time

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

ENCRYPT_KEY = "vSymCJ2mpD4Y4GoHcGWt5rOh2D7ADx1F"
VERIFY_TOKEN = "KKSyLCr18q3J0RfaygAMfbkVYOCZ0MLW"
BASE = "http://127.0.0.1:8000"


def encrypt(body_str: str, key: str) -> str:
    k = hashlib.sha256(key.encode("utf-8")).digest()
    iv = b"0123456789abcdef"
    cipher = AES.new(k, AES.MODE_CBC, iv)
    return base64.b64encode(iv + cipher.encrypt(pad(body_str.encode("utf-8"), AES.block_size))).decode()


# 1. 加密 challenge，不带签名头 → 应返回 200 + challenge
challenge_payload = {"challenge": "mock-challenge-789", "token": VERIFY_TOKEN, "type": "url_verification"}
body_bytes = json.dumps({"encrypt": encrypt(json.dumps(challenge_payload), ENCRYPT_KEY)}).encode("utf-8")
r1 = httpx.post(f"{BASE}/api/feishu/webhook", content=body_bytes, headers={"Content-Type": "application/json"})
print(f"[challenge-no-sig] status={r1.status_code} body={r1.text}")
assert r1.status_code == 200 and r1.json().get("challenge") == "mock-challenge-789"
print("[challenge-no-sig] PASS")

# 2. 明文 challenge，不带签名头 → 应返回 200 + challenge（兼容未开加密的情况）
r2 = httpx.post(f"{BASE}/api/feishu/webhook", content=json.dumps(challenge_payload).encode(), headers={"Content-Type": "application/json"})
print(f"[plain-challenge] status={r2.status_code} body={r2.text}")
assert r2.status_code == 200 and r2.json().get("challenge") == "mock-challenge-789"
print("[plain-challenge] PASS")

# 3. 正式事件（加密）带正确签名 → 走统一处理（此处 event 为空 dict，process 会怎样？）
# 构造一个假 event 会进入 process_feishu_event，可能报错。用无 event 的载荷测试是否先通过验签。
event_payload = {"schema": "2.0", "header": {"event_id": "evt_mock_1"}, "event": {}, "type": "event_callback"}
eb = json.dumps({"encrypt": encrypt(json.dumps(event_payload), ENCRYPT_KEY)}).encode("utf-8")
ts = str(int(time.time()))
nonce = "nonce-x"
sig = hashlib.sha256((ts + nonce + ENCRYPT_KEY).encode() + eb).hexdigest()
r3 = httpx.post(
    f"{BASE}/api/feishu/webhook",
    content=eb,
    headers={"Content-Type": "application/json", "X-Lark-Signature": sig, "X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": nonce},
)
print(f"[event-good-sig] status={r3.status_code} body={r3.text}")
# 事件 payload 缺少 chat_id 等字段，process 可能 400/500，但不应 403（验签已过）
assert r3.status_code != 403, "正式事件带正确签名不应 403"
print("[event-good-sig] PASS (not 403)")

# 4. 正式事件（加密）带错误签名 → 应 403
bad_sig = hashlib.sha256(b"wrong").hexdigest()
r4 = httpx.post(
    f"{BASE}/api/feishu/webhook",
    content=eb,
    headers={"Content-Type": "application/json", "X-Lark-Signature": bad_sig, "X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": nonce},
)
print(f"[event-bad-sig] status={r4.status_code}")
assert r4.status_code == 403
print("[event-bad-sig] PASS")

print("ALL_PASS")
