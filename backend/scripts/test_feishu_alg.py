# -*- coding: utf-8 -*-
"""容器内严格复刻飞书验签：直接用 app.config 的值加密+签名，请求本地 webhook。"""
import base64
import hashlib
import json
import time

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from app import config

ENCRYPT_KEY = config.FEISHU_ENCRYPT_KEY
VERIFY_TOKEN = config.FEISHU_VERIFY_TOKEN
BASE = "http://127.0.0.1:8000"

print(f"ENCRYPT_KEY len={len(ENCRYPT_KEY)}")
print(f"VERIFY_TOKEN len={len(VERIFY_TOKEN)}")


def encrypt(body_str: str, key: str) -> str:
    k = key.encode("utf-8")
    if len(k) != 32:
        k = hashlib.md5(k).digest()
    cipher = AES.new(k, AES.MODE_CBC, k[:16])
    return base64.b64encode(cipher.encrypt(pad(body_str.encode("utf-8"), AES.block_size))).decode()


def post(sign_key: str, use_encrypt: bool):
    challenge_payload = {"challenge": "mock-challenge-456", "token": VERIFY_TOKEN, "type": "url_verification"}
    inner = json.dumps(challenge_payload)
    if use_encrypt:
        body_bytes = json.dumps({"encrypt": encrypt(inner, ENCRYPT_KEY)}).encode("utf-8")
    else:
        body_bytes = inner.encode("utf-8")
    ts = str(int(time.time()))
    nonce = "test-nonce-2"
    b1 = (ts + nonce + sign_key).encode("utf-8")
    sig = hashlib.sha256(b1 + body_bytes).hexdigest()
    r = httpx.post(
        f"{BASE}/api/feishu/webhook",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Lark-Signature": sig,
            "X-Lark-Request-Timestamp": ts,
            "X-Lark-Request-Nonce": nonce,
        },
    )
    print(f"[sign_key={'ENCRYPT' if sign_key == ENCRYPT_KEY else 'VERIFY'}][encrypt={use_encrypt}] status={r.status_code} body={r.text}")
    return r


# 四种组合全试一遍，找出 webhook 实际接受的组合
post(ENCRYPT_KEY, True)
post(ENCRYPT_KEY, False)
post(VERIFY_TOKEN, True)
post(VERIFY_TOKEN, False)
print("DONE")
