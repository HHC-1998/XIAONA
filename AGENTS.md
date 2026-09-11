# AGENTS.md — Agent 启动须知

本文件是每个 Agent 会话启动时必须阅读的约定。它约束「如何开发 XIAONA 项目」与「如何反哺文档」。

## 项目结构

```
d:\XIAONA\
├── backend/                 # FastAPI 后端（app/ 源码，docker-compose.yml）
├── frontend/                # React 18 + Vite 5 + Tailwind 前端
├── 项目计划/
│   ├── 01_单Agent聊天助手/   # 12 份原计划/详解文档（01~12）
│   ├── 02_多Agents执行功能/ # 多 Agent 任务子项目群（01 总纲 + 02 爬取详解 + 03 任务确认与执行详解 + 04 生成PDF详解 + 05 生成Word详解）
│   └── 所遇问题及解决方案/   # 按日期归档的问题台账（如 2026年8月15日_所遇问题及解决方案.md）
├── start.py                 # 跨平台一键启动（python start.py，替代原 start.bat/start.sh）
├── .env.example / .env      # 环境变量（DB 密码、Neo4j、OpenAI API Key 等）
└── AGENTS.md                # 本文件
```

## 硬性规则：所有工作必须反哺文档

用户明确要求：**任何代码修改、问题排查、用户问答结论，都必须反哺到文档**，共两层：

1. **反哺原计划文档**：按模块归到 `项目计划/01_单Agent聊天助手/` 下对应文档（如前端改动 → `05_前端详解.md`，会话 → `07_会话历史记录详解.md`，一键启动 → `06_一键启动详解.md`，文件上传 → `12_多类型文件上传详解.md`，等等）。更新对应章节的代码块与说明，保持文档与实现一致。
2. **写当天问题台账**：在 `项目计划/所遇问题及解决方案/` 下创建或追加「当天日期」文档（如 `2026年8月16日所遇问题及解决方案.md`），记录现象、根因分析、解决方案、改动文件清单、验证结论。当天已存在该文档则追加，不存在则新建。

## 常见验证命令

- 前端构建：`cd frontend && npm run build`
- 后端语法：`python -m compileall -q backend/app`（从项目根运行）
- Compose 校验：`docker compose -f backend/docker-compose.yml config --quiet`
- 一键启动：`python start.py`（前台运行，Ctrl+C 停止）
- 访问入口：前端 http://localhost:8080、后端文档 http://localhost:8000/docs、后端健康 http://localhost:8000/api/health

## 前端约定（2026-08-15 起）

- 全站 UI 风格：Tailwind 手写。主色为 indigo→violet 渐变，白底卡片 + 细边框 + 柔和阴影，公共类 `.input-base` / `.btn-primary` / `.btn-secondary` 定义在 `frontend/src/index.css`。
- **依赖例外（2026-08-15 起）**：前端 Markdown 渲染引入 `react-markdown@^10` + `remark-gfm@^4`；后端多类型文件上传引入 `pypdf` + `python-docx` + `openpyxl`（均为用户确认的例外）。除此外仍不引入新依赖。
- 认证状态单一来源：登录/注册成功后必须调用 `useAuth().login(token, user)`（更新全局 user），不能只写 localStorage，否则 `AuthRoute` 会把用户弹回登录页。
- 历史会话不再有独立页面（`/history` 已移除），统一在聊天页左侧可收起/可删除的会话侧边栏中展示。
- 关系网 3D 图为白色背景（`Graph3D.jsx` 的 `backgroundColor="#ffffff"`），勿改回深色。
- **删除当前会话清 URL（2026-08-16 晚起）**：`Chat.jsx` 删除当前会话后 `setSearchParams({}, {replace:true})` 清掉 URL 的 `session_id`，防止刷新后被旧参数"复活"回已删会话。
- **`onSession` 只同步不中断（2026-08-16 晚起）**：`streamChat.onSession` 首帧回调只更新 `sessionIdRef` + URL + sessionStorage，**不 setSessionId**（否则触发会话 effect 的 abort 中断在途 SSE 流，与日志 499 吻合）；发送/刷新侧边栏一律用 `sessionIdRef.current`。
- **ChatInput IME/双击锁（2026-08-16 晚起）**：Enter 发送须 `!e.nativeEvent.isComposing && e.keyCode !== 229`（中文选词不误发）；`sendingRef` 发送锁防双击/连点重复提交。
- **输入框 textarea 换行保真（2026-09-01 起）**：`ChatInput.jsx` 与 `DesktopAssistant.jsx` 的输入框均为 **`<textarea>` 多行控件**（`rows={1}` + `onInput` 按 scrollHeight 自动增高，`resize-none overflow-hidden`），**Enter 发送、Shift+Enter 换行**——旧单行 `<input>` 会把粘贴的多行文本剥掉所有 `\n`，导致 DB 用户消息零换行、文档工具识别不出表格/标题（"生成 Word 一坨文字"根因，见 2026-09-01 台账三）；勿回退单行 input。后端 `generate_pdf/docx` 的 `_parse_markdown_blocks` 据此对纯文本表格（Tab/全角/单空格对齐，**数据行列数与表头一致（容差 ±1）即可，含中文对照表**；表头含金额词时数据行末列须为数字；标题行不吞进表格）与中文编号标题（`一、`/`第X章`/`（一）`→H1，`1.1` 短行→H2）做结构识别渲染，`title` 空时取 content 首个非空行自动提取（文件名语义化兜底）。
- **VoiceRecorder 末句保留（2026-08-16 晚起）**：`lastInterimRef` 记录最近完整文本，`currentText() = finalRef || lastInterimRef`，发送瞬间 Web Speech interim 末句不再丢失（此前 resolve 的是 finalRef，末句可能没走 final）。
- **AuthContext 仅 401 登出（2026-08-16 晚起）**：`GET /api/auth/me` 仅 401 清 token 登出；5xx/断网保留 token、不覆盖登录态，下次刷新自动重试。
- **`streamChat` 必须传 `source`（2026-08-16 起）**：`streamChat({message, sessionId, source='web', files, ...})` 默认 `web`，桌面助手 `sendTurn` 传 `source: 'desktop'`（后端按 source 分流 thinking=low 浅度思考）；**禁止恢复硬编码 `source='web'`**（曾导致桌面助手永远走 web→high 深度思考、回复变慢，见当天台账二十七）。
- **消息拉取竞态防护（2026-08-16 起）**：`Chat.jsx` 加载会话消息用 `loadSeqRef` 请求序号，响应返回时 `seq !== loadSeqRef.current` 则丢弃（快速切会话时旧响应不覆盖新消息），并加 `.catch` 记日志。
- **桌面助手 busy 守卫（2026-08-16 起）**：`sendTurn` 开头 `if (busyRef.current) interrupt()`——语音流在途时文字输入/转写并发先打断旧流，防两个 SSE 并存内容串扰；卸载 cleanup 除 `stopListening()` 外必须 `abortRef.current?.abort()`（防卸载后 setState-on-unmounted）。
- **设置页 `assignModel` flag key（2026-08-16 起）**：`func.replace('_model_id', '')` 得 `chat/extract/embedding` 前缀（`chat_model_id` → `chat_configured`），**勿用 `replace('_id','')`**（会拼成 `chat_model_configured`，乐观更新提示行永不刷新）。
- **绑定手机号 mock 验证码（2026-08-16 起）**：`Settings.jsx` 的 `sendBindCode` 要 `res.code` 自动填入绑定验证码框 + alert 提示（与 Login/Register 一致），勿丢弃返回的 mock 验证码。

## 聊天页实时语音输入约定（2026-08-15 起）

- `VoiceRecorder` 主路径用浏览器原生 **Web Speech API**（`webkitSpeechRecognition`，`continuous + interimResults=true`），说话时实时回调 `onInterim(全文)` 整串写入输入框；Firefox/Safari 不支持时**降级**为 MediaRecorder + 后端 `/api/voice/transcribe` 识别（不能改回"必须点结束才出字"）。
- **降级路径为 VAD 分段"边录边出字"**：`getUserMedia` 后建 `AudioContext` + `AnalyserNode`，rAF 循环用时间域 RMS + 噪声底 EMA/clamp + 迟滞阈值检测音量（与桌面助手同参数，见「桌面助手约定·VAD」）；有声音 → `recorder.start()` 开录；**停录判定用 `level < offTh` 迟滞**（offTh~onTh 中间区段保持录音，2026-08-16 晚修正，此前只看 `<= onTh` 会切段过勤）；静音持续 `SILENCE_MS=1200` → `stop()` 当前段 → 后端转写 → 追加上屏 → 继续录下一段。`network / service-not-allowed / audio-capture` 错误、`start()` 抛错都自动切到该降级路径（提示"已自动切换本地识别"），不能只报错不降级。
- **降级路径 AudioContext 用户激活兜底（2026-08-23 起）**：`startMediaRecorder` 建 ctx 后 `suspended` 先 `await resume()`，仍不 `running` 注册一次性 `pointerdown/keydown` 手势监听（`gestureResumeRef` 记录，`stop()`/卸载 cleanup 移除）恢复；`vadLoop` 每帧顶部再 resume 兜底——与桌面助手同参数（getUserMedia 权限弹窗可能耗尽浏览器 5s "用户激活"窗口致 ctx 挂起、VAD 音量恒 0 听不到，聊天页此前缺此兜底导致降级后"录音中但不出字"，见当天台账四）。
- **Web Speech 失败记忆（2026-08-23 起）**：`rec.onerror` 的 `network/service-not-allowed/audio-capture` 置 `webSpeechFailedRef=true`，仅首次显示温和提示「已自动切换本地识别模式」（勿弹吓人的「语音服务不可用」）；`start()` 在 `webSpeechFailedRef=true` 时直接走 `startMediaRecorder`——国内网络 Chrome 下 Web Speech 必失败，本会话后续点击直达本地识别，不再反复试 Google 服务。
- **降级路径转写前先转 WAV（2026-08-16 起）**：每段 `onstop` 先 `blobToWav16k(blob)`（`frontend/src/utils/audio.js`）解码为 16kHz 单声道 WAV 再 `voiceApi.transcribe(wav)`——ffmpeg 7.x 无法解析 Chrome MediaRecorder webm（AVERROR_EOF 187），见「后端 ASR 约定」与当天台账九。`blobToWav16k` 复用模块级共享 `AudioContext` 单例（`getSharedCtx()` 懒建不 close），避免每段录音新建 ctx 的初始化开销（2026-08-16 晚起）。
- **自动关闭**：录音中点击麦克风按钮之外任意处（输入框/页面空白/发送按钮）或切换路由（组件卸载）自动 `stop()`，保留已识别文字；录音态通过 `onRecordingChange` 同步给 ChatInput 做视觉反馈（红框 + 呼吸圈）。
- **一次点击「关麦并发送」（2026-08-16 起）**：录音中点击发送按钮 = 一次完成「关麦 + 发送」。`VoiceRecorder` 用 `forwardRef` + `useImperativeHandle` 暴露 `stopAndGetText(): Promise<string>`（幂等复用同一 Promise，8s 兜底超时；`stopInFlightRef` 标记停止流程中，避免与 `pointerdown` 自动 `stop()` 重复收尾），`ChatInput.handleSend` 在录音中先 `await` 它拿最终语音文本（Web Speech 经 `rec.onend`、MediaRecorder 经 `recorder.onstop`→转写完成统一 `flushTextAndResolve` resolve）再发送——否则 `pointerdown` 自动停止的文本确认是异步的，`click` 发送读到旧值导致第一次点击只关麦、第二次才发送。`canSend` 在录音中恒为 true（识别为空则不发送）。
- **语音发送后清理输入框 + 追加模式（2026-08-23 起，2026-08-24 补 onresult 拦截与追加）**：`VoiceRecorder` 有 `sentRef` + `markTextSent()`，`emitText` 开头拦截 `if (sentRef.current) return;`，`start()` 重置标志；**Web Speech 主路径 `rec.onresult` 开头同样加 `if (sentRef.current) return;`**——onresult 直接 `onInterim` 绕过 emitText，8-23 只拦了 emitText 路径，迟到的 final 仍会把文字写回输入框（2026-08-24 补）。`ChatInput.handleSend` 发送成功后调 `voiceRecorderRef.current?.markTextSent?.()` 并 `setText('')`。**追加模式（2026-08-24 起）**：语音转写是「输入框已有内容之外」的新文本——`ChatInput` 新增 `voiceBaseRef`，录音开始（`onRecordingChange(true)`）记录 `voiceBaseRef.current = text`，`onInterim={(t) => setText(voiceBaseRef.current + t)}` 追加；多轮录音无限累加；录音中发送时完整内容 = `voiceBaseRef.current + voiceText`（勿用 `text` state——`await stopAndGetText` 期间 React setState 未提交、读到旧值）；**只有点一次发送才全清输入框**（见当天台账十四）。
- **竞态与卸载保护**：`startGenRef` 代际计数防 `getUserMedia` 异步期间被 stop/卸载后泄漏流；`aliveRef` 卸载保护——组件卸载后 `onstop` 转写回调不再刷新输入框（防 setState-on-unmounted）。

