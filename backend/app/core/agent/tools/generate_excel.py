"""Excel 生成工具（02 §7）：用 openpyxl 把结构化数据写入 xlsx 产物。

产物落盘到 artifacts/{user_id}/{task_id}/{filename}，路径由 ctx 传入；
返回相对产物根目录的路径供 API 下载链接使用（绝对路径不泄露给前端）。
"""
import json
import logging
import os
import uuid
from datetime import datetime

logger = logging.getLogger("xiaona")


def _safe_filename(name: str) -> str:
    """清洗文件名：只保留安全字符并强制 .xlsx 后缀，防路径穿越。"""
    import re
    base = re.sub(r"[\\/:*?\"<>|\s]", "_", name or "").strip("._") or f"产物_{uuid.uuid4().hex[:6]}"
    if not base.endswith(".xlsx"):
        base += ".xlsx"
    return base


async def generate_excel(params: dict, ctx: dict | None = None) -> dict:
    """生成 xlsx。params: {filename?, sheet?, headers?, rows?} → {ok, path?, size?, sheet?, error?}。

    - rows 支持两种形态：
      - 二维数组 [[...], [...]]：直接按行写入（headers 可选作为首行）；
      - 对象数组 [{colA: v, colB: v}, ...]：自动取第一个对象键作为表头。
    - ctx: {"user_id", "task_id", "artifacts_dir"}。缺 ctx 时只验证数据、不落盘。
    """
    rows = (params or {}).get("rows") or []
    headers = (params or {}).get("headers") or []
    sheet_name = (params or {}).get("sheet") or "Sheet1"

    if not isinstance(rows, list) or not rows:
        return {"ok": False, "error": "rows 不能为空：请提供至少一行数据"}
    if not isinstance(headers, list):
        return {"ok": False, "error": "headers 必须是字符串数组"}

    # 对象数组 → 展开为 [[表头...], [值...]]
    if isinstance(rows[0], dict):
        headers = headers or list(rows[0].keys())
        data = [[row.get(h, "") for h in headers] for row in rows]
    else:
        data = [[_cell(v) for v in row] for row in rows]

    # 无 headers 且对象数组已展开为空时兜底
    if not headers:
        return {"ok": False, "error": "headers 不能为空：请提供列名"}
    for row in data:
        if len(row) != len(headers):
            return {"ok": False, "error": f"行数据列数（{len(row)}）与表头列数（{len(headers)}）不一致"}

    if not ctx:
        return {"ok": True, "path": "", "size": 0, "sheet": sheet_name, "rows": len(data)}

    from openpyxl import Workbook
    try:
        wb = Workbook()
        ws = wb.active
        ws.title = sheet_name[:31]  # Excel 工作表名 ≤31 字符
        ws.append([str(h) for h in headers])
        for row in data:
            ws.append(row)

        user_id = ctx.get("user_id", "")
        task_id = ctx.get("task_id", "")
        artifacts_dir = ctx.get("artifacts_dir", "")
        task_dir = os.path.join(artifacts_dir, user_id, task_id)
        os.makedirs(task_dir, exist_ok=True)
        filename = _safe_filename((params or {}).get("filename") or f"产物_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
        path = os.path.join(task_dir, filename)
        wb.save(path)
        size = os.path.getsize(path)
        rel = os.path.relpath(path, artifacts_dir).replace("\\", "/")
        logger.info("generate_excel ok: %s (%d rows, %d bytes)", rel, len(data), size)
        return {"ok": True, "path": rel, "size": size, "sheet": sheet_name, "rows": len(data)}
    except Exception as e:
        logger.warning("generate_excel failed: %s", e, exc_info=True)
        return {"ok": False, "error": f"Excel 生成失败：{e.__class__.__name__}"}


def _cell(v):
    """单元格值归一化（None/对象序列化，防写入 TypeError）。"""
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return str(v)
