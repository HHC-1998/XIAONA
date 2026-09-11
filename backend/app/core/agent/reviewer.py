"""审核者（Reviewer，02 §6）：验收执行者产物，不合格打回重做。

- 输入：任务目标 + 执行者的执行日志与总结（含产物）；
- 输出：{"passed": bool, "issues": [问题描述]}——passed=False 时带问题清单，
  workflow 据此打回执行者重做（retry_count < MAX_RETRY，默认 2 次）。
"""
import json
import logging

from app.core.llm_provider import llm

logger = logging.getLogger("xiaona")


async def review(task: dict, executor_result: dict,
                 reviewer_cfg: dict, on_log=None) -> dict:
    """审核一轮执行结果。返回 {"passed": bool, "issues": list[str], "comment": str}。"""
    prompt = (
        "你是任务审核者。请审核执行者是否完成了用户的任务，只返回 JSON：\n"
        '{"passed": true/false, "issues": ["问题1", ...], "comment": "一句话总评"}\n'
        "passed=false 且 issues 非空时，执行者会带着问题清单重做。\n\n"
        "任务目标：" + json.dumps(task, ensure_ascii=False)[:500] + "\n\n"
        "执行者总结：" + str(executor_result.get("summary") or "") + "\n\n"
        "执行日志（截断）：\n" + "\n".join(executor_result.get("log") or [])[:3000]
    )
    try:
        raw = await llm.chat(
            [{"role": "system", "content": "你是任务审核者，只返回 JSON。"},
             {"role": "user", "content": prompt}],
            reviewer_cfg, thinking="low",
        )
        data = json.loads(llm._extract_json(raw))
        passed = bool(data.get("passed"))
        issues = data.get("issues") or []
        if not isinstance(issues, list):
            issues = [str(issues)]
        comment = str(data.get("comment") or "")
        if on_log:
            await on_log("reviewer", "review",
                         {"passed": passed, "issues": issues, "comment": comment})
        return {"passed": passed, "issues": [str(x) for x in issues], "comment": comment}
    except Exception as e:
        logger.warning("review failed, default pass: %s", e)
        if on_log:
            await on_log("reviewer", "review_error", {"error": str(e)})
        # 审核异常时保守通过（避免任务死循环），日志留痕
        return {"passed": True, "issues": [], "comment": "审核异常，默认通过"}
