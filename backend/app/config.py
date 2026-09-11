"""全局配置：所有环境变量在此汇总，供各模块引用（文档中 `config.xxx` 即本模块）。

- 本地开发可直接用默认值；Docker 部署时由 compose 注入环境变量覆盖（见 06）。
- 模型库（settings）与 LLM 相关配置见 app/core/llm_provider.py。
"""
import os


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


# ---------- 数据库 ----------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./xiaona.db")
# compose 内：postgresql://xiaona:PASS@postgres:5432/xiaona（见 06 §3）

# ---------- Chroma ----------
CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8001"))  # compose 内为 8000，本地直连为 8001
CHROMA_COLLECTION_PREFIX = "user_"  # collection 按 user_id 隔离：user_{user_id}

# ---------- Neo4j ----------
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "xiaona_pass")
KG_MERGE_THRESHOLD = _float("KG_MERGE_THRESHOLD", 0.88)  # 实体去重余弦阈值（03 §5）

# ---------- LLM ----------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# 默认聊天模型（未在设置页分配时的兜底；OpenAI 兼容协议，Ollama 也走该协议）
DEFAULT_CHAT_MODEL = os.getenv("DEFAULT_CHAT_MODEL", "gpt-4o-mini")
DEFAULT_CHAT_BASE_URL = os.getenv("DEFAULT_CHAT_BASE_URL", "https://api.openai.com/v1")
# 默认记忆抽取模型与向量化模型
DEFAULT_EXTRACT_MODEL = os.getenv("DEFAULT_EXTRACT_MODEL", "gpt-4o-mini")
# 向量化：Chroma 向量由设置页分配的 embedding 模型生成（OpenAI 兼容 /v1/embeddings，
# 见 02 §8）。EMBEDDING_MODEL 仅为向后兼容保留（chroma_client 已不再引用，
# 2026-08-23 起不再硬编码 sentence-transformers）
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")

# ---------- JWT 认证 ----------
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = _int("JWT_EXPIRE_HOURS", 24 * 7)

# ---------- 记忆/遗忘参数（02 §5） ----------
ARCHIVE_THRESHOLD = _float("ARCHIVE_THRESHOLD", 0.25)  # 权重低于该值触发归档
MEMORY_LAMBDA = _float("MEMORY_LAMBDA", 0.05)
WEIGHT_ALPHA = _float("WEIGHT_ALPHA", 0.3)   # recency
WEIGHT_BETA = _float("WEIGHT_BETA", 0.3)     # frequency
WEIGHT_GAMMA = _float("WEIGHT_GAMMA", 0.25)  # importance
WEIGHT_DELTA = _float("WEIGHT_DELTA", 0.15)  # emotion

# ---------- 数据管理（09） ----------
MAX_IMPORT_SIZE = 50 * 1024 * 1024  # 50MB 上限

# ---------- 飞书（10） ----------
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_VERIFY_TOKEN = os.getenv("FEISHU_VERIFY_TOKEN", "")
FEISHU_ENCRYPT_KEY = os.getenv("FEISHU_ENCRYPT_KEY", "")
FEISHU_BOT_OPEN_ID = os.getenv("FEISHU_BOT_OPEN_ID", "")
FEISHU_BOT_NAME = os.getenv("FEISHU_BOT_NAME", "XIAONA")
# 账号绑定状态机 TTL 与验证码策略均在 auth/feishu 模块内，无独立配置项

# ---------- 验证码（mock 模式直接打印/返回） ----------
SMS_MOCK = os.getenv("SMS_MOCK", "1") == "1"  # mock 阶段不发真实短信

# ---------- 遗忘定时任务 ----------
FORGET_INTERVAL_MINUTES = _int("FORGET_INTERVAL_MINUTES", 60)  # 定时遗忘任务间隔

# ---------- 多 Agent 任务（02 §5） ----------
# 任务产物落盘目录：backend/artifacts/{user_id}/{task_id}/
ARTIFACTS_DIR = os.getenv("ARTIFACTS_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "artifacts"))
# 产物保留天数：超过该天数的产物文件/空任务目录由每日清理任务删除（04 §9 / 05 §9）
ARTIFACTS_TTL_DAYS = _int("ARTIFACTS_TTL_DAYS", 30)
# 执行者 function calling 循环最大迭代次数（防模型陷入无限工具调用）
AGENT_MAX_TOOL_ROUNDS = _int("AGENT_MAX_TOOL_ROUNDS", 6)
# 审核打回重做最大次数（02 §6：打回且 retry_count < MAX_RETRY 时重做）
AGENT_MAX_RETRY = _int("AGENT_MAX_RETRY", 2)

# ---------- 电商数据源（02 爬取 §7.1） ----------
# Etsy 官方 Open API v3 key（到 Etsy 开发者后台注册）；未配置时 fetch_etsy_trending 返回降级错误
ETSY_API_KEY = os.getenv("ETSY_API_KEY", "")
# Pinterest API v5 Bearer token（到 Pinterest 开发者后台注册 app 后 OAuth 获取，scope user_accounts:read）；
# 未配置时 fetch_pinterest_trends 返回降级错误（2026-08-24 起）
PINTEREST_API_TOKEN = os.getenv("PINTEREST_API_TOKEN", "")
