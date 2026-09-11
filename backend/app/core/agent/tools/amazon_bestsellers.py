"""Amazon 电商数据源工具（02 爬取 §5/§6）：抓取 Amazon 榜单/搜索人气数据。

零新依赖：httpx（已有）+ stdlib HTMLParser。两条路径：

1. 主路径：Amazon 搜索页按人气排序（`/s?k={关键词}&s=exact-aware-popularity-rank`）——
   天然按销量热度排名、支持关键词直接筛细分品类（银戒指/silver ring），返回
   排名/标题/价格/主图/商品链接/评分/评论数。
2. 兜底：显式传入 BSR 类目页 URL（`url` 参数）时解析该榜单页。

地区（2026-08-23 起）：`site` 参数选站点（us/uk/ca/au/de/fr/it/es），cookie 强制对应
货币/语言（如 uk 站 i18n-prefs=GBP + lc-main=en_GB），商品链接清洗域名随站点。
默认 us（北美）。支持 min/max_price、min/max_rating、min/max_reviews 过滤，以及
color/material（颜色/材质，中文/英文均可，并入搜索词缩小范围；无法解析的字段不参与
过滤，避免误杀；Etsy 无评分字段但 Amazon 有）。

合规：限速、单 UA、只取公开页面字段；大规模抓取请改用官方 SP-API（02 爬取 §8）。
"""
import asyncio
import logging
import re
from html.parser import HTMLParser

import httpx

logger = logging.getLogger("xiaona")

# 抓取超时 / UA 与 fetch_url 一致
_TIMEOUT = 30
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 支持的地区站点：site 参数（用户按地区选站点，2026-08-23 起）。
# 货币/语言 cookie 强制对应站点渲染，避免出口 IP 被判定为其它地区返回本地货币（见模块 docstring）。
_SITES = {
    "us": {"domain": "www.amazon.com",    "currency": "USD", "locale": "en_US"},
    "uk": {"domain": "www.amazon.co.uk",  "currency": "GBP", "locale": "en_GB"},
    "ca": {"domain": "www.amazon.ca",     "currency": "CAD", "locale": "en_CA"},
    "au": {"domain": "www.amazon.com.au", "currency": "AUD", "locale": "en_AU"},
    "de": {"domain": "www.amazon.de",     "currency": "EUR", "locale": "de_DE"},
    "fr": {"domain": "www.amazon.fr",     "currency": "EUR", "locale": "fr_FR"},
    "it": {"domain": "www.amazon.it",     "currency": "EUR", "locale": "it_IT"},
    "es": {"domain": "www.amazon.es",     "currency": "EUR", "locale": "es_ES"},
}

# 兼容外部传入的站点别名 → 规范化 site key
_SITE_ALIASES = {
    "us": "us", "usa": "us", "amazon.com": "us", "north america": "us",
    "uk": "uk", "britain": "uk", "england": "uk", "united kingdom": "uk",
    "amazon.co.uk": "uk", "gb": "uk",
    "ca": "ca", "canada": "ca", "amazon.ca": "ca",
    "au": "au", "australia": "au", "amazon.com.au": "au",
    "de": "de", "germany": "de", "amazon.de": "de",
    "fr": "fr", "france": "fr", "amazon.fr": "fr",
    "it": "it", "italy": "it", "amazon.it": "it",
    "es": "es", "spain": "es", "amazon.es": "es",
}


def _normalize_site(site: str) -> str:
    """站点参数归一化：接受 us/uk/ca 等短码，或 amazon.co.uk / britain 等别名。"""
    if not site:
        return "us"
    s = site.strip().lower()
    if s in _SITES:
        return s
    return _SITE_ALIASES.get(s, "us")


def _site_headers(site_cfg: dict) -> dict:
    """按站点生成请求头（语言 cookie 随站点）。

    2026-08-27 升级为完整浏览器指纹头组：此前只有 UA + Accept-Language，特征太像裸 bot，
    Movers 的 /gp/ 路径反爬比搜索页更严（Amazon Movers 稳定性增强，见当天台账二十九）。
    bestsellers/movers 共用经 _get 调用，一并受益。
    """
    return {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": f"{site_cfg['locale'].replace('_', '-')},en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }


def _site_cookies(site_cfg: dict) -> dict:
    """强制站点货币/语言渲染。"""
    return {"i18n-prefs": site_cfg["currency"], "lc-main": site_cfg["locale"]}


