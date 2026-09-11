import { ThinkingBlock } from './ThinkingBlock';
import { Markdown } from './Markdown';

// 解析后端返回的 attachments JSON（[{name,kind,size}]）或前端待发文件（{name,size}）
function parseAttachments(message) {
  if (Array.isArray(message?.attachments) && message.attachments.length) {
    return message.attachments;
  }
  if (typeof message?.attachments === 'string' && message.attachments) {
    try {
      return JSON.parse(message.attachments);
    } catch {
      return [];
    }
  }
  return [];
}

export function ChatMessage({ message, onRetry, streaming = false, showThinking = true }) {
  const isUser = message.role === 'user';
  // 桌面助手不渲染深度思考块（showThinking=false），仅聊天页文字输入显示（2026-08-19 起）
  const showThinkBlock = !isUser && showThinking && (message.thinking || streaming);
  const attachments = parseAttachments(message);

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'} mb-3`}>
      <div
        className={`max-w-[70%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
          isUser
            ? 'bg-gradient-to-r from-indigo-500 to-violet-500 text-white rounded-br-sm'
            : 'bg-white border border-slate-200 text-slate-700 rounded-bl-sm shadow-card'
        }`}
      >
        {/* 用户消息附件名 chips（12 §3） */}
        {isUser && attachments.length > 0 && (
          <div className="flex flex-wrap gap-1.5 mb-1.5">
            {attachments.map((a, i) => (
              <span
                key={`${a.name || 'file'}_${i}`}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-white/25 text-xs"
              >
                <svg className="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
                  <path d="M4 3a2 2 0 012-2h4.586A2 2 0 0112 1.586L16.414 6A2 2 0 0117 7.414V17a2 2 0 01-2 2H6a2 2 0 01-2-2V3z" />
                </svg>
                <span className="max-w-[10rem] truncate">{a.name}</span>
              </span>
            ))}
          </div>
        )}
        {showThinkBlock && <ThinkingBlock thinking={message.thinking || ''} streaming={streaming} />}
        {isUser ? (
          message.content
        ) : message.content ? (
          <Markdown>{message.content}</Markdown>
        ) : streaming ? (
          <div className="flex items-center gap-1.5 text-slate-400">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-indigo-400 animate-pulse" />
            {/* 桌面助手无思考块时占位提示「正在回复…」，聊天页保持「思考中…」 */}
            <span className="animate-pulse">{showThinking ? '思考中…' : '正在回复…'}</span>
          </div>
        ) : null}
        {message.interrupted && (
          <div className="mt-1 text-xs text-slate-400">（已打断）</div>
        )}
        {message.error && (
          <div className="mt-1 text-sm text-red-400">
            出错：{message.error}
            <button onClick={onRetry} className="ml-2 underline">重试</button>
          </div>
        )}
      </div>
    </div>
  );
}
