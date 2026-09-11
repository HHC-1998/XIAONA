"""PDF 生成工具（04 详解 §6）：用 reportlab 把结构化内容渲染为美观模板 PDF。

模板约定（04 详解 §4/§5）：
- 中文字体：UnicodeCIDFont("STSong-Light")（reportlab 默认字体无中文字形，必须注册）；
- 封面页（title 非空时）→ 章节（heading/paragraphs/bullets/table）→ 页脚页码；
- 表格单元格内 URL 显示完整文本并用 <link href> 生成可点击链接注解（可点可复制）；
- 图片经 httpx 下载嵌入，失败降级跳过不阻塞生成。

产物落盘到 artifacts/{user_id}/{task_id}/{filename}，返回相对路径供下载接口使用。
"""
import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime

logger = logging.getLogger("xiaona")

# 品牌主色（indigo→violet 系，与前端 UI 一致）
_INDIGO = "#4F46E5"
_INDIGO_DARK = "#4338CA"
_LIGHT_BG = "#EEF2FF"
_ZEBRA = "#F8FAFC"
_GRAY = "#6B7280"
_LINE = "#E5E7EB"

# 图片嵌入尺寸上限：宽 300pt；高不超过 A4 可用高（841.89 − 上下边距各 50 ≈ 741.89），
# 再减去 Frame 默认上下 padding 6×2 后实际内容区约 729.89pt——取 720 留足余量。
# 竖长图按宽算高会超页高，Image 不可分页会抛 LayoutError，须超限反向折算宽度。
_IMG_MAX_W = 300
_IMG_MAX_H = 720

# 中文字体注册（幂等：reportlab 默认字体无中文字形，不注册中文输出为空白/方框）
_FONT = "STSong-Light"
try:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    if _FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
except Exception:
    logger.warning("reportlab CID font registration failed", exc_info=True)


def _safe_filename(name: str) -> str:
    """清洗文件名：只保留安全字符并强制 .pdf 后缀，防路径穿越；超长截断（≤60 字符）。"""
    base = re.sub(r"[\\/:*?\"<>|\s]", "_", name or "").strip("._") or f"文档_{uuid.uuid4().hex[:6]}"
    base = base[:60]
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base


def _safe_text(v) -> str:
    """文本归一化：None→""，对象→JSON，清洗 PDF 特殊/控制字符防 reportlab 抛异常。"""
    if v is None:
        return ""
    if not isinstance(v, str):
        try:
            v = json.dumps(v, ensure_ascii=False)
        except Exception:
            v = str(v)
    # 去掉 C0/C1 控制字符（保留换行），防 <link> 等 XML 解析报错
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", v)


def _escape_xml(text: str) -> str:
    """转义 XML 特殊字符（Paragraph 走 XML 解析，& < > 需转义）。"""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _is_url(text: str) -> bool:
    return isinstance(text, str) and text.strip().startswith(("http://", "https://"))


# 图片扩展名正则（表格「图片」列识别用，2026-08-24 起）
_IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|gif|webp)(?:\?|$)", re.IGNORECASE)


def _is_image_url(text: str) -> bool:
    """识别单元格里的图片：`![alt](https://.../x.jpg)` 或纯图片 URL（.jpg/.png/.gif/.webp）。"""
    if not isinstance(text, str):
        return False
    t = text.strip()
    m = re.match(r"^!\[[^\]]*\]\((\S+)\)$", t)
    if m:
        return bool(_IMG_EXT_RE.search(m.group(1)))
    return bool(_is_url(t) and _IMG_EXT_RE.search(t))


def _extract_image_url(text: str) -> str:
    """从 `![alt](url)` 或纯 URL 里提取图片 URL。"""
    if not isinstance(text, str):
        return ""
    m = re.match(r"^!\[[^\]]*\]\((\S+)\)$", text.strip())
    return (m.group(1) if m else text).strip()


