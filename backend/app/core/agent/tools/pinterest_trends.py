"""Pinterest Trends 数据源工具（02 爬取 §3/§7.1）：官方 API v5 趋势词（收藏种草/前瞻）。

维度：Pinterest 是时尚/生活方式趋势的「收藏种草/前瞻」源——用户在 Pinterest 收藏/搜索
往往比实际购买早 3~4 个月，官方趋势词可当「未来趋势款」的早期信号（2026-08-24 接入）。

接入方式（2026-08-24 调研结论）：
- trends.pinterest.com 的匿名内部接口在数据中心/云 IP 下会被拒（返回空结果），
  pinterest-cli / trendgetter 等第三方封装也受 IP 限制——不可用，改走**官方 API v5**；
- 官方 `GET /v5/trends/keywords/{region}/top/{trend_type}`，免费（Trial/Standard 均免费）
  但需注册开发者 app + OAuth Bearer token（scope `user_accounts:read`），只能查**今天**
  的 top 趋势词、不能查历史时序（官方限制，见 docstring 返回说明）。
- key 解析回退链（仿 etsy_trending）：ctx.user_id 查 `data_source_configs` source="pinterest"
  → 环境变量 PINTEREST_API_TOKEN（.env 兜底）。两处未配置返回结构化降级错误（不阻断）。

返回 {ok, source:"pinterest", region, trend_type, items:[{rank, keyword, pct_growth_wow,
      pct_growth_mom, pct_growth_yoy}], summary?, note?}。无 markdown_table（趋势词不是商品榜）。
"""
import logging

import httpx

logger = logging.getLogger("xiaona")

_TIMEOUT = 20
_API_BASE = "https://api.pinterest.com/v5"
_VALID_TREND_TYPES = ("top", "growing", "monthly", "yearly", "seasonal")
_MAX_LIMIT = 50


async def _resolve_api_token(ctx: dict | None) -> str:
    """解析 Pinterest API token：ctx.user_id 查 per-user 配置 → 回退环境变量。

    数据库查询走 SessionLocal 独立短会话（工具调用在异步事件循环中，不依赖
    FastAPI 请求作用域），查询失败只记日志不阻断（回退 env / 报未配置）。
    """
    user_id = (ctx or {}).get("user_id")
    if user_id:
        try:
            from app.db.database import SessionLocal
            from app.db.sql_models import DataSourceConfig

            with SessionLocal() as db:
                row = (
                    db.query(DataSourceConfig)
                    .filter_by(user_id=user_id, source="pinterest")
                    .first()
                )
                if row and row.api_key:
                    return row.api_key
        except Exception as e:
            logger.warning("pinterest datasource key lookup failed (user=%s): %s", user_id, e)
    from app.config import PINTEREST_API_TOKEN

    return PINTEREST_API_TOKEN or ""


def _build_summary(items: list[dict], region: str, trend_type: str) -> str:
    """一句话概括趋势词与增长（给执行者标 Pinterest 种草信号）。"""
    if not items:
        return f"Pinterest {region} 站暂无「{trend_type}」类趋势词"
    top = items[0]
    growth = top.get("pct_growth_wow")
    if growth:
        return (f"Pinterest {region} 站当前最热趋势词是「{top['keyword']}」，"
                f"周环比增长 {growth}%")
    return f"Pinterest {region} 站当前最热趋势词是「{top['keyword']}」"


async def fetch_pinterest_trends(params: dict, ctx: dict | None = None) -> dict:
    """抓取 Pinterest 官方趋势词（收藏种草/前瞻维度）。

    params:
      - keyword: 可选，品类英文关键词（作为 include_keywords 过滤 top 趋势词，如 diamond ring）
      - region: 可选，地区（默认 US，ISO 3166-2 国家码）
      - trend_type: 可选，趋势类型（默认 growing，支持 top/growing/monthly/yearly/seasonal）
      - limit: 可选，返回条数上限，默认 20（≤50）
    返回 {ok, source:"pinterest", region, trend_type, items:[{rank, keyword,
          pct_growth_wow, pct_growth_mom, pct_growth_yoy}], summary, note?}。
    注意：官方 API 只返回今天 top 趋势词、无历史时序；无具体商品链接。
    """
    p = params or {}
    keyword = (p.get("keyword") or "").strip()
    region = str(p.get("region") or "US").strip().upper() or "US"
    trend_type = str(p.get("trend_type") or "growing").strip()
    if trend_type not in _VALID_TREND_TYPES:
        trend_type = "growing"
    try:
        limit = max(1, min(_MAX_LIMIT, int(p.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20

    token = await _resolve_api_token(ctx)
    if not token:
        return {"ok": False,
                "error": "Pinterest 数据源未配置 API token（设置页「数据源 API」填写或 .env 配 "
                         "PINTEREST_API_TOKEN，注册地址 https://developers.pinterest.com/），"
                         "可跳过 Pinterest 维度用 Amazon/Etsy 继续"}

    url = f"{_API_BASE}/trends/keywords/{region}/top/{trend_type}"
    params_q = {"limit": limit}
    if keyword:
        params_q["include_keywords"] = keyword
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params=params_q,
                                    headers={"Authorization": f"Bearer {token}"})
    except httpx.TimeoutException:
        return {"ok": False, "error": f"Pinterest API 请求超时（{_TIMEOUT}s）"}
    except httpx.ConnectError:
        return {"ok": False, "error": "无法连接 Pinterest API：网络不可达或被墙，请配置代理后重试"}
    if resp.status_code != 200:
        logger.warning("pinterest trends HTTP %s: %s", resp.status_code, resp.text[:200])
        if resp.status_code in (401, 403):
            return {"ok": False, "error": "Pinterest API token 无效或无权限（401/403），请在设置页重新配置"}
        return {"ok": False, "error": f"Pinterest API 请求失败（HTTP {resp.status_code}）"}

    try:
        data = resp.json()
        trends = data.get("trends") or []
    except Exception as e:
        logger.warning("pinterest trends parse failed: %s", e, exc_info=True)
        return {"ok": False, "error": f"Pinterest API 数据解析失败：{e.__class__.__name__}"}

    items = []
    for idx, t in enumerate(trends[:limit], start=1):
        items.append({
            "rank": idx,
            "keyword": (t.get("keyword") or "").strip(),
            "pct_growth_wow": t.get("pct_growth_wow"),
            "pct_growth_mom": t.get("pct_growth_mom"),
            "pct_growth_yoy": t.get("pct_growth_yoy"),
        })
    if not items:
        return {"ok": True, "source": "pinterest", "region": region, "trend_type": trend_type,
                "items": [], "summary": f"Pinterest {region} 站暂无趋势词（或关键词「{keyword}」无命中）",
                "note": "Pinterest 无趋势词命中"}

    summary = _build_summary(items, region, trend_type)
    logger.info("pinterest trends ok: %s/%s (%d keywords, keyword=%s)", region, trend_type, len(items), keyword)
    return {"ok": True, "source": "pinterest", "region": region, "trend_type": trend_type,
            "items": items, "summary": summary,
            "note": f"关键词过滤：{keyword}" if keyword else ""}
