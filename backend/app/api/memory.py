"""记忆/图谱路由（03 §9）：关系网查询与节点删除。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import get_current_user, verify_password
from app.db.neo4j_client import neo4j_client
from app.db.sql_models import User

router = APIRouter(prefix="/api/memory", tags=["memory"])


class DeleteNodeRequest(BaseModel):
    password: str


@router.get("/graph")
def get_graph(user: User = Depends(get_current_user)):
    """按 user_id 拉取全图（带数量上限，防止大图拖垮前端）。

    2026-08-15：查询前兜底确保自我节点存在（兼容自我节点功能上线前注册的账号，
    且登录兜底未执行过的情况），保证新账号/旧账号打开图谱页至少有 1 个节点；
    并清理历史「我/我的」等与自我节点重复的节点（consolidate_self_refs）。
    """
    # 兜底确保自我节点存在 + 归并自称/机器人重复节点，失败不阻断查询（Neo4j 未就绪时前端展示空态）
    try:
        from app.core.logger import logger
        neo4j_client.ensure_self_node(user.id, user.nickname or f"用户{user.phone}")
        neo4j_client.consolidate_self_refs(user.id)
        neo4j_client.consolidate_bot_refs(user.id)
    except Exception:
        logger.warning(f"ensure self node failed for user {user.id}")
    return neo4j_client.get_full_graph(user_id=user.id, edge_limit=500)


@router.delete("/graph/node/{node_id}")
def delete_node(
    node_id: str,
    req: DeleteNodeRequest,
    user: User = Depends(get_current_user),
):
    # 密码确认（统一命名，见 07/09 约定）
    if not verify_password(user, req.password):
        raise HTTPException(status_code=403, detail="密码错误")
    neo4j_client.delete_or_archive(node_id, user_id=user.id)
    return {"ok": True}