def _parse_markdown_table(md: str) -> tuple[list[str], list[list[str]]]:
    """把 markdown 表格原文解析为 (headers, rows)（2026-08-24 起，2026-09-01 去首尾空 cell）。

    跳过分隔行（|---），按 | 切列并 strip；图片单元格 `![alt](URL)` 原样保留，
    交由 img_cols 识别嵌入。**去首尾空 cell**（2026-09-01 起）：`| | 评估维度 | 分析结论 | |`
    这类行 split 后首尾是空串（LLM 残留管道符），剥离后列数才与表头一致。非表格输入返回空。
    """
    if not isinstance(md, str):
        return [], []
    lines = [ln.strip() for ln in md.splitlines() if ln.strip()]
    if len(lines) < 2:
        return [], []
    headers: list[str] = []
    rows: list[list[str]] = []
    for ln in lines:
        if not ln.startswith("|") or "|" not in ln[1:]:
            continue
        body = ln.strip("|")
        cells = [c.strip() for c in body.split("|")]
        # 去首尾空 cell（`| | a | b | |` → [a, b]）
        while cells and not cells[0]:
            cells.pop(0)
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        # 分隔行（全部是 - / : 组成）
        if all(re.match(r"^:?-{1,}:?$", c or "-") for c in cells):
            continue
        if not headers:
            headers = cells
        else:
            rows.append(cells)
    return headers, rows


# 文本表格/中文标题识别（2026-09-01 起：换行保真后，纯文本表格与中文编号标题按结构渲染）
_ZH_H1_RE = re.compile(
    r"^(?:[一二三四五六七八九十]+、|第[一二三四五六七八九十百零\d]+[章节部分]|（[一二三四五六七八九十]+）)"
)
_NUM_H2_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?\s+\S")
# 表格表头关键词（表头行判定：含任一关键词 或 列数≥3；2026-09-01 扩充覆盖中文对照表：
# 组件/数据对象/对比项/访问地址/前置条件/交付物/维度/指标 等，让「评估维度 分析结论」式表头命中）
_TABLE_HEADER_KW = ("序号", "项目", "金额", "规格", "名称", "数量", "单价",
                    "说明", "内容", "配置", "维度", "指标", "结论", "费用", "小计",
                    "组件", "数据对象", "对比项", "访问地址", "前置条件", "交付物",
                    "访问方式", "安全要求", "典型场景", "评估维度", "包干金额",
                    "数据源", "关键词", "趋势词", "商品名", "排名", "评分", "评论")


def _split_cols(line: str, simple: bool = False) -> list[str]:
    """表格行分列：Tab 分隔优先，全角空格（　）次之，2+ 半角空格兜底。

    simple=True 时额外支持单空格对齐行（报价表/中文对照表数据行）：
    - 行内 ≥2 个空格 → 「首列+中间+末列」3 列（`1 NAS 主机 … 7,000` → [1, 说明, 7,000]，
      金额由调用方对齐到表头末列）；
    - 恰 1 个空格 → 「首列+第二列」2 列（中文对照表 `架构适配性 系统由 FastAPI…构成。`），
      首列须短（≤15 字且不含句号结尾）防正文段落误判。
    """
    if "\t" in line:
        cols = [c.strip() for c in line.split("\t") if c.strip()]
        if len(cols) >= 2:
            return cols
    if "　" in line:
        cols = [c.strip() for c in re.split(r"　+", line) if c.strip()]
        if len(cols) >= 2:
            return cols
    cols = [c.strip() for c in re.split(r"\s{2,}", line.strip()) if c.strip()]
    if len(cols) >= 2:
        return cols
    if simple:
        stripped = line.strip()
        first_sp = stripped.find(" ")
        last_sp = stripped.rfind(" ")
        if first_sp > 0:
            if first_sp < last_sp:
                # ≥2 空格：首列+中间+末列 3 列（报价表数据行：序号 说明 金额）
                return [stripped[:first_sp].strip(),
                        stripped[first_sp + 1:last_sp].strip(),
                        stripped[last_sp + 1:].strip()]
            # 恰 1 空格：首列+第二列 2 列（中文对照表数据行）
            head, tail = stripped[:first_sp].strip(), stripped[first_sp + 1:].strip()
            if head and tail and len(head) <= 15 and "。" not in head:
                return [head, tail]
    return []


def _is_num(x: str) -> bool:
    return bool(re.match(r"^\d[\d,.\-–—%]*$", x))


