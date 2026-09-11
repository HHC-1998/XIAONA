# XIAONA · 小娜

> A personal AI agent system with long-term memory, knowledge graph, real-time voice interaction, and multi-agent task execution.

面向个人的 AI 智能体系统：具备**三层长期记忆**、**知识图谱**、**实时语音对话**、**桌面助手**、**飞书机器人**、**CLI 客户端**，以及**多 Agent 任务执行**（数据采集 / 文档生成）能力。

---

## 功能特性

### 单 Agent 聊天助手
- 流式对话（SSE），思考过程与最终回复**逐字打字机**呈现，深度思考独立折叠展示；
- **三层长期记忆**：最近消息 + 向量语义检索（Chroma）+ 知识图谱子图（Neo4j）；
- 会话自动生成标题、侧边栏管理、跨页面保持当前会话、会话导出（Markdown / JSON）；
- 回复风格精简精准，思考深度按来源分流（网页端深度思考 / 桌面助手浅度思考）。

### 知识图谱
- 从对话中自动抽取实体与三元组，构建个人关系网；
- 3D 星空风格可视化：自我节点居中、悬停显示关系类型、可点击查看节点详情。

### 实时语音输入
- 聊天页：浏览器原生 **Web Speech API** 边说边出字；不支持时**自动降级**为 MediaRecorder + VAD 分段识别；
- 桌面助手：纯自动监听（VAD 断句），**可随时打断**助手说话，句级流式 TTS 语音回复；
- 语音转写统一走后端 `/api/voice/transcribe`，未配置云端 Key 时自动使用本地 **faster-whisper**。

### 多类型文件上传
- 支持图片（多模态直接识别）、PDF、Word、Excel、纯文本；
- 单文件 ≤ 10MB，一次最多 5 个；音频与超大文件按聊天式友好回复处理。

### 飞书 IM 机器人
- 公网 Webhook 接入（签名验证 + AES 事件解密），支持单聊与群聊 @；
- **账号绑定**：手机号 + 密码 + 短信验证码三步验证，只服务已绑定用户；
- 群聊未 @ 的图片/文件仅记录元数据，按需才下载解析。

### CLI 客户端
- 轻量命令行对话入口，可指定服务端地址。

### 多 Agent 任务执行
- 意图识别 → 反问确认 → 执行者/审核者多轮协作 → 交付产物；
- **数据采集**：Amazon Best Sellers 榜单（支持多站点与价格/评分/评论数/颜色/材质/排除词过滤）、Etsy 热门商品、Pinterest 趋势词；
- **文档生成**：Word / PDF / Excel，产物自动落盘并可下载；
- **正文直通优先**：粘贴长文生成文档时由规则渲染原文、LLM 不重写内容，保证逐字保真。

---

## 技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | FastAPI 0.115 · SQLAlchemy 2.0 · Pydantic v2 · Uvicorn |
| 前端 | React 18 · Vite 5 · Tailwind CSS 3 · react-force-graph-3d · react-markdown |
| 数据 | PostgreSQL 15 · Chroma 1.0（向量）· Neo4j 5（知识图谱） |
| 语音 | Web Speech API · faster-whisper · edge-tts · ffmpeg |
| 文档 | reportlab（PDF）· python-docx（Word）· openpyxl（Excel）· pypdf |
| 编排 | Docker Compose（PostgreSQL / Chroma / Neo4j / 后端 / 前端 五服务一键启动） |

---

## 快速开始

### 前置要求
- **Docker** 与 Docker Compose v2
- **Python 3.10+**（仅用于运行一键启动脚本）

### 启动步骤

```bash
# 1. 准备环境变量（Windows 用 copy .env.example .env）
cp .env.example .env

# 2. 按需填写 .env 中的配置（数据库密码、Neo4j、模型 API Key、飞书凭据等）

# 3. 一键启动（自动构建镜像并拉起全部服务）
python start.py
```

启动脚本会执行 `docker compose --env-file .env up --build`。首次构建需下载依赖镜像，耗时较长。

`Ctrl+C` 停止服务。

### 访问入口

| 入口 | 地址 |
| --- | --- |
| 前端页面 | http://localhost:8080 |
| 后端 API 文档 | http://localhost:8000/docs |
| 后端健康检查 | http://localhost:8000/api/health |
| Neo4j Browser | http://localhost:7474 |

### 模型配置

系统采用**三模型弹性配置**，在网页端「设置」页添加模型后分配功能：

- **对话模型**（必需）：聊天、任务执行与审核；
- **提炼模型**（可选）：知识图谱三元组抽取、记忆摘要；
- **向量化模型**（可选）：Chroma 语义检索。

兼容 OpenAI 协议的服务均可接入（DeepSeek、Kimi、智谱、通义、OpenAI、Ollama 本地模型等）。本地 Ollama 可直接填 `http://localhost:11434`，后端会自动解析为宿主机地址。

---

## 目录结构

```
XIAONA/
├── backend/                  # FastAPI 后端
│   ├── app/
│   │   ├── api/              # 路由：chat / auth / sessions / memory / settings / voice / feishu / agent / data
│   │   ├── core/             # 核心：memory_engine（三层记忆）· llm_provider · asr/tts_provider
│   │   │   ├── agent/        # 多 Agent：director（意图）· executor · reviewer · workflow
│   │   │   │   └── tools/    # 工具：数据采集 / 文档生成 / 网页抓取
│   │   │   └── ...           # 媒体解析 · 遗忘调度 · 飞书客户端
│   │   └── db/               # PostgreSQL 模型 · Chroma · Neo4j 客户端
│   ├── scripts/              # 备份 / 恢复 / 飞书链路自测
│   └── docker-compose.yml    # 五服务编排
├── frontend/                 # React 18 + Vite 前端
│   └── src/
│       ├── pages/            # 聊天页 · 桌面助手 · 关系网 · 设置 · 登录注册
│       ├── components/       # 输入框 · 消息 · 思考块 · 语音 · 3D 图谱 · Markdown
│       ├── api/              # 后端接口封装（含 SSE 流式解析）
│       └── utils/            # 音频转 WAV · 文本清洗
├── cli/                      # 命令行客户端
├── 项目计划/                  # 设计与实现文档（12 份单 Agent + 5 份多 Agent + 问题台账）
├── artifacts/                # 任务产物落盘目录（运行期生成，不入库）
├── start.py                  # 跨平台一键启动
├── AGENTS.md                 # 开发约定与项目规约
└── .env.example              # 环境变量模板
```

---

## 文档

- [AGENTS.md](AGENTS.md) —— 开发约定、模块规约与常见验证命令（**推荐先读**）；
- `项目计划/01_单Agent聊天助手/` —— 聊天、记忆、图谱、语音、前端、启动、会话、桌面助手、数据管理、飞书、CLI、文件上传；
- `项目计划/02_多Agents执行功能/` —— 多 Agent 总纲、数据源爬取、任务确认与执行、PDF / Word 生成；
- `项目计划/所遇问题及解决方案/` —— 按日期归档的问题台账（现象 / 根因 / 方案 / 验证）。

---

## 安全说明

- `.env` 内含数据库密码与第三方凭据，**已在 `.gitignore` 中排除，请勿提交**；
- 生产环境务必显式配置 `JWT_SECRET`（默认为开发用占位值）；
- 用户数据支持导出为 JSON，清空数据需二次密码确认；
- 飞书绑定信息（密码、验证码）不落库、不进入记忆与日志。

---

## 许可

本项目为私人项目，未附开源许可协议。如需使用请联系作者。