## 会话软删除约定（2026-08-15 起）

- 聊天界面删除会话 = **免密软删除**（`DeleteConfirm` 传 `requirePassword={false}`，`DELETE /api/sessions/{id}` 无 body）。仅置 `sessions.is_deleted=True`，SQL/Chroma/KG 三层数据全部保留，供记忆检索与模型思考继续使用。
- 设置页「清空数据」（`POST /api/data/clear`）仍是**需密码的硬删除**，会连软删会话一起清掉。
- **清空数据按 FK 依赖逆序删除（2026-08-24 起）**：`clear_messages_and_sessions` 依次删 `AgentRunLog(task_id→agent_tasks)` → `AgentTask(session_id→sessions)` → `FeishuSessionMap(session_id→sessions)` → `Message` → `Session`（均带 `user_id` 过滤 + `asyncio.to_thread`）。**禁止只删 messages→sessions**——`agent_tasks`/`feishu_session_map` 对 `sessions.id` 无 `ON DELETE CASCADE`，删 sessions 会 IntegrityError 整个事务回滚（见当天台账）。
- 后端所有会话查询接口与 `memory_engine.ensure_session` 必须过滤 `is_deleted=False`，防止软删会话被"复活"继续写入。
- 修改 `Session` 模型加列时，记得同步 `init_db()` 的 `_apply_light_migrations()`（`create_all` 不会给已有表补新列）。

## 知识图谱自我节点约定（2026-08-15 起）

- 每个用户注册时在 Neo4j 创建 `is_self=true` 的 Person 自我节点（名称=昵称，无昵称则"用户+手机号"），`neo4j_client.ensure_self_node` 幂等，register/login 都兜底调用，Neo4j 未就绪不阻断注册。
- `get_full_graph` 必须返回孤立节点（含自我节点），否则新用户图谱是空的。
- **`import_graph` 幂等（2026-08-24 起）**：导入前先 `clear_graph(user_id)`（备份恢复语义，天然幂等，重复导入不复制节点）；导入节点时**剥离 `is_self`/`is_bot` 特殊属性**（自我/机器人节点由 `ensure_self_node`/`consolidate_bot_refs` 兜底），防复制不变量节点。
- `GET /api/memory/graph` 查询前必须兜底 `ensure_self_node`（兼容自我节点功能上线前的旧账号），保证任何账号打开图谱页至少有 1 个节点。
- 图谱页全屏布局：`main` 用 `relative`，`Graph.jsx` 顶层 `absolute inset-0`，`ResizeObserver` 量取容器尺寸显式传给 `Graph3D width/height`，图大小用滚轮/3D 相机缩放控制、不产生页面滚动条。
- **自我节点居中布局（2026-08-15 起）**：`Graph3D.jsx` 用 `graphData` 预处理——自我节点 `fx/fy/fz=0` 固定原点 + `val=40` 大质量当引力中心，其它节点初始分布在半径 60~100 随机球壳上；`cameraPosition={{x:0,y:0,z:150}}` + `onReady` 里 `centerAt([0,0,0],0)` + `camera().lookAt(0,0,0)` 让相机对准原点，`autoRotate` 绕自我节点自转。**星空简洁风（2026-08-15 晚改）**：节点小（`nodeRelSize=1.6`、自我节点发光球半径 2.4/1.7）、边为静态细线（`linkColor=rgba(99,102,241,0.55)`、`linkWidth=1.5`，**去掉箭头/流动粒子/曲率**），`linkLabel` 悬停显示 `关系：${type}`、`linkHoverPrecision=10` 保证易命中。
- **机器人节点归并（2026-08-15 晚起）**：对话中「你/小娜/XIAONA」指代聊天机器人，`neo4j_client.ensure_bot_node` 建 `is_bot=true` 的 Person 节点（名称=小娜），`merge_entity` 在自称归并后判断 `resolve_bot_ref`，把「你」等指代归并到机器人节点；`GET /api/memory/graph` 查询前兜底 `consolidate_bot_refs` 把历史孤立「你」节点的关系转移后删除。图谱不再出现突兀的「你」节点。
- **Neo4j 5 取节点 id 必须用 `elementId(x)` 函数（2026-08-15 起）**：Cypher 查询字符串里 `x.element_id`（属性语法）返回 `NULL` 并告警 `UnknownPropertyKeyWarning`，导致 `merge_*` 拿不到 id、`link()` 匹配不到节点——**边全部静默建不出来、关系网一直只有节点没有边**（见当天台账五十六）。凡 Cypher 内取 id 一律 `RETURN elementId(x) AS id` / `WHERE elementId(x) = $id`；仅 Python 驱动对象属性 `rec["n"].element_id`（如 `get_full_graph` 组装返回）保持原样。前端 `links` 为空时先查 Neo4j 是否真有边，勿只调前端参数。

## 桌面助手约定（2026-08-15 起）

