#!/usr/bin/env python3
"""XIAONA CLI 客户端（11_CLI客户端详解.md）。

独立于 backend，token 存 ~/.xiaona/config.json。
所有命令都先读取 config.json，未登录则提示 `xiaona login`。
"""
import io
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests

CONFIG_DIR = Path.home() / ".xiaona"
CONFIG_PATH = CONFIG_DIR / "config.json"
API = "http://localhost:8000"   # 可被 --server 覆盖


def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {"server_url": API, "token": None, "user_id": None, "current_session_id": None}


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)  # Windows 无 os.chmod 权限位语义，忽略
    except OSError:
        pass


def auth_headers(cfg):
    return {"Authorization": f"Bearer {cfg['token']}"} if cfg.get("token") else {}


def require_login(cfg):
    if not cfg.get("token"):
        print("未登录，请先运行 xiaona login")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
def cmd_login():
    cfg = load_config()
    phone = input("手机号: ").strip()
    method = input("登录方式 [1]验证码 [2]密码（默认 1）: ").strip() or "1"
    if method == "2":
        password = input("密码: ").strip()
        payload = {"phone": phone, "password": password}
    else:
        resp = requests.post(f"{cfg['server_url']}/api/auth/send_code", json={"phone": phone})
        resp.raise_for_status()   # 验证码发送失败立即中止
        code = input("验证码: ").strip()
        payload = {"phone": phone, "code": code}
    resp = requests.post(f"{cfg['server_url']}/api/auth/login", json=payload)
    resp.raise_for_status()
    data = resp.json()
    cfg.update({"token": data["token"], "user_id": data["user"]["id"]})
    save_config(cfg)
    print(f"登录成功：{data['user']['nickname']}")


def cmd_register():
    cfg = load_config()
    phone = input("手机号: ").strip()
    resp = requests.post(f"{cfg['server_url']}/api/auth/send_code", json={"phone": phone})
    resp.raise_for_status()
    code = input("验证码: ").strip()
    password = input("设置密码（必填，与 Web 注册约束一致）: ").strip()
    nickname = input("昵称（选填）: ").strip() or None
    resp = requests.post(f"{cfg['server_url']}/api/auth/register",
                         json={"phone": phone, "code": code, "password": password,
                               "nickname": nickname})
    resp.raise_for_status()
    data = resp.json()
    cfg.update({"token": data["token"], "user_id": data["user"]["id"]})
    save_config(cfg)
    print(f"注册成功：{data['user']['nickname']}")


def cmd_logout():
    cfg = load_config()
    cfg.update({"token": None, "user_id": None})
    save_config(cfg)
    print("已退出登录")


def cmd_whoami():
    """显示当前登录用户（GET /api/auth/me）。"""
    cfg = load_config()
    if not cfg.get("token"):
        print("未登录，请先运行 xiaona login")
        return
    resp = requests.get(f"{cfg['server_url']}/api/auth/me", headers=auth_headers(cfg))
    resp.raise_for_status()
    user = resp.json()
    print(f"昵称：{user.get('nickname')}  手机号：{user.get('phone')}  user_id：{user.get('id')}")


# ---------------------------------------------------------------------------
# 会话管理
# ---------------------------------------------------------------------------
def cmd_new():
    cfg = load_config()
    require_login(cfg)
    resp = requests.post(f"{cfg['server_url']}/api/sessions",
                         headers=auth_headers(cfg),
                         json={"source": "cli"})
    resp.raise_for_status()
    s = resp.json()
    cfg["current_session_id"] = s["id"]
    save_config(cfg)
    print(f"新会话：{s['id']}")


def cmd_continue(session_id):
    """继续指定会话：写入 current_session_id 后进入交互对话模式。"""
    cfg = load_config()
    require_login(cfg)
    cfg["current_session_id"] = session_id
    save_config(cfg)
    print(f"继续会话：{session_id}")
    cmd_start()


def cmd_history(page=1, page_size=20):
    cfg = load_config()
    require_login(cfg)
    resp = requests.get(f"{cfg['server_url']}/api/sessions",
                        headers=auth_headers(cfg),
                        params={"page": page, "page_size": page_size})
    resp.raise_for_status()
    for s in resp.json()["items"]:
        print(f"{s['id']}  {s['title']}  {s['updated_at']}")


# ---------------------------------------------------------------------------
# 对话（非流式）
# ---------------------------------------------------------------------------
def cmd_start():
    cfg = load_config()
    require_login(cfg)
    if not cfg.get("current_session_id"):
        cmd_new()
        cfg = load_config()   # cmd_new 只写磁盘，重新加载才能拿到新会话 id
    while True:
        text = input("你> ").strip()
        if text.lower() in ("exit", "quit", "q"):
            break
        if text.lower() in ("new", "n"):       # 输入 new 开启新会话
            cmd_new()
            cfg = load_config()                # 刷新进程内配置，否则后续消息仍发往旧会话
            continue
        resp = requests.post(
            f"{cfg['server_url']}/api/chat",
            headers=auth_headers(cfg),
            json={"message": text, "session_id": cfg["current_session_id"], "source": "cli"},
        )
        resp.raise_for_status()
        print(f"XIAONA> {resp.json()['reply']}")


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def cmd_export(session_id, fmt="markdown"):
    cfg = load_config()
    require_login(cfg)
    resp = requests.get(
        f"{cfg['server_url']}/api/sessions/{session_id}/export",
        headers=auth_headers(cfg),
        params={"format": fmt},
    )
    resp.raise_for_status()
    out = Path(f"{session_id}.{fmt}")
    out.write_bytes(resp.content)
    print(f"已导出到 {out}")