_BSR_HOME = "https://www.amazon.com/Best-Sellers/zgbs/"
_SEARCH_SORT = "exact-aware-popularity-rank"  # 搜索页按人气（近似销量）排序

# 搜索路径翻页参数（2026-08-27 起，见当天台账三十）：
# 单页约 16~20 个商品，排除词一筛候选池就空了（top10 只给 1 条）。
# 翻 5 页约 80~100 候选，先过滤后取足 top N；页间 _PAGE_DELAY 节流控制反爬。
_MAX_SEARCH_PAGES = 5
_PAGE_DELAY = 1.2

# 大类目词（2026-08-28 起，见当天台账一）：executor 提示词禁止把 Jewelry/Accessories
# 等大类目词当 category 传（并入搜索词会放大混入耳环/玩具等无关商品），此前只是
# 提示词约束、代码零拦截——这里工具端同步拦截。仅「整词命中」才剔除（不拆散
# jewelry box 等多词合法品类），只传大类目词时兜底保留一个避免直接报错。
_BROAD_CATEGORY_WORDS = {
    "jewelry", "accessories", "apparel", "electronics", "home", "clothing",
    "shoes", "beauty", "toys", "sports", "outdoors", "kitchen", "office",
    "books", "automotive",
}


def _strip_html(raw: str) -> str:
    """HTML → 纯文本（与 fetch_url 相同逻辑，独立实现避免跨模块耦合）。"""
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&amp;?", "&", text)
    text = re.sub(r"&lt;?", "<", text)
    text = re.sub(r"&gt;?", ">", text)
    text = re.sub(r"&quot;?", '"', text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _clean_product_url(href: str, domain: str = "www.amazon.com") -> str:
    """清洗 Amazon 商品链接：提取 /dp/ASIN 并转绝对 URL（去 slug 前缀与 ref/sr= 追踪参数）。

    前缀域名按站点传入（us→www.amazon.com、uk→www.amazon.co.uk，2026-08-23 起）。
    兼容多种形式：绝对 URL、/dp/ASIN、/slug/dp/ASIN/ref=xx?query=xx。
    """
    if not href:
        return ""
    m = re.search(r"(?:https?://(?:[^/]*\.)?amazon\.\w{2,3}(?:\.\w{2})?)?(/dp/[A-Z0-9]{10})", href)
    if m:
        return f"https://{domain}" + m.group(1)
    return href.split("?")[0]


class _SearchParser(HTMLParser):
    """解析 Amazon 搜索页商品卡（s-result-item）。

    每张卡收集：title（h2 下链接文本）、price（a-offscreen）、image（s-image）、
    product_url（s-no-outline 链接）、rating（a-icon-alt "4.6 out of 5 stars"）、
    reviews（s-underline-text "4.5K"）。
    仅当卡内出现 /dp/ 商品链接才收（过滤 Results/More results 等无链接卡）。
    """

    _JUNK_TITLES = ("results", "more results", "showing", "results for", "related searches")

    def __init__(self, domain: str = "www.amazon.com"):
        super().__init__(convert_charrefs=True)
        self.domain = domain  # 链接清洗前缀域名按站点（uk→www.amazon.co.uk，2026-08-23 修）
        self.items: list[dict] = []
        self._cur: dict | None = None
        self._buf: list[str] = []
        self._capture: str | None = None  # title/price/rating/reviews

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class", "")
        # 商品卡容器：开新卡（当前卡非空先收尾）
        if tag == "div" and "s-result-item" in cls:
            self._commit()
            self._cur = {}
            # data-asin 兜底（2026-08-24）：新版页面商品卡链接位置多变，/dp/ 链接可能解析不到，
            # 先记 ASIN，_commit 时 product_url 为空则拼 https://{domain}/dp/{asin}
            asin = (attrs.get("data-asin") or "").strip()
            if asin:
                self._cur["_asin"] = asin
            return
        if self._cur is None:
            return
        if tag == "h2":
            # 标题兜底（2026-08-24）：新版页面 h2 可能带 aria-label 而链接文本为空，
            # 此时直接以 aria-label 作为标题（后续文本捕获跳过，title 已存在不覆盖）
            aria = attrs.get("aria-label") or ""
            if aria:
                self._cur.setdefault("title", aria)
                self._capture = None
                return
            self._capture = "title"
            self._buf = []
            return
        if tag == "span" and "a-offscreen" in cls:
            self._capture = "price"
            self._buf = []
            return
        if tag == "span" and "a-icon-alt" in cls:
            self._capture = "rating"
            self._buf = []
            return
        if tag == "span" and "s-underline-text" in cls:
            self._capture = "reviews"
            self._buf = []
            return
        if tag == "img":
            src = attrs.get("data-src") or attrs.get("src") or ""
            if src and ("images-amazon.com" in src or "m.media-amazon.com" in src):
                self._cur.setdefault("image_url", src)
            return
        if tag == "a" and ("s-no-outline" in cls or "s-link" in attrs.get("data-cy", "")):
            href = attrs.get("href") or ""
            if "/dp/" in href:
                self._cur.setdefault("product_url", _clean_product_url(href, self.domain))
                # 标题兜底（2026-08-24）：新版页面标题可能不在 h2 内（h2 被换掉/改结构），
                # 此时以商品链接文本为标题捕获（</a> 时闭合；_commit 的 _JUNK_TITLES 会过滤误收）
                if self._capture is None:
                    self._capture = "title_fallback"
                    self._buf = []
            return

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if not self._capture:
            return
        end_tags = {
            "title": "h2", "price": "span", "rating": "span",
            "reviews": "span", "title_fallback": "a",
        }
        if end_tags.get(self._capture) != tag:
            return
        text = "".join(self._buf).strip()
        if text and self._cur is not None:
            if self._capture in ("title", "title_fallback"):
                self._cur.setdefault("title", text)
            elif self._capture == "price":
                self._cur.setdefault("price", text)
            elif self._capture == "rating":
                self._cur.setdefault("rating", text)
            elif self._capture == "reviews":
                self._cur.setdefault("reviews", text)
        self._capture = None

    def _commit(self):
        cur = self._cur
        self._cur = None
        self._capture = None
        if not cur:
            return
        # 必须有商品链接才是有效商品卡（过滤 Results/More results/广告横幅）
        if not cur.get("product_url"):
            asin = cur.get("_asin") or ""
            # data-asin 兜底拼 /dp/ 链接（2026-08-24：/dp/ href 解析不到时保证链接列不空）
            if asin:
                cur["product_url"] = f"https://{self.domain}/dp/{asin}"
            else:
                return
        title = (cur.get("title") or "").strip().lower()
        if not cur.get("title") or title in _SearchParser._JUNK_TITLES:
            return
        self.items.append(cur)

    def close(self):
        super().close()
        self._commit()


class _BSRParser(HTMLParser):
    """解析 BSR 类目页商品卡（显式 url 路径，02 爬取 §5 原始方案）。"""

    _RANK_CLASS_HINT = "zg-bdg-text"          # rank 徽标（#1）
    _PRICE_SEL = "p13n-sc-price"              # 价格 span 类名（多版本页面共用）
    _LINK_HINT = "a-link-normal"              # 商品链接

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._cur: dict | None = None
        self._buf: list[str] = []
        self._capture: str | None = None  # "title" / "price" / "rank"
        self._pending_img: str | None = None
        self._pending_link: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class", "")
        if tag in ("span", "div") and _BSRParser._RANK_CLASS_HINT in cls:
            self._capture = "rank"
            self._buf = []
            return
        if tag == "a" and _BSRParser._LINK_HINT in cls and "/dp/" in (attrs.get("href") or ""):
            self._capture = "title"
            self._buf = []
            self._pending_link = attrs["href"]
            return
        if tag in ("span", "div") and _BSRParser._PRICE_SEL in cls:
            self._capture = "price"
            self._buf = []
            return
        if tag == "img":
            src = attrs.get("data-src") or attrs.get("src") or ""
            if src and ("images-amazon.com" in src or "m.media-amazon.com" in src):
                self._pending_img = src
            return

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if not self._capture:
            return
        end_tags = {"rank": ("span", "div"), "title": ("a",), "price": ("span", "div")}
        if tag not in end_tags.get(self._capture, ()):
            return
        text = "".join(self._buf).strip()
        if not text:
            self._capture = None
            return
        if self._capture == "rank":
            self._start_item({"rank_text": text})
        elif self._capture == "title":
            if self._cur is None:
                self._start_item({})
            self._cur["title"] = text
        elif self._capture == "price":
            if self._cur is None:
                self._start_item({})
            self._cur["price"] = text
        self._capture = None

    def _start_item(self, seed: dict):
        if self._cur is not None:
            self._commit()
        self._cur = dict(seed)

    def _commit(self):
        item = self._cur
        if not item:
            return
        if item.get("title") or item.get("rank_text"):
            if self._pending_img:
                item.setdefault("image_url", self._pending_img)
            if self._pending_link:
                item["product_url"] = self._pending_link
            self.items.append(item)
        self._cur = None
        self._pending_img = None
        self._pending_link = None

    def close(self):
        super().close()
        self._commit()


def _is_bot_check(raw: str) -> bool:
    """检测 Amazon 反爬挑战页（bm-verify 浏览器验证 / Robot Check）。"""
    return "bm-verify" in (raw or "") or "Robot Check" in (raw or "") or (
        "enter the characters you see below" in (raw or "").lower()
    )


async def _get(url: str, site_cfg: dict) -> str:
    """GET 并返回 HTML（404/反爬返回非 200 → 抛异常由调用方统一转中文错误）。

    附加站点货币/语言 cookie 与 Accept-Language，强制按站点渲染（如 uk 显示英镑）。
    非 200 时 sleep 2s 重试 1 次（Movers 页面比搜索页更易触发反爬限流，2026-08-27 起）；
    仍非 200 才抛 RuntimeError。
    """
    for attempt in range(2):
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers=_site_headers(site_cfg),
                cookies=_site_cookies(site_cfg),
            )
        if resp.status_code == 200:
            return resp.text
        if attempt == 0:
            logger.info("amazon get non-200 (%s), retry once", resp.status_code)
            await asyncio.sleep(2)
    raise RuntimeError(f"HTTP {resp.status_code}")