- 桌面助手问答统一归档到固定会话：`localStorage['desktop_session_id']` 复用，无则 `sessionsApi.create('desktop')`。流式 `POST /api/chat/stream` **首帧回传实际 `session_id`**，前端 `onSession` 回调同步最新会话 id（防软删后碎片化）。
- **转写先转 WAV（2026-08-16 起）**：`recorder.onstop` 先 `blobToWav16k(blob)` 解码为 16kHz 单声道 WAV 再 `processVoice(wav)`（ffmpeg 7.x 解析 Chrome MediaRecorder webm 报 AVERROR_EOF(187)、转写全线 500 的修复，见「后端 ASR 约定」与当天台账九）；`processVoice` catch 优先展示后端 `detail`，仅真实网络错误才显示「网络异常」。
- **纯自动监听**：桌面助手无手动开始/停止按钮，打开页面即自动监听、离开页面（组件卸载）自动停止并释放麦克风；`stopListening` 时 `pause()` 中断播放中的回复。`getUserMedia` 用 `{echoCancellation:true, noiseSuppression:true, autoGainControl:true}` 请求 AEC（浏览器消除 TTS 回声，与打断/自打断修复双保险；个别设备不支持对象约束则降级重试）。
- **启动顺序（VAD 首帧防护）**：`startListening` 中**必须先赋值 `mediaRecorderRef.current = recorder` 再 `setupVAD(source)`**——`setupVAD` 首帧同步执行 `checkVolume`，recorder 未就绪会访问 `null.state` 抛 TypeError，导致监听启动崩溃、页面永远「已停止监听」。建 AudioContext → 建 recorder → setupVAD 整段包 try/catch（失败释放流 + 提示 + 重试按钮）；`checkVolume` 顶部对 `mediaRecorderRef.current` 为 null 直接重排 rAF 返回。
- **AudioContext 用户激活兜底**：`getUserMedia` 权限弹窗可能耗尽浏览器 5s "用户激活"窗口致 `resume()` 静默失败（context 仍 suspended → VAD 音量恒 0 → 听不到）；`resume` 后若 `state !== 'running'`，注册一次性 `document` `pointerdown/keydown` 监听器，用户点击页面时 `resume()` 并自移除（`stopListening`/卸载时移除，防泄漏）。
- **切页/失焦即关麦**：`visibilitychange`（`document.hidden`）与 `window.blur` 时立即 `stopListening()` 关麦，回焦自动恢复监听（幂等防重复）。停止流程：`cancelAnimationFrame`（VAD rAF id 存 `rafIdRef`）→ 若录音中先 `recorder.stop()`（在途对话经 onstop→processVoice 落库不中断）→ 停 `streamRef` 的 tracks 第一时间关麦 → 关 AudioContext。卸载置 `aliveRef=false`，TTS 播放前检查之，不播声音但已落库。
- **可打断的实时对话**：思考/回复期间持续监听（不屏蔽 VAD）。`busyRef`（思考中）+ `suppressRef`（TTS 播放中）为 true 时，检测到用户语音即 `interrupt()`：`genRef+1` 令在途 `processVoice`/`sendTurn` 过期、**`abortRef.abort()` 中断 SSE 流式请求**、`pause()` 停止回复、清空句级 TTS 队列（`speechQueueRef`）、**固定被中断一轮的最终状态**（把剩余未揭晓思考并入累积，保留已生成的思考+文字到面板并标「（已打断）」，仅删除完全空白占位气泡）、若合并缓冲还有未发送内容则重新挂合并窗口（防句子永久滞留）、立即录新语音。`processVoice`（转写）与句级 TTS 播放链每个 await 后校验代际与 alive，被打断则丢弃旧回复不播放（已发后端对话照常落库）。**打断"确认即开录" + 累积判定（2026-08-16 起）**：检测到超阈值**第一帧就 `recorder.start()`**（用户开口不丢）；打断判定用 `interruptAccumRef` **累积+半速衰减**——超阈值帧 `+= 实际帧长`、低于阈值帧 `-= 帧长/2`（不清零；帧长取 rAF 时间戳差值，120Hz/144Hz 屏不再把每帧当 16ms 累加，2026-08-16 晚修正防误打断），累积到 `INTERRUPT_CONFIRM_MS=200` 才 `interrupt()`，自然说话的字间隙不清零、鼠标点击（短促尖峰）衰减后到不了阈值（见当天台账八/十/十一）；瞬态段丢弃仅当 `belowSinceRef`（低于阈值）>400ms 且累积已清零才 `stop+drop`。思考时打断阈值 `min(0.08, max(0.03, noiseFloor×4))`；TTS 播放中改**增量式突发**：`baseAvg`（含回复回声，慢 EMA，仅非语音帧更新）→ `burstTh = baseAvg + max(0.02, baseAvg×0.5)`，用户声音叠在回声上相对基线的增量超过即判定说话（替代倍数 `×2.2`——回声大时倍数阈值过高导致"打断不了"）；**预热期 300ms**（`suppressStartRef`）**每帧都做 baseAvg EMA、不累加打断量、不开录、不判定打断**（2026-08-16 晚落实：此前实现在预热期照常累加，回复起播约 0.3s 累积满 200 触发 `interrupt()`——助手每播一句话就打断自己、回声段又被当成用户说话转写 → 无限循环/胡言乱语，见当天台账十四），防回复开头回声自打断。
- **VAD 用时间域 RMS + 自适应噪声底 EMA/clamp + 迟滞阈值（勿回退频域平均、勿回退单调取最小）**：`analyser.fftSize=1024`、`getFloatTimeDomainData` 填满整个数组算 RMS。噪声底仅在**非语音帧**校准 `noiseFloor = clamp(noiseFloor*0.95 + level*0.05)`（clamp 0.005~0.03，初始 0.01），环境变化能跟上、不被噪声/回声一次性锁高；开录/停录分离迟滞 `onTh = min(0.06, max(0.02, noiseFloor×3))` / `offTh = onTh×0.7`，中间区段保持录音，消除阈值附近抖动导致的碎片段（"一句话被拆几句"根因，见当天台账十）。**禁止** `new Uint8Array(analyser.fftSize)` + `getByteFrequencyData`——频域数据只填前 `frequencyBinCount=fftSize/2` 个元素、后一半恒 0，音量平均被稀释一半，阈值 30 实际等效 60，人声很难触发（"显示监听中但不响应说话"根因，见当天台账四十八）。`processVoice` 空转写（`!text`）静默恢复「正在监听...」，不显示「出错」。
- **一句话完整说完 + 防语音重叠（2026-08-16 起）**：段级静音 `SILENCE_MS=1600` 切段并转写（2026-08-16 晚由 1.2s 提到 1.6s——中文口语换气/想词停顿的常见上限，勿改回 2.5s——拆分由合并窗口愈合）；**多句合并统一回复**：段转写结果**追加进 `pendingTextRef` 缓冲**并挂 `MERGE_WINDOW_MS=2500` 合并窗口，静默到点 `flushPendingTurn` 把累积所有句子作为**一轮**统一 `sendTurn`（修"一句话被拆成多个回复"，见当天台账十一/二十三）；**录音中/有声音帧一律取消合并窗口**（含 offTh~onTh 轻声区，防轻声说话被截断，2026-08-16 晚审查修复；2026-08-16 深夜起删掉「短确认窗立即 flush」分支——转写完成一律挂满 2.5s 窗口，与用户"停顿 2.5s 自动发送"心智对齐）；**用户说话实时上屏**：段转写完成即通过 `activeUserTurnIdRef` 创建/追加转录面板用户消息（边说边上屏），`flushPendingTurn` 不再封口、改由 `sendTurn` 统一封口（文字输入路径也在 sendTurn 创建 user turn，见当天台账十二）；缓冲 >`MAX_PENDING_LEN=500` 立即 flush；`interrupt()` 若缓冲还有未发送内容重新挂窗口（防句子永久滞留）；`stopListening()` 取消定时器但**保留 pendingTextRef**、`startListening` 回来重新挂窗口（切页不丢已听内容）。最小有效语音时长 `MIN_VOICE_MS=300`（短噪声段 300ms 即丢）；`recorder.start()`/`interrupt()` 时记录 `recStartTimeRef`。**TTS 播放前必须停旧 audio**：`sendTurn`（用户新话）与 `kickSpeech`（播下一句前）都要 `if (audioRef.current) { audioRef.current.pause(); audioRef.current = null; }`，否则新旧回复语音重叠同时播（见当天台账五十）。
- **流式 + 转录面板**：桌面助手回复走 `streamChat`（SSE），每轮「用户话 → ThinkingBlock → Markdown 回复」渲染在麦克风下方转录面板；面板容器 `ref={transcriptRef}`，`turns/streamingId` 变化时 `scrollTop = scrollHeight` **自动滚动跟随最新**。聊天页消息列表同理由 `messagesRef` + `messages/streamingId` effect 自动滚动（见当天台账五十一）。
- **错误提示展示具体原因（勿笼统「出错请重试」）**：桌面助手 `onError(msg, code)` 用 `friendlyError(raw, code)` 映射（`chat_model_not_configured`→配置引导、`network_error`→「网络异常，请确认后端服务已启动」，其余直用后端中文友好句如「模型响应超时」）；转写/建流 `catch` 同样给中文提示（见当天台账五十三）。
- **TTS 去 Markdown + 去 emoji**：播放前必须 `stripMarkdown(reply)`（`frontend/src/utils/text.js`）清洗 Markdown 符号并过滤 emoji（`\p{Extended_Pictographic}` + ZWJ/VS16）；后端 `/api/voice/tts` 另有 `_strip_markdown` 正则 + `unicodedata` 类别兜底（双保险），防止语音念出 * # 等符号或表情描述文字。清理后空文本兜底合成"嗯"。
- **静音开关/重播/加载历史（2026-08-16 晚起）**：页面提供 🔊/🔇 语音回复开关（静音时 TTS 跳过合成播放、文字照常渲染落库）；「重播上一条」按钮（`lastReplyRef` + 公共 `playReply(text, gen)`，`finish()` 与重播共用，重播不落库）；打开页面时把固定会话最近 40 条消息加载进转录面板（`hist_` 前缀 id，不与本地 turnSeq 冲突），切走再切回能看到历史对话；切段状态用「识别中...」而非常用「思考中...」，`你说：` 超过 50 字截断。
- **句级流式 TTS（2026-08-16 晚提速，深夜 1.25x）**：`onDelta` 按句切分（`lastSentenceBoundary`：。！？!?…换行）入 `speechQueueRef`，`kickSpeech(gen)` 串行逐句合成播放（首句就绪即出声，回复语音不再滞后已流式的文字 2~3s）；`finish()` 尾句入队后由播放链收尾（队列空 + 流结束 → 复位 busy/suppress）；打断/静音/失焦清队列或跳过播放；`interrupt()`/`stopListening()`/`replayLast` 须复位 `speechQueueRef`+`speechPlayingRef`（audio pause 后 onended 不触发，防播放链卡死）。**播放一律 `audio.playbackRate = 1.25`**（`playReply` 重播与 `kickSpeech` 句级播放两处，2026-08-16 深夜由 1.5x 回调——1.5x 太快听不清，见当天台账二十六）。
- **失焦不出声（2026-08-16 晚审查修复）**：`pageFocusedRef` 在 blur/visibilitychange(hidden) 置 false、focus 置 true；`playReply` 与 `kickSpeech` 开头检查，未聚焦只落库渲染不播声音（修复"切到别的标签页时尾段录音触发对话突然冒出回复声"）。
- **文字输入兜底 + 实时音量条（2026-08-16 晚起）**：转录面板下方文字输入框走同一 `sendTurn` 链路（麦克风/识别故障或不想说话仍可对话）；VAD `level` 每 150ms 节流渲染麦克风下方音量条（说话时可见视觉反馈）。
- **后端 TTS 加固（2026-08-16 晚起）**：`tts_provider.synthesize` 加 `asyncio.wait_for` 30s 超时（防 edge-tts 断网挂起）；`/api/voice/tts` 清洗后 >5000 字符返回 422「文本过长」、合成失败返回 422「语音合成失败，请稍后重试」（不再裸 500）；移除 `tts_provider = __import__(__name__)` hack，`voice.py` 直接 `from app.core.tts_provider import synthesize`。

## 三模型弹性配置约定（2026-08-15 起）

