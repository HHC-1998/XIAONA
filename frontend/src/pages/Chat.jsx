import { useState, useEffect, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ChatInput } from '../components/ChatInput';
import { ChatMessage } from '../components/ChatMessage';
import { DeleteConfirm } from '../components/DeleteConfirm';
import { sessionsApi } from '../api/sessions';
import { streamChat } from '../api/chat';
import { agentApi } from '../api/agent';
import { useModelStatus } from '../hooks/useModelStatus';
import { useAuth } from '../hooks/useAuth';

let tmpId = 0; // 临时消息 id（发送中尚未持久化）

export default function Chat() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { user } = useAuth();
  // 当前会话 id 持久化：sessionStorage 按用户隔离（key=chat_active_session_{user.id}）。
  // 切页重挂载时恢复会话；URL 参数优先（刷新/带参数直达仍可恢复）。
  // 只有浏览器完全关闭重新打开，sessionStorage 清空后才回到新会话界面（07 §6 扩展）。
  const activeSessionKey = user ? `chat_active_session_${user.id}` : null;
  const [sessionId, setSessionId] = useState(
    () =>
      searchParams.get('session_id') ||
      (activeSessionKey ? sessionStorage.getItem(activeSessionKey) : null),
  );
  const [messages, setMessages] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [collapsed, setCollapsed] = useState(false);   // 侧边栏收起状态
  const [deleteTarget, setDeleteTarget] = useState(null); // 待删除的会话对象
  const [streamingId, setStreamingId] = useState(null);   // 正在流式输出的 assistant 消息 id
  const abortRef = useRef(null); // 用于中止未完成的 SSE 流
  const pendingRef = useRef('');         // 打字机：待显示的剩余回复内容
  const thinkingPendingRef = useRef(''); // 打字机：待显示的剩余思考内容
  const revealTimerRef = useRef(null);   // 打字机：逐字 reveal 定时器
  const sessionIdRef = useRef(sessionId); // 实际会话 id（onSession 自愈同步用，见 sendMessage）
  // 懒创建会话的守卫（2026-08-15 修复"新会话发消息丢失"）：
  // sendMessage 内 setSessionId 会触发两个依赖 sessionId 的 effect，必须放行本轮：
  // - pendingSendRef：reload effect 不拉取新会话的空消息覆盖乐观消息；
  // - noAbortRef：abort effect 不中止刚发起的流（否则 SSE 以 499 断开，消息全丢）。
  const pendingSendRef = useRef(false);
  const noAbortRef = useRef(false);
  const messagesRef = useRef(null);   // 消息列表容器 DOM（自动滚动跟随最新）
  const loadSeqRef = useRef(0);       // 消息拉取请求序号：切会话时丢弃过期响应（2026-08-17 加）
  const { chat_configured: chatConfigured } = useModelStatus();
  const [activeTaskId, setActiveTaskId] = useState(null); // 执行中的 Agent 任务 id（02 §5，2026-08-17 加）

  // Agent 任务进度轮询：收到 task_started 帧后每 2s 查一次状态，
  // 执行中（executing/reviewing/revising）刷新当前会话消息，让进度气泡逐条出现；
  // done/failed/cancelled 停止轮询并刷新消息（新消息含产物下载链接，Markdown 可点）。
  // 执行中仅当任务 updated_at 变化才刷新（后端写进度消息会 touch updated_at，2026-08-23 起），
  // 避免每 2s 无谓请求。
  useEffect(() => {
    if (!activeTaskId) return;
    let stopped = false;
    let lastUpdatedAt = '';
    const timer = setInterval(async () => {
      try {
        const t = await agentApi.getTask(activeTaskId);
        if (stopped) return;
        if (t.status === 'done' || t.status === 'failed' || t.status === 'cancelled') {
          clearInterval(timer);
          setActiveTaskId(null);
          if (sessionIdRef.current) {
            const seq = ++loadSeqRef.current;
            sessionsApi
              .get(sessionIdRef.current)
              .then((s) => {
                if (seq === loadSeqRef.current) setMessages(s.messages);
              })
              .catch((e) => console.error('refresh messages after task failed:', e));
            refreshSessions();
          }
          return;
        }
        // 任务进行中：updated_at 有变化 → 刷新当前会话消息以显示新的进度气泡
        if (sessionIdRef.current && t.updated_at && t.updated_at !== lastUpdatedAt) {
          lastUpdatedAt = t.updated_at;
          const seq = ++loadSeqRef.current;
          sessionsApi
            .get(sessionIdRef.current)
            .then((s) => {
              if (seq === loadSeqRef.current) setMessages(s.messages);
            })
            .catch((e) => console.error('refresh messages during task:', e));
        }
      } catch (e) {
        // 轮询失败静默重试（任务状态短时不可达不影响聊天）
      }
    }, 2000);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [activeTaskId]);

  // 消息列表自动滚动：messages/流式状态变化时始终跟随最新消息，无需手动翻
  useEffect(() => {
    const el = messagesRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, streamingId]);

  // 打字机收尾/中断：清空待显示队列与定时器（防止切会话/卸载后继续写旧消息）
  const clearReveal = () => {
    if (revealTimerRef.current) {
      clearInterval(revealTimerRef.current);
      revealTimerRef.current = null;
    }
    pendingRef.current = '';
    thinkingPendingRef.current = '';
  };

  useEffect(() => {
    // 懒创建会话触发的 reload 放行：仅跳过消息拉取（乐观消息不覆盖），会话列表照常刷新
    if (sessionId && !pendingSendRef.current) {
      const seq = ++loadSeqRef.current;   // 请求序号：快速切换会话时丢弃过期响应（2026-08-17 修）
      sessionsApi
        .get(sessionId)
        .then((s) => {
          if (seq === loadSeqRef.current) setMessages(s.messages);
        })
        .catch((e) => console.error('load messages failed:', e));
    }
    pendingSendRef.current = false;
    sessionsApi.list().then((r) => setSessions(r.items));
  }, [sessionId]);

  // 会话 id 同步到 URL：刷新/切页返回后可恢复当前会话（07 §6 约定）。
  // 删除当前会话（sessionId 置空）时清掉 URL 里的旧 session_id 参数，
  // 避免刷新后被旧参数"复活"回已删会话（404/消息发送到已删会话）。
  useEffect(() => {
    if (sessionId && searchParams.get('session_id') !== sessionId) {
      setSearchParams({ session_id: sessionId }, { replace: true });
    } else if (!sessionId && searchParams.get('session_id')) {
      setSearchParams({}, { replace: true });
    }
  }, [sessionId, searchParams, setSearchParams]);

  // 同步 sessionIdRef（onSession 自愈回调读取最新会话 id 用）
  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

  // 会话 id 同步到 sessionStorage：切页重挂载（卸载→重挂）后仍恢复当前会话；
  // 删除当前会话（sessionId 置空）时移除存储（07 §6 扩展）
  useEffect(() => {
    if (!activeSessionKey) return;
    if (sessionId) sessionStorage.setItem(activeSessionKey, sessionId);
    else sessionStorage.removeItem(activeSessionKey);
  }, [sessionId, activeSessionKey]);

  useEffect(() => {
    // 切换会话/离开页面时中止旧流并清空打字机队列，避免继续写状态。
    // 但 sendMessage 懒创建会话（内部 setSessionId）触发的本次变化必须放行，
    // 否则刚发起的流会被立即 abort（SSE 499，消息丢失）。
    return () => {
      clearReveal();
      const skipAbort = noAbortRef.current;
      noAbortRef.current = false;   // 一次性消费
      if (!skipAbort) abortRef.current?.abort();
    };
  }, [sessionId]);

  const refreshSessions = () => {
    sessionsApi.list().then((r) => setSessions(r.items));
  };

  const createNewSession = async () => {
    const s = await sessionsApi.create();
    setSessions((prev) => [s, ...prev]);
    setSessionId(s.id);
    setMessages([]);
  };

  const handleDeleteSession = async () => {
    const target = deleteTarget;
    await sessionsApi.remove(target.id);
    setSessions((prev) => prev.filter((x) => x.id !== target.id));
    // 若删除的是当前会话，清空当前会话状态
    if (sessionId === target.id) {
      setSessionId(null);
      setMessages([]);
    }
    setDeleteTarget(null);
  };

  // 后端/网络错误 → 友好文案（01 §5.5 错误帧约定）
  const friendlyError = (raw, code) => {
    if (code === 'chat_model_not_configured') return '您还没有配置对话模型，请到设置页配置';
    if (code === 'network_error') return '网络异常，请确认服务已启动（python start.py）';
    return raw;
  };

  const retryLast = () => {
    const lastUser = [...messages].reverse().find((m) => m.role === 'user');
    if (lastUser) sendMessage(lastUser.content);
  };

  const sendMessage = async (text, files) => {
    // 中止上一条未完成的流（如果存在），并清空打字机队列
    abortRef.current?.abort();
    clearReveal();

    // 无会话时先创建（拿到真实 id 再发送，避免后端自愈新建导致前端 sessionId 失联）
    let sid = sessionIdRef.current;
    if (!sid) {
      const s = await sessionsApi.create();
      sid = s.id;
      sessionIdRef.current = sid;
      // 守卫标记：本次 setSessionId 由发送流程内部触发，reload/abort effect 均需放行
      pendingSendRef.current = true;
      noAbortRef.current = true;
      setSessions((prev) => [s, ...prev]);
      setSessionId(sid);
    }

    const userMsg = { id: `tmp_${++tmpId}`, role: 'user', content: text, attachments: files || [] };
    const assistantMsg = { id: `tmp_${++tmpId}`, role: 'assistant', content: '', thinking: '' };
    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setStreamingId(assistantMsg.id);

    let assistantText = '';
    let thinkingText = '';
    let failed = null;

    const patchAssistant = (patch) => {
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (!last || last.role !== 'assistant') return prev;
        return [...prev.slice(0, -1), { ...last, ...patch }];
      });
    };
    // 打字机：把 onDelta/onThinking 的增量先入队，定时器每 30ms 揭晓 ~3 字符。
    // 优先揭晓思考队列、思考清空后再揭晓回复队列（DeepSeek/Ollama 均先思考后回复），
    // 避免多次 delta 在同一 JS tick 内同步触发被 React 自动批处理合并成"一次性输出"。
    const startReveal = () => {
      if (revealTimerRef.current) return;   // 已在运行
      revealTimerRef.current = setInterval(() => {
        const tPending = thinkingPendingRef.current;
        if (tPending) {                     // 优先揭晓思考
          const chunk = tPending.slice(0, 3);
          thinkingPendingRef.current = tPending.slice(3);
          thinkingText += chunk;
          patchAssistant({ content: assistantText, thinking: thinkingText });
          return;
        }
        const pending = pendingRef.current; // 思考清空后再揭晓回复
        if (!pending) {
          clearReveal();
          return;
        }
        const chunk = pending.slice(0, 3);
        pendingRef.current = pending.slice(3);
        assistantText += chunk;
        patchAssistant({ content: assistantText, thinking: thinkingText });
      }, 30);
    };
    const flushPending = () => {
      // 流结束时把剩余未显示的思考与回复一次性补全
      if (thinkingPendingRef.current) {
        thinkingText += thinkingPendingRef.current;
        thinkingPendingRef.current = '';
      }
      if (pendingRef.current) {
        assistantText += pendingRef.current;
        pendingRef.current = '';
      }
      clearReveal();
      // 合并后的最终内容必须写回 state，否则单帧整段回复（agent 确认文案：一条 delta 帧
      // 后紧跟 [DONE]，30ms 打字机未及 tick）会因 content 恒空渲染成空白小白气泡
      //（2026-08-23 修复，对齐 DesktopAssistant.finish 的 patchTurn）
      patchAssistant({ content: assistantText, thinking: thinkingText });
    };
    const finish = () => {
      flushPending();
      setStreamingId(null);
      abortRef.current = null;
      if (failed) {
        const msg = friendlyError(failed.msg, failed.code);
        patchAssistant({ content: assistantText, thinking: thinkingText, error: msg });
      }
      refreshSessions();   // 流结束刷新侧边栏（标题自动更新后同步）
    };

    abortRef.current = streamChat({
      message: text,
      files,
      sessionId: sid,
      onSession: (newSid) => {
        // 后端可能自愈新建会话（如当前会话被软删/失效）。只同步 ref/URL/存储，
        // 不动 sessionId state——否则触发 abort effect 会中断在途的 SSE 流。
        if (newSid && newSid !== sid) {
          sessionIdRef.current = newSid;
          if (activeSessionKey) sessionStorage.setItem(activeSessionKey, newSid);
          if (searchParams.get('session_id') !== newSid) {
            setSearchParams({ session_id: newSid }, { replace: true });
          }
        }
      },
      onDelta: (d) => {
        // 只入队不直接刷屏；由定时器逐字揭晓（打字机平滑）
        pendingRef.current += d;
        startReveal();
      },
      onThinking: (t) => {
        // 只入队不直接刷屏；由定时器逐字揭晓（思考打字机，优先于回复揭晓）
        thinkingPendingRef.current += t;
        startReveal();
      },
      onMediaReply: (text) => {
        // 媒体类错误以聊天式自然语言回复呈现（外观与正常回复一致，无报错代码）
        flushPending();
        assistantText = text;
        patchAssistant({ content: text, thinking: thinkingText });
      },
      onTaskStarted: ({ task_id: tid, session_id: tsid }) => {
        // Agent 任务确认后（02 §5）：记录任务 id 开始轮询；任务专属会话（桌面助手
        // 新建）自动跟随，避免用户停留在旧会话看不到任务进度
        setActiveTaskId(tid);
        if (tsid && tsid !== sessionIdRef.current) {
          sessionIdRef.current = tsid;
          if (activeSessionKey) sessionStorage.setItem(activeSessionKey, tsid);
          if (searchParams.get('session_id') !== tsid) {
            setSearchParams({ session_id: tsid }, { replace: true });
          }
        }
      },
      onError: (msg, code) => {
        failed = { msg, code };
        finish();
      },
      onDone: finish,
    });
  };

  return (
    <div className="flex h-full bg-slate-50">
      {/* 会话侧边栏：可收起 */}
      {!collapsed ? (
        <aside className="w-64 bg-white border-r border-slate-200 flex flex-col shrink-0">
          <div className="p-3 flex items-center justify-between border-b border-slate-100">
            <button
              onClick={createNewSession}
              className="flex-1 mr-2 py-2 rounded-lg bg-gradient-to-r from-indigo-500 to-violet-500 text-white text-sm font-medium hover:from-indigo-600 hover:to-violet-600"
            >
              + 新会话
            </button>
            <button
              onClick={() => setCollapsed(true)}
              title="收起侧边栏"
              className="px-2 py-2 rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-100"
            >
              ‹
            </button>
          </div>
          <div className="flex-1 overflow-auto p-2">
            {sessions.map((s) => {
              const active = s.id === sessionId;
              return (
                <div
                  key={s.id}
                  onClick={() => setSessionId(s.id)}
                  className={`group flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer mb-0.5 ${
                    active
                      ? 'bg-indigo-50 text-indigo-700'
                      : 'text-slate-700 hover:bg-slate-100'
                  }`}
                >
                  <span className="truncate text-sm">{s.title}</span>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setDeleteTarget(s);
                    }}
                    title="删除会话"
                    className="ml-2 shrink-0 text-slate-300 opacity-0 group-hover:opacity-100 hover:text-red-500"
                  >
                    ×
                  </button>
                </div>
              );
            })}
            {sessions.length === 0 && (
              <p className="text-sm text-slate-400 text-center py-6">暂无会话</p>
            )}
          </div>
        </aside>
      ) : (
        <button
          onClick={() => setCollapsed(false)}
          title="展开侧边栏"
          className="w-8 shrink-0 bg-white border-r border-slate-200 text-slate-400 hover:text-indigo-600 flex items-center justify-center"
        >
          ›
        </button>
      )}

      {/* 消息区 */}
      <div className="flex-1 flex flex-col min-w-0">
        {!chatConfigured && (
          <div className="px-4 py-2 bg-amber-50 border-b border-amber-200 text-amber-700 text-sm">
            您还没有配置对话模型，请到
            <a href="/settings" className="mx-1 underline font-medium">设置页</a>
            配置后再开始对话。
          </div>
        )}
        <div ref={messagesRef} className="flex-1 overflow-auto p-4">
          {messages.length === 0 && (
            <div className="h-full flex items-center justify-center text-slate-400 text-sm">
              开始一段新对话吧
            </div>
          )}
          {activeTaskId && (
            <div className="mb-3 flex items-center gap-2 text-sm text-indigo-600 bg-indigo-50 border border-indigo-100 rounded-lg px-3 py-2">
              <span className="inline-block w-2 h-2 rounded-full bg-indigo-500 animate-pulse" />
              任务执行中…完成后本页将自动刷新结果
            </div>
          )}
          {messages.map((m) => (
            <ChatMessage
              key={m.id}
              message={m}
              streaming={m.id === streamingId}
              onRetry={m.error ? retryLast : undefined}
            />
          ))}
        </div>
        <ChatInput onSend={sendMessage} />
      </div>

      <DeleteConfirm
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        onConfirm={handleDeleteSession}
        title="删除会话"
        description="会话将从列表中移除，但聊天数据会保留在数据库中以供后续记忆检索（可在设置中彻底清空）。"
        confirmText="删除"
        requirePassword={false}
      />
    </div>
  );
}
