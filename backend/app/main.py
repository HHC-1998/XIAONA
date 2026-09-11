"""FastAPI 入口（01 §8 目录树 app/main.py）。

- 启动时建表 + 启动遗忘定时任务（asyncio 后台循环，02 §8；原 APScheduler 描述已过时）。
- 注册全部路由（auth / chat / sessions / settings / memory / voice / data / health / feishu）。
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.database import init_db
from app.config import FORGET_INTERVAL_MINUTES, JWT_SECRET
from app.api import (
    auth, chat, sessions, settings, memory, voice, data, health, feishu, agent,
)

logger = logging.getLogger("xiaona")


async def _forget_loop():
    """后台定时遗忘任务（02 §6）：重算权重 + 三层同步处理低权重消息。"""
    from app.core.forgetter import process_forgetting
    from app.db.chroma_client import chroma
    from app.db.neo4j_client import neo4j_client
    from app.core.llm_provider import llm
    from app.db.database import SessionLocal

    interval = FORGET_INTERVAL_MINUTES * 60
    while True:
        await asyncio.sleep(interval)
        try:
            with SessionLocal() as db:
                await process_forgetting(db, chroma, neo4j_client, llm)
        except Exception as e:
            logger.error(f"forgetting task failed: {e}")


async def _artifact_clean_loop():
    """产物清理任务（04 §9 / 05 §9）：每日删除超过 TTL 的产物文件与空目录。"""
    from app.core.artifact_cleaner import cleanup_artifacts

    interval = 24 * 3600
    while True:
        await asyncio.sleep(interval)
        try:
            await cleanup_artifacts()
        except Exception as e:
            logger.error(f"artifact cleanup task failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if JWT_SECRET == "dev-secret-change-me":
        # 生产必须显式配置 JWT_SECRET，否则任何人都可伪造 token（2026-08-17 审查告警）
        logger.warning("JWT_SECRET 仍为默认值 dev-secret-change-me，生产部署必须通过环境变量配置！")
    init_db()
    logger.info("XIAONA backend started")
    task = asyncio.create_task(_forget_loop())
    clean_task = asyncio.create_task(_artifact_clean_loop())
    yield
    task.cancel()
    clean_task.cancel()


app = FastAPI(title="XIAONA", lifespan=lifespan)

# 开发模式跨域（生产经 nginx 同源代理，不需要）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(sessions.router)
app.include_router(settings.router)
app.include_router(memory.router)
app.include_router(voice.router)
app.include_router(data.router)
app.include_router(health.router)
app.include_router(feishu.router)
app.include_router(agent.router)
