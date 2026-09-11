"""任务产物清理（04 §9 / 05 §9）：删除超过 TTL 的产物文件与空任务目录。

产物落盘结构：{ARTIFACTS_DIR}/{user_id}/{task_id}/xxx.pdf|docx|xlsx|临时图片。
- 按文件 mtime 判断是否超过 ARTIFACTS_TTL_DAYS（默认 30 天）；
- 清理后若任务目录为空则整目录删除（含 user_id 目录为空时）；
- 文件遍历为同步 IO，由 main 的后台循环 asyncio.to_thread 下沉线程池。
"""
import asyncio
import logging
import os
import time

from app.config import ARTIFACTS_DIR, ARTIFACTS_TTL_DAYS

logger = logging.getLogger("xiaona")


def _cleanup_once() -> int:
    """执行一轮清理，返回删除文件数。同步函数（供 to_thread 下沉）。"""
    if not os.path.isdir(ARTIFACTS_DIR):
        return 0
    cutoff = time.time() - ARTIFACTS_TTL_DAYS * 86400
    removed = 0
    for user_id in os.listdir(ARTIFACTS_DIR):
        user_dir = os.path.join(ARTIFACTS_DIR, user_id)
        if not os.path.isdir(user_dir):
            continue
        for task_id in os.listdir(user_dir):
            task_dir = os.path.join(user_dir, task_id)
            if not os.path.isdir(task_dir):
                continue
            for name in os.listdir(task_dir):
                path = os.path.join(task_dir, name)
                try:
                    if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed += 1
                except OSError as e:
                    logger.warning("artifact cleanup skip %s: %s", path, e)
            # 任务目录已空则删除
            try:
                if not os.listdir(task_dir):
                    os.rmdir(task_dir)
            except OSError:
                pass
        try:
            if not os.listdir(user_dir):
                os.rmdir(user_dir)
        except OSError:
            pass
    if removed:
        logger.info("artifact cleanup removed %d expired files (TTL=%dd)", removed, ARTIFACTS_TTL_DAYS)
    return removed


async def cleanup_artifacts() -> int:
    """异步入口：文件遍历下沉线程池，防阻塞事件循环。"""
    return await asyncio.to_thread(_cleanup_once)