- **对话模型必须、提炼/向量化可选**：未配置对话模型时 `memory_engine.chat/chat_stream` 抛 `ChatModelNotConfigured`，SSE 错误帧带 `code='chat_model_not_configured'`；聊天/关系网/桌面助手页显示引导 banner（共享 hook `hooks/useModelStatus.js` 拉取 `GET /api/settings/function-models` 的 `chat_configured` 等布尔）。
- 未配置提炼模型 → 跳过 KG 子图检索与三元组抽取（配置后 `extract_triples` / `extract_entities_from_text` 用该模型）。未配置向量化模型 → 跳过 Chroma query/upsert。**Chroma/embedding/KG 任何异常都降级日志告警，绝不阻断聊天主流程**（2026-08-15 曾因容器内 huggingface 拉 embedding 失败中断整个 chat_stream，见当天台账二十六）。**降级点必须覆盖**：`retrieve_memory` 的 KG 子图检索（`kg.query_subgraph`）、`_mention_hits` 权重刷新（2026-08-15 补兜底，见当天台账五十三）——遗漏会整条 `chat_stream` 中断、前端弹「出错请重试」。
- **向量由配置的 embedding 模型真正生成（2026-08-23 起）**：`chroma_client._embeddings` 调 `{base_url}/v1/embeddings`（OpenAI 兼容，Ollama bge-m3 / 云端均可，`base_url` 已由 `_ensure_v1` 补齐 `/v1`）；`save_chat_message`/`retrieve_memory`/agent `_write_message`/forgetter 摘要向量统一传 `embedding_cfg`（来自 `_model_configs` 的 `embedding` 键），**不再是「配置只是开关、实际用 sentence-transformers」**。未配置则传 `None` 跳过。**禁止回退硬编码 `SentenceTransformer(EMBEDDING_MODEL)`**（初始化联网 huggingface.co 校验，网络不通反复 MaxRetryError，每次写消息/查记忆卡死，见当天台账）。旧 collection 384 维与新模型 1024 维不一致时 `chroma.upsert/query` 捕获维度异常自动 `delete_collection` 重建。
- `_model_configs(db, user_id)` 一次性解析三个分配；`FunctionAssignment` 加字段/改逻辑时注意同步。
- **提炼/遗忘用配置的抽取模型（2026-08-24 起）**：`llm.summarize` 带 `model_config` 参数；`forgetter.process_forgetting` 按消息 `user_id` 解析 `extract_cfg` 传入 `summarize`/`extract_triples`。**勿回退「无参数默认 env 模型」**——本地 Ollama 用户 key 空时会把摘要退化成 `text[:50]` 截断。
- **当前消息不重复注入 LLM 上下文（2026-08-24 起）**：`_persist_user` 返回新消息 `id`，`retrieve_memory`/`get_recent_messages` 接受 `exclude_ids`（`chat`/`chat_stream` 把 `msg_id` 传入）——recent 排除最后一条 user 消息、语义命中过滤 `original_id in exclude_ids`。**勿回退「先落库再无条件取 recent/chroma」**，否则当前用户输入在上下文里出现两次。
- **Chroma `query()` 返回嵌套结构，消费前必须展平**：`query(include=["documents","metadatas","distances"])` 的每个键都是「每 query_text 一个内层 list」的嵌套（`metadatas = [[{...},{...}]]`），把外层当扁平 list 遍历会拿内层 list 调 `.get()` 抛 `'list' object has no attribute 'get'`、整条 `chat_stream` 中断（见当天台账七，预存在 bug 直到用户配置向量化模型才暴露）。统一用 `memory_engine._flatten_chroma(raw)`（取内层 list + 过滤 None）展平后再消费；该 bug 曾把 DeepSeek 云端对话一并搞崩。

## 思考流与 Markdown 渲染约定（2026-08-15 起）

- **思考深度按来源分流（2026-08-16 深夜起）**：`memory_engine.chat/chat_stream` 按 `source` 给 `llm.chat/chat_stream` 传 `thinking` 参数——`desktop`（桌面助手语音）→ `low`（浅度思考，快速回话）；`web`/`cli`/`feishu` → `high`（深度思考，思路更广更准）。`llm_provider._complete/chat/chat_stream` 均透传该参数，注入 `reasoning_effort` 时用 `cfg.get("reasoning_effort", thinking or "high")`，**仅 `base_url` 含 `deepseek.com` 才注入**（Ollama/OpenAI 等不识别该参数，传了 4xx）；Ollama 本次不分流（其 `think` 只有 true/false 开关，见当天台账二十六）。
- `llm.chat_stream` 产出 `(content_delta, thinking_delta)` 元组：思考来自 `reasoning_content`（DeepSeek）或 `thinking`（Ollama），**不允许丢弃思考通道**。
- SSE 帧格式：`data: {"session_id": "..."}`（首帧）→ `data: {"delta": "...", "thinking": "..."}` → `data: [DONE]`；媒体类帧 `data: {"media_reply": "...", "code": "media_unsupported|media_not_supported"}`（聊天式回复，前端按正常气泡渲染）；错误帧 `data: {"error": "...", "code": "..."}`。前端统一用 `frontend/src/api/chat.js` 的 `streamChat` 解析（onSession/onDelta/onThinking/onMediaReply/onError/onDone）。`POST /api/chat/stream` 为 **multipart/form-data**（message/session_id/source 走 Form，files[] 走 File）；非流式 `POST /api/chat` 仍为 JSON（兼容 CLI）。
- 前端渲染：思考过程用 `components/ThinkingBlock.jsx`（折叠块，"深度思考"标题）；最终回复用 `components/Markdown.jsx`（react-markdown），用户看不到原始 Markdown 符号。
- **桌面助手不渲染思考块（2026-08-19 起）**：`ChatMessage` 增加 `showThinking` prop（默认 true），桌面助手转录面板传 `showThinking={false}` 且 `streamChat` 不传 `onThinking`（thinking 不采集、思考打字机不启动）——语音回复只显示回复内容；深度思考块仅聊天页文字输入显示（见当天台账）。
- `Message.thinking` 列存思考过程；`get_session`/`/messages`/导出均返回 thinking，改消息序列化时注意带上。

## 流式打字机约定（2026-08-15 起）

- 聊天最终回复**必须逐字浮现**（打字机平滑），不能"一次性整段输出"：`Chat.jsx` 用 `pendingRef`（待显示队列）+ `revealTimerRef`（30ms 定时器，每 tick 揭晓 ~3 字符）本地实现，`onDelta` 只入队不直接刷屏，与网络/模型时序解耦。
- **深度思考同样必须逐字浮现（2026-08-16 起）**：思考增量（`onThinking`）也走打字机队列（`Chat.jsx` 的 `thinkingPendingRef` / 桌面助手的 `thinkingPendingRef` + `thinkingTimerRef`），**优先揭晓思考队列、思考清空后再揭晓回复队列**（DeepSeek/Ollama 均先思考后回复）——先流式输出深度思考，再流式输出最终回复；否则 SSE 多帧在同一同步批次被 React 批处理合并成"思考一次性整段出现"。**DeepSeek 思考深度（2026-08-16 深夜起）**：`llm_provider._complete` 对 `deepseek.com` 请求注入 `reasoning_effort=low`（默认 high），思考更短、首字更快；桌面助手思考打字机每 tick 揭晓 6 字符（原 3，30ms 不变），思考更快显示完。
- `finish()` / 流中断 / 切换会话（`abortRef.abort()`）必须 `clearReveal()` 清队列并补全剩余内容；思考与回复两个队列都要清（`Chat.jsx` 的 `clearReveal`/`flushPending`、桌面助手的 `flushThinking` 均同时处理两队列）；**`flushPending` 末尾必须 `patchAssistant({ content, thinking })` 把合并后的最终内容写回 state**——否则单帧整段回复（agent 确认文案：一条 `delta` 帧后紧跟 `[DONE]`，30ms 打字机未及 tick）因 `content` 恒空渲染成空白小白气泡（2026-08-23 台账三，对齐 `DesktopAssistant.finish` 的 `patchTurn`）；桌面助手 `interrupt()` 把 `thinkingPendingRef` 剩余并入 `thinkingTextRef` 后清 `thinkingTimerRef` 定时器（打断保留思路到面板，见「桌面助手约定·可打断的实时对话」）；`stopListening()`（卸载）才真正清空 `thinkingPendingRef`，防离开页面后定时器泄漏继续揭晓旧思考。

## 麦克风权限与竞态约定（2026-08-15 起）

- **桌面助手**：`startListening` 用 `startGenRef` 代际计数——`await getUserMedia` 之后必须校验 `gen === startGenRef.current && aliveRef.current`，否则立即 `stream.getTracks().forEach(t => t.stop())` 并 return（防异步 start 覆盖 stop、防卸载后泄漏麦克风）；`stopListening` 开头 `startGenRef.current++`。
- 权限错误分类提示：`NotAllowedError/SecurityError`→「点地址栏锁图标允许麦克风」、`NotFoundError`→「未检测到麦克风设备」、`NotReadableError`→「被其他应用占用」；桌面助手启动失败渲染「重新启动监听」按钮。
- **聊天页 `VoiceRecorder`**：`onError(message)` 分类提示（输入框下方红色小字 3 秒消失）；`rec.start()` 失败必须**降级** MediaRecorder + 后端转写（VAD 分段，见「聊天页实时语音输入约定」）；`getUserMedia` 失败同样分类提示。`no-speech/aborted` 静默不提示。

## 后端 ASR 约定（2026-08-15 起）

- 转写统一走 `/api/voice/transcribe`（聊天页降级路径与桌面助手共用），`asr_provider` 未配置 OpenAI Key 时自动用本地 **`faster-whisper small`**（2026-08-16 起由 base 升级：中文/专名识别显著变准，"小娜→麦麦小妮"类错乱根治，见当天台账八）。**占位符视为未配置（2026-08-19 起）**：`_build_asr` 用 `_PLACEHOLDER_KEYS` 过滤 `your-key-here` 等占位符，占位符/空串一律走本地模型——否则占位符被当真 key 调 OpenAI 云转写报 401、「音频转写失败，请重试或稍后再试」（见当天台账）。
- **浏览器录音前端转 WAV 上传（2026-08-16 起，勿回退 webm 直传）**：MediaRecorder 产出的 webm blob 必须先经 `frontend/src/utils/audio.js` 的 `blobToWav16k()`（`AudioContext.decodeAudioData` + `OfflineAudioContext` 重采样 16kHz 单声道 + 手写 44 字节头 WAV 编码）解码后上传，后端对 16kHz 单声道 16-bit PCM WAV **直接透传、不再调 ffmpeg**——ffmpeg 7.x 解析 Chrome MediaRecorder 非 seekable webm 报 `AVERROR_EOF`(187) 导致转写全线 500（后端镜像重建从 bookworm/ffmpeg5 升到 trixie/ffmpeg7 的回归，见当天台账九）。`audio_utils.convert_to_wav` 用 `_is_16k_mono_pcm_wav()` 探测 WAV 头命中即返回原路径；其余格式（飞书 `.opus` 等）仍走 ffmpeg 兜底，输出 `*_16k.wav` 独立路径，失败必须 `logger.error` 记录 stderr（勿再 `capture_output` 吞错）。
- **转写参数（勿删）**：`LocalWhisperASR.transcribe` 必须保留 `language="zh"` + `beam_size=5`（专名解码）+ `vad_filter=True` + `vad_parameters=dict(min_silence_duration_ms=500)`（Silero VAD 只转写人声段，滤环境噪声）+ `initial_prompt="小娜，XIAONA，你好，请问。"`（专名热词）+ `condition_on_previous_text=False`（分段独立解码防污染）。`OpenAIWhisperASR.transcribe` 同样带 `prompt="小娜，XIAONA，你好，请问。这是一段中文口语对话。"`（OpenAI 的 prompt 参数引导中文口语/专名解码，减少短句末尾字识别错误，2026-08-16 深夜起，见当天台账二十三）。
- **容器内必须构建时预下载 whisper 模型**：`backend/Dockerfile` 的 `RUN python -c "from faster_whisper import WhisperModel; WhisperModel('small', ...)"`（走 Clash 代理）；否则运行时去 huggingface.co 下载失败，`_build_asr()` 初始化异常被吞 → 转写恒空/500（见当天台账四十六）。
- `ASRProvider` 空实现 `transcribe` 返回空字符串（不抛 `NotImplementedError`）；`_build_asr()` 初始化失败必须 `logger.warning(exc_info=True)`，不能静默吞错。
- **转写失败返回友好错误**：`/api/voice/transcribe` 的转码/ASR 异常包 try/except 返回 `{"detail": "音频转写失败…"}` 422，前端据此展示具体原因，不再裸 500（防桌面助手误报「网络异常」，见当天台账九）。

