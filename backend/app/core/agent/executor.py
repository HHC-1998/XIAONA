"""执行者（Executor，02 §5/§6）：function calling 循环调用工具完成具体任务。

流程：
1. 注入工具描述（TOOLS）与系统提示（提炼后的任务）；
2. 模型选工具 → call_tool → 结果以 tool 角色回填 → 继续；
3. 直到模型不再调用工具、给出最终结论，或达到最大轮次。
每轮动作经 `on_log(role, action, payload)` 回调记录（AgentRunLog）。
"""
import asyncio
import json
import logging
import re

from app.config import AGENT_MAX_TOOL_ROUNDS
from app.core.agent.tools.registry import TOOLS, call_tool
from app.core.llm_provider import llm

logger = logging.getLogger("xiaona")


def _strip_tail_instruction(text: str) -> str:
    """清洗用户原文末尾的「生成文档」类指令句（2026-09-01 起）。

    用户粘贴正文后常跟「请生成 word 文档」「把上面的内容整理成 pdf」等指令，
    拼接进 content 前要去掉，否则文档正文尾部混入指令。
    """
    if not text:
        return ""
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines:
        tail = lines[-1].strip()
        if re.search(r"(生成|整理|输出|做成|弄成|导出).{0,10}(word|pdf|文档|文件|ppt|excel|表格)", tail, re.IGNORECASE):
            lines.pop()
        else:
            break
    return "\n".join(lines).strip()


# 原文直通判定（2026-09-01 起）：用户「粘贴大段原文 → 生成 Word/PDF」默认**原样直通**——
# 正文用会话原文直接渲染，LLM 不重写；只有用户明确要求加工（总结/提炼/精简等）才走 LLM 提炼。
# 用户把整篇文章原样贴进来时，通常正文本身就是用户想要的文档内容（报价/方案/报告）。
_DOC_PASSTHROUGH_WORDS = (
    "总结", "提炼", "精简", "概括", "缩写", "改写", "重写",
    "优化", "润色", "摘要", "要点", "大纲", "整理成表格", "归纳",
)


def _want_passthrough(task: dict) -> bool:
    """判定文档整理任务是否走「原文直通」（默认 True，除非用户要求加工）。

    用户指令含总结/提炼等加工词时返回 False（走 LLM 提炼）；否则默认原样直通。
    """
    desc = str(task.get("description") or "") + " " + json.dumps(task.get("params") or {}, ensure_ascii=False)
    return not any(w in desc for w in _DOC_PASSTHROUGH_WORDS)


# 原文 → 规范 markdown 化（2026-09-01 LLM markdown 化直通方案）：
# 无论用户输入是零换行"一坨文字"、Tab/空格对齐的纯文本表格，还是带指令句的粘贴，
# 一律由 LLM 分块扫描为规范 markdown（直通保字保句只加格式 / 加工按需总结），
# 再交给 generate_docx/generate_pdf 标准 markdown 渲染——彻底绕开后端规则猜格式。
_MARKDOWNIFY_SYSTEM_PROMPT = (
    "你是文档格式美化器。任务：把用户提供的内容整理成规范 markdown，供后续渲染成 Word/PDF 文档。\n"
    "- **直通模式（用户未要求总结/提炼）**：必须**保留全部文字内容，一个标点都不准增、删、改**；"
    "只做结构化：中文编号标题 `一、`/`第X章`/`（一）` → `#` 标题，`1.1`/`2.2` → `##` 标题；"
    "纯文本表格（Tab/全角空格/多个空格对齐，含中文对照表与报价表）转成 markdown 表格"
    "（第一行是表头原样保留，数据行按表头列数对齐，末列是数字金额就放最后一列，行首序号是 `1` `2` `3` 也放第一列）；"
    "序号列表（`1` `2` `3` 短行）转 `- ` 列表；句子/段落之间用空行分开；"
    "**删除文末的『生成/变成/整理成/输出成 word/pdf/文档/文件』指令句**。\n"
    "- **加工模式（用户明确要求总结/提炼/精简/改写/润色）**：按用户要求概括/提炼为 markdown，保留必要结构。\n"
    "- 只输出 markdown 正文，不要任何解释说明，不要 ``` 代码围栏。"
)
_CHUNK_MAX_CHARS = 2000  # 每块字数上限（LLM 输出 max_tokens 上限内，防超长截断）
_CHUNK_HEAD_RE = re.compile(r"^(?:[一二三四五六七八九十]+、|第[一二三四五六七八九十百零\d]+[章节部分]|（[一二三四五六七八九十]+）|\d+(?:\.\d+)*[\s、])")
_CHUNK_SENT_END = re.compile(r"[。；;！？!?\n]")


