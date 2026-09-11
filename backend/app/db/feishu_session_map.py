"""飞书会话映射（10 §5.1）：feishu_user_id + chat_id + channel_type ↔ session_id。

2026-08-18 起：影子账号机制废弃，未绑定 open_id 不再自动建号。
- get_feishu_bind_user：只查映射，未绑定返回 None（由调用方走 0 token 引导 / 绑定状态机）；
- bind_feishu_user：绑定成功后写入 feishu_user_map（一个账号可绑多个 open_id）；
- ensure_feishu_session：绑定用户才建飞书会话映射。

同步 DB 操作：webhook 是 async 函数，但 SQLAlchemy 同步会话块事件循环极小，
用 with 确保 session 关闭，避免连接泄漏。
"""
import uuid
from datetime import datetime

from app.db.sql_models import FeishuSessionMap, FeishuUserMap, Session, User
from app.db.database import SessionLocal


def get_feishu_bind_user(feishu_user_id: str) -> User | None:
    """通过飞书 open_id 查映射，返回绑定的 XIAONA 用户；未绑定返回 None（不创建账号）。"""
    with SessionLocal() as db:
        mapping = db.query(FeishuUserMap).filter_by(feishu_user_id=feishu_user_id).first()
        if not mapping:
            return None
        user = db.query(User).filter_by(id=mapping.user_id).first()
        if user is None:
            return None
        db.refresh(user)  # commit 后属性过期，refresh 加载所有列防 detached 后访问报错
        return user


def bind_feishu_user(feishu_user_id: str, user_id: str) -> bool:
    """把 open_id 绑定到指定账号（幂等：已绑定则返回 False）。

    一个 XIAONA 账号可绑多个飞书 open_id（feishu_user_map 无唯一约束）。
    """
    with SessionLocal() as db:
        exists = (
            db.query(FeishuUserMap)
            .filter_by(feishu_user_id=feishu_user_id, user_id=user_id)
            .first()
        )
        if exists:
            return False
        db.add(FeishuUserMap(
            id=str(uuid.uuid4()),
            feishu_user_id=feishu_user_id,
            user_id=user_id,
        ))
        db.commit()
        return True


def ensure_feishu_session(user: User, feishu_user_id: str, chat_id: str, channel_type: str) -> str:
    """确保飞书会话映射存在并返回 session_id（调用方保证该 open_id 已绑定）。

    - 私聊（p2p）：按用户绑定一个稳定会话；
    - 群聊（group）：按 chat_id 绑定，同一群聊共享一个会话。
    每次调用刷新 user.last_active_at 与 feishu_user_map.last_active_at
    （open_id 级活跃统计；设置页绑定身份列表按 open_id 显示，2026-08-24 起）。
    """
    with SessionLocal() as db:
        u = db.query(User).filter_by(id=user.id).first()
        if u is None:
            raise ValueError(f"user {user.id} not found")
        u.last_active_at = datetime.utcnow()
        # open_id 级活跃刷新：同一账号绑多个飞书身份时各自可区分
        feishu_map = (
            db.query(FeishuUserMap)
            .filter_by(feishu_user_id=feishu_user_id, user_id=user.id)
            .first()
        )
        if feishu_map:
            feishu_map.last_active_at = datetime.utcnow()

        if channel_type == "p2p":
            scope = {"feishu_user_id": feishu_user_id, "channel_type": "p2p"}
        else:
            scope = {"chat_id": chat_id, "channel_type": "group"}

        mapping = db.query(FeishuSessionMap).filter_by(**scope).first()
        if mapping:
            db.commit()
            db.refresh(u)
            return mapping.session_id

        session = Session(id=str(uuid.uuid4()), user_id=u.id, title=f"飞书-{feishu_user_id}", source="feishu")
        db.add(session)
        db.flush()

        db.add(FeishuSessionMap(
            id=str(uuid.uuid4()),
            feishu_user_id=feishu_user_id,
            chat_id=chat_id,
            channel_type=channel_type,
            session_id=session.id,
        ))
        db.commit()
        db.refresh(session)
        return session.id