## 后端健壮性约定（2026-08-16 晚起）

- **同步落库 + embedding 一律走 `asyncio.to_thread`**：`memory_engine` 的 `_persist_user`/`_persist_reply`/`chat_media_reply` 与 feishu `_record_media_meta` 对 `sql.save_chat_message` 的调用全部 `asyncio.to_thread(...)`——`SentenceTransformer.encode` 为 CPU 密集（0.3~数秒/次），同步执行会阻塞事件循环、每次对话全站卡顿（见当天台账十六）。
- **Cypher 关系类型/标签白名单校验**：`neo4j_client` 用模块级 `_REL_TYPE_RE`/`_LABEL_RE = ^[\u4e00-\u9fffA-Za-z0-9_]{1,32}$`，`link()` 与 `import_graph` 开头校验，非法类型 `logger.warning` 后跳过（LLM 抽取的 `pred` / 导入 label 拼 Cypher 的注入防护）。
- **上传/转写大小预检（勿回退"读进内存再判断"）**：`parse_upload` 在 `len(data) > MAX_FILE_SIZE`（10MB）时直接返回 `{kind:"unsupported", reason:"too_large"}`，不读取内容；`chat._collect_attachments` 用 `UploadFile.size` 预检；`voice /transcribe` 超限返回 422；`unsupported_reply(names, too_large=True)` 给「超过 10MB 限制」文案。
- **zip 解压限量 + 导入友好报错**：`data.py` 导入用 `_LimitedReader` 限流（累计上限 `MAX_IMPORT_SIZE*5`），缺 `xiaona_export.json`（KeyError）/ `BadZipFile` / JSON 解析失败 / 非 dict 均返回 400 中文提示（zip 炸弹防护）。
- **ffmpeg 加 `timeout=60`**：`audio_utils.convert_to_wav` 的 `subprocess.run` 超时先清理残留输出文件再抛错，畸形音频不再永久挂起。
- **消息排序带 id 次级键**：`sql_models.py` `Session.messages` relationship `order_by=lambda: (Message.created_at, Message.id)`，同一秒 user/assistant 消息顺序稳定（`get_session`/导出同源受益）。**注意 SQLAlchemy 2.0 relationship `order_by` 不接受字符串列表**（`order_by=["Message.created_at", ...]` 会把字符串当 SQL 字面量 → 首次 ORM 查询时全局 mapper configure 崩溃，`login`/`register` 全 500，见当天台账二十四），须用 lambda 返回列元组。
- **空回复不落库**：`chat_stream` 的 finally 中 `final_reply.strip()` 为空跳过 `_persist_reply`（LLM 中途异常不产生空白 assistant 消息，与非流式 `chat` 行为一致）。
- **会话导出带 thinking**：`GET /api/sessions/{id}/export` markdown 导出 assistant 消息附 `> 深度思考：` 块、json 导出补 `thinking` 字段（补齐 AGENTS.md 导出约定）。**导出 json 也带 `attachments`**，`import_session` 导入时原样重建 `thinking`/`attachments`/`source`（source 回退会话级），保证导出→清空→导入往返不丢字段（见当天台账二十七）。
- **附件元数据透传 file_key/message_id**：`_attachments_meta` 除 `{name,kind,size}` 外透传 `file_key`/`message_id`（仅飞书媒体 meta 携带）；`_latest_media`（feishu.py）取**最近 10 条内第一个带 file_key 且非 video 的附件**，不只看最新一条（最新带附件消息可能 file_key 为空，见当天台账二十七）。
- **文件/媒体解析一律 to_thread**：`parse_upload`（chat.py + feishu.py 各调用点）与 `feishu_client.download_resource` 下载后的大小校验（`> MAX_FILE_SIZE` 抛错）——pypdf/python-docx/openpyxl 解析 CPU 密集，同步执行卡事件循环（见当天台账二十七）。
- **forgetter 线程内完成读写返回纯 dict**：`process_forgetting` 的查询+权重计算+commit 整体下沉 `asyncio.to_thread`，且线程内返回纯 dict（id/user_id/content），主循环不跨线程访问 ORM 对象（commit 后属性过期会触发 reload 同步查询）；归档标记用批量 `id.in_()` update 再 to_thread commit（见当天台账二十七）。
- **认证限流**：`send_sms_code` 同号 60s 发送冷却（防刷短信接口）；`verify_sms_code` 连续失败 5 次作废验证码（防暴力穷举），前端 mock 模式（SMS_MOCK=1）自动填入验证码提示（见当天台账二十七）。
- **JWT_SECRET 启动告警**：`main.py` lifespan 检测 `JWT_SECRET == "dev-secret-change-me"` 时 `logger.warning`，生产必须显式配置环境变量。
- **`models.capabilities` 迁移**：`_apply_light_migrations` 给已有 `models` 表补 `capabilities VARCHAR(128) DEFAULT 'chat'`（能力自动探测写回，见当天台账二十七）。
- **kg_extractor 已删除**：`backend/app/core/kg_extractor.py` 全库零引用，2026-08-16 已删除（连带 `llm_provider.json_complete`、`neo4j_client.merge_trait/search_persons/create_person/add_alias`、`chroma_client.delete_session`）；文档 03 §5/§6 已标注「向量去重/事件蒸馏未接入」，实现时勿再引用这些方法。

## 回复风格约定（2026-08-16 起）

- **简洁精准（勿回退长回复）**：`memory_engine._build_messages` 的 system prompt 必须保留「回复要简洁精准：简单问题两三句话内说清，不堆砌语气词、口头禅和表情符号；复杂问题可适当展开，但同样去掉冗余修饰，直接给出关键信息。」——该 prompt 全局共用（聊天页/桌面助手/飞书），收窄所有端口的回复风格（用户明确要求，见当天台账十二）。

## 多 Agent 任务约定（2026-08-17 起，02 子项目群）

