"""Etsy 官方 Open API v3 榜单工具（02 爬取 §5/§6）：按关键词取活跃 listing。

- 调 `/v3/application/listings/active?keywords=...&sort_on=score`（官方免费接口，
  10000 请求/天，02 爬取 §7.1）；对每条 listing 调 `/images/{listing_id}` 取主图。
- 按 `num_favorers` 降序近似「最受欢迎」排序（无真实销量公开，02 爬取 §2.1）。
- API key 解析回退链：ctx.user_id 查数据库 per-user 配置（设置页「数据源 API」）
  → 全局环境变量 ETSY_API_KEY（.env 兜底，2026-08-19 起）。两处都未配置 →
  返回结构化错误（不阻断，执行者可改走 Amazon 源）。

合规：走官方 API、注册 key 填写；不爬 Etsy 网页（02 爬取 §8）。
"""
import asyncio
import logging
import re
import urllib.parse

import httpx

logger = logging.getLogger("xiaona")

_TIMEOUT = 30
_API_BASE = "https://openapi.etsy.com/v3/application"
_MAX_FETCH = 100          # 单次批量拉取上限（官方 100）
_IMAGE_BATCH = 5          # 主图请求并发度（防限频）


async def _resolve_api_key(ctx: dict | None) -> str:
    """解析 Etsy API key：ctx.user_id 查 per-user 配置 → 回退环境变量。

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
                    .filter_by(user_id=user_id, source="etsy")
                    .first()
                )
                if row and row.api_key:
                    return row.api_key
        except Exception as e:
            logger.warning("etsy datasource key lookup failed (user=%s): %s", user_id, e)
    from app.config import ETSY_API_KEY

    return ETSY_API_KEY or ""


async def _get(url: str, key: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers={"x-api-key": key})
    if resp.status_code != 200:
        raise RuntimeError(f"Etsy API HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def _fetch_images(listing_ids: list[str], key: str) -> dict[str, str]:
    """批量取每条 listing 的主图 URL（按小并发切块，避免 429）。"""
    out: dict[str, str] = {}
    for i in range(0, len(listing_ids), _IMAGE_BATCH):
        batch = listing_ids[i:i + _IMAGE_BATCH]
        for lid in batch:
            try:
                data = await _get(f"{_API_BASE}/listings/{lid}/images", key)
                imgs = data.get("results") or []
                if imgs:
                    src = (imgs[0].get("url_75x75")
                           or imgs[0].get("url_170x135")
                           or imgs[0].get("url_fullxfull")
                           or "")
                    out[lid] = src
            except Exception as e:
                logger.warning("etsy image fetch failed listing=%s: %s", lid, e)
        await asyncio.sleep(0.2)  # 轻度限频
    return out


def _cell(text) -> str:
    """Markdown 表格单元格清洗：| 换全角、换行/制表符换空格、空值填「-」。

    None → 「-」；rank/num_favorers 等数字单元格先 str() 再清洗（2026-08-24 修：
    此前 `(text or "").strip()` 对 int 直接调 .strip() 抛 AttributeError，
    导致 fetch_etsy_trending 组 markdown_table 时整工具异常）。
    """
    if text is None:
        return "-"
    t = str(text).strip()
    if not t:
        return "-"
    t = t.replace("|", "｜").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    t = re.sub(r"\s+", " ", t)
    return t or "-"


def _build_markdown_table(items: list[dict]) -> str:
    """把 Etsy 商品列表生成为严格 8 列 Markdown 表格（2026-08-24 起）。

    Etsy 官方 API 无评分字段 → 评分列填「-」，收藏数列放 num_favorers。
    由代码保证列对齐与完整 URL，替代执行者 LLM 手拼表格。rank 须已重排（过滤后 1..N）。
    """
    if not items:
        return ""
    headers = ("排名", "图片", "商品名", "链接", "价格", "评分", "收藏", "来源")
    rows = []
    for it in items:
        url = (it.get("product_url") or "").strip()
        link = f"[{url}]({url})" if url else "-"
        rows.append((
            _cell(it.get("rank")),
            f"![{_cell(it.get('title'))}]({(it.get('image_url') or '').strip() or '#'})",
            _cell(it.get("title")),
            link,
            _cell(it.get("price")),
            "-",
            _cell(it.get("num_favorers")),
            _cell("Etsy"),
        ))
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


async def fetch_etsy_trending(params: dict, ctx: dict | None = None) -> dict:
    """抓取 Etsy 热门 listing。

    params:
      - keywords: 必填，搜索关键词（如 "silver ring"）
      - limit: 可选，返回条数上限，默认 20（≤50）
      - min_price / max_price: 可选，价格区间过滤（按 listing 本币数值比较，不做汇率换算）
      - min_favorers: 可选，最低收藏数过滤
    注意：Etsy 是全球平台（无地区概念），且官方 API 不含评分字段（只能按价格/收藏过滤）。
    返回 {ok, source:"etsy", query, items:[{rank, title, price, image_url, product_url, num_favorers, views, taxonomy}],
          markdown_table?, note?}（markdown_table：严格 8 列 Markdown 表格，2026-08-24 起由后端生成，
          workflow 自动附加到最终回复，执行者不再手拼表格）
    """
    p = params or {}
    keywords = (p.get("keywords") or "").strip()
    if not keywords:
        return {"ok": False, "error": "keywords 必填：请提供 Etsy 搜索关键词（如 silver ring）"}
    try:
        limit = max(1, min(50, int(p.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    try:
        min_price = float(p.get("min_price")) if p.get("min_price") not in (None, "") else None
    except (TypeError, ValueError):
        min_price = None
    try:
        max_price = float(p.get("max_price")) if p.get("max_price") not in (None, "") else None
    except (TypeError, ValueError):
        max_price = None
    try:
        min_favorers = int(float(p.get("min_favorers"))) if p.get("min_favorers") not in (None, "") else None
    except (TypeError, ValueError):
        min_favorers = None

    api_key = await _resolve_api_key(ctx)
    if not api_key:
        return {"ok": False,
                "error": "Etsy 数据源未配置 API key（设置页「数据源 API」填写或 .env 配 ETSY_API_KEY），"
                         "可改用 fetch_amazon_bestsellers"}
    try:
        data = await _get(
            f"{_API_BASE}/listings/active"
            f"?keywords={urllib.parse.quote_plus(keywords)}"
            f"&limit={_MAX_FETCH}&sort_on=score",
            api_key,
        )
    except httpx.TimeoutException:
        return {"ok": False, "error": f"Etsy API 请求超时（{_TIMEOUT}s）"}
    except httpx.ConnectError:
        return {"ok": False, "error": "无法连接 Etsy API：网络不可达或被墙，请配置代理后重试"}
    except Exception as e:
        logger.warning("etsy listings fetch failed: %s", e, exc_info=True)
        return {"ok": False, "error": f"Etsy API 请求失败：{e}"}

    listings = data.get("results") or []
    if not listings:
        return {"ok": True, "source": "etsy", "query": keywords,
                "items": [], "note": f"未找到关键词「{keywords}」的相关商品"}

    # 按 num_favorers 降序近似「最受欢迎」排序（无真实销量公开）
    listings.sort(key=lambda x: x.get("num_favorers") or 0, reverse=True)

    # 过滤条件摘要（note 提示用户；无法解析的字段不参与过滤）
    filter_notes = []
    if min_price is not None:
        filter_notes.append(f"价格≥{min_price:g}")
    if max_price is not None:
        filter_notes.append(f"价格≤{max_price:g}")
    if min_favorers is not None:
        filter_notes.append(f"收藏≥{min_favorers}")

    # 全量候选池扫描过滤（2026-08-28 起，见当天台账一）：不再 [:limit*2] 截断——
    # 池子只有 40 条时价格/收藏门槛一过滤就凑不齐 top N，与 Amazon「先凑 100 候选
    # 再取足」原则一致；过滤后才为最终保留的 ≤limit 条拉主图，避免为淘汰品白拉图。
    items = []
    for ls in listings:
        if len(items) >= limit:
            break
        lid = str(ls.get("listing_id") or "")
        price = ls.get("price") or {}
        if isinstance(price, dict):
            amount = (price.get("amount") or 0) / 100
            price_v = amount
            price_str = f"{amount:.2f} {price.get('currency_code', 'USD')}"
        else:
            price_v = None
            price_str = str(price)
        favorers = ls.get("num_favorers") or 0
        # 按用户要求过滤（价格按 listing 本币数值比较）
        if min_price is not None and price_v is not None and price_v < min_price:
            continue
        if max_price is not None and price_v is not None and price_v > max_price:
            continue
        if min_favorers is not None and favorers < min_favorers:
            continue
        items.append({
            "rank": 0,  # 过滤后统一重排（2026-08-24：此前 enumerate(top) 在 continue 后 rank 跳号）
            "title": (ls.get("title") or "").strip(),
            "price": price_str,
            "image_url": "",
            "product_url": ls.get("url") or f"https://www.etsy.com/listing/{lid}",
            "num_favorers": favorers,
            "views": ls.get("views") or 0,
            "taxonomy": (ls.get("taxonomy_path") or [])[-1] if ls.get("taxonomy_path") else "",
            "_lid": lid,  # 内部字段：过滤后批量拉图用，返回前剔除
        })
    if items:
        images = await _fetch_images([it["_lid"] for it in items], api_key)
        for it in items:
            it["image_url"] = images.get(it["_lid"], "")
            it.pop("_lid", None)
    for i, it in enumerate(items, start=1):
        it["rank"] = i

    if not items:
        return {"ok": True, "source": "etsy", "query": keywords, "items": [],
                "note": f"未找到满足条件的关键词「{keywords}」商品" + (f"（已按 {'、'.join(filter_notes)} 过滤）" if filter_notes else "")}

    note = f"已按 {'、'.join(filter_notes)} 过滤" if filter_notes else ""
    logger.info("etsy trending ok: %s (%d items)", keywords, len(items))
    return {"ok": True, "source": "etsy", "query": keywords, "items": items,
            "markdown_table": _build_markdown_table(items), "note": note}
