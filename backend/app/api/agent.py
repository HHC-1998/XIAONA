"""多 Agent 任务 API（02 §5/§6）：任务列表 / 详情 / 产物下载 / 取消。

- GET  /api/agent/tasks                 当前用户任务列表（倒序，含状态/类型/时间）
- GET  /api/agent/tasks/{id}            任务详情（含提炼需求 / 审核日志 / 产物）
- GET  /api/agent/tasks/{id}/artifacts/{name}  认证下载产物（产物目录按 user_id 隔离）
- POST /api/agent/tasks/{id}/cancel     取消 pending/confirmed 任务
"""
import json
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session as DbSession

from app.core.auth import get_current_user
from app.db.sql_models import AgentTask, AgentRunLog, User
from app.dependencies import get_db
from app.config import ARTIFACTS_DIR

router = APIRouter(prefix="/api/agent", tags=["agent"])


def _task_dict(t: AgentTask) -> dict:
    return {
        "id": t.id,
        "session_id": t.session_id,
        "status": t.status,
        "task_type": t.task_type,
        "goal": t.goal,
        "refined": t.refined,
        "retry_count": t.retry_count,
        "channel": t.channel,
        "error": t.error or "",
        "created_at": t.created_at.isoformat() if t.created_at else "",
        "updated_at": t.updated_at.isoformat() if t.updated_at else "",
    }


def _artifacts_list(t: AgentTask) -> list[dict]:
    """解析产物元数据 → [{name, path}]（路径存相对产物根目录，安全下载时拼完整路径）。"""
    try:
        paths = json.loads(t.artifacts or "[]")
    except Exception:
        return []
    out = []
    for p in paths:
        name = os.path.basename(p)
        if name:
            out.append({"name": name, "path": p})
    return out


def _log_dict(r: AgentRunLog) -> dict:
    return {
        "role": r.role,
        "action": r.action,
        "payload": r.payload,
        "created_at": r.created_at.isoformat() if r.created_at else "",
    }


@router.get("/tasks")
def list_tasks(db: DbSession = Depends(get_db), user: User = Depends(get_current_user)):
    items = (
        db.query(AgentTask)
        .filter(AgentTask.user_id == user.id)
        .order_by(AgentTask.created_at.desc())
        .limit(50)
        .all()
    )
    return {"tasks": [_task_dict(t) for t in items]}


@router.get("/tasks/{task_id}")
def get_task(task_id: str, db: DbSession = Depends(get_db),
             user: User = Depends(get_current_user)):
    t = db.query(AgentTask).filter_by(id=task_id, user_id=user.id).first()
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    logs = db.query(AgentRunLog).filter_by(task_id=task_id).order_by(AgentRunLog.created_at).all()
    return {
        **_task_dict(t),
        "artifacts": _artifacts_list(t),
        "logs": [_log_dict(r) for r in logs],
    }


@router.get("/tasks/{task_id}/artifacts/{name}")
def download_artifact(task_id: str, name: str, db: DbSession = Depends(get_db),
                      user: User = Depends(get_current_user)):
    t = db.query(AgentTask).filter_by(id=task_id, user_id=user.id).first()
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    artifacts = _artifacts_list(t)
    # 只允许下载任务自身登记的产物文件名（防路径穿越：name 必须是登记的 basename）
    hit = next((a for a in artifacts if a["name"] == name), None)
    if not hit:
        raise HTTPException(status_code=404, detail="产物不存在")
    full = os.path.abspath(os.path.join(ARTIFACTS_DIR, hit["path"]))
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="产物文件已不存在")
    return FileResponse(full, filename=name)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str, db: DbSession = Depends(get_db),
                user: User = Depends(get_current_user)):
    t = db.query(AgentTask).filter_by(id=task_id, user_id=user.id).first()
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    if t.status not in ("pending", "confirmed"):
        raise HTTPException(status_code=400, detail=f"当前状态（{t.status}）不可取消")
    t.status = "cancelled"
    db.commit()
    return {"ok": True, "status": "cancelled"}