- **任务链路接入统一走 `agent_service.handle`**：`chat.py`（SSE 流式 + 非流式）与 `feishu.py`（text/audio/media）的消息都先过 `app.core.agent.service` 的 `handle()`——有 pending 确认任务先判确认（是/否/其它），无 pending 且意图命中（`director.is_task_intent` 关键词预筛：扒/抓取/爬/生成/做一份/汇总/分析/excel/pdf/ppt/表格/报表）则提炼建 pending 反问，都落空才回落 `memory_engine`。**禁止绕过 service 直接调 memory_engine 聊天**（否则任务意图永远不触发）。
- **确认交互是文本/语音问答**（「是/好/执行」即确认，`director.classify_confirmation` 判 yes/no/other），全渠道统一；任务卡片为后续增强。确认回复经 `_confirm` 确定目标会话 + `asyncio.create_task(start_task_async)` 后台执行，SSE 多一帧 `data: {"task_started": {task_id, session_id}}`（前端 `streamChat` 的 `onTaskStarted` 回调收到后每 2s 轮询 `GET /api/agent/tasks/{id}`，done/failed 停轮询并刷新消息）。
- **任务进度消息流（2026-08-23 起）**：执行者每轮动作经可 await 回调 `on_log(role, action, payload)` 通知（`executor.run`/`reviewer.review` 内 `await on_log(...)`），`workflow._on_log` 拦截两动作向会话写进度消息：`call_tool` → `🔧 正在{中文标签}{参数摘要}…`；`tool_result` 且 `ok=false` → `⚠️ {中文标签}未成功（原因），正在尝试其它方案…`（成功不额外写，避免一进一出刷屏）。工具名用 `_TOOL_LABELS` 中文映射（fetch_url→读取网页内容、fetch_amazon_bestsellers→抓取 Amazon 商品榜单、fetch_etsy_trending→抓取 Etsy 热门商品榜单、fetch_pinterest_trends→抓取 Pinterest 趋势、generate_excel→生成 Excel 表格），**不暴露英文函数名**。`_tool_args_summary` 展示 keywords/keyword/category/url/filename + 过滤参数（site/min_price/min_rating 等 + region/trend_type）。**`_write_message` 带 `task_id` 时写消息后 touch 任务 `updated_at`**（`_touch_task` 独立 DB 会话），前端 2s 轮询仅在 `updated_at` 变化时刷新会话消息，让进度气泡逐条出现（2026-08-23 修「进度一次性输出」）。
- **Amazon/Etsy 数据源地区与过滤（2026-08-23 起，2026-08-24 补区间/颜色/材质/排除词/过滤顺序）**：`fetch_amazon_bestsellers` 支持 `site`（us 默认/uk/ca/au/de/fr/it/es，兼容 `amazon.co.uk`/`britain` 等别名）——cookie 货币/语言与链接清洗域名随站点；过滤参数 `min_price/max_price`（单位随站点货币）/`min_rating`/`max_rating`（0~5 区间）/`min_reviews`/`max_reviews`（评论数区间）/`color`（颜色，中文/英文均可）/`material`（材质，中文/英文均可）/`exclude_keywords`（排除词，逗号分隔，中文/英文均可，标题含任一排除词直接丢弃，如 玩具/toy,派对,耳环），无法解析的字段不参与过滤；`color/material` 并入搜索词（`_ZH_TO_EN` 长词优先翻译，如 玫瑰金→rose gold），排除词**不并入搜索词**、仅本地过滤；`_ZH_TO_EN` 含排除类词（玩具/派对/儿童/装扮/假发/礼品等）。**过滤顺序（2026-08-24 起）：先排除词 → 再价格/评分/评论区间 → 最后才截断取 top N + rank 从 1 重排**——此前先 `[:limit]` 截断再过滤，top10 混入玩具/耳环一过滤就凑不齐 N 个。**搜索路径翻 5 页（2026-08-27 起，见当天台账三十）**：`fetch_amazon_bestsellers` 搜索路径翻 `_MAX_SEARCH_PAGES=5` 页（约 80~100 候选，单页仅 16~20 个——排除词一筛候选池就空了、top10 只给 1 条），逐页解析后按 `product_url`（ASIN）去重合并，页间 `_PAGE_DELAY=1.2s` 节流，单页失败/被拦用已抓到的页继续、候选池为空才报错；BSR 类目页路径保持单页。`fetch_etsy_trending` 支持 `min_price/max_price`（按 listing 本币，`price.amount` 分转元）/`min_favorers`；Etsy 无地区概念、官方 API 无评分字段（颜色/材质并入 keywords 英文）；**全量候选池（2026-08-28 起）**：不再 `[:limit*2]` 截断，按收藏排序后全量 100 条扫描过滤取足 top N，过滤后才为保留条目拉主图（严苛门槛凑不齐 top N 的问题根治）。执行者系统提示「地区与筛选」段落引导用法（用户指定地区→site、区间/颜色/材质/排除词→对应参数、未指定的维度不传，勿自己从结果挑）。**榜单表格由后端生成（2026-08-24 起，替代 LLM 手拼）**：`fetch_amazon_bestsellers`/`fetch_etsy_trending` 返回 `markdown_table` 字段（严格列对齐、链接列显示完整 https:// 商品网址、缺失填「-」；Amazon 7 列——排名|图片|商品名|链接|价格|评分|评论数，Etsy 8 列——评分列「-」另加「收藏/来源」），executor 的 `run()` 收集 `tool_results`，workflow 最终消息**自动附加全部来源榜单表格**（`_format_ranking_table`，2026-08-27 起：正序遍历 `tool_results` 收集所有 ok 且带 markdown_table 的榜单结果、每张前标来源中文标签、**同源保留最后一次**——执行者最后一次调用通常排除词更全筛选更严，曾出现第一次宽松结果混入耳环/玩具与严格总结错位；`final_msg` 已含表格分隔行则跳过）；执行者**禁止手拼表格**（LLM 少写一个 | 就整行右移、长 URL 被省略，提示词约束两轮仍错位），最终总结只写文字结论；两数据源各一张表。**链接列兜底（2026-08-24 起）**：`_SearchParser` 开新卡读商品卡 `data-asin` 存 `_asin`，`_commit` 时 `product_url` 为空则拼 `https://{domain}/dp/{asin}`——Amazon 新版页面 `/dp/` href 解析不到时聊天表格链接列不再空缺。**交叉验证基线（2026-08-27 起，2026-08-28 移除 Movers/Trends）**：任何榜单/选品任务默认第一步**只调 `fetch_amazon_bestsellers`**（现状主榜，免 key）；`fetch_etsy_trending`/`fetch_pinterest_trends` 为可选叠加层（需配置 key/token，未配置时工具返回降级错误、直接跳过勿重试）。最终总结**只补实际调用且成功的源**：「Pinterest 种草：趋势词「XX」周环比增长 Y%」。**任务执行韧性（2026-08-27 起）**：执行者每轮 `llm.chat_with_tools` 用 `timeout=300`，超时/断连重试 1 次、重试仍失败**保留已抓取 `tool_results` 返回**（失败消息仍自动附加榜单表格，不空手而归）；商品源失败最多重试 1 次，重试仍失败立即放弃该源；佐证源（`fetch_pinterest_trends`）失败**静默不弹 ⚠️ 进度气泡**（workflow `_SILENT_DEGRADE_TOOLS`，未配置很常见）；`amazon_bestsellers._get` 非 200 sleep 2s 重试 1 次；`amazon_bestsellers` **大类目词工具级拦截 + 翻译后词级去重（2026-08-28 起，见当天台账一）**：组装搜索词时**整词命中 `_BROAD_CATEGORY_WORDS`（jewelry/accessories/apparel 等大类目词）的参数剔除**并记入 note「已忽略大类目词」（仅整词命中才剔，不拆散 jewelry box 等多词品类；只传大类目词时兜底保留一个避免报错）；**去重移到 `_translate_zh_keywords` 之后按词精确去重**——此前翻译前 part 级子串去重判不出跨语言重复（keywords="diamond ring"+category="钻石戒指" 翻译后仍拼成 "diamond ring diamond ring"）；不做词级子串去重（会把 "earrings" 里的 "ring" 删掉）；审核模型异常降级视为通过（防任务卡 `reviewing`）。
- **TikTok Creative Center 暂缓（2026-08-24 调研，2026-08-28 随 Movers/Trends 卸载一并归档；2026-08-28 用户明确 TikTok 亦不在可用源之列）**：浏览器免登录可看 Top Products（销量区间+销售增长），但内部 `creative_radar_api` 需签名头+住宅代理、无浏览器环境，批量自动化不可行——勿再规划自动接入。**当前可用数据源仅三源**：Amazon Best Sellers（主榜）+ Etsy（需 key）+ Pinterest（需 token）。
- **Pinterest Trends 数据源（2026-08-24 起，收藏种草/前瞻）**：`fetch_pinterest_trends`（`backend/app/core/agent/tools/pinterest_trends.py`）调官方 API v5 `GET /v5/trends/keywords/{region}/top/{trend_type}`（Trial/Standard 均免费，需注册 app + OAuth Bearer token，scope `user_accounts:read`；只返回当天 top 趋势词、无历史时序）。key 回退链仿 etsy：`data_source_configs` source="pinterest" → env `PINTEREST_API_TOKEN`（`SUPPORTED_DATA_SOURCES = ("etsy","pinterest")`）。参数：`keyword`（`include_keywords` 过滤）/`region`（默认 US）/`trend_type`（默认 growing）/`limit`。返回 `{ok, source:"pinterest", region, trend_type, items:[{rank, keyword, pct_growth_wow, pct_growth_mom, pct_growth_yoy}], summary}`；**无 markdown_table**（趋势词不是商品榜，不自动附加）。**勿走 trends.pinterest.com 匿名接口**——数据中心 IP 被拒返回空（pinterest-cli/trendgetter 同受 IP 限制）。executor 交叉验证标「Pinterest 种草：趋势词「XX」周环比增长 Y%」；未配置/401/403 降级不阻断。
- **执行者条数不足如实交付（2026-08-23 起，2026-08-24 补先过滤后取足条数）**：工具返回 `ok:true` 且 `items` 非空时，无论条数多少直接交付（工具已按用户价格/评分门槛过滤，返回即符合条件的真实结果），条数少如实说明「符合全部条件的商品仅 N 条」，**不要反复换关键词重试**（曾导致严苛条件任务 6 轮换词耗尽报「达到最大工具调用轮次」，见当天台账七）；只有 `ok:false` 或 `items` 为空才换关键词/换数据源（最多 1-2 次）。**取足条数（2026-08-24 起）**：工具**先排除再取足条数**——用户要 top10 就应给足 10 条（搜索页能解析出几十张卡，排除玩具/耳环后从符合的池子里取 top N），只有过滤后池子本身不足 N 条才如实说明；执行者**不要从工具返回结果里手动砍**（手动砍会导致 top10 只剩 5 个），排除交给 `exclude_keywords` 参数。Amazon 工具在过滤后 `len(items) < 3` 时 note 追加「符合全部条件的商品较少，可能受价格/评分门槛限制」。**数据源边界如实告知（2026-08-27 起，2026-08-28 移除 Movers/Trends 后更新）**：用户请求的维度不在数据源能力范围时必须**如实说明「当前没有公开数据源可抓取该维度」**，勿硬凑/勿伪造/勿用其它维度冒充。可提供：现状销量榜（Amazon Best Sellers，翻 5 页约 100 候选）、Etsy 热门榜（需 key）、Pinterest 趋势词（需 token）。**无源维度**：商品级「下降最快/跌幅最大」榜与「24h 起量/涨幅」榜（曾接入的 Movers/Trends 因反爬限流已卸载）、品类搜索热度走势（曾接入的 Google Trends 已卸载）、单品总销量/累计销量绝对值（各平台均不公开）、国内平台（小红书/抖音/淘宝/京东）数据（未接入）。有最接近的可替代维度则说明并提供（如「商品级下跌/起量趋势没有，但我可以给你当前卖得好的 top N」）；已有维度照常交付，不因缺某个维度而放弃整个任务。
- **确认词判定顺序（2026-08-19 起）**：强肯定短语（没问题/可以/好的/好/行/确认/执行/开始/ok/yes）→ 强否定短语（不是/不用/不要/取消/算了/放弃/不了/等等/再说/no）→ 弱否定（不/没/否）→ 弱肯定（是）→ LLM 兜底。「没问题」含「没」，必须由强肯定短语先拦截，否则被误判取消（见当天台账二·Bug1）。**禁止恢复「先判取消词再判确认词」的旧顺序**。**否定组合预检（2026-08-28 起，见当天台账一）**：强肯定判定前先拦「不+确认词」组合（不行/不可以/不好/不确认/不执行/不同意/不开始，去空白匹配兼容「不 行」）——单字「行/好/可以/同意」会被「不X」反义命中，此前用户回复「不行」也会触发任务执行烧 token。
- **pending 只认当前会话+渠道（2026-08-19 起）**：`_latest_pending(user_id, session_id, channel)` 按 session_id + channel 过滤——web 会话 A 有待确认任务时，用户在会话 B / 飞书 / 桌面发消息不得被误判为确认或取消（见当天台账二·Bug2）。
- **agent 后台写消息走 to_thread（2026-08-19 起）**：`workflow._write_message` 是 async 且内部 `asyncio.to_thread` 包裹 `save_chat_message`（Chroma embedding 为 CPU 密集，同步执行会阻塞事件循环导致任务执行期间全站卡顿）。**禁止恢复为同步调用**（见当天台账二·Bug3）。
- **渠道分流**：web→当前会话；desktop→确认后**后端新建专属会话**（`ensure_session(user_id, "", "desktop")`），任务全程写入，用户回聊天页侧边栏可见；feishu→映射会话（**未绑定 open_id 不触发任务**，走 0 token 引导，见「飞书账号绑定约定」）。
- **任务状态机**：`pending → confirmed → executing → reviewing → revising(N≤2) → done/failed`，另加 `cancelled`（只有 pending/confirmed 可取消）。审核打回 `retry_count < 3`，超限终止。
- **模型分配回退链**（`workflow.start_task_async`）：`executor_cfg = executor || chat`；`reviewer_cfg = reviewer || executor || chat`。设置页功能分配下拉（聊天/抽取/执行者/审核者只列对话能力模型，向量化只列 embedding 能力）。
- **`llm.chat_with_tools` 返回完整 message**（含 `tool_calls` 时），由 executor 循环回填 `role:"tool"` 结果后再次调用；工具结果统一 `{ok:bool,...}`，错误也返回 `{ok:false, error}` 不抛异常（回填模型继续决策）。工具注册白名单在 `agent/tools/registry.py`（`TOOLS` + `call_tool`），未知工具/参数异常结构化返回。
- **产物落盘** `backend/artifacts/{user_id}/{task_id}/`，`AgentTask.artifacts` 存相对路径 JSON，API 下载 `/api/agent/tasks/{id}/artifacts/{name}` 只放行任务自身登记的 basename（防路径穿越）；会话消息里产物以 `[下载 xxx](/api/agent/tasks/{id}/artifacts/xxx)` Markdown 链接呈现。**TTL 已实现（2026-08-24 起）**：`ARTIFACTS_TTL_DAYS=30`，`artifact_cleaner` 每日随 lifespan `_artifact_clean_loop` 扫描清理过期文件与空目录；LRU（`last_accessed_at` 按下载重置）未实现，勿在文档中标注「TTL 未做」。
- **文档整理类工具（2026-08-24 起）**：`generate_pdf`（reportlab，注册 `UnicodeCIDFont('STSong-Light')` 中文字体，`<link href>` 完整 URL 可点可复制）/ `generate_docx`（python-docx，`add_hyperlink` 真实超链接 + 完整 URL 文本）/ `fetch_session_messages`（经 ctx.session_id 读会话最近 N 条消息）三工具注册于 `tools/registry.py`；数据契约 `{filename?, title?, subtitle?, date?, source?, sections?, images?, markdown_tables?}`（sections: `[{heading?, paragraphs?, bullets?, table?}]`）。URL 一律完整 `https://` 绝对地址，禁止截断；图片 httpx 下载失败降级跳过不阻断。**表格图片列（2026-08-24 起）**：榜单商品图填在表格「图片」列（单元格 `![商品名](完整图片URL)`），`generate_pdf` 自动识别下载、`Image` 嵌入该列（宽限列宽、高限 `_IMG_MAX_H`、宽高比缩放居中），**禁止再用独立 `images` 参数重复传商品图**（独立 images 会排到文档末尾、不在图片列）。**markdown 表格代码注入渲染（2026-08-24 起）**：`fetch_session_messages` 提取会话里的 markdown 榜单表格为 `markdown_tables`（图片列 `![alt](URL)` 原样保留），executor 调用 `generate_pdf`/`generate_docx` 前**自动注入 `args["markdown_tables"]`**（LLM 无需感知）；`generate_pdf`/`generate_docx` 用 `_parse_markdown_table` 解析原文、`render_table` 渲染——图片列 100% 保留、列严格对齐，**勿回退「让 LLM 手工把表格转写成 sections JSON」**（LLM 转写会丢图片列/列错位，见当天台账二十二）。**content 兜底注入 + 格式硬锁（2026-09-01 起）**：executor 收集会话 user 消息 >300 字正文拼接为 `session_user_content`，调 generate_pdf/generate_docx 时 `args.content` 为空或明显短于原文（疑似截断）**自动注入原文**——不再依赖 LLM 把超长全文手工回传（输出 token 超限必截断 →「sections 不能为空」）；content 注入时只附加 `assistant_tables`（用户表格已在原文里，避免重复渲染）；**方案 C 直通 · 正文永不经过 LLM（2026-09-01 起）**：用户「粘贴大段原文 → 生成 Word/PDF」默认**原样直通**——executor `_want_passthrough(task)`（task_type 为 generate 且描述/params 不含总结/提炼/精简/概括/改写/润色等加工词）为真时，调文档工具**无条件注入会话原文到 `args["content"]`**、移除 sections/markdown_tables/title/subtitle/date/source（LLM 编造的封面日期会污染文档，title 由工具从 content 首行自动提取），正文完全用会话原文渲染、LLM 只定格式与 filename，**不重写/不总结正文**（根治「生成文档内容被 LLM 改写、20+ 表格只剩 1 个」，见当天台账四/五/六）。渲染链路：`generate_pdf`/`generate_docx` 入口 `_smart_break_lines`（零换行旧消息语义分行，超长行在中文/数字编号标题、表头关键词行前插换行）→ `_parse_markdown_blocks` 结构化（**连续 `|` 行即表格**，不再强制 `---` 分隔行；`_parse_markdown_table` 去首尾空 cell——`| | a | b | |`；`_split_text_table` **拒含 `|` 与 `- ` 开头行**；`_strip_md_symbols` 剥 `**`/`*`/`` ` `` 残留）；规则吃不准的表格由 **LLM 只返回行级标记指令**（`_markup_document` → `_ask_table_marks`：候选行带行号发 LLM，返回 `{line, anchor}` 锚定校验防漂移，程序 `_split_cols` 切列在原行插 `|` 表格标记后重解析校验覆盖 ≥80% 才采用，**LLM 输出正文一律丢弃**），失败/非法 JSON/候选行过多回落原文直注（宁可保真不保结构）；模型未先调 `fetch_session_messages` 时 executor 主动拉取一次；**仅含加工词才走 LLM 提炼**（`_markdownify_content` 并行分块 `Semaphore(4)` + `wait_for(180)` 提速，此时保留上述 content 兜底 + 表格分源注入）；执行者提示词硬锁：要 word/文档/文件（默认 Word）→ 只用 `generate_docx`、要 pdf → 只用 `generate_pdf`，**禁止因某格式工具失败改调另一种格式工具**（交付格式与要求不符是严重错误）。文档工具入口有非 dict params 防御 + 图片收集 None.table 守卫（`table.get("rows")` 对纯文字 section 的 None 崩溃已修，见当天台账二）。**文件名语义化（2026-08-24 起）**：`filename` 缺失或「文档_<时间戳>」占位名时用 `title` 生成 `{safe_title}_{时间戳}.{pdf|docx}`（60 字符截断）；executor 提示词要求 6~12 字语义名、禁止「文档_时间戳」。`workflow` 的 `_TOOL_LABELS`/`_tool_args_summary` 已含文档工具中文标签；`ChatMessage.jsx` 把 `/api/agent/tasks/` 开头的链接渲染为「下载」按钮。**数据源优先会话内容（2026-08-24 起）**：用户说「把上面的/刚才的/这个榜单整理成文档」= 当前会话已有内容，执行者必须先用 `fetch_session_messages` 读会话作数据源，**禁止再调 `fetch_amazon_bestsellers`/`fetch_etsy_trending` 等数据源工具重新抓取**（director 提炼 params 会带 `内容来源: 会话已有内容`）；只有会话确认无相关内容才允许数据源补充。**产物落盘（2026-08-24 起）**：`docker-compose.yml` backend 挂载 `../artifacts:/app/artifacts`，产物落回宿主机 `d:\XIAONA\artifacts/{user_id}/{task_id}/`；`artifact_cleaner` TTL 清理对挂载目录同样生效；`_format_artifact_list` 最终回复附落盘路径提示。**前端下载带鉴权（2026-08-24 起）**：`Markdown.jsx` 产物下载按钮用 `fetch + Authorization: Bearer {token}` 取 Blob 再触发浏览器保存（裸 `<a href>` 导航不带 JWT → 401 打不开，勿回退）。详见 `02_多Agents执行功能/04_生成PDF文件详解.md` 与 `05_生成Word文档文件详解.md`。
- **`_model_configs` 返回五键**：`{chat, extract, embedding, executor, reviewer}`，新增键不影响旧调用点（按键取值）。
- 详细见 `项目计划/02_多Agents执行功能/03_任务确认与执行详解.md`。

## 多类型文件上传约定（2026-08-15 起）

- **不做厂商能力预判**：图片直接按多模态 `{"type":"image_url"}` 发给模型；模型拒绝（HTTP 400/422/415 且含图）→ `llm_provider.VisionNotSupportedError` → 聊天引擎转成正常 assistant 回复（"很抱歉，我当前使用的模型（…）不支持识别图片…"）并落库。无法识别的类型（音频/视频/超限）→ `unsupported_reply` 聊天式回复。
- **媒体类错误绝不上屏报错代码/错误码**：一律是完整礼貌的句子，作为正常消息返回并落库；HTTP 状态/堆栈只进后端日志。
- 解析工具集中 `backend/app/core/media_utils.py`（`detect_kind` / `parse_upload` / `unsupported_reply`，聊天页与飞书共用）；支持 image / pdf / word / excel / text，单文件 ≤10MB、一次 ≤5 个。
- `Message.attachments` 列存附件元数据 JSON `[{name,kind,size}]`（**图片二进制不落库**）；文本类附件的提取内容拼入 user content 进入三层记忆；改消息序列化注意带上。
- 前端：`ChatInput.jsx` 拖拽/选择 + 文件 chips 删除 + 文字并存；`streamChat` 传 FormData；`ChatMessage` 显示附件名 chips；`onMediaReply` 把后端聊天式回复按正常气泡渲染。

## 飞书媒体按需监听约定（2026-08-15 起）

- 群聊未 @ 的 image/file/media：只轻量记录「用户发送了图片/文件：xxx」元数据 user 消息（`Message.attachments` 存 `file_key`/`message_id`），**不下载、不调 LLM、不回复**。
- 群聊 @ 或单聊的 image/file：按需下载（`feishu_client.download_resource(message_id, file_key, resource_type="file")`）→ `parse_upload` → 多模态 `chat_core.chat(attachments=...)`；@ 文字含"图/文件/文档/分析"等关键词时自动带最近媒体。**图片必须传 `resource_type="image"`**（飞书 `type=image`），文件/音频传 `"file"`——固定 `type=file` 会导致图片下载全部失败（2026-08-24 修）。
- 视频（media）只记元数据不拉取；表情包（sticker）忽略；音频维持实时转写。

## 飞书 Webhook 约定（2026-08-17 起，10 §8）

- **接入方式为公网 Webhook**：飞书事件订阅填请求地址 `https://你的公网地址/api/feishu/webhook`（本地调试用 ngrok 暴露，见 10 §8）。**不使用长连接**——`feishu_ws.py` / `lark-oapi` 依赖已于 2026-08-17 删除（用户实际使用公网连接，见当天台账）。
- **签名与解密**：`webhook` 端点先解密（事件加密模式下 `_decrypt_feishu_body` AES-256-CBC）→ challenge 直接返回 → 正式事件才验签（`sha256(timestamp + nonce + sign_key + body)` 直接拼接，`sign_key = FEISHU_ENCRYPT_KEY or FEISHU_VERIFY_TOKEN`，**勿回退 HMAC + `\n` 拼接旧算法**）。
- **统一处理入口**：`process_feishu_event(event, event_id)`（`backend/app/api/feishu.py`）为 webhook 唯一处理入口——去重 → 会话映射 → 绑定校验（未绑定走 0 token 引导 / 绑定状态机，已绑定走正常流程）→ 媒体/音频/文本分流 → agent/chat → 回复发送。**不得绕过该函数直接复制处理逻辑**。
- 事件去重 `event_id` 从 `payload.event_id` 提取；`FEISHU_VERIFY_TOKEN` / `FEISHU_ENCRYPT_KEY` 均来自飞书后台「加密策略」，`.env` 已配置。