def _split_text_table(lines: list[str], start: int):
    """文本表格块探测（2026-09-01 起）：纯文本报价表格/中文对照表（无 | 管道符）按结构识别。

    规则（保守）：
    - 表头行：可分列（≥2 列，Tab/全角/2+ 空格/单空格首切）且（含表格关键词 或 列数≥3）
      且首/末列非数字；**拒绝含 `|` 的行**（markdown 表格交给 markdown 路径，防 `|`/空列/`---` 残留）
      与 `- `/`* ` 开头行（列表误判）；
    - 数据行：可分列且**列数与表头一致（容差 ±1）**即可——不再强制首列序号/末列金额，
      覆盖中文对照表（末列为中文说明，如「评估维度 分析结论」表下的「架构适配性 系统由…构成。」）；
      数字末列仍作对齐到表头末列的加分信号；
    - 数据行 ≥2 返回 (headers, rows, next_index)；不命中返回 None。
    表头前的行（如「（一）硬件费用　包干封顶价 HKD 13,000」）不吞进表格，
    留给主循环按标题/段落渲染。
    """
    j = start
    n = len(lines)
    header = None
    header_cols: list[str] = []
    header_is_money = False  # 表头含金额类词（金额/HKD/价格/费用/小计/单价）时，数据行末列须为数字
    rows: list[list[str]] = []
    while j < n:
        line = (lines[j] or "").strip()
        if not line:
            break
        if "|" in line or re.match(r"^\s*[-*]\s", line):
            break  # markdown 表格行 / 列表行：不是文本表格，交给主循环其它分支
        if header is None:
            cols_std = _split_cols(line)  # 标准分隔（Tab/全角/2+ 空格）
            cols = cols_std or _split_cols(line, simple=True)
            if not cols:
                break
            # 「列数≥3」仅认标准分隔切出的列（单空格 simple 切 3 列不算数——
            # 普通长句含 2 个空格就切 3 列，会误判表头）；含关键词仍算表头
            has_kw = any(kw in line for kw in _TABLE_HEADER_KW)
            is_hdr = has_kw or (bool(cols_std) and len(cols_std) >= 3)
            if is_hdr and not (_is_num(cols[0]) or _is_num(cols[-1])):
                header = cols
                header_cols = cols
                header_is_money = any(k in "".join(cols) for k in ("金额", "HKD", "价格", "费用", "小计", "单价"))
            else:
                break  # 第一个可分列的行不是表头：放弃探测（回落主循环，防吞标题/正文）
        else:
            # 标题行（一、/（一）/1.1）不吞进表格：立即收尾
            if _ZH_H1_RE.match(line) or _NUM_H2_RE.match(line):
                break
            if re.match(r"^\s*[-*]\s", line):
                break  # 列表行（防 `- ` 数据行误判成表格行）
            cols = _split_cols(line) or _split_cols(line, simple=True)
            if not cols:
                break
            if abs(len(cols) - len(header_cols)) > 1:
                break  # 列数与表头差太多：不像该表格的行
            if header_is_money and not _is_num(cols[-1]):
                break  # 报价表数据行末列须为金额（防表格后总结句混入）
            if len(cols) < len(header_cols):
                if _is_num(cols[-1]):
                    # 数字末列（报价表：序号 说明 金额）→ 对齐到表头末列（金额列）
                    rows.append([cols[0]] + cols[1:-1]
                                + [""] * (len(header_cols) - len(cols)) + [cols[-1]])
                else:
                    # 非数字（中文对照表）→ 顺排靠前，不足列补空
                    rows.append(cols + [""] * (len(header_cols) - len(cols)))
            elif len(cols) > len(header_cols):
                # 超过表头列数（单空格对齐行含多个空格时 3 列切过细）：前 ncols-1 列保留、
                # 多余列合并进最后一列（防丢内容）
                rows.append(cols[:len(header_cols) - 1]
                            + [" ".join(cols[len(header_cols) - 1:])])
            else:
                rows.append(cols)
        j += 1
    if header is not None and len(rows) >= 2:
        ncols = max(len(header_cols), max(len(r) for r in rows))
        header_out = header_cols + [""] * (ncols - len(header_cols))
        rows_out = [r + [""] * (ncols - len(r)) for r in rows]
        return header_out, rows_out, j
    return None


