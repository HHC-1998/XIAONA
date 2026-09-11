"""多 Agent 任务执行（02 §5/§6）：交互者/执行者/审核者三模型分工。

- director：意图分类 + 需求提炼 + 确认判定；
- executor：function calling 调工具干活；
- reviewer：验收产物、不合格打回；
- workflow：渠道分流 + 后台执行 + 会话留痕；
- service：接入 chat.py / feishu.py 的统一入口（agent_service.handle）。
"""
from app.core.agent.service import HandleResult, agent_service

__all__ = ["HandleResult", "agent_service"]