## 飞书账号绑定约定（2026-08-17 起，取代「飞书影子账号生命周期约定」）

- **影子账号机制已废弃（2026-08-17 用户确认）**：原「未绑定自动创建 `feishu_{open_id}` 账号 + 限时记忆 + 定期提醒绑定」整体废除。原因：陌生人白耗主人 token（自动建号即可调 LLM）；影子账号无模型配置必然 `ChatModelNotConfigured` → 500 无法回复。代码中 `is_shadow_account` / `_shadow_reminder` / `_task_shadow_notice` / `shadow_cleaner` / `get_or_create_user_by_feishu` 自动建号逻辑**全部删除**（存量 `feishu_` 影子账号用一次性脚本清理三层数据）。
- **核心原则**：小娜只服务**已绑定账号**的人。未绑定 open_id 发消息 → **不创建账号、不调 LLM**，只回 0 token 固定引导文案（「你还没有连接到小娜，请绑定。回复你的 XIAONA 手机号」）。任何陌生人无法消耗主人的大模型 token。
- **绑定 = 手机号 + 密码 + 短信验证码 三步验证**，全程在飞书对话框内分步引导（一步一问，避免整串输入易错）：
  1. 用户报手机号 → 查账号表（不存在 → 「该手机号未注册，请先到官方网页注册」）；
  2. 用户报密码 → `verify_password` 校验（错误 → 「密码错误，请到前端界面修改密码」，不退回手机号）；
  3. 密码正确 → 向手机发短信验证码 → 用户报验证码 → `verify_sms_code` 校验 → 写入 `feishu_user_map(open_id → user_id)` → 绑定成功。
