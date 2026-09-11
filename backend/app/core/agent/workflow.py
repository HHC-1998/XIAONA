"""任务编排（02 §5/§6）：渠道分流 → 后台执行 → 审核打回 → 会话留痕。

职责：
- 确定目标会话：web→当前会话、desktop→新建专属会话、feishu→映射会话（仅已绑定 open_id）；
- 后台 `asyncio.create_task` 跑 执行→审核→打回重做（≤AGENT_MAX_RETRY 次），全程更新 AgentTask 状态；
- 每阶段向目标会话写 assistant 消息（含产物下载链接），保证用户回聊天页即可看到任务留痕；
- 完成/失败通过 `notify` 回调（飞书回发）。
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime

from app.config import AGENT_MAX_RETRY, ARTIFACTS_DIR
from app.core.agent.executor import run as executor_run
from app.core.agent.reviewer import review as reviewer_review
from app.core.memory_engine import sql as memory_sql

logger = logging.getLogger("xiaona")

# 产物下载链接前缀（前端 Markdown 可点击，对应 api/agent.py 的 artifacts 下载接口）
_ARTIFACT_LINK = "/api/agent/tasks/{task_id}/artifacts/{name}"

# 工具中文名映射：进度消息不暴露英文函数名，用用户可读文案（2026-08-23 起）
_TOOL_LABELS = {
    "fetch_url": "读取网页内容",
    "fetch_session_messages": "读取聊天记录",
    "fetch_amazon_bestsellers": "抓取 Amazon 商品榜单",
    "fetch_etsy_trending": "抓取 Etsy 热门商品榜单",
    "fetch_pinterest_trends": "抓取 Pinterest 趋势",
    "generate_excel": "生成 Excel 表格",
    "generate_pdf": "生成 PDF 文档",
    "generate_docx": "生成 Word 文档",
}


# 纯佐证信号源：失败不影响交付，进度消息里不弹 ⚠️ 警告（2026-08-27 起，
# Pinterest 未配置很常见，警告只会吓用户；2026-08-28 移除 Google Trends/Movers）
_SILENT_DEGRADE_TOOLS = {"fetch_pinterest_trends"}


def _tool_label(name: str) -> str:
    return _TOOL_LABELS.get(name, name)


def _tool_args_summary(args: dict) -> str:
    """从工具参数里提炼给用户看的摘要（不展示完整参数避免刷屏）。"""
    if not args:
        return ""
    bits = []
    if args.get("keywords"):
        bits.append(f"关键词：{args['keywords']}")
    elif args.get("keyword"):
        bits.append(f"关键词：{args['keyword']}")
    if args.get("category"):
        bits.append(f"类目：{args['category']}")
    if args.get("url"):
        bits.append(args["url"][:80])
    if args.get("filename"):
        bits.append(f"文件：{args['filename']}")
    if args.get("title"):
        bits.append(f"标题：{args['title']}")
    if args.get("limit"):
        bits.append(f"条数：{args['limit']}")
    if args.get("region"):
        bits.append(f"地区：{args['region']}")
    if args.get("trend_type"):
        bits.append(f"趋势类型：{args['trend_type']}")
    # 过滤条件（2026-08-23 起，配合进度消息让用户看到执行者按什么门槛在抓；
    # 2026-08-24 补评分≤/评论≤/颜色/材质）
    for key, label in (("site", "站点"), ("min_price", "价格≥"), ("max_price", "价格≤"),
                       ("min_rating", "评分≥"), ("max_rating", "评分≤"),
                       ("min_reviews", "评论≥"), ("max_reviews", "评论≤"),
                       ("min_favorers", "收藏≥"), ("color", "颜色"), ("material", "材质"),
                       ("exclude_keywords", "排除")):
        v = args.get(key)
        if v is None or v == "":
            continue
        if key == "site" or key in ("color", "material", "exclude_keywords"):
            bits.append(f"{label}：{v}")
        else:
            bits.append(f"{label}{v}")
    if not bits:
        return ""
    return "（" + "，".join(bits) + "）"


def _status_sync(task_id: str, status: str) -> None:
    """同步更新任务状态（独立 DB 会话，可被后台任务安全调用）。"""
    from app.db.database import SessionLocal
    from app.db.sql_models import AgentTask
    try:
        with SessionLocal() as db:
            t = db.query(AgentTask).filter_by(id=task_id).first()
            if not t:
                return
            t.status = status
            t.updated_at = datetime.utcnow()
            db.commit()
    except Exception:
        logger.warning("agent status update failed: task=%s status=%s", task_id, status, exc_info=True)


async def _status(task_id: str, status: str) -> None:
    """更新任务状态（async 包装：同步 DB 下沉线程池，2026-08-24 统一）。"""
    await asyncio.to_thread(_status_sync, task_id, status)


def _log_sync(task_id: str, role: str, action: str, payload: dict, round_no: int = 0) -> None:
    """同步追加一条 AgentRunLog（独立 DB 会话）。round 为执行/审核轮次。"""
    from app.db.database import SessionLocal
    from app.db.sql_models import AgentRunLog
    try:
        with SessionLocal() as db:
            db.add(AgentRunLog(
                id=str(uuid.uuid4()), task_id=task_id, role=role, round=round_no,
                action=action, payload=json.dumps(payload, ensure_ascii=False),
            ))
            db.commit()
    except Exception:
        logger.warning("agent log write failed", exc_info=True)


async def _log(task_id: str, role: str, action: str, payload: dict, round_no: int = 0) -> None:
    """追加一条 AgentRunLog（async 包装：同步 DB 下沉线程池，2026-08-24 统一）。"""
    await asyncio.to_thread(_log_sync, task_id, role, action, payload, round_no)


def _load_task(task_id: str) -> dict | None:
    """读取任务（独立 DB 会话，返回纯 dict）。"""
    from app.db.database import SessionLocal
    from app.db.sql_models import AgentTask
    with SessionLocal() as db:
        t = db.query(AgentTask).filter_by(id=task_id).first()
        if not t:
            return None
        return {
            "id": t.id, "user_id": t.user_id, "session_id": t.session_id,
            "status": t.status, "task_type": t.task_type, "goal": t.goal,
            "refined": t.refined, "retry_count": t.retry_count,
            "artifacts": t.artifacts, "channel": t.channel,
        }


async def _write_message(user_id: str, session_id: str, content: str, source: str,
                         embedding_cfg: dict | None = None, task_id: str | None = None) -> None:
    """向目标会话写一条 assistant 消息（async：落库 + Chroma embedding 走 to_thread，
    避免 embedding 调用阻塞事件循环，见 AGENTS.md 后端健壮性约定）。

    task_id 非空时写消息后 touch 任务 updated_at（独立 DB 会话），让前端 Agent
    任务轮询能感知进度变化，进度气泡逐条出现（2026-08-23 起）。
    """
    try:
        await asyncio.to_thread(
            memory_sql.save_chat_message,
            user_id, session_id, "assistant", content, source,
            embedding_cfg=embedding_cfg,
        )
    except Exception:
        logger.warning("agent session write failed", exc_info=True)
    if task_id:
        await _touch_task(task_id)


def _touch_task_sync(task_id: str) -> None:
    """同步 touch 任务 updated_at（独立 DB 会话，可被后台任务安全调用）。"""
    from app.db.database import SessionLocal
    from app.db.sql_models import AgentTask
    try:
        with SessionLocal() as db:
            t = db.query(AgentTask).filter_by(id=task_id).first()
            if not t:
                return
            t.updated_at = datetime.utcnow()
            db.commit()
    except Exception:
        logger.warning("agent task touch failed: task=%s", task_id, exc_info=True)


async def _touch_task(task_id: str) -> None:
    """touch 任务 updated_at（async 包装：同步 DB 下沉线程池，2026-08-24 统一）。"""
    await asyncio.to_thread(_touch_task_sync, task_id)


def _persist_artifacts_sync(task_id: str, artifacts: list[str]) -> None:
    """同步把产物相对路径写入 AgentTask.artifacts（JSON）。"""
    from app.db.database import SessionLocal
    from app.db.sql_models import AgentTask
    try:
        with SessionLocal() as db:
            t = db.query(AgentTask).filter_by(id=task_id).first()
            if not t:
                return
            existing = []
            try:
                existing = json.loads(t.artifacts or "[]")
            except Exception:
                pass
            merged = list(dict.fromkeys(existing + artifacts))
            t.artifacts = json.dumps(merged, ensure_ascii=False)
            db.commit()
    except Exception:
        logger.warning("agent artifacts persist failed", exc_info=True)


async def _persist_artifacts(task_id: str, artifacts: list[str]) -> None:
    """把产物相对路径写入 AgentTask.artifacts（async 包装，2026-08-24 统一）。"""
    await asyncio.to_thread(_persist_artifacts_sync, task_id, artifacts)


def _format_artifact_list(task_id: str, artifacts: list[str], user_id: str = "") -> str:
    """把产物路径列表转成会话消息里的下载链接文本（附落盘位置提示，2026-08-24 起）。"""
    if not artifacts:
        return ""
    lines = ["\n\n📄 已生成产物："]
    for name in artifacts:
        base = name.rsplit("/", 1)[-1]
        lines.append(f"- [{base}]({_ARTIFACT_LINK.format(task_id=task_id, name=base)})")
    if user_id:
        lines.append(f"\n> 文件保存在项目根目录 `artifacts/{user_id}/{task_id}/` 下（容器已挂载到宿主机），也可点上方链接直接下载。")
    return "\n".join(lines)


# 榜单数据源工具（其 ok 且带 markdown_table 的结果由 workflow 自动附加到最终回复）
_RANKING_TOOLS = ("fetch_amazon_bestsellers", "fetch_etsy_trending")


def _format_ranking_table(result: dict | None, final_msg: str = "") -> str:
    """从执行者结果里取全部榜单 markdown_table，拼成可附加的文本（2026-08-27 改：多表 + 同源保留最后一次）。

    表格由数据源工具后端生成（严格 7/8 列、列对齐、完整商品网址、缺失填「-」），
    替代执行者 LLM 手拼表格（LLM 少写 | 就整行右移、长 URL 被省略）。
    收集**所有** ok:true 且带 markdown_table 的榜单工具结果，每张表前加来源中文标签。
    同源多次调用（如换词重试）**保留最后一次**——执行者最后一次调用通常排除词更全、
    筛选更严（曾出现第一次宽松结果混入耳环/玩具、表格展示与严格总结错位，2026-08-27 修复）。
    仍按首次出现顺序输出（同源表格顺序稳定）。
    final_msg 已含表格分隔行（|---）时跳过——兼容执行者旧手拼行为的兜底，避免重复表格。
    """
    if not result or "|---" in (final_msg or ""):
        return ""
    first_order: list[str] = []
    latest: dict[str, str] = {}
    for tr in (result.get("tool_results") or []):
        if isinstance(tr, dict) and tr.get("name") in _RANKING_TOOLS \
                and tr.get("ok") and tr.get("markdown_table"):
            name = tr["name"]
            if name not in latest:
                first_order.append(name)
            latest[name] = str(tr["markdown_table"]).strip()
    blocks = []
    for name in first_order:
        label = _tool_label(name)
        blocks.append(f"\n\n📊 **{label}**\n" + latest[name])
    return "".join(blocks)


async def start_task_async(task_id: str, notify=None) -> None:
    """后台执行任务：执行者 → 审核者 → 打回重做，全程更新状态 + 会话留痕。

    由 service 在用户确认后 `asyncio.create_task` 启动（不阻塞 SSE 流）。
    notify：异步回调 async (最终消息文本) -> None，飞书渠道任务完成后主动回发。
    """
    task = _load_task(task_id)
    if not task:
        return
    # 取消检查（2026-08-24 修）：用户确认后、后台任务真正启动前可能已取消
    #（_confirm 置 confirmed → create_task，期间 cancel API 把状态改成 cancelled），
    # 若不做检查会继续执行一个已被取消的任务
    if task["status"] == "cancelled":
        logger.info("agent task %s cancelled before start, skip", task_id)
        return
    user_id, session_id, channel = (
        task["user_id"], task["session_id"], task["channel"],
    )
    source = "desktop" if channel == "desktop" else ("feishu" if channel == "feishu" else "web")
    artifacts_dir = ARTIFACTS_DIR

    try:
        refined = json.loads(task["refined"] or "{}")
    except Exception:
        refined = {"task_type": task["task_type"], "description": task["goal"], "params": {}}

    from app.core.memory_engine import _model_configs
    from app.db.database import SessionLocal
    with SessionLocal() as db:
        cfgs = _model_configs(db, user_id)
    # 执行者未分配 → 回退聊天模型；审核者未分配 → 回退执行者 → 聊天模型（02 §3）
    executor_cfg = cfgs.get("executor") or cfgs.get("chat")
    reviewer_cfg = cfgs.get("reviewer") or cfgs.get("executor") or cfgs.get("chat")
    if not executor_cfg:
        await _status(task_id, "failed")
        msg = "执行者模型不可用（未配置对话模型），任务无法执行。"
        if session_id:
            await _write_message(user_id, session_id, msg, source, cfgs.get("embedding"),
                                 task_id=task_id)
        await _log(task_id, "executor", "fail", {"reason": "no_chat_model"})
        return

    async def _on_log(role: str, action: str, payload: dict) -> None:
        # round 记录真实轮次：外层 attempt（打回重做轮）+ 内层 payload 中的工具轮
        await _log(task_id, role, action, payload, round_no=attempt + 1)
        # 工具调用/失败节点向会话写进度消息（用户可感知执行到哪一步，2026-08-23 起）
        if not session_id:
            return
        try:
            if action == "call_tool":
                label = _tool_label((payload.get("tool") or ""))
                summary = _tool_args_summary(payload.get("args") or {})
                await _write_message(
                    user_id, session_id,
                    f"🔧 正在{label}{summary}…", source, cfgs.get("embedding"),
                    task_id=task_id)
            elif action == "tool_result" and not payload.get("ok"):
                # 佐证信号源失败静默：不写 ⚠️ 进度气泡（2026-08-27 起）
                if (payload.get("tool") or "") in _SILENT_DEGRADE_TOOLS:
                    return
                label = _tool_label((payload.get("tool") or ""))
                err = str(payload.get("error") or "")[:60]
                suffix = f"（{err}）" if err else ""
                await _write_message(
                    user_id, session_id,
                    f"⚠️ {label}未成功{suffix}，正在尝试其它方案…", source, cfgs.get("embedding"),
                    task_id=task_id)
        except Exception:
            logger.warning("agent progress message write failed", exc_info=True)

    await _status(task_id, "executing")
    if session_id:
        await _write_message(user_id, session_id,
                             f"▶ 开始执行任务：{refined.get('description') or task['goal']}",
                             source, cfgs.get("embedding"), task_id=task_id)

    ctx = {"user_id": user_id, "task_id": task_id, "artifacts_dir": artifacts_dir,
           "session_id": session_id}
    result = None
    final_msg = None
    for attempt in range(1 + AGENT_MAX_RETRY):  # 初始 + AGENT_MAX_RETRY 次打回重做

        # 打回重做时把审核意见带给执行者（作为下一轮 task 的补充约束）
        if attempt > 0:
            await _status(task_id, "revising")
            review_notes = result.get("issues") or []
            if review_notes:
                refined["review_issues"] = review_notes
            if session_id:
                await _write_message(user_id, session_id,
                                     f"↻ 审核未通过（第 {attempt} 次重做）：" + "；".join(review_notes)[:200],
                                     source, cfgs.get("embedding"), task_id=task_id)
        else:
            await _status(task_id, "executing")
        try:
            result = await executor_run(refined, executor_cfg, on_log=_on_log, ctx=ctx)
        except Exception as e:
            # 异常也落入下方统一失败处理（不 break，否则 final_msg 保持 None，
            # 会话留痕会写成「❌ None」，见 2026-08-19 台账）
            logger.warning("executor run failed: %s", e, exc_info=True)
            result = {"success": False, "summary": f"执行出错：{e}", "artifacts": [], "log": []}

        # 执行者明确失败（无可用工具/超轮次）→ 不再审核，直接失败
        if not result.get("success"):
            final_msg = result.get("summary") or "任务执行失败。"
            await _log(task_id, "executor", "fail", {"summary": final_msg})
            break

        # 审核（未分配审核者模型时回退执行者→聊天模型；02 §3）
        await _status(task_id, "reviewing")
        if session_id:
            await _write_message(user_id, session_id,
                                 f"🧐 执行完成，正在验收…", source, cfgs.get("embedding"),
                                 task_id=task_id)
        try:
            verdict = await reviewer_review(refined, result, reviewer_cfg, on_log=_on_log)
        except Exception as e:
            # 审核模型超时/异常：降级视为通过（passed=True），防任务永远卡在 reviewing
            #（2026-08-27 任务韧性修复，见当天台账二十六）
            logger.warning("reviewer_review failed, pass through: %s", e, exc_info=True)
            verdict = {"passed": True, "issues": []}

        if verdict.get("passed"):
            final_msg = result.get("summary") or "任务已完成。"
            break
        # 打回重做（最多 AGENT_MAX_RETRY 次）
        retry_count = attempt + 1
        from app.db.database import SessionLocal
        from app.db.sql_models import AgentTask
        with SessionLocal() as db:
            t = db.query(AgentTask).filter_by(id=task_id).first()
            if t:
                t.retry_count = retry_count
                db.commit()
        await _log(task_id, "reviewer", "reject", {"round": retry_count, "issues": verdict.get("issues")},
                   round_no=retry_count)
        if retry_count > AGENT_MAX_RETRY:
            final_msg = "经多次验收仍未通过，任务终止。" + ("；问题：" + "；".join(verdict.get("issues") or []) if verdict.get("issues") else "")
            result["success"] = False
            break
        result["issues"] = verdict.get("issues") or []
    else:
        final_msg = "任务执行中断。"
        result = result or {"success": False, "artifacts": [], "log": []}

    artifacts = result.get("artifacts") or []
    if artifacts:
        await _persist_artifacts(task_id, artifacts)
    ok = bool(result.get("success"))

    # 会话留痕（回发由 notify 处理）
    if session_id:
        artifact_txt = _format_artifact_list(task_id, artifacts, user_id)
        table_txt = _format_ranking_table(result, final_msg)
        status_icon = "✅" if ok else "❌"
        write_msg = f"{status_icon} {final_msg}{table_txt}{artifact_txt}"
        await _write_message(user_id, session_id, write_msg, source, cfgs.get("embedding"),
                             task_id=task_id)
        # 会话标题自动生成兜底：任务消息触发一次（避免专属会话一直叫「新会话」）
        try:
            from app.core.memory_engine import _maybe_auto_title
            await _maybe_auto_title(
                user_id, session_id,
                (refined.get("description") or task["goal"]), cfgs.get("chat"),
            )
        except Exception:
            logger.warning("agent auto title failed", exc_info=True)

    # 飞书渠道：任务完成后主动回发结果
    if notify:
        try:
            artifact_txt = _format_artifact_list(task_id, artifacts, user_id)
            table_txt = _format_ranking_table(result, final_msg)
            status_icon = "✅" if ok else "❌"
            final_text = f"{status_icon} 任务「{refined.get('description') or task['goal']}」已完成：{final_msg}{table_txt}{artifact_txt}"
            await notify(final_text)
        except Exception:
            logger.warning("agent notify callback failed", exc_info=True)

    await _status(task_id, "done" if ok else "failed")
    logger.info("agent task %s finished ok=%s", task_id, ok)
