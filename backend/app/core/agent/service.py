"""Agent 服务入口（02 §5/§6）：供 chat.py（网页/桌面）与 feishu.py（飞书）复用。

处理顺序（每条用户消息只走一条路径）：
1. 有 pending 确认任务 → 判定确认/取消/其它；
2. 无 pending 但意图命中 → 提炼任务 + 创建 pending + 反问确认；
3. 都不是 → 回落到 memory_engine.chat/chat_stream（正常聊天链路）。

渠道分流（确认执行时确定目标会话）：
- web（聊天页）→ 当前会话；
- desktop（桌面助手）→ 新建专属会话（source=desktop），任务全程写入；
- feishu → 映射会话（仅已绑定 open_id 才会走到 agent 链路）。
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime
from dataclasses import dataclass, field

from app.core.agent import director
from app.core.agent.workflow import start_task_async
from app.core.memory_engine import sql as memory_sql
from app.core.memory_engine import _model_configs
from app.db.database import SessionLocal

logger = logging.getLogger("xiaona")

# 后台任务句柄强引用集合（2026-08-24 修）：asyncio.create_task 只持弱引用，
# 事件循环没有强引用时任务可能被 GC 回收而从不执行（任务创建后立刻确认、后台静默不跑）。
# 完成/异常时从集合移除。
_background_tasks: set = set()


@dataclass
class HandleResult:
    """一次消息的处理结果。

    - reply=None 表示回落正常聊天链路（调用方自行调 memory_engine）；
    - reply 非空表示 agent 已处理（确认文案 / 确认开始 / 取消），reply 即回复文本；
    - task_started：确认执行时非空 {task_id, session_id}（SSE 多一帧，前端据此轮询）。
    """
    session_id: str
    reply: str | None = None
    task_started: dict | None = None
    events: list = field(default_factory=list)  # 附加事件（保留给调用方扩展）


def _create_task(user_id: str, session_id: str, channel: str,
                 goal: str, refined: dict) -> str:
    """创建 pending 任务，返回 task_id。"""
    from app.db.database import SessionLocal
    from app.db.sql_models import AgentTask
    task_id = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(AgentTask(
            id=task_id, user_id=user_id, session_id=session_id,
            status="pending", task_type=refined.get("task_type") or "other",
            goal=goal, refined=json.dumps(refined, ensure_ascii=False),
            channel=channel,
        ))
        db.commit()
    return task_id


def _mark_status(task_id: str, status: str) -> None:
    from app.db.database import SessionLocal
    from app.db.sql_models import AgentTask
    try:
        with SessionLocal() as db:
            t = db.query(AgentTask).filter_by(id=task_id).first()
            if t:
                t.status = status
                t.updated_at = datetime.utcnow()
                db.commit()
    except Exception:
        logger.warning("agent status update failed", exc_info=True)


def _latest_pending(user_id: str, session_id: str, channel: str) -> dict | None:
    """取当前用户在当前会话+渠道下最近一条待确认任务（纯 dict）。

    按 session_id + channel 过滤：避免用户在另一个会话或飞书/桌面发消息时，
    把其它会话的 pending 任务误判为确认/取消（2026-08-19 审查修复，见当天台账）。
    """
    from app.db.database import SessionLocal
    from app.db.sql_models import AgentTask
    with SessionLocal() as db:
        t = (db.query(AgentTask)
             .filter(AgentTask.user_id == user_id,
                     AgentTask.status == "pending",
                     AgentTask.session_id == session_id,
                     AgentTask.channel == channel)
             .order_by(AgentTask.created_at.desc()).first())
        if not t:
            return None
        return {"id": t.id, "session_id": t.session_id, "channel": t.channel,
                "goal": t.goal, "refined": t.refined}


def _write_session_message(user_id: str, session_id: str, role: str, content: str,
                           source: str, embedding_cfg: dict | None = None) -> None:
    """会话留痕（同步落库，调用方在 async 上下文里 to_thread 包装）。"""
    memory_sql.save_chat_message(
        user_id, session_id, role, content, source,
        embedding_cfg=embedding_cfg,
    )


async def _persist_user_and_reply(user_id: str, session_id: str, user_text: str,
                                  reply: str, source: str,
                                  embedding_cfg: dict | None = None) -> None:
    """把「用户消息 + assistant 确认文案」写入目标会话（agent 路径的留痕）。"""
    await asyncio.to_thread(
        _write_session_message, user_id, session_id, "user", user_text, source,
        embedding_cfg,
    )
    await asyncio.to_thread(
        _write_session_message, user_id, session_id, "assistant", reply, source,
        embedding_cfg,
    )


def _make_confirmation_reply(refined: dict) -> str:
    """第一轮反问确认文案。"""
    desc = refined.get("description") or ""
    task_type_label = {
        "scrape": "网页/数据采集", "generate": "文件生成", "analyze": "汇总分析",
    }.get(refined.get("task_type"), "功能任务")
    return (
        f"我准备开始执行这个任务（{task_type_label}）：{desc}\n"
        "是否确认执行？回复「是」开始，回复「取消」放弃。"
    )


class AgentService:
    async def handle(self, text: str, user_id: str, session_id: str, source: str,
                     attachments: list[dict] | None = None,
                     notify=None) -> HandleResult:
        """统一入口。返回 HandleResult（reply=None → 调用方回落正常聊天链路）。

        notify：异步回调 async (最终消息文本) -> None，供飞书渠道在任务完成后主动
        回发结果（聊天页/桌面无需）。
        """
        # 确保会话存在（聊天页/飞书用当前会话；desktop 确认后才会新建专属会话）
        sid = await asyncio.to_thread(memory_sql.ensure_session, user_id, session_id or "", source)

        with SessionLocal() as db:
            cfgs = _model_configs(db, user_id)
        chat_cfg = cfgs.get("chat")

        # 1) 有 pending 确认任务 → 判定（仅限当前会话+渠道，防跨会话/跨渠道串扰）
        pending = _latest_pending(user_id, sid, _channel(source))
        if pending:
            verdict = await director.classify_confirmation(text, chat_cfg)
            if verdict == "yes":
                return await self._confirm(pending, user_id, text, source, notify)
            if verdict == "no":
                _mark_status(pending["id"], "cancelled")
                reply = "好的，已取消这个任务。需要别的帮助随时告诉我。"
                if pending["session_id"]:
                    await _persist_user_and_reply(
                        user_id, pending["session_id"], text, reply, source,
                        cfgs.get("embedding"),
                    )
                return HandleResult(session_id=sid, reply=reply)
            # other：保持待确认，再次提示（2026-08-28 起补落库：此前此分支不落库，
            # 刷新会话后用户这句话与重问消失，与 no/yes 分支行为不一致）
            reply = "不好意思，没听清楚。是否执行刚才那个任务？回复「是」开始，回复「取消」放弃。"
            if pending["session_id"]:
                await _persist_user_and_reply(
                    user_id, pending["session_id"], text, reply, source,
                    cfgs.get("embedding"),
                )
            return HandleResult(session_id=sid, reply=reply)

        # 2) 无 pending 但命中任务意图 → 提炼 + 创建 + 反问
        if director.is_task_intent(text):
            # 附件透传给提炼器：附件名/提取文本拼进任务上下文，执行者才能感知
            #（此前附件被 agent 丢弃，2026-08-24 审核修复）
            refined = await director.refine_task(text, chat_cfg, attachments=attachments)
            task_id = _create_task(user_id, sid, _channel(source), text, refined)
            logger.info("agent task %s created (pending) channel=%s", task_id, source)
            reply = _make_confirmation_reply(refined)
            # 会话留痕：写当前会话（desktop 当前会话也写，确认后换专属会话）
            await _persist_user_and_reply(
                user_id, sid, text, reply, source, cfgs.get("embedding"),
            )
            try:
                from app.core.memory_engine import _maybe_auto_title
                await _maybe_auto_title(user_id, sid, text, chat_cfg)
            except Exception:
                logger.warning("agent auto title failed", exc_info=True)
            return HandleResult(session_id=sid, reply=reply)

        # 3) 回落正常聊天
        return HandleResult(session_id=sid)

    async def _confirm(self, pending: dict, user_id: str, user_text: str,
                       source: str, notify) -> HandleResult:
        """用户确认执行：确定目标会话 → 后台启动任务 → 返回确认回复。"""
        task_id = pending["id"]
        old_sid = pending["session_id"] or ""
        with SessionLocal() as db:
            cfgs = _model_configs(db, user_id)
        embedding_cfg = cfgs.get("embedding")

        # 渠道分流确定目标会话
        target_sid = old_sid
        if source == "desktop":
            # 桌面助手确认后新建专属会话（source=desktop），任务全程写入
            target_sid = await asyncio.to_thread(
                memory_sql.ensure_session, user_id, "", "desktop"
            )
        # feishu/web：沿用映射/当前会话

        _mark_status(task_id, "confirmed")
        # 记录目标会话（后台任务据此写入留痕）
        try:
            from app.db.sql_models import AgentTask
            with SessionLocal() as db:
                t = db.query(AgentTask).filter_by(id=task_id).first()
                if t:
                    t.session_id = target_sid
                    t.channel = _channel(source)
                    db.commit()
        except Exception:
            logger.warning("agent confirm persist failed", exc_info=True)

        reply = "好的，开始执行任务。我会在后台处理，完成后结果会展示在这里（或飞书回复中），请稍候。"
        # 把确认对话写入目标会话留痕
        if target_sid:
            await _persist_user_and_reply(
                user_id, target_sid, user_text, reply, _session_source(source),
                embedding_cfg,
            )

        # 后台执行（不阻塞 SSE 流；notify 供飞书完成后回发）
        # 句柄存入模块级集合防 GC 回收（2026-08-24 修），完成时自移除
        task = asyncio.create_task(start_task_async(task_id, notify=notify))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return HandleResult(
            session_id=target_sid or old_sid,
            reply=reply,
            task_started={"task_id": task_id, "session_id": target_sid or old_sid},
        )


def _channel(source: str) -> str:
    return "desktop" if source == "desktop" else ("feishu" if source == "feishu" else "web")


def _session_source(source: str) -> str:
    return source  # 写入会话的 source 与渠道一致


agent_service = AgentService()