- **绑定消息不落库、不进记忆、不进日志**（密码/验证码明文只存在于飞书消息到内存校验链路）；绑定中间状态（等密码/等验证码）存模块级内存 dict（按 open_id 索引，5 分钟 TTL，重启清零）。
- **绑定只允许私聊（2026-08-24 起）**：未绑定 open_id 在群聊 @ 机器人只回「请私聊小娜完成绑定」引导，**不进入绑定状态机**（防验证码/密码明文进群聊，见当天台账）。
- **手机号正则放宽 + 验证码重发（2026-08-24 起）**：`_PHONE_RE = ^\d{6,15}$`（支持非 1 开头号码，勿回退「^1\d{10}$」）；验证码错误/过期时回复附带「回复『重发』可重新发送」入口（`_RESEND_KEYWORDS`），用户重发验证码不必从头走绑定流程。
- **一个小娜账号（一个手机号）可绑多个飞书 open_id**（`feishu_user_map.feishu_user_id` **去 unique 约束**，同手机号注册的多个飞书账号共享同一账号三层记忆）。
- **设置页**：已绑定显示「绑定身份列表」（open_id 后 8 位 + 绑定时间 + 最后活跃，`last_active_at` 按 **open_id 记录**在 `FeishuUserMap.last_active_at`，设置接口回退 `user.last_active_at`），每项可**解绑**（需手机号+验证码确认，只删映射、三层记忆保留）与**修改密码**（需手机号+验证码+新密码）。
- 修改 `User`/映射表模型加列时，记得同步 `init_db()` 的 `_apply_light_migrations()`（`create_all` 不会给已有表补新列）；迁移 SQL 类型必须用 **PostgreSQL 兼容类型**（时间列用 `TIMESTAMP`，**禁止 MySQL 方言 `DATETIME`**，否则 PG 启动即 `ProgrammingError: type "datetime" does not exist`，见当天台账二十二）。

## 会话持久化与自动标题约定（2026-08-15 起）

- **用户消息先行落库**：`_persist_user`（LLM 前执行）+ `_persist_reply`（流后 finally），LLM 失败/中断不丢用户记录。`save_message` 落库时必须**显式刷新所在会话 `updated_at`**（SQLAlchemy `onupdate` 只在该行被 UPDATE 时触发，单纯 INSERT 新消息不会刷新会话行），否则侧边栏"最近活跃"排序失真（2026-08-16 晚起）。
- **聊天页 URL 同步**：`Chat.jsx` 用 `setSearchParams({ session_id })`（replace）同步当前会话，刷新/返回可恢复；`sessionId` 为空时先 `create()` 拿到真实 id 再发送。
- **聊天页会话跨页面保持（2026-08-16 起）**：当前会话 id 另存 `sessionStorage`（key=`chat_active_session_{user.id}`，**按用户隔离**，登录后可用）。切页（关系网/桌面助手/设置）导致 `Chat` 卸载重挂时，`sessionId` 初始化「URL 参数优先，其次 sessionStorage」自动恢复——打开哪个会话，切走再切回仍是那个会话；删除当前会话（`setSessionId(null)`）同步移除存储；**只有浏览器完全关闭重新打开**（sessionStorage 清空）才回到新会话界面。
- **会话标题自动生成**：标题仍为「新会话」时，`_maybe_auto_title` 用聊天模型概括（≤12 字），失败回退首条消息前 15 字；前端流结束 `sessionsApi.list()` 刷新侧边栏。
- 修改 `Message`/`Session` 模型加列时，记得同步 `init_db()` 的 `_apply_light_migrations()`（`create_all` 不会给已有表补新列）。

## 模型管理约定（2026-08-15 起）

- **添加模型不限类型（2026-08-16 起）**：添加模型时**不需要也无法手动选能力**（`capabilities` 默认空），对话模型/嵌入模型/向量化模型都可以添加。模型能力由后端 `detect_capabilities` **自动探测**：`test_model` 调 `/v1/chat/completions`（对话能力）与 `/v1/embeddings`（向量化能力），探测成功后写回 `model.capabilities`（如 `"chat"` / `"embedding"` / `"chat,embedding"`）。前端 `ModelCard` 据此显示能力标签（对话/嵌入/未确认）。
- **功能分配严格校验（2026-08-16 起）**：`PUT /api/settings/function-models` 分配时校验目标模型能力——`chat_model_id`/`extract_model_id` 必须含 `chat`，`embedding_model_id` 必须含 `embedding`，不匹配返回 400 中文提示（「该模型不支持对话/向量化」）。模型能力为空（未测过）时先自动探测写回；探测失败（服务不可达）不阻断分配。前端功能分配三个下拉按能力过滤（聊天/抽取只列对话模型，向量化只列向量化模型，能力未确认的模型仍可选）。
- 厂商下拉含主流 AI 厂商（openai/anthropic/google/ollama/kimi/deepseek/zhipu/qwen/baidu/doubao/mistral/groq/xai/openrouter/together/siliconflow/huggingface/azure/localai）+ 末尾 `custom` 自定义项（`provider` 取自定义输入值）；编辑时列表外 provider 自动落到 custom 并回填。
- **纯手填表单**：Base URL / 模型名 / API Key 全部是**可编辑文本框**，Base URL 默认留空、不自动填充，模型名不是下拉列表。`PROVIDERS` 表只含 `{value,label}`，切换厂商只更新 provider/customProvider，不改 base_url。（曾做「默认 URL 预填 + 自动扫描下拉」后按用户要求回退，见当天台账。）
- **Ollama 本地模型注意**：后端运行在 Docker 容器内，容器内 `http://localhost:11434` 指向容器自身而非宿主机 Ollama。但 `llm_provider._resolve_host` 已做自动改写：容器内检测到 localhost/127.0.0.1/0.0.0.0 开头的 base_url 时，自动替换为 `host.docker.internal`（Docker Desktop 内置宿主机别名，Linux 由 compose `extra_hosts: host-gateway` 支持）——**用户在设置页填 `http://localhost:11434` 即可连通，无需手改地址**（2026-08-16 现场验证：localhost 与 host.docker.internal 填法的 `qwen3:8b` 均连接成功）。若仍连不通，多为宿主机 Ollama 未启动 / 防火墙拦截 / 模型名错误，`test_connection` 会给出对应指引（见下）。
- 后端接口：`POST /api/settings/models/scan`（扫描模型列表，`llm.list_models` 8s 短超时，Ollama 优先 `/api/tags` 再 OpenAI 兼容 `/models`；**必须定义在 `/models/{model_id}/test` 之前**，否则 "scan" 会被当 model_id 捕获）、`POST /api/settings/models/{id}/test`（连通性测试，`llm.test_connection` 用 **60s** 超时——本地 Ollama 大模型首次调用需冷启动加载进内存（qwen3:8b 实测约 10s），8s 会误判为连不通；错误信息不泄露 api_key，且对超时/连接失败给出针对性提示）、`PUT /api/settings/models/{id}`（修改）、`DELETE /api/settings/models/{id}`（删除，被功能分配引用时同步置空对应分配）。
- **`test_connection` 对 Ollama 的精确诊断（2026-08-16 起）**：`provider == ollama` 时用模块级 `_ollama_model_names(base_url)`（探测 `/api/tags` 拿可用模型列表，8s 短超时）区分场景——`ConnectError` 且 `/api/tags` 可达 → 「模型「{name}」不存在，可用模型：…」；`/api/tags` 也不可达 → 「确认 Ollama 已启动且监听 11434、防火墙放行；若容器内仍连不上宿主机，可设 `OLLAMA_HOST=0.0.0.0:11434` 后重启」；HTTP 400 且响应体含 `does not support chat` → 「模型不支持对话（它可能是嵌入/向量化模型），请选择对话模型」（典型：对 bge-m3 等嵌入模型点测试）；HTTP 404 → 同样列出可用模型。前端 `ModelCard.jsx` 把 detail 展示在按钮下方独立行（`break-words`），长提示可完整显示。

## 其他说明

- Docker 网络：国内网络拉镜像走 daemon.json 的 registry-mirrors 与 proxies（Clash 127.0.0.1:7890），构建内 pip/npm 走 build.args 代理 + 清华源/npmmirror 兜底（详见 06 §8）。
- 删除操作密码策略：**聊天会话删除免密**（软删除）；图谱节点删除、设置页数据清空仍需密码，密码放 JSON body 不进 URL。
- **验证方式约定（2026-08-24 起，用户确认的省 token 工作流）**：后端改动验证只跑 `python -m compileall -q backend/app`；**不重建 Docker 容器、不做容器内实测、不等待健康检查**——运行验证由用户手动 `python start.py`（自带 `--build`）完成，无需 Agent 代跑。前端改动不代跑 `npm run build`，由用户自行构建确认。探索代码用 Grep/Glob 精准定位、只读最小片段，不无谓通读大文件。台账「验证」栏如实写「静态检查通过，待用户手动启动实测」，不虚构运行结论。