def _strip_md_symbols(text: str) -> str:
    """剥离文本里的 markdown 符号（**加粗**/*斜体*/`代码`/~~删除~~）。

    2026-09-01 起：直通渲染时原文可能残留 LLM 加的 markdown 标记，
    渲染到 Word/PDF 不应出现 `**`、`*` 等符号。
    """
    if not isinstance(text, str):
        return ""
    t = text.strip()
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"\*(.+?)\*", r"\1", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"~~(.+?)~~", r"\1", t)
    t = re.sub(r"!\[([^\]]*)\]\((\S+)\)", r"\1", t)  # 图片标记 → 保留 alt
    return t.strip()


def _smart_break_lines(content: str) -> str:
    """零换行/极少换行长文本按规则插换行（2026-09-01 方案 C，纯规则、保真、不调 LLM）。

    历史消息经旧前端单行 input 粘贴时换行被剥成一行 7800 字，规则块解析无从下手。
    此函数对**超长行（>100 字）**在「中文编号标题 / 数字编号标题 / 编号列表项 / 表格表头关键词行」
    前插换行，让 `_parse_markdown_blocks` 能识别标题/表格/段落。
    - 逐行处理：已有换行的文本不被破坏（短行原样保留，含 Tab 表格行）；
    - 压缩行内多空格但**保留单 Tab/单空格**（表格行分隔不丢）；
    - 已有合理换行（≥6 个）则原样返回。
    """
    if not content or content.count("\n") >= 6:
        return content
    out: list[str] = []
    for ln in content.splitlines():
        if not ln or len(ln) <= 100:
            out.append(ln)
            continue
        text = re.sub(r"[ \t　]{2,}", " ", ln).strip()
        if not text:
            out.append(ln)
            continue
        text = _BREAK_RE.sub(r"\n", text)  # 只插换行，不重复插入断点文本
        out.extend(x for x in text.split("\n"))
    return "\n".join(out)


# _smart_break_lines 的断点正则（中文标题 | 数字编号标题 | 编号列表项 | 表头关键词行）
_BREAK_RE = re.compile(
    r"(?<!\n)(?=("
    r"(?:[一二三四五六七八九十]+、|第[一二三四五六七八九十百零\d]+[章节部分]|（[一二三四五六七八九十]+）)"  # 中文标题
    r"|\d+(?:\.\d+){1,2}[\s、]"  # 数字编号标题（1.1 / 2.2）
    r"|\d+[.、)]\s+\S"  # 编号列表项（1. 系统部署…）
    r"|(?:序号|项目|金额|规格|名称|数量|单价|说明|内容|配置|维度|指标|结论|费用|小计|"
    r"组件|数据对象|对比项|访问地址|前置条件|交付物|访问方式|安全要求|典型场景|评估维度|包干金额)"
    r"\s*(?:[\t　]|$)"  # 表头关键词后跟 Tab/全角空格/行尾
    r"))"
)