def _chunk_text(text: str, max_chars: int = _CHUNK_MAX_CHARS) -> list[str]:
    """把超长原文按编号标题优先、句子边界兜底切成 ≤max_chars 的块。

    优先在 `一、`/`（一）`/`1.1` 等标题处切（保证每块独立成章、标题不跨块）；
    无标题可切时在句号/分号/换行后切满 max_chars；实在找不到边界（零换行长串）硬切。
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    # 1) 按编号标题行切块
    lines = text.splitlines()
    chunks: list[str] = []
    cur: list[str] = []
    for ln in lines:
        stripped = ln.strip()
        if stripped and _CHUNK_HEAD_RE.match(stripped) and cur and sum(len(x) for x in cur) >= max_chars * 0.5:
            chunks.append("\n".join(cur).strip())
            cur = []
        cur.append(ln)
        if sum(len(x) for x in cur) >= max_chars and cur:
            chunks.append("\n".join(cur).strip())
            cur = []
    if cur:
        chunks.append("\n".join(cur).strip())
    # 2) 仍有超长块 → 按句子边界二次切分
    out: list[str] = []
    for c in chunks:
        if len(c) <= max_chars:
            out.append(c)
            continue
        part = ""
        for sent in _CHUNK_SENT_END.split(c):
            if not sent:
                continue
            if len(part) + len(sent) + 1 > max_chars and part:
                out.append(part.strip())
                part = ""
            part = part + sent + "。"
        if part.strip():
            out.append(part.strip())
    return [c for c in out if c]


async def _markdownify_content(text: str, cfg: dict, summarize: bool = False) -> str | None:
    """LLM 分块把用户原文转成规范 markdown；失败返回 None（调用方回落原文直注）。

    - text：会话用户原文（可能零换行）；
    - cfg：执行者模型配置（deepseek 走 thinking='low' 省思考 token，max_tokens 抬高防截断）；
    - summarize：True=加工模式（用户要求总结/提炼），False=直通模式（保字保句只加格式）。

    2026-09-01 起：分块**并行**转（asyncio.gather + Semaphore(4) + 单块 wait_for 180s），
    超长文档不再串行 4-7 次调用卡数分钟；任一块失败整体回落原文（保守，防半截 markdown）。
    """
    if not text:
        return None
    system = _MARKDOWNIFY_SYSTEM_PROMPT
    user_note = "（直通模式：保留全部文字，只加格式）" if not summarize else "（加工模式：按用户要求总结提炼）"
    chunks = _chunk_text(text)
    sem = asyncio.Semaphore(4)

    async def _mdify_one(i: int, chunk: str) -> str | None:
        async with sem:
            try:
                md = await asyncio.wait_for(
                    llm.chat(
                        [{"role": "system", "content": system},
                         {"role": "user", "content": f"第 {i + 1} 段：\n{chunk}\n\n{user_note}"}],
                        {**cfg, "max_tokens": 4096}, thinking="low"),
                    timeout=180)
            except Exception as e:
                logger.warning("markdownify chunk %d failed: %s", i, e)
                return None
            return md.strip() if md and md.strip() else None

    results = await asyncio.gather(*(_mdify_one(i, c) for i, c in enumerate(chunks)))
    if any(r is None for r in results):
        return None  # 任一块失败整体回落原文（保守，防半截 markdown）
    parts = [r for r in results if r]
    if not parts:
        return None
    return "\n\n".join(parts)


# 方案 C 标记指令（2026-09-01 起）：规则打底，表格吃不准时由 LLM 返回行级标记指令，
# 程序在原行插入 `|` 表格标记，**正文永不经过 LLM**（杜绝 LLM 编造/改写原文）。
_TABLE_MARK_SYSTEM = (
    "你是文档表格标注器。我会给你一段文档的若干行（每行带行号），其中一部分是纯文本表格"
    "（用 Tab/空格/全角空格对齐，第一行是表头，含 序号/项目/金额/规格/名称/说明/组件/数据对象/对比项 等关键词）。\n"
    "请输出表格涉及的全部行号（表头行 + 数据行），返回 JSON：{\"marks\":[{\"line\": 行号, \"anchor\": \"该行开头6个字\"}]}。\n"
    "要求：表头行必须是表格第一行；数据行紧跟在表头后且各行列数与表头一致；普通段落不要标；"
    "只输出 JSON，不要任何解释或代码围栏。"
)
_MARK_CAND_RE = re.compile(
    r"(?:序号|项目|金额|规格|名称|数量|单价|说明|内容|配置|维度|指标|结论|费用|小计|组件|数据对象|"
    r"对比项|访问地址|前置条件|交付物|访问方式|安全要求|典型场景|评估维度|包干金额|[\t　])"
)


async def _ask_table_marks(lines: list[str], cand: list[int], cfg: dict) -> set[int] | None:
    """把候选行（含行号）发给 LLM，解析返回的 {marks:[{line, anchor}]}。

    - 过滤非法行号；anchor 非空时须是该行子串（防行号漂移）；
    - 解析失败/超时返回 None（调用方回落原文直注）。
    """
    numbered = "\n".join(f"{i}\t{lines[i]}" for i in cand)
    try:
        resp = await llm.chat(
            [{"role": "system", "content": _TABLE_MARK_SYSTEM},
             {"role": "user", "content": numbered}],
            {**cfg, "max_tokens": 1024}, thinking="low")
    except Exception as e:
        logger.warning("ask_table_marks failed: %s", e)
        return None
    try:
        data = json.loads(resp.strip())
    except Exception:
        return None
    ok: set[int] = set()
    for m in (data.get("marks") or []):
        try:
            li = int(m.get("line", -1))
        except (TypeError, ValueError):
            continue
        if not (0 <= li < len(lines)):
            continue
        anchor = str(m.get("anchor") or "").strip()
        if anchor and anchor not in lines[li]:
            continue  # 锚定文本不匹配：行号可能漂移，丢弃
        ok.add(li)
    return ok or None


async def _markup_document(text: str, cfg: dict) -> str | None:
    """方案 C 直通渲染兜底：规则解析为主，表格候选行未被规则识别时由 LLM 出标记指令。

    - 返回加好 `|` 表格标记的 markdown 文本（正文仍是原文，一个标点不改）；
    - 规则已吃准 / LLM 失败 / 可疑行过多 → 返回 None（调用方回落原文直注，正文保真优先）。
    """
    try:
        from app.core.agent.tools.generate_docx import (
            _parse_markdown_blocks, _smart_break_lines, _split_cols)
    except Exception as e:
        logger.warning("markup_document import failed: %s", e)
        return None
    try:
        broken = _smart_break_lines(text)
        blocks = _parse_markdown_blocks(broken)
        lines = broken.splitlines()
        # 规则识别出的表格数据行数（不含表头）
        tbl_rows = sum(len(b[2]) for b in blocks if b[0] == "table")
        # 表格候选行：含表头关键词 / Tab / 全角空格 的行
        cand = [i for i, ln in enumerate(lines) if _MARK_CAND_RE.search(ln)]
        if not cand or len(cand) < 4:
            return broken  # 无明显表格：规则结果直接用（正文保真）
        if tbl_rows >= len(cand) * 0.8:
            return broken  # 表格基本被规则识别：直接用（正文保真）
        if len(cand) > 80:
            return None  # 可疑行过多：不调 LLM，回落原文直注
        marks = await _ask_table_marks(lines, cand, cfg)
        if not marks:
            return None
        out = []
        for i, ln in enumerate(lines):
            if i in marks:
                cols = _split_cols(ln) or _split_cols(ln, simple=True) or [ln]
                out.append("| " + " | ".join(cols) + " |")
            else:
                out.append(ln)
        marked_text = "\n".join(out)
        # 应用标记后重新解析，确认表格真的成形（LLM 漏标会断表，宁可用原文）
        new_blocks = _parse_markdown_blocks(marked_text)
        new_tbl_rows = sum(len(b[2]) for b in new_blocks if b[0] == "table")
        if new_tbl_rows >= len(cand) * 0.8:
            return marked_text
        return None
    except Exception as e:
        logger.warning("markup_document failed: %s", e)
        return None

_SYSTEM_PROMPT = (
    "你是一名任务执行者。根据给定任务目标，自主决定是否调用工具来完成，并按顺序执行：\n"
    "1. 需要外部信息（网页/数据）时调用 fetch_url 获取；\n"
    "2. 需要产出结构化表格文件时调用 generate_excel；\n"
    "3. 工具返回后基于结果继续，直到任务完成；\n"
    "最终必须用一句简洁的中文总结任务完成情况（做了什么、产出什么、关键结论）。\n"
    "如果工具无法完成任务或数据不足，如实说明，不要编造。\n"
    "\n"
    "【电商选品/榜单类任务】优先用专门的数据源工具（2026-08-19 起，2026-08-24 补 pinterest）：\n"
    "- fetch_amazon_bestsellers：Amazon 榜单（内置保底，无需 key）。"
    "keywords/category 一律传英文关键词，用户说的中文品类词要先翻成英文（如 玩具→toys、文具→stationery、"
    "银戒指→silver ring、积木→building blocks、水杯→water bottle），不要直接传中文；"
    "需要筛材质/类型时用 keyword_filter（如 银/silver、钛/titanium、钻石/diamond）；\n"
    "- fetch_etsy_trending：Etsy 官方 API（需用户配置 key）。keywords 同样传细分品类英文（如 silver ring）；\n"
    "- fetch_pinterest_trends：Pinterest 官方趋势词（需用户配置 token）——收藏种草/前瞻维度；\n"
    "- 关键词要贴合用户说的具体品类，不要用泛词（如用户说「银戒指」用 silver ring，不用 jewelry 或 ring）；"
    "**category 只传具体品类词（如 ring/watch），禁止传 Jewelry/Accessories 等大类目词（2026-08-27 起）**"
    "——大类目并入搜索词会放大混入耳环/玩具等无关商品，逼出第二次严格筛选；\n"
    "- 用户指定了排名范围（如 top10/前5%/前20）时，用 limit 控制返回条数并截取对应段位；\n"
    "- 若用户指定了产品类型（如银戒指），务必按类型筛选，不要把混类结果直接交出；\n"
    "- **排除不符合品类的商品用 exclude_keywords（2026-08-24 起）**：用户说「只要 X、不要 Y」（如"
    "「只要正经首饰戒指，不要玩具/派对装饰/项链/耳环」）→ 把不要的词传 `fetch_amazon_bestsellers` 的"
    "exclude_keywords 参数（如 玩具/toy,派对,耳环,项链，中英文均可，逗号分隔），工具会先排除再取足条数，"
    "**不要从工具返回的结果里手动砍**——手动砍会导致 top10 只剩 5 个；\n"
    "- 单个数据源失败返回 ok:false 时不要放弃整个任务：商品源（Amazon 榜 / Etsy 榜）各自独立，"
    "失败一个不影响其它，改走仍可用的源继续，并把失败原因如实告知用户；\n"
    "- **条数不足不要换词（2026-08-23 起，2026-08-24 补取足条数，2026-08-27 补翻页候选池）**：工具会**翻页抓取约 100 个候选商品**，再**先排除后取足条数**——"
    "用户要 top10 就应在排除/筛选后给足 10 条（搜索页翻 5 页约 80~100 张卡，排除玩具/耳环后从符合的池子里取 top N）；"
    "只有过滤后池子本身不足 N 条才如实说明「符合全部条件的商品仅 N 条」，并给出未过滤前的概况即可，"
    "**不要反复换关键词重试**；只有 ok:false（反爬/解析失败）或 items 为空时才换关键词/换数据源（最多再试 1 次，勿把轮次全花在同源上）。\n"
    "\n"
    "【地区与筛选（2026-08-23 起，2026-08-24 补区间/颜色/材质）】\n"
    "- 用户指定地区（英国/加拿大/澳洲/德国/法国/意大利/西班牙）时，fetch_amazon_bestsellers 传 site 参数"
    "（uk→amazon.co.uk、ca→amazon.ca、au→amazon.com.au、de→amazon.de、fr→amazon.fr、it→amazon.it、es→amazon.es），"
    "未指定地区默认 us（北美）。货币随站点（uk 英镑、de 欧元等）；\n"
    "- 用户指定价格区间（如「不低于 15 美元」「不超过 20」「15~20 美元」）→ 用 min_price/max_price 参数"
    "（数值，单位随站点货币），不要自己从结果里挑；\n"
    "- 用户指定评分区间（如「不低于 4.5」「不高于 4.6」「4.5~4.6 分」）→ 用 min_rating/max_rating 参数（0~5）；"
    "指定评论数区间（如「评论不低于 3 万」「评论不高于 2 万」「1000~20000」）→ 用 min_reviews/max_reviews 参数；\n"
    "- 用户指定颜色（白色/黑色/黄色/红色/蓝色/玫红等）→ 传 color 参数（中文/英文均可），"
    "指定材质（玫瑰金/白金/925银/不锈钢/钛等）→ 传 material 参数，工具会并入搜索词并翻译成英文；\n"
    "- **未指定的维度一律不传**（不传 = 不限制），不要自作主张补门槛；\n"
    "- Etsy 是全球平台（无地区概念）且官方 API 不含评分字段：只能按价格（min_price/max_price）与收藏数"
    "（min_favorers）过滤，用户要评分时用 Amazon 源；Etsy 的颜色/材质并入 keywords 英文（如 white / rose gold）。\n"
    "\n"
    "【数据源边界如实告知（2026-08-27 起，2026-08-28 移除 Movers/Trends 后更新）】\n"
    "- **当前可提供的数据源维度**：现状销量榜（fetch_amazon_bestsellers，翻 5 页约 100 候选）；"
    "Etsy 热门榜（需 key）；Pinterest 趋势词（需 token）；\n"
    "- **没有公开数据源的维度（用户问到必须如实说明，勿硬凑/勿伪造/勿用其它维度冒充）**：\n"
    "  - 商品级「下降最快/跌幅最大」榜单与「24h 起量/涨幅」榜——无公开数据源可抓取（此类数据依赖"
    "Amazon 内部页面的持续跟踪，已停用）；\n"
    "  - 品类搜索热度走势（上升/下降/平稳）——无公开数据源可抓取（曾接入 Google Trends，因网络限流"
    "已停用）；\n"
    "  - 单品总销量/累计销量绝对值——各平台均不公开，只有排名/评分/评论数等相对指标；\n"
    "  - 国内平台（小红书/抖音/淘宝/京东）数据——尚未接入；\n"
    "- 处理规则：用户请求的维度不在能力范围内 → 直接告知「当前没有公开数据源可抓取该维度」，"
    "如有最接近的可替代维度则说明并提供（如「商品级下跌/起量趋势没有，但我可以给你当前卖得好的 top N」）；"
    "已有维度照常交付，不因缺某个维度而放弃整个任务。\n"
    "\n"
    "【榜单交叉验证工作流（2026-08-27 重构，2026-08-28 移除 Movers/Trends）】\n"
    "任何榜单/选品任务，默认第一步只调 **fetch_amazon_bestsellers**（现状主榜，免 key，必调）：\n"
    "  - fetch_amazon_bestsellers —— 现状主榜（现在在卖什么，含价格/评分/评论/链接）。\n"
    "- **可选叠加层（需用户配置 key/token，未配置时工具返回降级错误，直接跳过、勿重试，主榜照常）**：\n"
    "  - fetch_etsy_trending（需 Etsy API key）—— 手作垂类圈热度，按收藏数排序的副榜；\n"
    "  - fetch_pinterest_trends（需 Pinterest token）—— 收藏种草/前瞻，趋势词 + 周环比增长。\n"
    "- 按数据源分别展示排名榜（Amazon 榜 / Etsy 榜各自一张表格），不要混在一个表里；\n"
    "- 对比多榜商品共同出现的高频品类/材质/风格特征，给每一条结论标「双源确认」（≥2 个商品源共同印证）"
    "或「单源信号」（仅一源出现）；\n"
    "- 末尾用 2~3 句总结「多平台共同趋势」（如：925 银/素圈简约款在两平台都是主流，双源确认）。\n"
    "- 最终总结按可用源补结论（**只补实际调用且成功的源**，未调用的维度不提）：\n"
    "  - 「Pinterest 种草：趋势词「XX」周环比增长 Y%」（来自 fetch_pinterest_trends，配置了才补）。\n"
    "\n"
    "【榜单回复格式（2026-08-24 改：后端生成表格）】最终总结**禁止输出 Markdown 表格**："
    "不写 | 分隔的行、不复制商品 URL 列表。榜单明细表格已由数据源工具后端生成"
    "（`fetch_amazon_bestsellers`/`fetch_etsy_trending` 返回的 `markdown_table` 字段，"
    "严格 7/8 列、列对齐、完整商品网址、缺失填「-」），系统会在最终消息里**自动附加展示**，不需要你拼。\n"
    "- 最终总结只写**文字结论**：共筛选出 N 条（先排除后取足、列来源等如实说明）、"
    "榜单的主要发现（如主流材质/价格段/头部品牌）、两平台共同趋势（若用了双源）；\n"
    "- 不要试图复述表格内容（如「第 1 名是 xxx」），用户看自动附的表格即可；\n"
    "- 若任务最终要生成 PDF/Word 文档，则按「文档整理类任务」把表格结构写入 sections，不受本条限制。\n"
    "\n"
    "【文档整理类任务（2026-08-24 起，改：表格由代码注入渲染）】用户要求「把上面的/刚才的/之前的对话/这个榜单/这个列表整理成 PDF/Word 文档」时：\n"
    "- **数据源优先会话已有内容（2026-08-24 起）**：用户说「把上面的…」「把刚才的…」「把这个榜单…」整理成文档，"
    "指的就是**当前会话里已有的内容**——必须先用 fetch_session_messages 读取当前会话聊天记录，"
    "把会话中已有的榜单表格/回复内容作为文档数据源；**禁止再调用 fetch_amazon_bestsellers / fetch_etsy_trending "
    "等数据源工具重新抓取**（用户没要求重新抓取）；只有 fetch_session_messages 确认会话里没有相关内容时，"
    "才允许用数据源工具补充；\n"
    "- 先调 fetch_session_messages 读取当前会话的聊天记录；\n"
    "- 按用户主题**只提取相关部分**（用户说「整理某某相关」就只提取相关段落，不要全量堆砌）；\n"
    "- **表格由系统自动附带（2026-08-24 改）**：会话里的榜单 markdown 表格已由 fetch_session_messages 提取、"
    "会在你调用 generate_pdf/generate_docx 时由系统**自动注入渲染**（图片列 100% 保留、列严格对齐）——"
    "**不要手工把表格转写成 sections JSON**（LLM 转写会丢图片列/列错位），sections 里只放文字部分"
    "（heading/paragraphs/bullets/总结分析）；\n"
    "- **仅当会话里没有现成榜单表格**（如用户首次抓取后直接要求整理成文档、fetch_session_messages 未返回表格）"
    "才自行构造 `sections[].table`，此时图片列填 `![商品名](完整图片URL)`；\n"
    "- **超长原文直接传 content（2026-09-01 起）**：用户提供的是大段粘贴文本（如整份报价/方案/报告，"
    "通常 >2000 字或含大量表格）时，**禁止手工把全文转成 sections JSON**（输出 token 超限会截断 JSON、"
    "sections 为空报错）——把 fetch_session_messages 读到的用户消息原文按序拼接放进 generate_pdf/generate_docx "
    "的 `content` 参数，由后端直接解析渲染（`#` 标题 / markdown 表格 / `- ` 列表 / 段落自动识别）；"
    "sections 只放少量补充分析或省略；短文本（无表格、数百字内）仍用 sections（heading/paragraphs/bullets/table）结构；\n"
    "- **原文直通（2026-09-01 起，默认）**：用户把整篇文章原样粘贴进来、只要求「变成 Word/PDF 文档」时，"
    "正文会由系统**自动转成规范 markdown 并注入**（generate_pdf/generate_docx 的 content 参数）：先由模型分块扫描"
    "用户原文——识别纯文本表格（Tab/空格对齐，含中文对照表与报价表）为 markdown 表格、中文编号标题（`一、`/`1.1`）"
    "为 `#`/`##`、删掉文末「生成 word」类指令句，**文字内容一个标点都不改**——**你无需也不得自行回传全文**"
    "（省 token、防改写）——只需决定格式（用户说 pdf → generate_pdf；word/文档/文件 → generate_docx）"
    "与 filename/title（6~12 字语义名）。不要修改、总结或重写正文；只有用户明确要求「总结/提炼/精简/改写」时才自行加工；\n"
    "- 把提炼结果结构化为 sections（heading/paragraphs/bullets/table）或 content（原文）后调 generate_pdf（用户说 pdf）"
    "或 generate_docx（用户说 word/文档/文件，未明确格式时默认 Word 便于用户二次编辑）；\n"
    "- **输出格式硬锁（2026-09-01 起）**：用户要 word/文档/文件（未明确格式默认 Word）→ **只用 generate_docx**；"
    "用户要 pdf → **只用 generate_pdf**。**禁止因某格式工具失败而改调另一种格式工具**——交付格式与用户要求不符"
    "是严重错误；工具失败时改参数重试（content 传原文 / sections 精简），仍失败则如实报告失败原因与建议，"
    "不要擅自换成其它格式交付。\n"
    "- **filename 用 6~12 字简洁语义名**（如 `北美钻石戒指精选榜`，工具自动加时间戳与扩展名），"
    "**禁止「文档_时间戳」这种占位名**；title 同理用能概括内容的标题；\n"
    "- **URL 必须完整（2026-08-24 起）**：sections 表格单元格与 images 里的 URL 一律传数据源返回的"
    "完整 https:// 地址，禁止截断、禁止省略协议头——生成的文档里链接要能点击跳转、至少能复制到浏览器打开；"
    "拿不到完整 URL 时写「无链接」，不要编造；\n"
    "- **忽略进度消息（2026-08-24 起）**：fetch_session_messages 已过滤任务**瞬态进度**消息"
    "（▶/🔧/⚠️/🧐/↻ 开头的助手消息，如「🔧 正在抓取…」）；**最终结果消息（✅/❌ 开头，"
    "含榜单表格与产物下载链接）会保留**，须作为正文内容整理，不要再自行挑选进度气泡。"
)


async def run(task: dict, executor_cfg: dict,
              on_log=None, ctx: dict | None = None) -> dict:
    """执行一轮任务（可在审核打回后重跑）。

    返回 {"success": bool, "summary": str, "artifacts": [相对路径], "log": [str]}。
    - task：refine_task 的产物 {task_type, description, params}
    - executor_cfg：执行者模型配置（llm.chat_with_tools 的 model_config）
    - ctx：{user_id, task_id, artifacts_dir}，传给工具落盘
    - on_log：可 await 回调 (role, action, payload)，每轮动作通知（workflow 用于写进度消息 + 落 AgentRunLog）
    """
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "任务类型：" + str(task.get("task_type") or "other") + "\n"
                "任务目标：" + str(task.get("description") or "") + "\n"
                "补充参数：" + json.dumps(task.get("params") or {}, ensure_ascii=False) + "\n"
                + (
                    "审核意见（上一轮验收未通过，必须逐条修订后再交付）："
                    + json.dumps(task.get("review_issues") or [], ensure_ascii=False) + "\n"
                    if task.get("review_issues")
                    else ""
                )
                + "开始执行，完成后给出简洁总结。"
            ),
        },
    ]

    artifacts: list[str] = []
    logs: list[str] = []
    tool_results: list[dict] = []  # 榜单表格收集（2026-08-24：workflow 据此自动附加最终表格）
    session_tables: list[str] = []  # 会话 markdown 表格原文（2026-08-24：fetch_session_messages 提取，文档工具直接渲染）
    session_assistant_tables: list[str] = []  # 仅助手消息的表格（2026-09-01：content 兜底注入后避免用户表格重复渲染）
    session_user_content: str = ""  # 会话用户原文拼接（2026-09-01：文档工具 content 兜底，防 LLM 输出截断）
    max_rounds = AGENT_MAX_TOOL_ROUNDS  # 防模型陷入无限工具调用（config 配置）

    for round_no in range(1, max_rounds + 1):
        logs.append(f"第 {round_no} 轮思考/决策")
        if on_log:
            await on_log("executor", "think", {"round": round_no})

        # 单轮模型调用：大上下文 + reasoning 用 300s 超时；超时/断连重试 1 次，
        # 重试仍失败不再抛异常——保留已抓取数据交给 workflow 兜底（2026-08-27 任务韧性修复，
        # 否则 TimeoutError 穿透到 workflow 重建的 result 里 tool_results 为空，榜单数据全丢）
        for attempt in range(2):
            try:
                msg = await llm.chat_with_tools(messages, executor_cfg, tools=TOOLS, timeout=300)
                break
            except (TimeoutError, ConnectionError) as e:
                if attempt == 1:
                    logs.append(f"模型调用超时/断连：{e}")
                    if on_log:
                        await on_log("executor", "think", {"round": round_no, "timeout": True})
                    return {"success": False,
                            "summary": "模型响应超时，但已抓取到部分数据（见下方榜单表格），可重试获取完整总结。",
                            "artifacts": artifacts, "tool_results": tool_results, "log": logs}
                logs.append(f"模型调用超时/断连（第 {attempt + 1} 次），重试一次")
                continue
        # 类型兜底：_complete 带 tools 已保证返回完整 message dict，但防止未来底层回归返回
        # content 字符串导致 .get("tool_calls") 崩溃（2026-08-23 台账），此处把字符串包成 dict
        if isinstance(msg, str):
            msg = {"role": "assistant", "content": msg}
        tool_calls = (msg or {}).get("tool_calls")
        if not tool_calls:
            summary = ((msg or {}).get("content") or "").strip()
            if not summary:
                summary = "任务执行完成，无工具调用。"
            logs.append("执行完成：" + summary[:200])
            return {"success": True, "summary": summary, "artifacts": artifacts,
                    "tool_results": tool_results, "log": logs}

        # 回填 assistant 消息（含 tool_calls）再逐个执行工具
        messages.append(msg)
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            logs.append(f"调用工具 {name}")
            if on_log:
                await on_log("executor", "call_tool", {"tool": name, "args": args})
            # 文档工具注入（2026-09-01 方案 C）：
            # - 直通（用户「粘贴大段原文 → 生成文档」，未要求总结/提炼）：**正文永不经过 LLM**——
            #   会话原文经 _markup_document（规则 _smart_break_lines 语义分行 + _parse_markdown_blocks
            #   解析，表格吃不准时由 LLM 只返回行级标记指令、程序在原行插 `|` 标记）后注入 content；
            #   纯规则识别不到表格才调一次 LLM，LLM 输出的正文一律丢弃（防编造/改写，见 2026-09-01 台账）；
            # - 加工（用户明确要求总结/提炼/润色）：才走 LLM 提炼（_markdownify_content 并行分块）。
            if name in ("generate_pdf", "generate_docx") and isinstance(args, dict):
                passthrough = _want_passthrough(task)
                # 模型可能没先调 fetch_session_messages，主动拉一次会话原文
                if not session_user_content:
                    try:
                        fr = await call_tool("fetch_session_messages", {"limit": 60}, ctx)
                        if isinstance(fr, dict) and fr.get("ok"):
                            parts = []
                            for m in fr.get("messages") or []:
                                if m.get("role") == "user":
                                    text = _strip_tail_instruction(m.get("content") or "")
                                    if len(text) > 300:  # 只收正文（>300 字），跳过寒暄/指令短句
                                        parts.append(text)
                            if parts:
                                session_user_content = "\n\n".join(parts)
                    except Exception as e:
                        logger.warning("doc inject fetch_session_messages failed: %s", e)
                if session_user_content:
                    if passthrough:
                        # 方案 C（2026-09-01 起）：直通 = 规则渲染原文 + 标记指令兜底，
                        # 正文永不经过 LLM（杜绝 LLM 编造/改写/丢内容，见 2026-09-01 台账）
                        marked = await _markup_document(session_user_content, executor_cfg)
                        args["content"] = marked or session_user_content
                        args.pop("sections", None)
                        args.pop("markdown_tables", None)
                        # 封面元信息一并清除：LLM 编的 title/subtitle/date/source 会污染文档
                        # （如 date=2026-08-24 错误日期），title 由工具从 content 首行自动提取
                        args.pop("title", None)
                        args.pop("subtitle", None)
                        args.pop("date", None)
                        args.pop("source", None)
                    else:
                        # 加工模式（用户明确要求总结/提炼/润色）：LLM 提炼
                        md = await _markdownify_content(session_user_content, executor_cfg,
                                                        summarize=True)
                        if md:
                            # 成功：正文用提炼后的 markdown，LLM 定格式与 filename/title
                            args["content"] = md
                            args.pop("sections", None)
                            args.pop("markdown_tables", None)
                        else:
                            # 加工且 markdownify 失败：回落原有 content 兜底注入逻辑
                            if session_user_content and (not isinstance(args.get("content"), str)
                                                         or not args.get("content").strip()
                                                         or len(args.get("content")) < len(session_user_content) * 0.9):
                                args["content"] = session_user_content
                            if session_tables:
                                if args.get("content"):
                                    args["markdown_tables"] = list(session_assistant_tables or session_tables)
                                else:
                                    args["markdown_tables"] = list(session_tables)
                elif session_tables:
                    # 会话原文为空（异常兜底）：退化为表格注入
                    args["markdown_tables"] = list(session_tables)
            result = await call_tool(name, args, ctx)
            logs.append(f"工具 {name} 返回：" + (json.dumps(result, ensure_ascii=False)[:300]))
            # 会话表格收集：fetch_session_messages 返回的 markdown_tables 原文（2026-08-24 起），
            # 供 generate_pdf/docx 代码直接渲染（图片列 100% 保留、列严格对齐，不依赖 LLM 转写）；
            # 2026-09-01 起：同时收集用户原文拼接（content 兜底）与助手表格（content 场景分源注入）
            if name == "fetch_session_messages" and isinstance(result, dict) and result.get("ok"):
                for md in result.get("markdown_tables") or []:
                    if md and md not in session_tables:
                        session_tables.append(md)
                for md in result.get("assistant_tables") or []:
                    if md and md not in session_assistant_tables:
                        session_assistant_tables.append(md)
                parts = []
                for m in result.get("messages") or []:
                    if m.get("role") == "user":
                        text = _strip_tail_instruction(m.get("content") or "")
                        if len(text) > 300:  # 只收正文（>300 字），跳过寒暄/指令短句
                            parts.append(text)
                if parts:
                    session_user_content = "\n\n".join(parts)
            # 榜单表格收集：工具 ok 且返回 markdown_table 时记录，供 workflow 自动附加
            #（2026-08-24：最终表格由后端生成，执行者不再手拼）
            if isinstance(result, dict) and result.get("ok") and result.get("markdown_table"):
                tool_results.append({"name": name, "ok": True, "markdown_table": result["markdown_table"]})
            # 工具结果回调（workflow 据此在失败时向会话写"正在尝试其它方案"进度消息）
            if on_log:
                await on_log("executor", "tool_result",
                             {"tool": name, "ok": bool(result.get("ok")),
                              "error": result.get("error")})
            if result.get("ok") and result.get("path"):
                artifacts.append(result["path"])
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or "",
                "content": json.dumps(result, ensure_ascii=False),
            })

    # 超过最大轮次：强制收尾
    logs.append("达到最大工具轮次，停止")
    return {
        "success": False,
        "summary": "执行达到最大工具调用轮次，任务未能完成，请简化目标后重试。",
        "artifacts": artifacts,
        "tool_results": tool_results,
        "log": logs,
    }
