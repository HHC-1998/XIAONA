# -*- coding: utf-8 -*-
"""本地模拟飞书事件加密推送，验证 webhook 验签 + AES 解密链路。

用法（容器内或宿主机均可，读 .env 的同名环境变量）：
    python scripts/test_feishu_encrypt.py
"""
import base64
import hashlib
import json
import os
import time

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

ENCRYPT_KEY = os.getenv("FEISHU_ENCRYPT_KEY", "vSymCJ2mpD4Y4GoHcGWt5rOh2D7ADx1F")
BASE = os.getenv("TEST_BASE", "http://127.0.0.1:8000")


def encrypt(body_str: str, key: str) -> str:
    k = key.encode("utf-8")
    if len(k) != 32:
        k = hashlib.md5(k).digest()
    cipher = AES.new(k, AES.MODE_CBC, k[:16])
    return base64.b64encode(cipher.encrypt(pad(body_str.encode("utf-8"), AES.block_size))).decode()


def main():
    # 1. 模拟 challenge 请求（明文载荷加密后推送）
    challenge_payload = {"challenge": "mock-challenge-123", "token": "KKSyLCr18q3J0RfaygAMfbkVYOCZ0MLW", "type": "url_verification"}
    body_bytes = json.dumps({"encrypt": encrypt(json.dumps(challenge_payload), ENCRYPT_KEY)}).encode("utf-8")
    ts = str(int(time.time()))
    nonce = "test-nonce-1"
    sig = hashlib.sha256((ts + nonce + ENCRYPT_KEY).encode() + body_bytes).hexdigest()

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
    print(f"[challenge] status={r.status_code} body={r.text}")
    assert r.status_code == 200, "challenge 应返回 200"
    assert r.json().get("challenge") == "mock-challenge-123", "challenge 应原样返回"
    print("[challenge] PASS")

    # 2. 错误签名应 403
    bad_sig = hashlib.sha256(b"wrong").hexdigest()
    r2 = httpx.post(
        f"{BASE}/api/feishu/webhook",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Lark-Signature": bad_sig,
            "X-Lark-Request-Timestamp": ts,
            "X-Lark-Request-Nonce": nonce,
        },
    )
    print(f"[bad-sig] status={r2.status_code}")
    assert r2.status_code == 403, "错误签名应 403"
    print("[bad-sig] PASS")

    print("ALL_PASS")


if __name__ == "__main__":
    main()
