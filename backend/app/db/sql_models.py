"""SQLAlchemy 统一模型（01 §5.1 / 02 §4 / 07 §4 / 10 §5）。

所有主键统一 String(36) uuid；所有业务数据带 user_id 隔离。
"""
from sqlalchemy import Column, String, DateTime, Float, Integer, ForeignKey, Text, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime

from app.db.database import Base


class User(Base):
    __tablename__ = "users"
    id = Column(String(36), primary_key=True)  # uuid
    phone = Column(String(128), unique=True, index=True)  # 手机号（2026-08-17 起不再创建 feishu_{open_id} 影子账号）
    password_hash = Column(String(256))
    nickname = Column(String(64), default="")  # 默认 f"用户{phone}"
    created_at = Column(DateTime, default=datetime.utcnow)
    # 最近活跃（飞书 webhook 每次消息刷新；2026-08-17 影子机制废弃后仅留作活跃统计）
    last_active_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # 原供影子账号绑定提醒限频（2026-08-17 影子机制废弃，列保留但不再写入）
    last_reminded_at = Column(DateTime, nullable=True)


class Session(Base):
    __tablename__ = "sessions"
    id = Column(String(36), primary_key=True)  # uuid
    user_id = Column(String(36), ForeignKey("users.id"), index=True)
    title = Column(String(128), default="新会话")
    source = Column(String(16), default="web")  # web / cli / feishu / desktop
    # 软删除标记（2026-08-15 起聊天删除免密软删，数据保留在三库供记忆检索；
    # 设置页「清空数据」仍为硬删除）
    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    messages = relationship(
        "Message", back_populates="session",
        # 排序加 id 次级键：同一秒内 user/assistant 消息多轮时按插入顺序稳定返回
        # 注意：SQLAlchemy 2.0 的 relationship order_by 不接受字符串列表
        #（coercions 把 "Message.created_at" 当 SQL 字面量 → mapper 配置即崩，见当天台账二十四），
        # 须用 lambda 延迟求值返回列元组（Message 定义在本文件靠后，lambda 避免 NameError）
        order_by=lambda: (Message.created_at, Message.id),
    )


class Message(Base):
    __tablename__ = "messages"
    id = Column(String(36), primary_key=True)  # uuid
    session_id = Column(String(36), ForeignKey("sessions.id"), index=True)
    user_id = Column(String(36), ForeignKey("users.id"), index=True)  # 便于跨会话过滤
    role = Column(String(16))  # user / assistant
    content = Column(Text)
    # 模型思考过程（DeepSeek reasoning_content / Ollama thinking），assistant 消息可能有（05 §12）
    thinking = Column(Text, default="")
    # 附件元数据（2026-08-15 起多类型文件上传，12 §）：JSON 字符串 [{name,kind,size}]，
    # 图片仅存元数据不存二进制（二进制只在当次请求发给模型，不落库）
    attachments = Column(Text, default="")
    source = Column(String(16), default="web")
    weight = Column(Float, default=0.5)  # 记忆权重
    importance = Column(Float, default=0.0)
    emotion_score = Column(Float, default=0.0)
    mention_count = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_mentioned_at = Column(DateTime, default=datetime.utcnow)
    is_archived = Column(Boolean, default=False)  # False=热记忆 True=已归档
    session = relationship("Session", back_populates="messages")


class FeishuUserMap(Base):
    """飞书 open_id ↔ user_id（10 §5）。

    2026-08-17 起：一个 XIAONA 账号（一个手机号）可绑定多个飞书 open_id，
    feishu_user_id 去掉唯一约束（同手机号注册的多个飞书账号共享同一账号三层记忆）。
    """
    __tablename__ = "feishu_user_map"
    id = Column(String(36), primary_key=True)  # uuid
    feishu_user_id = Column(String(128), index=True)  # 飞书 open_id（可重复，一账号多 open_id）
    user_id = Column(String(36), ForeignKey("users.id"))
    bound_at = Column(DateTime, default=datetime.utcnow)  # 绑定时间（设置页展示，2026-08-17 加）
    # 该 open_id 最近一次活跃时间（2026-08-24 加）：设置页「绑定身份列表」按 open_id
    # 各自显示最后活跃，此前一律用 User.last_active_at（多 open_id 共享一个值，无法区分）
    last_active_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class FeishuSessionMap(Base):
    """飞书会话映射：按 用户+chat_id+channel_type 绑定会话（10 §5）。"""
    __tablename__ = "feishu_session_map"
    id = Column(String(36), primary_key=True)  # uuid
    feishu_user_id = Column(String(128), index=True)  # 飞书 open_id
    chat_id = Column(String(128), index=True)         # 飞书 chat_id
    channel_type = Column(String(10))                 # p2p / group
    session_id = Column(String(36), ForeignKey("sessions.id"))
    created_at = Column(DateTime, default=datetime.utcnow)


