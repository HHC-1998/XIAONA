"""记忆检索引擎 + 统一聊天核心（01 §5.3 / 02 §7）。

chat(text, user_id, session_id, source)：所有端（网页/CLI/飞书）最终调用同一聊天核心。
chat_stream(...)：流式版，仅回复以 SSE 帧输出，检索与持久化逻辑相同。

SQL 辅助函数集中在 `sql` 命名空间对象（retrieve_memory 中 `sql.get_recent_messages` 即此处）。
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime

from sqlalchemy.orm import Session as DbSession

from app.db.sql_models import Message, Session as SessionModel
from app.db.chroma_client import chroma
from app.db.neo4j_client import neo4j_client as kg
from app.core.llm_provider import llm, VisionNotSupportedError

logger = logging.getLogger("xiaona")

# 情绪/重要性启发式关键词
_POSITIVE_WORDS = ("开心", "高兴", "喜欢", "爱", "棒", "太好了", "满意")
_NEGATIVE_WORDS = ("难过", "生气", "讨厌", "失望", "伤心", "烦", "愤怒")
_IMPORTANT_WORDS = ("记住", "重要", "务必", "以后", "家人", "朋友", "工作", "喜欢", "讨厌", "去过")


class ChatModelNotConfigured(Exception):
    """对话模型未分配时抛出（前端据此提示到设置页配置，见 05 §11）。"""

    def __init__(self, message: str = "您还没有配置对话模型，请到设置页配置后再开始对话"):
        super().__init__(message)


def estimate_importance(text: str) -> float:
    hits = sum(1 for w in _IMPORTANT_WORDS if w in text)
    # 长度作为次要信号
    return min(1.0, hits * 0.25 + min(1.0, len(text) / 200))


def estimate_emotion(text: str) -> float:
    score = 0.0
    if "！" in text or "!" in text:
        score += 0.3
    if any(w in text for w in _POSITIVE_WORDS):
        score += 0.4
    if any(w in text for w in _NEGATIVE_WORDS):
        score += 0.4
    return min(1.0, score)


def mention_message(db_session, msg, now=None):
    """再次被提及时刷新记忆（02 §6）。"""
    now = now or datetime.utcnow()
    msg.mention_count += 1
    msg.last_mentioned_at = now
    msg.weight = min(1.0, msg.weight + 0.15)
    db_session.add(msg)


# ---------------------------------------------------------------------------
# SQL 辅助存储
# ---------------------------------------------------------------------------
class _SqlStore:
    def get_recent_messages(self, user_id: str, session_id: str, k: int = 5,
                            exclude_ids: set[str] | None = None) -> list[dict]:
        """最近 K 轮（当前用户 + 当前会话，未归档优先）。

        exclude_ids：排除的消息 id 集合（2026-08-24 起）——用户消息先行落库后，
        检索时把刚写入的当前消息排除，否则它既出现在 recent 又作为 user 消息注入
        LLM 上下文（重复注入自身，见当天台账）。
        """
        from app.db.database import SessionLocal
        with SessionLocal() as db:
            q = (
                db.query(Message)
                .filter(Message.session_id == session_id, Message.user_id == user_id,
                        Message.is_archived.is_(False))
                .order_by(Message.created_at.desc())
            )
            if exclude_ids:
                # 过滤必须在 limit 之前（Query 已带 LIMIT 后再 filter 会抛 InvalidRequestError）
                q = q.filter(Message.id.notin_(exclude_ids))
            rows = q.limit(k * 2).all()
            items = sorted(rows, key=lambda m: m.created_at)
            return [{"role": m.role, "content": m.content} for m in items]

    def ensure_session(self, user_id: str, session_id: str, source: str) -> str:
        """会话不存在时创建（chat 自愈：session_id 无效/为空时新建）。"""
        from app.db.database import SessionLocal
        with SessionLocal() as db:
            if session_id:
                s = db.query(SessionModel).filter_by(
                    id=session_id, user_id=user_id, is_deleted=False
                ).first()
                if s:
                    return s.id
            s = SessionModel(id=str(uuid.uuid4()), user_id=user_id, title="新会话", source=source)
            db.add(s)
            db.commit()
            db.refresh(s)
            return s.id

    def save_message(self, user_id: str, session_id: str, role: str, content: str,
                     source: str, importance: float, emotion: float,
                     thinking: str = "", attachments: str = "") -> Message:
        from app.db.database import SessionLocal
        with SessionLocal() as db:
            msg = Message(
                id=str(uuid.uuid4()), session_id=session_id, user_id=user_id,
                role=role, content=content, source=source,
                weight=0.5, importance=importance, emotion_score=emotion,
                thinking=thinking, attachments=attachments,
            )
            db.add(msg)
            # 会话「最近活跃」随新消息刷新：updated_at 有 onupdate 但只在 UPDATE 该行时触发，
            # 这里对新消息所在会话显式刷新，保证侧边栏按活跃度排序正确（07 §4）
            db.query(SessionModel).filter(
                SessionModel.id == session_id, SessionModel.user_id == user_id
            ).update({SessionModel.updated_at: datetime.utcnow()})
            db.commit()
            db.refresh(msg)
            return msg

    def get_all_sessions_with_messages(self, user_id: str) -> list[dict]:
        from app.db.database import SessionLocal
        with SessionLocal() as db:
            sessions = db.query(SessionModel).filter_by(
                user_id=user_id, is_deleted=False
            ).all()
            return [{
                "id": s.id, "title": s.title, "source": s.source,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                # 导出补消息级 source（#9，2026-08-24 修）：导入时用它重建而非一律回退
                # 会话级 source，桌面助手（desktop）消息混在 web 会话里也能原样还原
                "messages": [{"id": m.id, "role": m.role, "content": m.content,
                              "thinking": m.thinking or "",
                              "attachments": m.attachments or "",
                              "source": m.source or "",
                              "created_at": m.created_at.isoformat() if m.created_at else None,
                              "is_archived": m.is_archived or False,
                              "weight": m.weight or 0.5,
                              "importance": m.importance or 0.0,
                              "emotion_score": m.emotion_score or 0.0,
                              "last_mentioned_at": m.last_mentioned_at.isoformat()
                              if m.last_mentioned_at else None}
                             for m in s.messages],
            } for s in sessions]

    def import_session(self, data: dict, user_id: str, skip_existing: bool = True) -> bool:
        """导入单个会话（幂等：已存在 id 跳过）。返回是否导入。

        导出→清空→导入往返不丢字段（2026-08-17 起带 thinking/attachments/source；
        2026-08-24 起补齐 created_at/is_archived/weight/importance/emotion_score/
        last_mentioned_at 与消息级 source，保证记忆权重/归档状态/活跃时间原样恢复，
        防止导入后权重重置导致历史消息被遗忘流程提前归档）。
        """
        from app.db.database import SessionLocal
        with SessionLocal() as db:
            existing = db.query(SessionModel).filter_by(id=data["id"], user_id=user_id).first()
            if existing:
                return False
            s = SessionModel(id=data["id"], user_id=user_id,
                             title=data.get("title", "新会话"), source=data.get("source", "web"))
            db.add(s)
            for m in data.get("messages", []):
                created_at = None
                if m.get("created_at"):
                    try:
                        created_at = datetime.fromisoformat(m["created_at"])
                    except ValueError:
                        created_at = None
                last_mentioned = None
                if m.get("last_mentioned_at"):
                    try:
                        last_mentioned = datetime.fromisoformat(m["last_mentioned_at"])
                    except ValueError:
                        last_mentioned = None
                db.add(Message(
                    id=m.get("id", str(uuid.uuid4())), session_id=data["id"],
                    user_id=user_id, role=m["role"], content=m["content"],
                    source=m.get("source") or data.get("source", "web"),
                    thinking=m.get("thinking", "") or "",
                    attachments=m.get("attachments", "") or "",
                    created_at=created_at,
                    is_archived=bool(m.get("is_archived", False)),
                    weight=float(m.get("weight", 0.5)),
                    importance=float(m.get("importance", 0.0)),
                    emotion_score=float(m.get("emotion_score", 0.0)),
                    last_mentioned_at=last_mentioned,
                ))
            db.commit()
            return True

    def clear_messages_and_sessions(self, user_id: str) -> None:
        """清空用户会话与消息（09 §6，需密码）。

        按外键依赖逆序删除，避免 PostgreSQL 下外键 IntegrityError：
        agent_tasks.session_id / feishu_session_map.session_id 均引用 sessions.id 且无
        ON DELETE CASCADE，必须先删 AgentRunLog(task_id→agent_tasks) → AgentTask(session_id→sessions)
        → FeishuSessionMap(session_id→sessions) → Message(session_id→sessions) → Session。
        此前只删 messages→sessions 会在任何跑过 Agent 任务或绑过飞书的账号上报
        IntegrityError 并整体回滚（2026-08-24 审核修复）。
        """
        from app.db.database import SessionLocal
        from app.db.sql_models import AgentRunLog, AgentTask, FeishuSessionMap
        with SessionLocal() as db:
            task_ids = [t[0] for t in db.query(AgentTask.id).filter(AgentTask.user_id == user_id).all()]
            if task_ids:
                db.query(AgentRunLog).filter(AgentRunLog.task_id.in_(task_ids)).delete(
                    synchronize_session=False
                )
            db.query(AgentTask).filter(AgentTask.user_id == user_id).delete(synchronize_session=False)
            # FeishuSessionMap 无 user_id 列（10 §5：按 open_id 绑定会话），
            # 按「会话属于该用户」过滤：先取该用户全部会话 id，再删映射
            session_ids = [s[0] for s in db.query(SessionModel.id).filter(
                SessionModel.user_id == user_id
            ).all()]
            if session_ids:
                db.query(FeishuSessionMap).filter(
                    FeishuSessionMap.session_id.in_(session_ids)
                ).delete(synchronize_session=False)
            db.query(Message).filter(Message.user_id == user_id).delete(synchronize_session=False)
            db.query(SessionModel).filter(SessionModel.user_id == user_id).delete(
                synchronize_session=False
            )
            db.commit()

    def save_chat_message(self, user_id: str, session_id: str, role: str, content: str,
                          source: str, embedding_cfg: dict | None = None, thinking: str = "",
                          attachments: str = "") -> str:
        """持久化消息 + 写 Chroma（供 chat 内同步调用）。

        embedding_cfg=None 时跳过向量化写入（用户未配置向量化模型，见 02 §8 门控约定）；
        向量由设置页分配的 embedding 模型生成（chroma.upsert 内调用），Chroma 写失败
        也不阻断 SQL 主流程。
        thinking 为 assistant 消息的思考过程（DeepSeek reasoning_content）。
        attachments 为附件元数据 JSON（12 §）：[{name,kind,size}]，图片仅存元数据。
        返回消息 id（2026-08-24 起，供检索时排除当前消息防重复注入）。
        """
        importance = estimate_importance(content)
        emotion = estimate_emotion(content)
        msg = self.save_message(user_id, session_id, role, content, source, importance, emotion,
                                thinking=thinking, attachments=attachments)
        if not embedding_cfg:
            return msg.id
        try:
            # 同步写 Chroma embedding（id=msg_{id}，is_archived=false）
            chroma.upsert(
                ids=[f"msg_{msg.id}"],
                documents=[content],
                metadatas=[{
                    "user_id": user_id, "session_id": session_id, "source": source,
                    "original_id": msg.id, "is_archived": False,
                }],
                user_id=user_id, embedding_cfg=embedding_cfg,
            )
        except Exception:
            logger.warning("chroma upsert failed, degraded (skip vector write)", exc_info=True)


sql = _SqlStore()


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def _flatten_chroma(raw) -> list:
    """把 Chroma query() 返回的嵌套结构展平为一层 list，并过滤 None。

    Chroma 的 query(include=[...]) 返回的 documents/metadatas/distances 均为
    嵌套结构：每个 query_text 对应一个内层 list（query_texts=[query] 时仅 1 个），
    例如 metadatas = [[{...}, {...}]]。消费方若不展平，遍历会拿到内层 list，
    对其调 .get() 会抛 "'list' object has no attribute 'get'"（2026-08-16 台账七）。
    """
    if not raw:
        return []
    first = raw[0] if isinstance(raw, list) else raw
    if not isinstance(first, list):
        first = [first]
    return [x for x in first if x is not None]


async def retrieve_memory(query: str, user_id: str, session_id: str, top_k=5,
                          embedding_cfg: dict | None = None,
                          extract_enabled: bool = True,
                          extract_cfg: dict | None = None,
                          exclude_ids: set[str] | None = None) -> dict:
    """按 user_id 隔离检索三层记忆（02 §7）。

    - embedding_cfg=None：跳过 Chroma 语义检索（未配置向量化模型，或服务故障降级）。
    - extract_enabled=False：跳过 KG 子图检索（未配置记忆抽取模型）。
    - extract_cfg：设置页分配的记忆抽取模型配置（配置后实体提取用该模型）。
    - exclude_ids：排除的消息 id（2026-08-24 起，当前消息先行落库后排除自身，
      避免 recent 与 Chroma 检索重复注入当前消息到 LLM 上下文）。
    """
    # 1. SQL 最近 K 轮（限定当前用户 + 当前会话，排除当前消息）
    recent = await asyncio.to_thread(
        sql.get_recent_messages, user_id=user_id, session_id=session_id, k=5,
        exclude_ids=exclude_ids,
    )

    # 2. Chroma 语义检索（collection 按 user_id 隔离）
    if embedding_cfg:
        try:
            vector_results = await asyncio.to_thread(
                chroma.query, user_id=user_id, query_texts=[query], n_results=top_k,
                embedding_cfg=embedding_cfg,
            )
        except Exception:
            logger.warning("chroma query failed, degraded to no semantic hits", exc_info=True)
            vector_results = {}
    else:
        vector_results = {}
    # Chroma query 返回嵌套结构（每个 query_text 一个内层 list），统一展平后再消费
    vector_results = {
        "documents": _flatten_chroma(vector_results.get("documents")),
        "metadatas": _flatten_chroma(vector_results.get("metadatas")),
        "distances": _flatten_chroma(vector_results.get("distances")),
    }
    for hit in vector_results["metadatas"]:
        hit["is_archived"] = hit.get("is_archived", False)
    # 排除当前消息自身的向量命中（用户消息先行落库会刚 upsert 自己，
    # 语义检索可能命中自己，把「刚刚说的这句话」当作相关记忆再次注入）
    if exclude_ids and vector_results["metadatas"]:
        kept = [
            (doc, meta, dist)
            for doc, meta, dist in zip(
                vector_results["documents"], vector_results["metadatas"],
                vector_results["distances"],
            )
            if meta.get("original_id") not in exclude_ids
        ]
        vector_results["documents"] = [k[0] for k in kept]
        vector_results["metadatas"] = [k[1] for k in kept]
        vector_results["distances"] = [k[2] for k in kept]

    # 3. 知识图谱子图（节点/边带 user_id）
    if extract_enabled:
        try:
            entities = await llm.extract_entities_from_text(query, model_config=extract_cfg)
            kg_results = await asyncio.to_thread(
                kg.query_subgraph, user_id=user_id, entity_names=entities, depth=1,
            )
        except Exception:
            # KG 异常降级（Neo4j 抖动/连接失败）绝不阻断聊天主流程（02 §8 门控约定）
            logger.warning("kg query failed, degraded to no kg facts", exc_info=True)
            kg_results = []
    else:
        kg_results = []

    return {
        "recent_messages": recent,
        "semantic_hits": vector_results,
        "kg_facts": kg_results,
    }


def _format_kg_facts(kg_results) -> list[str]:
    """把 Neo4j paths 转成可读 facts。"""
    facts = []
    for rec in kg_results or []:
        try:
            path = rec["path"]
            if hasattr(path, "relationships"):
                for rel in path.relationships:
                    facts.append(f"{rel.start_node.get('name')} {rel.type} {rel.end_node.get('name')}")
            else:
                facts.append(str(path))
        except Exception:
            continue
    return facts[:10]


def _attachments_meta(attachments: list[dict]) -> str:
    """附件元数据 JSON：[{name,kind,size}]，图片不存二进制（12 §2）。

    额外透传 file_key/message_id（仅飞书媒体 meta 携带）：供 `_latest_media`（feishu.py）
    按需拉取「再分析最近文件」。普通聊天页附件无这两个键，不受影响（2026-08-17 修）。
    """
    return json.dumps(
        [{
            "name": a.get("name", ""),
            "kind": a.get("kind", ""),
            "size": a.get("size", 0),
            "file_key": a.get("file_key", ""),
            "message_id": a.get("message_id", ""),
        } for a in attachments or []],
        ensure_ascii=False,
    )


def _compose_user_text(user_text: str, attachments: list[dict]) -> str:
    """用户消息的落库/展示文本：只含用户原文 + 附件名说明，不含文本类附件提取的正文。

    2026-08-15 修复：PDF/Word/Excel 等提取的全文只进 LLM 上下文（_compose_llm_text），
    不拼进持久化内容，避免用户对话框被整篇文档文字刷屏、记忆被文档内容污染。
    """
    parts = []
    if user_text and user_text.strip():
        parts.append(user_text.strip())
    for a in attachments or []:
        name = a.get("name", "附件")
        if a.get("kind") == "image":
            parts.append(f"[附件：{name}（图片）]")
        else:
            parts.append(f"[附件：{name}]")
    return "\n".join(parts)


def _compose_llm_text(user_text: str, attachments: list[dict]) -> str:
    """LLM 上下文用文本：用户原文 + 附件名说明 + 文本类附件（text/pdf/word/excel）提取的正文。

    图片附件只记「[附件：name（图片）]」说明（图像本体走多模态通道）。
    """
    parts = []
    if user_text and user_text.strip():
        parts.append(user_text.strip())
    for a in attachments or []:
        name = a.get("name", "附件")
        if a.get("kind") == "image":
            parts.append(f"[附件：{name}（图片）]")
        else:
            txt = (a.get("text") or "").strip()
            parts.append(f"[附件：{name}]\n{txt}" if txt else f"[附件：{name}]")
    return "\n".join(parts)


def _build_user_content(query: str, attachments: list[dict] | None):
    """构造 user 消息 content：有图片附件时返回多模态数组，否则纯文本。

    文本类附件（text/pdf/word/excel）把提取的文字拼进文本，供模型分析并进入记忆；
    展示/落库文本仍只含用户原文（_compose_user_text，见 12 §2 修复）。
    """
    images = [a for a in (attachments or []) if a.get("kind") == "image" and a.get("data_url")]
    user_text = _compose_llm_text(query, attachments or []) or "（发送了文件）"
    if not images:
        return user_text
    content = [{"type": "text", "text": user_text}]
    for img in images:
        content.append({"type": "image_url", "image_url": {"url": img["data_url"]}})
    return content


def _has_image(attachments: list[dict] | None) -> bool:
    return any(a.get("kind") == "image" for a in (attachments or []))


def _media_not_supported_reply(chat_cfg: dict | None) -> str:
    """模型不支持图片时的聊天式友好回复（12 §4，绝不上屏报错代码）。"""
    model_name = (chat_cfg or {}).get("model") or "当前模型"
    return (
        f"很抱歉，我当前使用的模型（{model_name}）不支持识别图片。您可以更换支持视觉的模型"
        "（如 gpt-4o、qwen-vl），或上传文字类文件（PDF/Word/Excel）供我分析。"
    )


def _build_messages(query: str, memory: dict, attachments: list[dict] | None = None) -> list[dict]:
    lines = []
    semantic = memory.get("semantic_hits") or {}
    for doc, meta in zip(
        semantic.get("documents") or [],
        semantic.get("metadatas") or [],
    ):
        src = meta.get("source", "")
        if src == "sql_archive_summary":
            lines.append(f"[归档摘要] {doc}")
        elif not meta.get("is_archived"):
            lines.append(f"[相关记忆] {doc}")

    kg_facts = _format_kg_facts(memory.get("kg_facts"))
    for f in kg_facts:
        lines.append(f"[图谱事实] {f}")

    memory_text = "\n".join(lines)
    recent = memory.get("recent_messages") or []

    messages = [{
        "role": "system",
        "content": (
            "你是 XIAONA，一个有长期记忆的个人聊天机器人。你能记住用户说过的话、认识的人、"
            "经历过的事。回答自然、亲切，与用户用中文交流。"
            "回复要简洁精准：简单问题两三句话内说清，不堆砌语气词、口头禅和表情符号；"
            "复杂问题可适当展开，但同样去掉冗余修饰，直接给出关键信息。"
            "下面是检索到的与该用户相关的记忆（可能为空）：\n"
            f"{memory_text or '（无相关记忆）'}"
        ),
    }]
    for m in recent[-6:]:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": _build_user_content(query, attachments)})
    return messages


def _model_configs(db: DbSession | None, user_id: str) -> dict:
    """一次解析设置页功能模型分配（05 §11 / 02 §5）：chat / extract / embedding / executor / reviewer。

    任一未分配或模型条目不存在时为 None（视为「未配置」）。返回 dict：
    {"chat": cfg|None, "extract": cfg|None, "embedding": cfg|None,
     "executor": cfg|None, "reviewer": cfg|None}。
    执行者/审核者未分配时由调用方回退（执行者→chat，审核者→执行者→chat）。
    """
    out = {"chat": None, "extract": None, "embedding": None,
           "executor": None, "reviewer": None}
    if db is None:
        return out
    from app.db.sql_models import FunctionAssignment, Model
    assignment = db.query(FunctionAssignment).filter_by(user_id=user_id).first()
    if not assignment:
        return out
    ids = {
        "chat": assignment.chat_model_id,
        "extract": assignment.extract_model_id,
        "embedding": assignment.embedding_model_id,
        "executor": assignment.executor_model_id,
        "reviewer": assignment.reviewer_model_id,
    }
    from app.core.llm_provider import _ensure_v1
    for key, mid in ids.items():
        if not mid:
            continue
        model = db.query(Model).filter_by(id=mid, user_id=user_id).first()
        if not model:
            continue
        out[key] = {
            "model": model.model or model.name,
            "base_url": _ensure_v1(model.base_url),
            "api_key": model.api_key,
            "temperature": model.temperature,
            "max_tokens": model.max_tokens,
        }
    return out


def _resolve_chat_model_config(db: DbSession | None, user_id: str) -> dict | None:
    """从设置页分配解析聊天模型配置；未配置返回 None（走默认模型）。"""
    return _model_configs(db, user_id)["chat"]


async def chat(text: str, user_id: str, session_id: str, source: str,
               attachments: list[dict] | None = None) -> tuple[str, str]:
    """统一聊天入口：检索三层记忆 → LLM 生成 → 三存储同步持久化。

    - user_id: 记忆检索与持久化的隔离维度（必传）
    - session_id: 当前会话
    - source: web / cli / feishu
    - attachments: 附件解析结果（12 §），图片走多模态、文本类拼入 user 消息
    - 对话模型未配置时抛 ChatModelNotConfigured（前端提示到设置页配置）。
    - 用户消息在 LLM 调用前先行落库，LLM 失败也不丢记录。
    - 模型不支持图片（VisionNotSupportedError）时以聊天式友好回复返回，不抛异常。
    返回 (reply, 实际使用的 session_id)——ensure_session 可能新建会话（软删/无效时自愈）。
    """
    session_id = sql.ensure_session(user_id, session_id, source)
    from app.db.database import SessionLocal
    with SessionLocal() as db:
        cfgs = _model_configs(db, user_id)
    if not cfgs["chat"]:
        raise ChatModelNotConfigured()
    msg_id = await _persist_user(user_id, session_id, text, source,
                                 embedding_cfg=cfgs["embedding"],
                                 attachments=attachments)
    exclude_ids = {msg_id} if msg_id else None

    memory = await retrieve_memory(_memory_query(text, attachments), user_id, session_id,
                                   embedding_cfg=cfgs["embedding"],
                                   extract_enabled=bool(cfgs["extract"]),
                                   extract_cfg=cfgs["extract"],
                                   exclude_ids=exclude_ids)

    # 再次被提及时刷新历史消息权重（SQL 命中的历史消息）
    await _mention_hits(user_id, session_id, memory)

    messages = _build_messages(text, memory, attachments=attachments)
    try:
        # 思考深度按来源分流：桌面助手语音（desktop）用 low 浅度思考快速回话，
        # 文字聊天/CLI/飞书（web/cli/feishu）用 high 深度思考（2026-08-16 深夜，见当天台账二十六）
        reply = await llm.chat(messages, cfgs["chat"], thinking="low" if source == "desktop" else "high")
    except VisionNotSupportedError:
        reply = _media_not_supported_reply(cfgs["chat"])
    await _persist_reply(user_id, session_id, _compose_user_text(text, attachments or []),
                         reply, source,
                         extract_cfg=cfgs["extract"],
                         embedding_cfg=cfgs["embedding"],
                         chat_cfg=cfgs["chat"])
    return reply, session_id


async def chat_stream(text: str, user_id: str, session_id: str, source: str,
                      attachments: list[dict] | None = None):
    """流式聊天：检索与持久化逻辑与 chat 一致，回复以增量片段产出。

    产出三元组 (delta, thinking, is_media)：is_media=True 时 delta 为模型不支持图片的
    聊天式友好回复（12 §4），由路由层转成 media_reply SSE 帧。
    """
    session_id = sql.ensure_session(user_id, session_id, source)
    from app.db.database import SessionLocal
    with SessionLocal() as db:
        cfgs = _model_configs(db, user_id)
    if not cfgs["chat"]:
        raise ChatModelNotConfigured()
    msg_id = await _persist_user(user_id, session_id, text, source,
                                 embedding_cfg=cfgs["embedding"],
                                 attachments=attachments)
    exclude_ids = {msg_id} if msg_id else None

    memory = await retrieve_memory(_memory_query(text, attachments), user_id, session_id,
                                   embedding_cfg=cfgs["embedding"],
                                   extract_enabled=bool(cfgs["extract"]),
                                   extract_cfg=cfgs["extract"],
                                   exclude_ids=exclude_ids)
    await _mention_hits(user_id, session_id, memory)

    messages = _build_messages(text, memory, attachments=attachments)

    reply_parts: list[str] = []
    thinking_parts: list[str] = []
    try:
        try:
            # 思考深度按来源分流：desktop 用 low 浅度思考快速回话，其他（web/cli/feishu）
            # 用 high 深度思考（2026-08-16 深夜，见当天台账二十六）
            thinking_effort = "low" if source == "desktop" else "high"
            async for delta, thinking in llm.chat_stream(messages, cfgs["chat"], thinking=thinking_effort):
                reply_parts.append(delta)
                thinking_parts.append(thinking)
                yield delta, thinking, False
        except VisionNotSupportedError:
            # 模型不支持图片：以聊天式回复呈现，照常落库（12 §4）
            reply = _media_not_supported_reply(cfgs["chat"])
            reply_parts = [reply]
            yield reply, "", True
    finally:
        # 流结束（正常或异常）后持久化回复；用户消息已在上方先行落库。
        # 空回复（LLM 中途异常未产出任何内容）不落库——避免落一条空白 assistant 消息，
        # 与非流式 chat 路径（异常时不落库）行为保持一致。
        final_reply = "".join(reply_parts)
        if final_reply.strip():
            await _persist_reply(user_id, session_id, _compose_user_text(text, attachments or []),
                                 final_reply, source,
                                 extract_cfg=cfgs["extract"],
                                 embedding_cfg=cfgs["embedding"],
                                 thinking="".join(thinking_parts),
                                 chat_cfg=cfgs["chat"])


def _memory_query(text: str, attachments: list[dict] | None) -> str:
    """记忆检索的 query：优先用户文字；仅上传文件时用首个文本附件的开头。"""
    if text and text.strip():
        return text.strip()[:2000]
    for a in (attachments or []):
        if a.get("text"):
            return a["text"][:2000]
    return text or ""


async def chat_media_reply(user_id: str, session_id: str, user_text: str, reply: str,
                           source: str, attachments: list[dict]) -> str:
    """媒体类（无法识别的文件类型）固定友好回复的轻量持久化。

    与正常聊天一致：用户消息先行落库、回复后落库、自动标题；不调用 LLM。
    返回实际 session_id（ensure_session 自愈）。
    """
    session_id = sql.ensure_session(user_id, session_id, source)
    from app.db.database import SessionLocal
    with SessionLocal() as db:
        cfgs = _model_configs(db, user_id)
    content = _compose_user_text(user_text, attachments or []) or "（发送了文件）"
    await asyncio.to_thread(
        sql.save_chat_message,
        user_id=user_id, session_id=session_id, role="user", content=content,
        source=source, embedding_cfg=cfgs["embedding"],
        attachments=_attachments_meta(attachments or []),
    )
    await asyncio.to_thread(
        sql.save_chat_message,
        user_id=user_id, session_id=session_id, role="assistant", content=reply,
        source=source, embedding_cfg=cfgs["embedding"],
    )
    if cfgs["chat"]:
        await _maybe_auto_title(user_id, session_id, content, cfgs["chat"])
    return session_id


async def _mention_hits(user_id: str, session_id: str, memory: dict) -> None:
    """对检索命中的历史消息刷新权重（02 §6 调用时机说明）。

    同步 DB 刷新下沉线程池（2026-08-24 统一），防权重刷新阻塞事件循环。
    """
    try:
        ids = []
        for meta in (memory.get("semantic_hits") or {}).get("metadatas") or []:
            if meta.get("source") in ("sql_archive", "sql_archive_summary"):
                continue
            if meta.get("original_id") and not meta.get("is_archived"):
                ids.append(meta["original_id"])
        if not ids:
            return
        await asyncio.to_thread(_mention_hits_sync, user_id, ids)
    except Exception:
        # 权重刷新异常不影响主流程（SQL 只读刷新，失败降级）
        logger.warning("mention hits refresh failed, degraded", exc_info=True)


def _mention_hits_sync(user_id: str, ids: list) -> None:
    """同步刷新命中消息权重（独立 DB 会话）。"""
    from app.db.database import SessionLocal
    try:
        with SessionLocal() as db:
            for m in db.query(Message).filter(Message.id.in_(ids), Message.user_id == user_id).all():
                mention_message(db, m)
            db.commit()
    except Exception:
        logger.warning("mention hits sync refresh failed", exc_info=True)


async def _persist_user(user_id: str, session_id: str, user_text: str, source: str,
                        embedding_cfg: dict | None = None,
                        attachments: list[dict] | None = None) -> str:
    """用户消息先行落库（LLM 调用前执行）：SQL 原文 + Chroma embedding。

    保证 LLM 生成失败/被中断时用户说的话也已进入三层数据，不丢记录。
    附件文本类拼入 content，元数据写入 Message.attachments（12 §）。
    返回已落库消息 id（2026-08-24 起，供检索/拼上下文时排除当前消息防重复注入）。
    """
    content = _compose_user_text(user_text, attachments or []) or "（发送了文件）"
    # 同步落库 + Chroma embedding（HTTP 向量化调用，须下沉到线程池，
    # 否则每次对话都阻塞事件循环、全站卡顿；to_thread 对线程池写 SQL 同样安全）
    msg_id = await asyncio.to_thread(
        sql.save_chat_message,
        user_id=user_id, session_id=session_id, role="user", content=content,
        source=source, embedding_cfg=embedding_cfg,
        attachments=_attachments_meta(attachments or []),
    )
    return msg_id or ""


async def _persist_reply(user_id: str, session_id: str, user_text: str, reply: str, source: str,
                         extract_cfg: dict | None = None,
                         embedding_cfg: dict | None = None,
                         thinking: str = "",
                         chat_cfg: dict | None = None) -> None:
    """回复持久化：SQL 原文 + Chroma embedding + KG 三元组。

    - extract_cfg 为空（未配置记忆抽取模型）→ 跳过三元组抽取与 KG 写入。
    - thinking 为模型思考过程，随 assistant 消息一起入库（05 §12）。
    - KG/Chroma 任一失败均不阻断 SQL 主流程（日志告警）。
    - chat_cfg 非空且会话标题仍为「新会话」时触发自动标题（07 §6）。
    """
    # SQL + Chroma（to_thread 下沉，防 embedding 阻塞事件循环）
    await asyncio.to_thread(
        sql.save_chat_message,
        user_id=user_id, session_id=session_id, role="assistant", content=reply,
        source=source, embedding_cfg=embedding_cfg, thinking=thinking,
    )

    # 自动会话标题：仅当标题仍是「新会话」时触发（与提炼/向量化模型无关）
    if chat_cfg:
        await _maybe_auto_title(user_id, session_id, user_text, chat_cfg)

    if not extract_cfg:
        return

    # KG：抽取用户消息的三元组写入图谱（Extractor 只写 KG，见 01 §3）
    try:
        triples = await llm.extract_triples(user_text, model_config=extract_cfg)
        # 自我节点：实体经「提到」边直连用户，保证整图与用户节点连通（03 §9）
        self_id = await asyncio.to_thread(kg.get_or_create_self_node, user_id)
        for sub, pred, obj in triples:
            # merge_entity：自称（我/我的/本人）或与昵称同名的实体归并到自我节点，
            # 避免图谱出现「HHC」与「我」两个关于用户自己的重复节点（03 §6）
            sub_id = await asyncio.to_thread(kg.merge_entity, sub, "Person", user_id)
            obj_kind = "Person" if pred == "KNOWS" else "Thing"
            obj_id = await asyncio.to_thread(kg.merge_entity, obj, obj_kind, user_id)
            await asyncio.to_thread(kg.link, sub_id, pred, obj_id, user_id=user_id)
            await asyncio.to_thread(kg.link, self_id, "提到", sub_id, user_id=user_id)
            await asyncio.to_thread(kg.link, self_id, "提到", obj_id, user_id=user_id)
    except Exception:
        # 图谱写入失败不阻断聊天（SQL/Chroma 已落盘，KG 可后续经遗忘蒸馏补齐）
        logger.warning("KG persist failed", exc_info=True)


async def _maybe_auto_title(user_id: str, session_id: str, user_text: str,
                            chat_cfg: dict) -> None:
    """用聊天模型把对话内容概括为会话标题（≤12 字）；失败/过短时回退截断前 15 字。

    幂等：仅当标题仍为「新会话」才更新，生成成功后不再重复触发（07 §6）。
    同步 DB 读写下沉线程池（2026-08-24 统一）。
    """
    def _title_needed() -> bool:
        from app.db.database import SessionLocal
        with SessionLocal() as db:
            session = db.query(SessionModel).filter_by(
                id=session_id, user_id=user_id, is_deleted=False
            ).first()
            return bool(session and session.title == "新会话")

    if not await asyncio.to_thread(_title_needed):
        return

    fallback = user_text.strip().replace("\n", " ")[:15]
    title = fallback
    try:
        prompt = (
            "请为下面的对话内容生成一个简短的中文标题，不超过 12 个字，"
            "不要加标点、引号或解释：\n" + user_text[:200]
        )
        raw = await llm.chat([{"role": "user", "content": prompt}], chat_cfg)
        cleaned = (raw or "").strip().strip('"').strip("“”").strip("「」").strip("'").strip("。")
        if cleaned and len(cleaned) <= 20:
            title = cleaned
    except Exception:
        logger.warning("auto title generation failed, use fallback", exc_info=True)

    def _apply_title() -> None:
        from app.db.database import SessionLocal
        with SessionLocal() as db:
            # 第二个查询同样过滤 is_deleted（软删除会话不应被"复活"更新标题，AGENTS.md 约定）
            session = db.query(SessionModel).filter_by(
                id=session_id, user_id=user_id, is_deleted=False
            ).first()
            if session and session.title == "新会话" and title:
                session.title = title
                db.commit()

    await asyncio.to_thread(_apply_title)


# chat_core 命名空间（10 §4 webhook 引用 `chat_core.chat(...)`，内部等价于 /api/chat 非流式）
from types import SimpleNamespace

chat_core = SimpleNamespace(chat=chat, chat_stream=chat_stream, chat_media_reply=chat_media_reply)
