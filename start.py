#!/usr/bin/env python3
"""XIAONA 一键启动（见 06_一键启动详解.md §7）。

用法：python start.py
跨平台统一入口（替代原 start.bat / start.sh）。
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    if not (ROOT / ".env").exists():
        print("未找到 .env，请先执行：cp .env.example .env 并填写密钥")
        return 1
    if not shutil.which("docker"):
        print("未检测到 docker，请先安装 Docker 并确保 docker 在 PATH 中")
        return 1
    print("Starting XIAONA...")
    try:
        # --env-file 显式指定根目录 .env：Compose 默认从 compose 文件所在目录（backend/）
        # 查找 .env，会把根目录的 DB/Neo4j/OpenAI/飞书凭据全部丢掉（DB_PASSWORD 等恰与默认值
        # 相同才未暴露，FEISHU_APP_ID 等非默认值必须显式传入，见 06 §7/§8）
        return subprocess.call(
            ["docker", "compose", "--env-file", str(ROOT / ".env"),
             "-f", str(ROOT / "backend" / "docker-compose.yml"),
             "up", "--build"],
            cwd=str(ROOT),
        )
    except KeyboardInterrupt:
        print("\n已停止 XIAONA")
        return 0


if __name__ == "__main__":
    sys.exit(main())
