"""工具注册表（02 §5）：集中管理执行者可用工具。

- `TOOLS`：JSON Schema 描述数组，直接注入执行者模型的 tools 参数；
- `call_tool(name, params, ctx)`：白名单分发 + 结构化错误（工具名不存在的错误
  返回 {ok:false, error} 而非抛异常——工具结果要回填给模型继续决策）。
- ctx：{user_id, task_id, artifacts_dir, session_id}，传给需要落盘/读会话的工具
  （generate_excel / generate_pdf / generate_docx / fetch_session_messages）。
"""
import logging

logger = logging.getLogger("xiaona")


def _make_tools():
    from app.core.agent.tools.fetch_url import fetch_url
    from app.core.agent.tools.fetch_session_messages import fetch_session_messages
    from app.core.agent.tools.generate_excel import generate_excel
    from app.core.agent.tools.generate_pdf import generate_pdf
    from app.core.agent.tools.generate_docx import generate_docx
    from app.core.agent.tools.amazon_bestsellers import fetch_amazon_bestsellers
    from app.core.agent.tools.etsy_trending import fetch_etsy_trending
    from app.core.agent.tools.pinterest_trends import fetch_pinterest_trends

    specs = [
        {
            "name": "fetch_url",
            "description": "抓取指定网页的正文文本，用于网页调研/数据采集。返回页面标题与正文（默认截断 8000 字）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整 URL，须以 http(s):// 开头"},
                    "max_chars": {"type": "integer", "description": "可选，正文截断长度，默认 8000"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "fetch_amazon_bestsellers",
            "description": "抓取 Amazon 商品榜单（电商选品数据源，默认北美，支持按地区选站点）。按搜索关键词或类目定位（如 银戒指 / silver ring），支持按商品标题关键词筛选、按价格/评分/评论数区间过滤、按颜色/材质筛选，返回按人气排序的前 N 条。返回每条含排名/标题/价格/图片/商品链接/评分/评论数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site": {"type": "string", "description": "可选，站点地区：us(默认)/uk/ca/au/de/fr/it/es，或 amazon.co.uk 等别名。用户指定英国→uk、加拿大→ca、澳洲→au、德国→de、法国→fr、意大利→it、西班牙→es"},
                    "keywords": {"type": "string", "description": "可选，搜索关键词（如 silver ring），与 category/keyword_filter 组合成搜索词"},
                    "url": {"type": "string", "description": "可选，BSR 类目页 URL；不传则按关键词搜索"},
                    "category": {"type": "string", "description": "可选，类目/品类词（如 银戒指 / silver ring；禁止传 Jewelry/Accessories 等大类目词，工具会整词剔除），并入搜索词"},
                    "keyword_filter": {"type": "string", "description": "可选，并入搜索词再筛标题，如 银 / silver / gold / titanium"},
                    "limit": {"type": "integer", "description": "可选，返回条数上限（默认 20，最大 50）"},
                    "min_price": {"type": "number", "description": "可选，最低价格（数值，单位随站点货币：uk 英镑/US 美元/DE 欧元），如 15 表示 ≥15"},
                    "max_price": {"type": "number", "description": "可选，最高价格（数值，单位随站点货币），如 20 表示 ≤20"},
                    "min_rating": {"type": "number", "description": "可选，最低评分（0~5，如 4.5 表示 4.5 星以上）"},
                    "max_rating": {"type": "number", "description": "可选，最高评分（0~5，如 4.6 表示 4.6 星以下）"},
                    "min_reviews": {"type": "integer", "description": "可选，最低评论数（如 30000 表示评论 ≥30000）"},
                    "max_reviews": {"type": "integer", "description": "可选，最高评论数（如 20000 表示评论 ≤20000）"},
                    "color": {"type": "string", "description": "可选，颜色（中文/英文均可，如 白色 / white），并入搜索词缩小范围"},
                    "material": {"type": "string", "description": "可选，材质（中文/英文均可，如 玫瑰金 / rose gold / 925银 / 925 silver），并入搜索词缩小范围"},
                    "exclude_keywords": {"type": "string", "description": "可选，排除词（逗号分隔，中文/英文均可，如 玩具/toy,派对,耳环,项链），标题含任一排除词的条目直接丢弃（先排除后取足条数）"},
                },
            },
        },
        {
            "name": "fetch_etsy_trending",
            "description": "抓取 Etsy 官方 API 热门 listing（饰品/手作品类数据源，全球平台无地区概念）。按搜索关键词（如 silver ring）取活跃商品，按收藏数排序返回排名前 N 条，含标题/价格/图片/商品链接/收藏数/浏览量。需用户在设置页配置 Etsy API key。无评分字段，只能按价格/收藏数过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {"type": "string", "description": "必填，Etsy 搜索关键词，如 silver ring / gold ring"},
                    "limit": {"type": "integer", "description": "可选，返回条数上限（默认 20，最大 50）"},
                    "min_price": {"type": "number", "description": "可选，最低价格（按 listing 本币数值比较，不做汇率换算）"},
                    "max_price": {"type": "number", "description": "可选，最高价格（按 listing 本币数值比较，不做汇率换算）"},
                    "min_favorers": {"type": "integer", "description": "可选，最低收藏数过滤（如 500 表示收藏 ≥500）"},
                },
                "required": ["keywords"],
            },
        },
        {
            "name": "fetch_pinterest_trends",
            "description": "抓取 Pinterest 官方趋势词（收藏种草/前瞻维度，时尚饰品趋势比实际购买早 3~4 个月）。按地区返回当天 top 趋势词及周/月/年环比增长（growing 类）。用于交叉验证品类在 Pinterest 的种草趋势，不返回具体商品。需用户在设置页配置 Pinterest API token。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "可选，品类英文关键词（过滤 top 趋势词，如 diamond ring / silver jewelry）"},
                    "region": {"type": "string", "description": "可选，地区（默认 US，ISO 3166-2 国家码，如 US/GB/CA）"},
                    "trend_type": {"type": "string", "description": "可选，趋势类型（默认 growing，支持 top/growing/monthly/yearly/seasonal）"},
                    "limit": {"type": "integer", "description": "可选，返回条数上限（默认 20，最大 50）"},
                },
            },
        },
        {
            "name": "generate_excel",
            "description": "生成 Excel(.xlsx) 文件，用于把采集/整理的数据输出为表格产物。rows 传二维数组（每行一个数组，与 headers 列对齐）或对象数组。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "可选，产物文件名（自动补 .xlsx）"},
                    "sheet": {"type": "string", "description": "可选，工作表名，默认 Sheet1"},
                    "headers": {"type": "array", "items": {"type": "string"}, "description": "表头（首行）"},
                    "rows": {
                        "type": "array",
                        "description": "数据行：二维数组 或 对象数组",
                        "items": {},
                    },
                },
                "required": ["rows"],
            },
        },
        {
            "name": "fetch_session_messages",
            "description": "读取当前会话的聊天记录（用户+助手消息），用于「把聊天整理成文档/总结对话」类任务。返回最近 N 条 {role, content}。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "可选，返回条数上限（默认 40，最大 60）"},
                },
            },
        },
        {
            "name": "generate_pdf",
            "description": "生成排版美观的 PDF 文档（reportlab 模板：封面+章节+表格+图片）。用于把聊天记录/采集数据整理成 PDF。sections 里表格单元格的 URL 必须传完整 https:// 地址（文档内可点击可复制）；images 传商品图 URL 列表自动嵌入（失败自动跳过）。会话里的榜单 markdown 表格会由系统自动注入 markdown_tables 渲染（图片列 100% 保留），你无需手工转写表格。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "可选，产物文件名（用 6~12 字简洁语义名，自动补 .pdf；禁止「文档_时间戳」占位名）"},
                    "title": {"type": "string", "description": "可选，文档大标题（生成封面页；也是文件名兜底来源）"},
                    "subtitle": {"type": "string", "description": "可选，副标题/摘要"},
                    "date": {"type": "string", "description": "可选，生成日期，默认今天"},
                    "source": {"type": "string", "description": "可选，数据来源说明（如 Amazon/Etsy 交叉验证）"},
                    "content": {
                        "type": "string",
                        "description": "可选，markdown 原文（2026-09-01 起）：用户提供的大段粘贴文本（报价/方案/报告等 >2000 字或含大量表格）直接传入，后端自动解析渲染（# 标题 / markdown 表格 / - 列表 / 段落）；有 content 时无需手工构造 sections，系统跳过 sections 校验",
                    },
                    "sections": {
                        "type": "array",
                        "description": "章节列表：[{heading, paragraphs[], bullets[], table:{headers, rows}}]；rows 单元格 URL 须完整 https://；短文本（无表格）用此结构，超长原文用 content；表格主体请交给系统注入的 markdown_tables，此处置空或只放文字章节",
                        "items": {"type": "object"},
                    },
                    "images": {
                        "type": "array",
                        "description": "可选，嵌入图片：[{url, caption?}]，URL 自动下载嵌入（仅文档末尾附图用，商品图走 markdown_tables）",
                        "items": {"type": "object"},
                    },
                    "markdown_tables": {
                        "type": "array",
                        "description": "系统自动注入的会话 markdown 表格原文，无需填写",
                        "items": {"type": "string"},
                    },
                },
                "required": [],
            },
        },
        {
            "name": "generate_docx",
            "description": "生成排版美观的 Word(.docx) 文档（python-docx 模板：封面+章节+表格+图片，可被用户二次编辑）。用于把聊天记录/采集数据整理成 Word 文档。sections 里表格单元格的 URL 必须传完整 https:// 地址（Word 内真实超链接可点、文本可复制）；images 传商品图 URL 列表自动嵌入（失败自动跳过）。会话里的榜单 markdown 表格会由系统自动注入 markdown_tables 渲染（图片列 100% 保留），你无需手工转写表格。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "可选，产物文件名（用 6~12 字简洁语义名，自动补 .docx；禁止「文档_时间戳」占位名）"},
                    "title": {"type": "string", "description": "可选，文档大标题（生成封面区；也是文件名兜底来源）"},
                    "subtitle": {"type": "string", "description": "可选，副标题/摘要"},
                    "date": {"type": "string", "description": "可选，生成日期，默认今天"},
                    "source": {"type": "string", "description": "可选，数据来源说明（如 Amazon/Etsy 交叉验证）"},
                    "content": {
                        "type": "string",
                        "description": "可选，markdown 原文（2026-09-01 起）：用户提供的大段粘贴文本（报价/方案/报告等 >2000 字或含大量表格）直接传入，后端自动解析渲染（# 标题 / markdown 表格 / - 列表 / 段落）；有 content 时无需手工构造 sections，系统跳过 sections 校验",
                    },
                    "sections": {
                        "type": "array",
                        "description": "章节列表：[{heading, paragraphs[], bullets[], table:{headers, rows}}]；rows 单元格 URL 须完整 https://；短文本（无表格）用此结构，超长原文用 content；表格主体请交给系统注入的 markdown_tables，此处置空或只放文字章节",
                        "items": {"type": "object"},
                    },
                    "images": {
                        "type": "array",
                        "description": "可选，嵌入图片：[{url, caption?}]，URL 自动下载嵌入（仅文档末尾附图用，商品图走 markdown_tables）",
                        "items": {"type": "object"},
                    },
                    "markdown_tables": {
                        "type": "array",
                        "description": "系统自动注入的会话 markdown 表格原文，无需填写",
                        "items": {"type": "string"},
                    },
                },
                "required": [],
            },
        },
    ]

    handlers = {
        "fetch_url": fetch_url,
        "fetch_session_messages": fetch_session_messages,
        "fetch_amazon_bestsellers": fetch_amazon_bestsellers,
        "fetch_etsy_trending": fetch_etsy_trending,
        "generate_excel": generate_excel,
        "generate_pdf": generate_pdf,
        "generate_docx": generate_docx,
        "fetch_pinterest_trends": fetch_pinterest_trends,
    }
    return specs, handlers


_SPECS, _HANDLERS = _make_tools()

# OpenAI 兼容端点（含 DeepSeek 官方 API）严格校验 tools 格式：每个工具必须是
# {"type": "function", "function": {...}} 标准包装，缺 `type` 直接 400
# "missing field `type`"（见 2026-08-19 台账）。Ollama / OpenAI 兼容端点均接受。
TOOLS = [{"type": "function", "function": spec} for spec in _SPECS]


def list_tools() -> list[dict]:
    """返回 JSON Schema 描述数组（直接传给 llm.chat_with_tools 的 tools 参数）。"""
    return TOOLS


async def call_tool(name: str, params: dict, ctx: dict | None = None) -> dict:
    """白名单分发工具调用。返回 {ok: bool, ...}，错误不抛异常（可回填模型继续决策）。"""
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": f"未知工具：{name}（可用：{', '.join(_HANDLERS)}）"}
    try:
        return await handler(params or {}, ctx or {})
    except Exception as e:
        logger.warning("call_tool %s failed", name, exc_info=True)
        return {"ok": False, "error": f"工具执行异常：{e.__class__.__name__}"}