def _fetch_error(e: Exception) -> dict:
    """把抓取异常转成结构化中文错误。"""
    if isinstance(e, httpx.TimeoutException):
        return {"ok": False, "error": f"Amazon 抓取超时（{_TIMEOUT}s），可能是网络/反爬，请稍后重试或换代理"}
    if isinstance(e, httpx.ConnectError):
        return {"ok": False, "error": "无法连接 amazon.com：网络不可达或被墙，请配置代理后重试"}
    logger.warning("amazon fetch failed: %s", e, exc_info=True)
    return {"ok": False, "error": f"Amazon 抓取失败：{e.__class__.__name__}（可能被反爬，稍后重试）"}


def _parse_rating(text: str) -> tuple[str, str]:
    """从「4.6 out of 5 stars」「1,234 ratings」等文案抽评分与评论数。"""
    m = re.search(r"([\d.]+)\s*out of 5", text or "")
    rating = m.group(1) if m else ""
    m2 = re.search(r"([\d,]+)\s*ratings?", text or "")
    reviews = m2.group(1) if m2 else ""
    return rating, reviews


def _parse_price(text: str) -> float | None:
    """从价格文案解析数值（美元/英镑/欧元/人民币符号、欧式逗号、区间取最低）。

    支持：$15.99 / £12.50 / €14,95 / 15.99 € / $15.99 - $25.99（取 15.99）。
    解析失败返回 None（不参与过滤，避免误杀）。
    """
    if not text:
        return None
    m = re.search(r"([\d]+(?:[.,][\d]+)?)", (text or "").replace(",", "."))
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def _reviews_to_number(text: str) -> int | None:
    """评论数文案转数值：4.5K → 4500、1,234 → 1234、（4.5K）→ 4500。"""
    if not text:
        return None
    s = re.sub(r"[^\d.KMkkm]+", "", text or "")
    if not s:
        return None
    try:
        low = s.lower()
        if low.endswith("k"):
            return int(float(low[:-1]) * 1000)
        if low.endswith("m"):
            return int(float(low[:-1]) * 1000000)
        return int(float(low.replace(",", "")))
    except (TypeError, ValueError):
        return None


