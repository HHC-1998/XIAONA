"""交互者（Director，02 §5）：意图分类 → 需求提炼 → 确认判定。

- `is_task_intent(text)`：关键词预筛（扒/抓取/爬/生成/做一份/汇总/分析/excel/pdf/表格/报表），
  命中才走 LLM 提炼——日常闲聊零开销；
- `refine_task(text, chat_cfg)`：LLM 把用户需求提炼为结构化任务 {task_type, description, params}，
  失败回退简单结构化（保留原文）；
- `classify_confirmation(reply, chat_cfg)`：LLM 判用户回复是「确认执行 / 取消 / 其它」，
  失败回退关键词（是/好/执行/开始 → yes；不/取消/算了 → no）。
"""
import json
import logging
import re

from app.core.llm_provider import llm

logger = logging.getLogger("xiaona")

# 意图预筛关键词（02 §5 工具清单：扒/抓取/爬/生成/做一份/汇总/分析/excel/pdf/ppt/表格/报表；
# 2026-08-19 补「搜索/排名/top/榜单/卖得最好」等电商榜单类词——否则用户问「北美卖得最好的戒指 top10」
# 会被回落普通聊天而得不到数据源工具响应）
_INTENT_KEYWORDS = (
    "扒", "抓取", "抓", "爬", "采集", "生成", "做一份", "做一份文件", "做个", "做一份excel",
    "汇总", "分析", "整理成", "整理成文档", "整理成文件", "做成表格", "做成文档", "生成报告",
    "excel", "xlsx", "pdf", "ppt", "word", "docx", "文档", "文件", "表格", "报表",
    "调研", "查询", "下载", "搜集", "整理",
    "搜索", "搜", "排名", "排行", "榜单", "销量", "最好卖", "卖得最好", "卖得好",
    "前十", "前五", "前三", "前10", "前5", "前3", "top",
)

# 确认/取消快速判定关键词（LLM 判定失败时的兜底）
# 注意判定顺序：强肯定短语 → 强否定短语 → 弱否定 → 弱肯定 → LLM。
# 「没问题」是最常用肯定答复且含「没」，必须在否定判定之前用强肯定短语拦截，
# 否则会被误判为取消（2026-08-19 审查修复，见当天台账）。
_STRONG_CONFIRM = ("没问题", "可以的", "可以", "好的", "好", "行", "确认",
                   "执行", "开始", "做吧", "来吧", "干吧", "同意", "ok", "yes")
_STRONG_CANCEL = ("不是", "不用", "不要", "取消", "算了", "放弃", "不了",
                  "别", "等等", "暂时", "再说", "no")
_WEAK_CANCEL = ("不", "没", "否")
_WEAK_CONFIRM = ("是",)


def is_task_intent(text: str) -> bool:
    """关键词预筛：命中任一关键词即视为功能需求（才走 LLM 提炼）。"""
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(k.lower() in t for k in _INTENT_KEYWORDS)


