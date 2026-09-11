"""读取会话消息工具（04/05 §7）：供执行者在「把聊天整理成文档」类任务中获取会话内容。

- 通过 ctx.session_id 读当前会话最近 N 条消息（用户 + 助手），返回纯文本列表；
- 查询按 created_at 降序取尾再反转保持时间正序（2026-08-24 修复：此前 asc+limit 取到
  会话最早 N 条，长会话下「把刚才的对话整理成文档」整理的是开头寒暄）；
- 过滤任务**瞬态进度**消息（▶/🔧/⚠️/🧐/↻ 开头的 assistant 消息，workflow 执行任务时
  写进度到同一会话，整理文档不应混入）；**最终结果消息（✅/❌ 开头，含榜单/下载链接）
  保留**——过滤条件下推 SQL，保证「最近 N 条非进度消息」（2026-08-24 二轮修复）；
- 返回值带 `markdown_tables`：从全部消息提取的 markdown 表格原文（2026-08-24）；
  `assistant_tables`：仅助手消息提取的表格（2026-09-01 起，executor 把用户原文兜底注入
  content 后，用户侧表格已在原文里、只注入助手表格避免重复渲染）；
  供 generate_pdf/docx 代码直接渲染（图片列 `![alt](URL)` 原样保留，不依赖 LLM 转写）；
- 校验会话属于 ctx.user_id 且未软删（防御性归属校验）。
"""
import asyncio
import logging
import re

from sqlalchemy import and_, not_, or_

logger = logging.getLogger("xiaona")

_MAX_MESSAGES = 60
# 任务瞬态进度消息前缀（workflow 写会话留痕用，整理文档时应忽略）。
# 注意：✅/❌ 不在其中——workflow 最终结果消息以此开头（含榜单/下载链接），须保留；
# ↻ 为审核打回重做（2026-08-24 二轮补上）。
_PROGRESS_PREFIXES = ("▶", "🔧", "⚠️", "🧐", "↻")

# markdown 表格分隔行（|---| / | :---: | 等），用于识别表格块
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def _extract_markdown_tables(messages: list[dict], roles: set[str] | None = None) -> list[str]:
    """从消息内容里提取 markdown 表格原文（含 `|---` 分隔行的连续表格块）。

    默认同时扫描 user 与 assistant 消息（2026-09-01 起：用户粘贴在聊天框的原文
    也常含表格，此前只扫 assistant 导致用户侧表格丢失）。`roles` 非 None 时只扫
    指定角色（assistant_tables 用）。保持原文（图片列 `![alt](URL)` 原样），供
    generate_pdf/docx 代码直接渲染，避免执行者 LLM 手工转写 sections 时丢弃
    图片列/列错位（2026-08-24）。
    """
    tables: list[str] = []
    for m in messages:
        if roles is not None and (m.get("role") or "") not in roles:
            continue
        lines = (m.get("content") or "").splitlines()
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            # 分隔行出现时，上一行「|」行视为表头，其后连续的「|」行是数据行
            if "---" in line and "|" in line and _TABLE_SEP_RE.match(line) and i > 0:
                header = lines[i - 1]
                if "|" in header:
                    block = [header, line]
                    j = i + 1
                    while j < n and "|" in lines[j] and not _TABLE_SEP_RE.match(lines[j]):
                        block.append(lines[j])
                        j += 1
                    tables.append("\n".join(block))
                    i = j
                    continue
            i += 1
    return tables


async def fetch_session_messages(params: dict, ctx: dict | None = None) -> dict:
    """读取会话最近消息。params: {limit?} → {ok, session_id?, messages?, total?}。

    - limit：可选，返回条数上限（默认 40，最大 60）；
    - ctx 必须带 session_id（workflow 在任务 ctx 中注入），否则返回结构化错误；
    - ctx.user_id 非空时校验会话归属（会话不属于该用户返回错误）。
    """
    from app.db.database import SessionLocal
    from app.db.sql_models import Message, Session

    session_id = (ctx or {}).get("session_id") or ""
    if not session_id:
        return {"ok": False, "error": "缺少会话上下文：无法读取聊天记录"}
    user_id = (ctx or {}).get("user_id") or ""
    try:
        limit = int((params or {}).get("limit") or 40)
    except (TypeError, ValueError):
        limit = 40
    limit = max(1, min(limit, _MAX_MESSAGES))

    def _load():
        with SessionLocal() as db:
            if user_id:
                # 软删会话视为不存在（AGENTS.md：后端会话查询一律过滤 is_deleted=False）
                sess = db.query(Session).filter_by(id=session_id, is_deleted=False).first()
                if not sess:
                    return {"error": "会话不存在"}
                if sess.user_id != user_id:
                    return {"error": "会话不属于当前用户，拒绝读取"}
            # 进度过滤下推 SQL：保证「最近 N 条非进度消息」（任务一次可写十几条进度，
            # 若先 limit 再内存过滤，返回的真实消息会明显少于请求条数）。
            # content.isnot(None)：like() 对 NULL 求值为 NULL，not_(and_(...)) 会整行排除
            # NULL 消息（SQL 三值逻辑），显式放行以与内存版 (m.content or "") 语义一致
            progress_clause = and_(
                Message.role == "assistant",
                Message.content.isnot(None),
                or_(*[Message.content.like(f"{p}%") for p in _PROGRESS_PREFIXES]),
            )
            rows = (
                db.query(Message)
                .filter(Message.session_id == session_id)
                .filter(not_(progress_clause))
                .order_by(Message.created_at.desc())
                .limit(limit)
                .all()
            )
            # 反转保持时间正序（最早在前、最近在后）
            items = [
                {"role": m.role, "content": m.content or "", "id": m.id}
                for m in reversed(rows)
            ]
            return {"messages": items}

    try:
        loaded = await asyncio.to_thread(_load)
    except Exception as e:
        logger.warning("fetch_session_messages failed", exc_info=True)
        return {"ok": False, "error": f"读取聊天记录失败：{e.__class__.__name__}"}
    if isinstance(loaded, dict) and loaded.get("error"):
        return {"ok": False, "error": loaded["error"]}
    messages = loaded["messages"]
    if not messages:
        return {"ok": True, "session_id": session_id, "messages": [], "total": 0,
                "markdown_tables": [], "assistant_tables": []}

    return {
        "ok": True,
        "session_id": session_id,
        "messages": messages,
        "total": len(messages),
        "markdown_tables": _extract_markdown_tables(messages),
        # 仅助手消息的表格（2026-09-01 起）：executor 把用户原文兜底注入 content 后，
        # 用户侧表格已在原文里，只注入助手表格避免重复渲染
        "assistant_tables": _extract_markdown_tables(messages, roles={"assistant"}),
    }