def _rating_to_number(text: str) -> float | None:
    """评分文案转数值，防裸 float() 崩溃（2026-08-24 修）。

    归一化条目的 rating 可能是「4.6」（英文站解析成功）、也可能是未匹配正则的原样文案
    （非英语站点如德/法/西的评分格式不同，如 "4,6 von 5 Sternen"）甚至空串——
    直接 float() 会抛 ValueError 导致整条过滤逻辑中断。这里用正则提取首个数值
    （`,`→`.` 兼容欧式小数），提取失败返回 None（不参与评分过滤，避免误杀）。
    """
    if not text:
        return None
    m = re.search(r"([\d]+(?:[.,][\d]+)?)", (text or "").replace(",", "."))
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def _normalize_reviews(text: str) -> str:
    """评论数规范化：去掉括号与逗号，保留 K/M 缩写（(4.5K) → 4.5K）。"""
    return re.sub(r"[^\d.KMkkm]+", "", text or "")


def _normalize_items(items: list[dict], domain: str = "www.amazon.com") -> list[dict]:
    """把解析器原始条目归一化为统一输出契约，剔除无标题/无链接的垃圾卡。"""
    out = []
    for idx, it in enumerate(items, start=1):
        title = (it.get("title") or "").strip()
        if not title:
            continue
        rating, reviews = _parse_rating(it.get("rating") or "")
        out.append({
            "rank": idx,
            "title": title,
            "price": (it.get("price") or "").strip(),
            "image_url": (it.get("image_url") or "").strip(),
            "product_url": _clean_product_url(it.get("product_url") or "", domain),
            "rating": rating or it.get("rating", ""),
            "reviews": reviews or _normalize_reviews(it.get("reviews") or ""),
        })
    return out