def _parse_markdown_blocks(content: str) -> list[tuple]:
    """把 markdown 原文解析为结构化块列表（2026-09-01 起，content 原文渲染用）。

    返回 [(kind, ...)]：
    - ("heading", level, text)：`#`/`##`/`###` 标题行 + 中文/编号标题（一、/第X章/（一）→1，1.1→2）；
    - ("table", headers, rows)：markdown 表格块（复用 _parse_markdown_table，图片列保留）
      或纯文本表格块（_split_text_table，报价表格末列金额）；
    - ("bullet", text)：`- ` / `* ` 列表行；
    - ("para", text)：普通段落（含 `1. ` 编号行，按段落渲染）。

    2026-09-01 方案 C 改动：
    - 入口先过 `_smart_break_lines`（零换行旧消息语义分行）；
    - markdown 表格识别扩展为「连续 `|` 行」：首行 `|` 开头且次行也 `|` 开头即成表，
      不再强制 `---` 分隔行（LLM 输出/原文可能无分隔行）；
    - 文本剥离 markdown 符号（`**`/`*`/`` ` ``）。
    """
    blocks: list[tuple] = []
    if not content:
        return blocks
    content = _smart_break_lines(content)
    lines = content.splitlines()
    i, n = 0, len(lines)
    while i < n:
        stripped = (lines[i] or "").strip()
        if not stripped:
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            blocks.append(("heading", len(m.group(1)), _strip_md_symbols(m.group(2))))
            i += 1
            continue
        # 表格块：连续 | 行（首行 | 开头且次行也 | 开头；有 --- 分隔行或无均可）
        if stripped.startswith("|") and i + 1 < n and lines[i + 1].strip().startswith("|"):
            block = [lines[i].strip()]
            j = i + 1
            while j < n and lines[j].strip().startswith("|"):
                block.append(lines[j].strip())
                j += 1
            headers, rows = _parse_markdown_table("\n".join(block))
            if headers:
                blocks.append(("table", headers, rows))
            i = j
            continue
        # 文本表格块（2026-09-01 起：纯文本报价表格按结构渲染；含 | 的行已被上面拦截）
        t = _split_text_table(lines, i)
        if t:
            blocks.append(("table", t[0], t[1]))
            i = t[2]
            continue
        # 中文一级标题（2026-09-01 起：一、 / 第X章 / （一））
        if _ZH_H1_RE.match(stripped):
            blocks.append(("heading", 1, _strip_md_symbols(stripped)))
            i += 1
            continue
        # 编号二级标题（2026-09-01 起：1.1 短行且不以数字结尾，防表格行误判）
        if _NUM_H2_RE.match(stripped) and len(stripped) <= 40 \
                and not re.search(r"\d[\d,.]*$", stripped):
            blocks.append(("heading", 2, _strip_md_symbols(stripped)))
            i += 1
            continue
        if re.match(r"^\s*[-*]\s+", stripped):
            blocks.append(("bullet", _strip_md_symbols(re.sub(r"^\s*[-*]\s+", "", stripped))))
        elif re.match(r"^\s*\d+[.、)]\s+", stripped):
            blocks.append(("para", _strip_md_symbols(re.sub(r"^\s*\d+[.、)]\s+", "", stripped))))
        else:
            blocks.append(("para", _strip_md_symbols(stripped)))
        i += 1
    return blocks


def _default_doc_name(title: str, ext: str) -> str:
    """文件名语义化兜底（2026-08-24 起）：用 title 提炼，title 空则「文档_时间戳」。"""
    if title:
        return f"{title}_{datetime.now():%Y%m%d_%H%M%S}{ext}"
    return f"文档_{datetime.now():%Y%m%d_%H%M%S}{ext}"


def _footer(canvas, doc):
    """页脚：页码 + 来源（canvas onPage 回调）。"""
    canvas.saveState()
    canvas.setFont(_FONT, 8)
    canvas.setFillColor(_GRAY)
    canvas.drawCentredString(doc.pagesize[0] / 2, 30, f"第 {doc.page} 页")
    canvas.drawRightString(doc.pagesize[0] - 40, 30, "小娜出品")
    canvas.restoreState()


async def _download_images(images: list[dict], task_dir: str) -> list[dict]:
    """下载图片列表到 task_dir（失败降级跳过，返回带本地路径的项）。"""
    if not images:
        return []
    import httpx
    out = []
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for i, img in enumerate(images[:20]):  # 最多嵌 20 张，防文档过大
            url = (img or {}).get("url") or ""
            if not url.startswith(("http://", "https://")):
                continue
            try:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 XIAONA"})
                if resp.status_code != 200:
                    continue
                ext = ".png" if resp.headers.get("content-type", "").startswith("image/png") else ".jpg"
                path = os.path.join(task_dir, f"_img_{i}{ext}")
                with open(path, "wb") as f:
                    f.write(resp.content)
                out.append({"path": path, "url": url, "caption": _safe_text((img or {}).get("caption"))})
            except Exception as e:
                logger.warning("pdf image download failed: %s (%s)", url, e.__class__.__name__)
    return out