class Model(Base):
    """模型库（05 §11）：用户为不同功能维护的模型条目。"""
    __tablename__ = "models"
    id = Column(String(36), primary_key=True)  # uuid
    user_id = Column(String(36), ForeignKey("users.id"), index=True)
    name = Column(String(64))
    provider = Column(String(16), default="openai")  # openai / ollama / claude
    base_url = Column(String(256), default="")
    api_key = Column(String(256), default="")
    model = Column(String(64), default="")
    temperature = Column(Float, default=0.7)
    max_tokens = Column(Integer, default=2048)
    capabilities = Column(String(128), default="chat")  # 逗号分隔：chat,extract,embedding
    created_at = Column(DateTime, default=datetime.utcnow)


class FunctionAssignment(Base):
    """功能模型分配：chat_model_id / extract_model_id / embedding_model_id（05 §11）。

    executor_model_id / reviewer_model_id：02 文档多 Agent 任务的执行者/审核者模型
    （2026-08-17 起，未分配时执行者回退 chat 模型、审核者回退执行者模型）。
    """
    __tablename__ = "function_assignments"
    id = Column(String(36), primary_key=True)  # uuid
    user_id = Column(String(36), ForeignKey("users.id"), unique=True, index=True)
    chat_model_id = Column(String(36), default="")
    extract_model_id = Column(String(36), default="")
    embedding_model_id = Column(String(36), default="")
    executor_model_id = Column(String(36), default="")
    reviewer_model_id = Column(String(36), default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DataSourceConfig(Base):
    """数据源 API key 配置（02 爬取 §7.1，2026-08-19 起）。

    每个用户按数据源存一个 key（如 Etsy Open API v3），存密文前由上层做简单
    混淆；Amazon BSR 内置无 key 不在此表。user_id + source 唯一。
    """
    __tablename__ = "data_source_configs"
    id = Column(String(36), primary_key=True)  # uuid
    user_id = Column(String(36), ForeignKey("users.id"), index=True)
    source = Column(String(64))  # 如 etsy
    api_key = Column(String(512), default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("user_id", "source", name="uq_datasource_user_source"),)


class AgentTask(Base):
    """多 Agent 任务（02 §5）：一次「用户提出功能需求 → 确认 → 执行 → 审核 → 留痕」的完整记录。

    - status：pending(待确认) / confirmed(已确认待执行) / executing / reviewing /
      revising(打回重做) / done / failed / cancelled
    - channel：web(聊天页) / desktop(桌面助手) / feishu(飞书)
    - artifacts：产物相对路径字符串数组 JSON（形如 ["user_id/task_id/xx.xlsx"]）
    """
    __tablename__ = "agent_tasks"
    id = Column(String(36), primary_key=True)  # uuid
    user_id = Column(String(36), ForeignKey("users.id"), index=True)
    session_id = Column(String(36), ForeignKey("sessions.id"), index=True)
    status = Column(String(16), default="pending", index=True)
    task_type = Column(String(32), default="")          # 意图分类：scrape / generate / analyze / summarize ...
    goal = Column(Text, default="")                     # 用户原始需求（未提炼）
    refined = Column(Text, default="")                  # 提炼后的结构化任务 JSON
    retry_count = Column(Integer, default=0)            # 审核打回次数
    artifacts = Column(Text, default="")                # 产物相对路径 JSON（list[str]）
    channel = Column(String(16), default="web")
    error = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AgentRunLog(Base):
    """Agent 执行日志（02 §6）：执行者/审核者每轮动作，用于前端展示任务进度与排查。"""
    __tablename__ = "agent_run_logs"
    id = Column(String(36), primary_key=True)  # uuid
    task_id = Column(String(36), ForeignKey("agent_tasks.id"), index=True)
    role = Column(String(16))  # executor / reviewer
    round = Column(Integer, default=1)          # 第几轮（初执行=1，审核打回重做依次递增；workflow 写 attempt+1）
    action = Column(String(64), default="")     # 动作名：call_tool / self_check / review / revise ...
    payload = Column(Text, default="")          # 动作详情 JSON
    created_at = Column(DateTime, default=datetime.utcnow)