def _cell(text) -> str:
    """Markdown 表格单元格清洗：| 换全角、换行/制表符换空格、空值填「-」。

    None → 「-」；rank/num_favorers 等数字单元格先 str() 再清洗（2026-08-24 修：
    此前 `(text or "").strip()` 对 int 直接调 .strip() 抛 AttributeError，
    导致 fetch_amazon_bestsellers 组 markdown_table 时整工具异常）。
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
    """把归一化后的商品列表生成为严格 7 列 Markdown 表格（2026-08-24 起）。

    由代码保证列对齐与完整 URL，替代执行者 LLM 手拼表格（LLM 少写一个 | 就会整行右移、
    长 URL 被省略，提示词约束两轮仍错位）。rank 须已重排（过滤后 1..N）。
    """
    if not items:
        return ""
    headers = ("排名", "图片", "商品名", "链接", "价格", "评分", "评论数")
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
            _cell(it.get("rating")),
            _cell(it.get("reviews")),
        ))
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def _parse_rank(text: str) -> int:
    m = re.search(r"#(\d+)", text or "")
    return int(m.group(1)) if m else 0


# 常见饰品类目的中英映射：Amazon 搜索只索引英文标题，中文关键词直接搜会 0 结果
#（2026-08-19 实测），executor 可能把用户的中文品类词（如 银戒指）直接传入，这里翻译后兜底。
# 颜色/材质（2026-08-24 起）：color/material 参数可传中文，与品类词一并翻译后并入搜索词
#（如 白色玫瑰金戒指 → white rose gold ring）。
_ZH_TO_EN = {
    "银戒指": "silver ring", "金戒指": "gold ring", "铂金戒指": "platinum ring",
    "钻石戒指": "diamond ring", "锆石戒指": "cubic zirconia ring", "钛钢戒指": "titanium steel ring",
    "钛戒指": "titanium ring", "珍珠戒指": "pearl ring", "素圈戒指": "plain band ring",
    "戒指": "ring", "银": "silver", "金": "gold", "黄金": "gold", "铂金": "platinum",
    "钛": "titanium", "钛钢": "titanium steel", "钻石": "diamond", "锆石": "cubic zirconia",
    "珍珠": "pearl", "玉": "jade", "绿松石": "turquoise", "银饰": "silver jewelry",
    "项链": "necklace", "手链": "bracelet", "耳环": "earrings", "脚链": "anklet",
    "胸针": "brooch", "吊坠": "pendant", "手镯": "bangle", "发夹": "hair clip",
    # 排除类（2026-08-24 起，供 exclude_keywords 排除玩具/派对/儿童/装扮等不符合品类的条目）
    "玩具": "toy", "派对": "party", "儿童": "kids", "孩子": "children",
    "装扮": "costume", "假发": "wig", "礼品": "gift", "万圣节": "halloween",
    # 颜色
    "白色": "white", "黑色": "black", "黄色": "yellow", "红色": "red",
    "蓝色": "blue", "绿色": "green", "粉色": "pink", "紫色": "purple",
    "玫红": "rose", "橙色": "orange", "灰色": "gray", "棕色": "brown", "银色": "silver",
    # 材质
    "白金": "white gold", "玫瑰金": "rose gold", "925银": "925 silver", "纯银": "sterling silver",
    "不锈钢": "stainless steel", "陶瓷": "ceramic", "水晶": "crystal", "镀金": "gold plated",
    "14k金": "14k gold", "18k金": "18k gold",
}


def _translate_zh_keywords(query: str) -> str:
    """把搜索词里的中文品类词翻译成英文（命中映射表才翻，未命中保留原文）。

    返回 {translated, used}: 是否发生翻译。
    """
    if not query:
        return query, False
    low = query.lower()
    if not re.search(r"[\u4e00-\u9fff]", query):
        return query, False  # 无中文，无需翻译
    # 先长词后短词（银戒指 优先于 戒指/银）
    out = low
    used = False
    for zh in sorted(_ZH_TO_EN, key=len, reverse=True):
        if zh in out:
            out = out.replace(zh, _ZH_TO_EN[zh])
            used = True
    return out, used


async def fetch_amazon_bestsellers(params: dict, ctx: dict | None = None) -> dict:
    """抓取 Amazon 商品榜单（按站点地区，默认北美/美元）。

    params:
      - site: 可选，站点地区（us/uk/ca/au/de/fr/it/es 或 amazon.co.uk 等别名），默认 us
      - keywords: 可选，搜索关键词（如 "silver ring"）；与 category/keyword_filter 拼成搜索词
      - category: 可选，类目/品类词（如「银戒指」/「silver ring」；禁止传 Jewelry/Accessories
        等大类目词，工具会整词剔除），并入搜索词
      - keyword_filter: 可选，并入搜索词再筛标题（如 银 / silver / gold / titanium）
      - url: 可选，显式 BSR 类目页 URL（此时走榜单页解析路径，且尊重传入域名）
      - limit: 可选，返回条数上限，默认 20（≤50）
      - min_price / max_price: 可选，价格区间过滤（按站点货币数值比较，如 uk 英镑）
      - min_rating / max_rating: 可选，评分区间过滤（0~5，仅 Amazon 有评分字段）
      - min_reviews / max_reviews: 可选，评论数区间过滤（支持 1000 或 "1000"）
      - color: 可选，颜色（中文/英文均可，如 白色 / white），并入搜索词缩小范围
      - material: 可选，材质（中文/英文均可，如 玫瑰金 / rose gold），并入搜索词
      - exclude_keywords: 可选，排除词（逗号/空格分隔，中文/英文均可，如 玩具/toy,派对,耳环），
        标题含任一排除词的条目直接丢弃（先排除后取足条数）
    返回 {ok, source:"amazon", query, items:[{rank, title, price, image_url, product_url, rating, reviews}],
          markdown_table?, note?}（markdown_table：严格 7 列 Markdown 表格，2026-08-24 起由后端生成，
          workflow 自动附加到最终回复，执行者不再手拼表格）
    """
    p = params or {}
    url = (p.get("url") or "").strip()
    category = (p.get("category") or "").strip()
    keyword = (p.get("keyword_filter") or "").strip()
    keywords = (p.get("keywords") or "").strip()
    color = (p.get("color") or "").strip()
    material = (p.get("material") or "").strip()
    exclude_raw = (p.get("exclude_keywords") or "").strip()
    site_key = _normalize_site(p.get("site") or "")
    site_cfg = _SITES[site_key]
    domain = site_cfg["domain"]
    try:
        limit = max(1, min(50, int(p.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    # 排除词：中英文均可，中文走 _ZH_TO_EN 翻译，标题含任一排除词（小写子串）的条目丢弃
    exclude_words: list[str] = []
    if exclude_raw:
        for w in re.split(r"[,，、\s]+", exclude_raw):
            w = (w or "").strip()
            if not w:
                continue
            low = w.lower()
            if re.search(r"[\u4e00-\u9fff]", w):
                t, _ = _translate_zh_keywords(w)
                if t != low:
                    low = t
            if low:
                exclude_words.append(low)
    # 过滤参数（解析失败/为空视为不过滤）
    def _fnum(key: str):
        v = p.get(key)
        if v in (None, ""):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    min_price = _fnum("min_price")
    max_price = _fnum("max_price")
    min_rating = _fnum("min_rating")
    max_rating = _fnum("max_rating")

    def _fint(key: str):
        v = p.get(key)
        if v in (None, ""):
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    min_reviews = _fint("min_reviews")
    max_reviews = _fint("max_reviews")

    # 组装搜索词：category 中文保留、keyword 并入，颜色/材质一并并入搜索词缩小范围
    #（Amazon 搜索对 white/silver/rose gold 等词命中标题+变体，比抓全量再本地筛更有效）
    parts: list[str] = []
    broad_hits: list[str] = []
    for w in (keywords, category, keyword, color, material):
        w = (w or "").strip()
        if not w:
            continue
        low = w.lower()
        # 大类目词工具级拦截（2026-08-28 起）：仅整词命中才剔除，避免 jewelry box
        # 等多词合法品类被误拆；剔除后记入 note 提示执行者用细分词更准。
        if low in _BROAD_CATEGORY_WORDS:
            broad_hits.append(low)
            continue
        parts.append(w)
    if not parts and not url:
        if not broad_hits:
            return {"ok": False, "error": "请至少提供 keywords 或 category（如 silver ring / 银戒指）"}
        parts = [broad_hits[0]]  # 只剩大类目词时兜底保留一个，避免直接报错
    # 中文品类词翻译成英文（Amazon 只索引英文标题，中文直接搜 0 结果，2026-08-19 实测）
    query = " ".join(parts)
    translated, used_zh = _translate_zh_keywords(query)
    if used_zh:
        query = translated
    # 翻译后词级去重（2026-08-28 起，由「翻译前 part 级子串去重」改来）：此前跨语言重复
    # 判不出——keywords="diamond ring" + category="钻石戒指" 翻译后仍拼成
    # "diamond ring diamond ring"。用精确重复去重（保留顺序），不做子串去重——
    # 词级子串去重会把 "earrings" 里的 "ring" 删掉，搜索结果失真。
    seen_words: set[str] = set()
    words: list[str] = []
    for w in query.split():
        if w not in seen_words:
            seen_words.add(w)
            words.append(w)
    query = " ".join(words)

    try:
        if url:
            raw = await _get(url, site_cfg)
            if _is_bot_check(raw):
                return {"ok": False, "error": "Amazon 触发了浏览器验证（频控/反爬），请稍后重试或降低请求频率"}
            parser = _BSRParser()
            try:
                parser.feed(raw)
                parser.close()
            except Exception as e:
                logger.warning("amazon bsr parse failed: %s", e, exc_info=True)
                return {"ok": False, "error": "Amazon 榜单解析失败：页面结构可能已变化"}
            raw_items = []
            for it in parser.items:
                raw_items.append({
                    "title": it.get("title") or "",
                    "price": it.get("price") or "",
                    "image_url": it.get("image_url") or "",
                    "product_url": it.get("product_url") or "",
                    "rating": "",
                    "reviews": "",
                    "_rank": _parse_rank(it.get("rank_text") or ""),
                })
            # 先按 BSR 徽标排名排序，再归一化（rank 稍后统一重排）
            raw_items.sort(key=lambda x: x["_rank"] or 999)
            items = _normalize_items(raw_items, domain)
        else:
            # 主路径：搜索页按人气排序（域名按站点，uk→www.amazon.co.uk）
            # 正则提前算好：f-string 表达式内不允许反斜杠（Python 3.11 语法限制），
            # 否则 SyntaxError 导致整个 agent 工具模块无法导入（2026-08-19 修复）
            search_kw = re.sub(r"\s+", "+", query)
            # 翻页抓取（2026-08-27 起）：单页约 16~20 个商品，排除玩具/耳环/项链后
            # 候选池不足导致 top10 只给 1 条。翻 _MAX_SEARCH_PAGES 页（约 80~100 候选），
            # 页间节流控制反爬；单页失败/被拦用已抓到的页继续，候选池为空才报错。
            raw_pages = []
            page_err: Exception | None = None
            for page in range(1, _MAX_SEARCH_PAGES + 1):
                page_url = f"https://{domain}/s?k={search_kw}&s={_SEARCH_SORT}&page={page}"
                try:
                    raw = await _get(page_url, site_cfg)
                except Exception as e:
                    page_err = e
                    logger.warning("amazon search page %d failed: %s", page, e)
                    break
                if _is_bot_check(raw):
                    logger.warning("amazon search page %d bot-check, use pages so far", page)
                    page_err = RuntimeError("bot-check")
                    break
                raw_pages.append(raw)
                if page < _MAX_SEARCH_PAGES:
                    await asyncio.sleep(_PAGE_DELAY)
            if not raw_pages:
                if isinstance(page_err, httpx.TimeoutException):
                    return _fetch_error(page_err)
                if isinstance(page_err, httpx.ConnectError):
                    return _fetch_error(page_err)
                return {"ok": False,
                        "error": "Amazon 触发了浏览器验证（频控/反爬），请稍后重试或降低请求频率"}
            # 逐页解析 + 按 product_url 去重合并（ASIN 相同即同款，翻页可能重复）
            merged: list[dict] = []
            seen_urls: set[str] = set()
            for raw in raw_pages:
                parser = _SearchParser(domain)
                try:
                    parser.feed(raw)
                    parser.close()
                except Exception as e:
                    logger.warning("amazon search parse failed: %s", e, exc_info=True)
                    continue
                for it in _normalize_items(parser.items, domain):
                    u = (it.get("product_url") or "").strip()
                    if u and u in seen_urls:
                        continue
                    if u:
                        seen_urls.add(u)
                    merged.append(it)
            items = merged

        # 按用户要求过滤：排除词 → 价格/评分/评论（先排除后截断，保证取足条数，
        # 2026-08-24 起：此前先 [:limit] 截断再过滤，topN 混入玩具/耳环一过滤就凑不齐 N 个）
        filter_notes = []
        if exclude_raw:
            filter_notes.append(f"排除：{exclude_raw}")
        if min_price is not None:
            filter_notes.append(f"价格≥{min_price:g}")
        if max_price is not None:
            filter_notes.append(f"价格≤{max_price:g}")
        if min_rating is not None:
            filter_notes.append(f"评分≥{min_rating:g}")
        if max_rating is not None:
            filter_notes.append(f"评分≤{max_rating:g}")
        if min_reviews is not None:
            filter_notes.append(f"评论≥{min_reviews}")
        if max_reviews is not None:
            filter_notes.append(f"评论≤{max_reviews}")
        if color:
            filter_notes.append(f"颜色：{color}")
        if material:
            filter_notes.append(f"材质：{material}")
        if filter_notes or exclude_words:
            kept = []
            for it in items:
                title_low = (it.get("title") or "").lower()
                if exclude_words and any(w in title_low for w in exclude_words):
                    continue
                price_v = _parse_price(it.get("price") or "")
                rating_v = _rating_to_number(it.get("rating"))
                reviews_v = _reviews_to_number(it.get("reviews") or "")
                if min_price is not None and price_v is not None and price_v < min_price:
                    continue
                if max_price is not None and price_v is not None and price_v > max_price:
                    continue
                if min_rating is not None and rating_v is not None and rating_v < min_rating:
                    continue
                if max_rating is not None and rating_v is not None and rating_v > max_rating:
                    continue
                if min_reviews is not None and reviews_v is not None and reviews_v < min_reviews:
                    continue
                if max_reviews is not None and reviews_v is not None and reviews_v > max_reviews:
                    continue
                kept.append(it)
            items = kept
        # 过滤后统一截断 + rank 重排 1..N（先过滤后取足 top N，2026-08-24 起）
        items = items[:limit]
        for i, it in enumerate(items, start=1):
            it["rank"] = i

    except httpx.TimeoutException:
        return _fetch_error(httpx.TimeoutException("t"))
    except httpx.ConnectError:
        return _fetch_error(httpx.ConnectError("c"))
    except Exception as e:
        return _fetch_error(e)

    note = f"（{site_key.upper()} 站"
    note_parts = list(filter_notes)
    if broad_hits:
        note_parts.append(f"已忽略大类目词 {'/'.join(broad_hits)}（改用细分品类词更准）")
    if note_parts:
        note += f"，已按 {'、'.join(note_parts)} 过滤"
    note += "）"
    if not items:
        return {"ok": True, "source": "amazon", "query": query,
                "items": [], "note": f"{note} 未找到满足条件的商品（可能被反爬或关键词无结果）"}

    # 条数过少提示：多半是价格/评分门槛过严，如实告知执行者（2026-08-23 起）
    if len(items) < 3:
        note += " 符合全部条件的商品较少，可能受价格/评分门槛限制"

    logger.info("amazon ok: %s (%d items, site=%s)", query or url, len(items), site_key)
    return {"ok": True, "source": "amazon", "query": query or url, "items": items,
            "markdown_table": _build_markdown_table(items), "note": note}