# ---------------------------------------------------------------------------
# 语音对话
# ---------------------------------------------------------------------------
_HAS_AUDIO = False
try:
    import numpy as np
    import sounddevice as sd
    import soundfile as sf
    import simpleaudio as sa
    _HAS_AUDIO = True
except ImportError:
    pass

SAMPLE_RATE = 16000
MAX_RECORD_SECONDS = 30.0   # 最长录音超时


def record_audio() -> bytes:
    """录制直到用户按回车停止，或达到 30s 超时，返回 wav 字节。"""
    frames = []
    stop_flag = {"stop": False}

    def wait_enter():
        input()                     # 阻塞直到回车
        stop_flag["stop"] = True

    threading.Thread(target=wait_enter, daemon=True).start()

    print("开始录音，说完按回车结束（最长 30 秒）...")
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16") as stream:
        start = time.time()
        while not stop_flag["stop"] and time.time() - start < MAX_RECORD_SECONDS:
            data, _ = stream.read(int(SAMPLE_RATE * 0.5))   # 500ms 一块
            frames.append(data)

    audio = np.concatenate(frames) if frames else np.zeros(0, dtype="int16")
    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, format="WAV")
    return buf.getvalue()


def cmd_voice():
    cfg = load_config()
    require_login(cfg)
    if not cfg.get("current_session_id"):
        cmd_new()
        cfg = load_config()   # cmd_new 已写入新会话 id 到磁盘，重新加载才能拿到
    if not _HAS_AUDIO:
        print("缺少语音依赖，请执行：pip install -r requirements-cli.txt")
        return
    wav_bytes = record_audio()

    # 1) ASR：上传 wav → 文字
    resp = requests.post(
        f"{cfg['server_url']}/api/voice/transcribe",
        headers=auth_headers(cfg),
        files={"file": ("voice.wav", wav_bytes, "audio/wav")},
    )
    resp.raise_for_status()
    text = resp.json()["text"]
    print(f"识别：{text}")
    if not text.strip():
        print("未识别到语音，请重试")
        return

    # 2) 对话（非流式）
    resp = requests.post(
        f"{cfg['server_url']}/api/chat",
        headers=auth_headers(cfg),
        json={"message": text, "session_id": cfg["current_session_id"], "source": "cli"},
    )
    resp.raise_for_status()
    reply = resp.json()["reply"]
    print(f"XIAONA> {reply}")

    # 3) TTS：返回音频文件流，保存为 mp3 后转 wav 播放（simpleaudio 只支持 wav）
    resp = requests.post(
        f"{cfg['server_url']}/api/voice/tts",
        headers=auth_headers(cfg),
        json={"text": reply},
    )
    resp.raise_for_status()
    mp3_path = CONFIG_DIR / "reply.mp3"
    mp3_path.write_bytes(resp.content)
    if not shutil.which("ffmpeg"):
        print("提示：未安装 ffmpeg，跳过语音播放（回复文字已在上方显示）。")
        return
    wav_path = CONFIG_DIR / "reply.wav"
    subprocess.run(["ffmpeg", "-y", "-i", str(mp3_path), str(wav_path)], check=True)
    play_obj = sa.WaveObject.from_wave_file(str(wav_path))
    play_obj.play().wait_done()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    """命令行入口：解析 --server 与子命令并分派。"""
    import argparse
    parser = argparse.ArgumentParser(prog="xiaona", description="XIAONA CLI 客户端")
    parser.add_argument("--server", help="后端服务地址，覆盖 config 中的 server_url（默认 http://localhost:8000）")
    sub = parser.add_subparsers(dest="command")

    p_register = sub.add_parser("register", help="注册新账号")
    p_login = sub.add_parser("login", help="登录")
    p_logout = sub.add_parser("logout", help="退出登录")
    p_new = sub.add_parser("new", help="新建会话")
    p_start = sub.add_parser("start", help="进入交互对话模式")
    p_continue = sub.add_parser("continue", help="继续指定会话")
    p_continue.add_argument("session_id")
    p_history = sub.add_parser("history", help="列出历史会话")
    p_history.add_argument("--page", type=int, default=1)
    p_history.add_argument("--page_size", type=int, default=20)
    p_export = sub.add_parser("export", help="导出会话")
    p_export.add_argument("session_id")
    p_export.add_argument("--format", default="markdown", choices=["markdown", "json"])
    p_voice = sub.add_parser("voice", help="语音对话模式")
    p_whoami = sub.add_parser("whoami", help="显示当前登录用户")

    # 子命令也接受 --server（与顶层同名，argparse 合并到 args.server）：
    # 兼容 `xiaona --server X register` 与 `xiaona register --server X` 两种写法
    for p in (p_register, p_login, p_logout, p_new, p_start, p_continue,
              p_history, p_export, p_voice, p_whoami):
        p.add_argument("--server", help=argparse.SUPPRESS)

    args = parser.parse_args()
    if args.server:
        cfg = load_config()
        cfg["server_url"] = args.server   # --server 覆盖 config 中的 server_url，持久化
        save_config(cfg)

    fn = {
        "register": cmd_register,
        "login": cmd_login,
        "logout": cmd_logout,
        "new": cmd_new,
        "start": cmd_start,
        "continue": lambda: cmd_continue(args.session_id),
        "history": lambda: cmd_history(page=args.page, page_size=args.page_size),
        "export": lambda: cmd_export(args.session_id, args.format),
        "voice": cmd_voice,
        "whoami": cmd_whoami,
    }.get(args.command)
    if fn:
        fn()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