async def generate_pdf(params: dict, ctx: dict | None = None) -> dict:
    """生成 PDF。params: {filename?, title?, subtitle?, date?, source?, sections?, images?, markdown_tables?, content?}。

    - sections：[{heading?, paragraphs?, bullets?, table?}]（table: {headers, rows}）；
    - images：[{url, caption?}]，URL 数组自动下载嵌入，失败降级；
    - markdown_tables：[str] 可选的 markdown 表格原文（2026-08-24 起，executor 注入的
      会话榜单表格），由代码解析渲染、图片列直接嵌入，不依赖 LLM 转写；
    - content：可选，markdown 原文（2026-09-01 起，用户粘贴的长文本直接渲染——
      标题/表格/bullets/段落自动解析，跳过 sections 校验，不依赖 LLM 手工转 JSON）。
    - ctx: {"user_id", "task_id", "artifacts_dir"}。缺 ctx 时只校验数据不落盘。
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle, Image)

    # 防御性归一：LLM 输出截断/畸形时 args 可能不是 dict（2026-09-01 起）
    params = params if isinstance(params, dict) else {}
    sections = params.get("sections") or []
    if not isinstance(sections, list):
        sections = []
    content = _safe_text(params.get("content"))
    if not content and not sections:
        return {"ok": False, "error": "sections 不能为空：请提供文档章节内容"}
    title = _safe_text(params.get("title"))
    # LLM 可能在 title 里传字面 \n 换行（封面排版会崩），清洗为空格（2026-09-01 起）
    title = title.replace("\r", " ").replace("\n", " ")
    # title 自动提取（2026-09-01 起）：LLM 未传 title 时用 content 首个非空行，
    # 文件名不再回退「文档_时间戳」（剥引号与首尾空白，≤40 字）
    if not title and content:
        first_line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
        title = first_line.strip("“”\"' 　")[:40]
    subtitle = _safe_text(params.get("subtitle"))
    date = _safe_text(params.get("date")) or datetime.now().strftime("%Y-%m-%d")
    source = _safe_text(params.get("source"))

    # 校验表格列数一致性（列数不一致会让 Table 抛 LayoutError）
    for sec in sections:
        table = sec.get("table") if isinstance(sec, dict) else None
        if not table:
            continue
        headers = table.get("headers") or []
        rows = table.get("rows") or []
        if not headers or not isinstance(headers, list):
            return {"ok": False, "error": "表格 headers 不能为空"}
        for row in rows:
            if len(row) != len(headers):
                return {"ok": False, "error": f"表格列数与表头不一致（期望 {len(headers)} 列）"}

    if not ctx:
        return {"ok": True, "path": "", "size": 0, "title": title, "sections": len(sections)}

    user_id = ctx.get("user_id", "")
    task_id = ctx.get("task_id", "")
    artifacts_dir = ctx.get("artifacts_dir", "")
    task_dir = os.path.join(artifacts_dir, user_id, task_id)
    os.makedirs(task_dir, exist_ok=True)

    # 收集图片：独立 images 参数 + 表格「图片」列里 ![alt](url) 的 URL（2026-08-24 起）
    img_urls: list[dict] = []
    seen: set[str] = set()
    for item in params.get("images") or []:
        u = (item or {}).get("url") or ""
        if u and u not in seen:
            seen.add(u)
            img_urls.append({"url": u, "caption": (item or {}).get("caption")})
    for sec in sections:
        # 非 dict 元素 / 无 table 键（纯文字 section）跳过，勿对 None 调 .get（2026-09-01 起）
        if not isinstance(sec, dict):
            continue
        table = sec.get("table") if isinstance(sec.get("table"), dict) else None
        if not table:
            continue
        for row in table.get("rows") or []:
            for c in row:
                if _is_image_url(c):
                    u = _extract_image_url(c)
                    if u and u not in seen:
                        seen.add(u)
                        img_urls.append({"url": u})
    # markdown_tables 里的图片 URL 一并收集（2026-08-24 起：executor 注入的会话表格原文）
    for md in params.get("markdown_tables") or []:
        _h, md_rows = _parse_markdown_table(md)
        for row in md_rows:
            for c in row:
                if _is_image_url(c):
                    u = _extract_image_url(c)
                    if u and u not in seen:
                        seen.add(u)
                        img_urls.append({"url": u})
    images = await _download_images(img_urls, task_dir)
    # 下载成功的图片路径按 URL 建索引，供表格单元格嵌入查找
    img_by_url = {}
    for img in images:
        if img.get("url"):
            img_by_url[img["url"]] = img["path"]

    styles = {
        "brand": ParagraphStyle("brand", fontName=_FONT, fontSize=9, textColor=colors.white),
        "title": ParagraphStyle("title", fontName=_FONT, fontSize=24, leading=32,
                                textColor=colors.HexColor(_INDIGO_DARK), alignment=1, spaceAfter=10),
        "subtitle": ParagraphStyle("subtitle", fontName=_FONT, fontSize=10, leading=15,
                                   textColor=colors.HexColor(_GRAY), alignment=1, spaceAfter=4),
        "heading": ParagraphStyle("heading", fontName=_FONT, fontSize=14, leading=20,
                                  textColor=colors.HexColor(_INDIGO_DARK), spaceBefore=14, spaceAfter=6),
        "subheading": ParagraphStyle("subheading", fontName=_FONT, fontSize=12, leading=17,
                                     textColor=colors.HexColor(_INDIGO_DARK), spaceBefore=10, spaceAfter=4),
        "body": ParagraphStyle("body", fontName=_FONT, fontSize=10.5, leading=17, spaceAfter=4),
        "bullet": ParagraphStyle("bullet", fontName=_FONT, fontSize=10.5, leading=17,
                                 leftIndent=14, bulletIndent=2, spaceAfter=3),
        "cell": ParagraphStyle("cell", fontName=_FONT, fontSize=9, leading=13),
        "cell_head": ParagraphStyle("cell_head", fontName=_FONT, fontSize=9.5, leading=13,
                                    textColor=colors.white),
        "caption": ParagraphStyle("caption", fontName=_FONT, fontSize=8.5, leading=12,
                                  textColor=colors.HexColor(_GRAY), alignment=1, spaceAfter=8),
    }

    def render_table(headers: list[str], data: list[list[str]], target: list) -> None:
        """渲染表格到 target（sections 表格与 markdown_tables 共用，2026-08-24 起）。
        「图片」列单元格为 ![alt](url) 时嵌入 Image（宽限列宽、高限 _IMG_MAX_H、按宽高比缩放居中）。
        """
        if not headers:
            return
        # 可用列宽 = A4 595pt − 左右边距各 40pt = 515pt；基准用 515 保证 5 列/6 列
        # 表格总宽不超 frame（基准 540 时 5 列 ×108=540 > 515 会抛 LayoutError）
        col_widths = [max(40, min(120, 515 // max(1, len(headers))))] * len(headers)
        # 「图片」列识别（表头含「图片」时嵌入商品图）
        img_cols = {i for i, h in enumerate(headers) if "图片" in h or "图" == h.strip()}
        grid = [[Paragraph(_escape_xml(h), styles["cell_head"]) for h in headers]]
        for row in data:
            cells = []
            for i, c in enumerate(row):
                if i in img_cols and _is_image_url(c):
                    path = img_by_url.get(_extract_image_url(c))
                    if path:
                        try:
                            from reportlab.lib.utils import ImageReader
                            iw, ih = ImageReader(path).getSize()
                            max_w = max(20, col_widths[i] - 12)  # 列宽减左右 padding
                            max_h = _IMG_MAX_H
                            width, height = max_w, max_w
                            if iw > 0 and ih > 0:
                                height = width * ih / iw
                                if height > max_h:
                                    height = max_h
                                    width = height * iw / ih
                            im = Image(path, width=width, height=height)
                            im.hAlign = "CENTER"
                            cells.append(im)
                            continue
                        except Exception as e:
                            logger.warning("pdf embed table image failed: %s", e.__class__.__name__)
                            # 降级为显示原文本，不阻断
                if _is_url(c):
                    cells.append(Paragraph(
                        f'<link href="{_escape_xml(c)}">{_escape_xml(c)}</link>',
                        styles["cell"],
                    ))
                else:
                    cells.append(Paragraph(_escape_xml(c), styles["cell"]))
            grid.append(cells)
        t = Table(grid, colWidths=col_widths, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(_INDIGO)),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor(_LINE)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        for i in range(1, len(grid)):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor(_ZEBRA)))
        t.setStyle(TableStyle(style))
        target.append(t)
        target.append(Spacer(1, 10))

    story = []

    # 封面页（title 非空时）
    if title:
        story.append(Spacer(1, 30))
        story.append(Table(
            [[Paragraph("XIAONA  ·  小娜智能文档", styles["brand"])]],
            colWidths=[None], rowHeights=[26],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(_INDIGO)),
                ("LEFTPADDING", (0, 0), (-1, -1), 18),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]),
        ))
        story.append(Spacer(1, 70))
        story.append(Paragraph(_escape_xml(title), styles["title"]))
        if subtitle:
            story.append(Paragraph(_escape_xml(subtitle), styles["subtitle"]))
        story.append(Spacer(1, 16))
        info = [f"生成日期：{date}"]
        if source:
            info.append(f"数据来源：{source}")
        story.append(Paragraph(_escape_xml("　　".join(info)), styles["subtitle"]))
        story.append(PageBreak())

    # content 原文渲染（2026-09-01 起：用户粘贴的 markdown 原文直接渲染，
    # 表格块复用 render_table，图片列保留嵌入，不依赖 LLM 转写 sections）
    for block in _parse_markdown_blocks(content):
        kind = block[0]
        if kind == "heading":
            st = styles["heading"] if block[1] <= 1 else styles["subheading"]
            story.append(Paragraph(_escape_xml(_safe_text(block[2])), st))
        elif kind == "table":
            render_table(block[1], block[2], story)
        elif kind == "bullet":
            story.append(Paragraph(_escape_xml(_safe_text(block[1])), styles["bullet"], bulletText="•"))
        else:  # para
            story.append(Paragraph(_escape_xml(_safe_text(block[1])), styles["body"]))

    # 章节（补充分析；sections 元素非 dict 时跳过，不抛异常）
    for raw in sections:
        sec = raw if isinstance(raw, dict) else {}
        heading = _safe_text(sec.get("heading"))
        if heading:
            story.append(Paragraph(_escape_xml(heading), styles["heading"]))
        for p in sec.get("paragraphs") or []:
            story.append(Paragraph(_escape_xml(_safe_text(p)), styles["body"]))
        for b in sec.get("bullets") or []:
            story.append(Paragraph(_escape_xml(_safe_text(b)), styles["bullet"], bulletText="•"))
        table = sec.get("table")
        if table:
            headers = [_safe_text(h) for h in table.get("headers") or []]
            data = [[_safe_text(c) for c in row] for row in table.get("rows") or []]
            render_table(headers, data, story)

    # markdown 表格原文（2026-08-24 起：fetch_session_messages 提取、executor 注入，
    # 由代码直接渲染——图片列 100% 保留且列严格对齐，不依赖 LLM 转写）
    for md in params.get("markdown_tables") or []:
        h, md_rows = _parse_markdown_table(md)
        if h:
            render_table(h, md_rows, story)

    # 图片章节（嵌入失败已在下载阶段降级）
    for img in images:
        try:
            from reportlab.lib.utils import ImageReader
            path = img["path"]
            iw, ih = ImageReader(path).getSize()
            # 以宽度为基准保持宽高比；高度超可用页高时反向折算宽度（不拉伸）
            width = min(90 * mm, _IMG_MAX_W)
            height = width
            if iw > 0 and ih > 0:
                height = width * ih / iw
                if height > _IMG_MAX_H:
                    height = _IMG_MAX_H
                    width = height * iw / ih
            im = Image(path, width=width, height=height)
            im.hAlign = "CENTER"
            story.append(im)
            if img["caption"]:
                story.append(Paragraph(_escape_xml(img["caption"]), styles["caption"]))
        except Exception as e:
            logger.warning("pdf embed image failed: %s", e.__class__.__name__)

    try:
        # 文件名语义化（2026-08-24 起）：filename 缺失或「文档_<时间戳>」占位名时用 title 提炼
        filename_in = params.get("filename") or ""
        if not filename_in or re.match(r"^文档_\d{8}_\d{6}$", filename_in.strip()):
            filename_in = _default_doc_name(title, "")
        filename = _safe_filename(filename_in or f"文档_{datetime.now():%Y%m%d_%H%M%S}.pdf")
        path = os.path.join(task_dir, filename)
        doc = SimpleDocTemplate(
            path, pagesize=A4,
            leftMargin=40, rightMargin=40, topMargin=50, bottomMargin=50,
            title=title or filename, author="XIAONA",
        )
        doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
        size = os.path.getsize(path)
        rel = os.path.relpath(path, artifacts_dir).replace("\\", "/")
        logger.info("generate_pdf ok: %s (%d bytes)", rel, size)
        return {"ok": True, "path": rel, "size": size, "title": title, "sections": len(sections)}
    except Exception as e:
        logger.warning("generate_pdf failed", exc_info=True)
        return {"ok": False, "error": f"PDF 生成失败：{e.__class__.__name__}"}
    finally:
        # 清理下载的临时图片
        for img in images:
            try:
                os.remove(img["path"])
            except OSError:
                pass