async def refine_task(text: str, chat_cfg: dict | None = None,
                      attachments: list[dict] | None = None) -> dict:
    """LLM 提炼结构化任务：{task_type, description, params}。

    - task_type：scrape(网页采集) / generate(生成文件) / analyze(汇总分析) / other；
    - description：一句话需求描述（供确认文案/会话留痕展示）；
    - params：自由结构，执行者据此选择工具。
    提炼失败时回退：task_type=other、description=原文截断、params={}。

    attachments（2026-08-24 起）：用户带附件发起的任务（如图片/文档），把附件名与
    提取文本拼进提炼 prompt 与 params，执行者才能感知附件内容（此前附件被 agent 丢弃，
    只有回落 memory_engine 的纯聊天路径才用得上）。
    """
    prompt = (
        "你是任务需求提炼器。把用户的一句话需求提炼为结构化 JSON，格式：\n"
        '{"task_type": "scrape|generate|analyze|other", "description": "一句话中文需求描述", '
        '"params": {"目标": "...", "要求": "...", "产品类型": "...", "排名范围": "...", '
        '"地区": "...", "价格区间": "...", "评分区间": "...", "评论数区间": "...", "颜色": "...", "材质": "..."}}\n'
        "task_type 取值：scrape=网页/数据抓取采集；generate=生成文件(如 Excel/PDF)；"
        "analyze=汇总/分析已有内容；other=其它功能需求。\n"
        "电商选品/榜单类需求请额外提取：\n"
        "- 产品类型：具体细分品类（如 银戒指 / 金戒指 / 钛钢戒指 / 钻石戒指 / silver ring），没有则留空；\n"
        "- 排名范围：用户要的排名区间（如 top10 / top5 / 前5% / 前20），没有则留空；\n"
        "- 地区：用户指定的地区/站点（如 北美/美国/英国/加拿大/德国），没有则留空；\n"
        "- 价格区间：用户说的价格范围（如「15 美元以上」「不超过 20 美元」「15~20 美元」），没有则留空；\n"
        "- 评分区间：用户说的评分范围（如「不低于 4.5」「不高于 4.6」「4.5~4.6 分」），没有则留空；\n"
        "- 评论数区间：用户说的评论数范围（如「评论不低于 3 万」「不高于 2 万」），没有则留空；\n"
        "- 颜色：用户指定的颜色（白色/黑色/黄色/红色/蓝色/玫红等），没有则留空；\n"
        "- 材质：用户指定的材质（玫瑰金/白金/925银/不锈钢/钛等），没有则留空；\n"
        "- **用户没提的维度一律不填（留空）**，不要编造。\n"
        "若用户说「把上面的/刚才的/之前的对话/这个榜单/这个列表整理成文档」，params 增加"
        '"内容来源": "会话已有内容"（执行者据此禁止重新抓取、只用会话里已有的内容做文档数据源）。\n'
        "只返回 JSON，不要解释。用户需求：\n" + text[:500]
    )
    if attachments:
        names = [f"{a.get('name', '文件')}({a.get('kind', '')})" for a in attachments]
        extracted = [a.get("text") for a in attachments if a.get("text")]
        prompt += (
            "\n\n用户同时上传了附件（附件名："
            + "、".join(names)
            + "）。请在 params 里增加「附件」字段描述附件，供执行者读取处理。"
        )
        if extracted:
            prompt += "\n附件提取文本：\n" + "\n".join(str(t)[:300] for t in extracted)[:1500]
    try:
        raw = await llm.chat(
            [{"role": "system", "content": "你是任务需求提炼器，只返回 JSON。"},
             {"role": "user", "content": prompt}],
            chat_cfg, thinking="low",
        )
        data = json.loads(llm._extract_json(raw))
        task_type = str(data.get("task_type") or "other")
        if task_type not in ("scrape", "generate", "analyze", "other"):
            task_type = "other"
        description = str(data.get("description") or text).strip()[:200]
        params = data.get("params") or {}
        if not isinstance(params, dict):
            params = {"内容": params}
        if attachments and "附件" not in params:
            params["附件"] = [a.get("name", "") for a in attachments]
        return {"task_type": task_type, "description": description, "params": params}
    except Exception as e:
        logger.warning("refine_task failed, fallback to raw: %s", e)
        params = {}
        if attachments:
            params["附件"] = [a.get("name", "") for a in attachments]
        return {
            "task_type": "other",
            "description": text.strip()[:200],
            "params": params,
        }


async def classify_confirmation(reply: str, chat_cfg: dict | None = None) -> str:
    """判定用户回复：yes(确认执行) / no(取消) / other(其它，保持待确认)。"""
    r = (reply or "").strip().lower()
    if not r:
        return "other"
    # 明确否定预检（2026-08-28 起）：先拦「不+确认词」组合——「不行/不可以/不好/
    # 不确认/不执行/不同意/不开始」都含确认单字（行/可以/好/同意…），若不先拦会被
    # 下面强肯定子串误判为确认执行，用户明确拒绝却触发后台任务烧 token。去空白后
    # 匹配，兼容「不 行」「不 好」等带空格输入。
    compact = re.sub(r"\s+", "", r)
    if any(f"不{n}" in compact for n in ("行", "可以", "好", "确认", "执行", "同意", "开始")):
        return "no"
    # 快速路径：明确关键词直接判定（免 LLM 调用）
    # 顺序：强肯定 → 强否定 → 弱否定 → 弱肯定（「不是」含「是」、强否定先于弱肯定；
    # 「没问题」含「没」、强肯定先于弱否定）
    if any(w in r for w in _STRONG_CONFIRM):
        return "yes"
    if any(w in r for w in _STRONG_CANCEL):
        return "no"
    if any(w in r for w in _WEAK_CANCEL):
        return "no"
    if any(w in r for w in _WEAK_CONFIRM):
        return "yes"
    try:
        prompt = (
            "用户之前被问到「是否执行这个任务」。现在用户回复：\n"
            f'「{reply}」\n'
            "请判断用户意图，只返回一个词：yes=确认执行任务，no=取消/拒绝任务，other=不确定/追问其它。"
        )
        raw = await llm.chat([{"role": "user", "content": prompt}], chat_cfg, thinking="low")
        w = (raw or "").strip().lower()
        if w.startswith("yes"):
            return "yes"
        if w.startswith("no"):
            return "no"
        return "other"
    except Exception as e:
        logger.warning("classify_confirmation failed, default other: %s", e)
        return "other"
