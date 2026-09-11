"""SQLAlchemy 引擎 / SessionLocal / Base（01 §8 目录树 db/database.py）。

被 app/db/feishu_session_map.py（SessionLocal）与 app/dependencies.py（get_db）引用。
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import DATABASE_URL

# SQLite 需 check_same_thread=False 以兼容 FastAPI 线程池；PostgreSQL 无需
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


def init_db():
    """建表 + 轻量迁移（main.py 启动时调用）。"""
    from app.db import sql_models  # noqa: F401  确保模型已注册
    Base.metadata.create_all(bind=engine)
    _apply_light_migrations()


def _apply_light_migrations():
    """create_all 不会给已存在的表补新列，这里手工补（兼容 SQLite/PostgreSQL）。

    2026-08-15：sessions 表新增 is_deleted（软删除标记）。
    2026-08-15：messages 表新增 thinking（模型思考过程，DeepSeek reasoning_content）。
    """
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "sessions" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("sessions")}
        if "is_deleted" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN is_deleted BOOLEAN DEFAULT FALSE"))
    if "messages" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("messages")}
        if "thinking" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE messages ADD COLUMN thinking TEXT DEFAULT ''"))
        # 2026-08-15：messages 表新增 attachments（多类型文件上传的附件元数据，12 §）
        if "attachments" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE messages ADD COLUMN attachments TEXT DEFAULT ''"))
    if "users" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("users")}
        # 2026-08-16：飞书影子账号生命周期管理（10 §5）——last_active_at 活跃刷新、last_reminded_at 提醒限频
        # 注意用 TIMESTAMP：PostgreSQL 不认 MySQL 的 DATETIME 类型（SQLite 两者皆可）
        if "last_active_at" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN last_active_at TIMESTAMP"))
        if "last_reminded_at" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN last_reminded_at TIMESTAMP"))
    if "models" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("models")}
        # 2026-08-16：模型能力自动探测（detect_capabilities 写回 capabilities）——
        # create_all 不会给已存在表补列，旧库缺列时 GET/PUT models 会 AttributeError 500
        if "capabilities" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE models ADD COLUMN capabilities VARCHAR(128) DEFAULT 'chat'"))
    if "function_assignments" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("function_assignments")}
        # 2026-08-17：多 Agent 任务（02 §5）执行者/审核者模型分配——缺列时设置页
        # 分配保存会 AttributeError 500，这里用 PostgreSQL 兼容类型补列
        if "executor_model_id" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE function_assignments ADD COLUMN executor_model_id VARCHAR(36) DEFAULT ''"))
        if "reviewer_model_id" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE function_assignments ADD COLUMN reviewer_model_id VARCHAR(36) DEFAULT ''"))
    # 2026-08-18：飞书账号绑定机制（10 §5.1）——一个账号可绑多个 open_id
    if "feishu_user_map" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("feishu_user_map")}
        # 去掉 feishu_user_id 唯一约束（PostgreSQL：DROP CONSTRAINT；SQLite 旧库无该约束时忽略）
        with engine.begin() as conn:
            try:
                conn.execute(text("ALTER TABLE feishu_user_map DROP CONSTRAINT IF EXISTS feishu_user_map_feishu_user_id_key"))
            except Exception:
                pass  # SQLite/无该约束时忽略
        # 补 bound_at 绑定时间列（设置页展示，TIMESTAMP 为 PG 兼容类型）
        if "bound_at" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE feishu_user_map ADD COLUMN bound_at TIMESTAMP"))
        # 2026-08-24：feishu_user_map 补 last_active_at（open_id 级最后活跃，
        # 设置页绑定身份列表按 open_id 显示，不再一律用 User.last_active_at）
        if "last_active_at" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE feishu_user_map ADD COLUMN last_active_at TIMESTAMP"))
